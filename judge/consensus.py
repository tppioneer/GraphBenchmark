"""Judge consensus and effective-score assembly (design §13, §14).

The consensus layer turns several validated Judge outputs (A, B and optionally
the arbiter C) into a single per-item consensus credit, a consolidated set of
critical errors and a human-review state. It is the only place the A/B/C Judge
protocol runs (§13.1); the deterministic scoring core
(:mod:`scoring.aggregator`) consumes the consensus credit and never decides
whether to call a Judge.

Frozen baseline (DEC-001 #5, §13.1, mirrored in ``profiles/common.yaml``):

* A/B triggers for the arbiter (Judge C):
  - any *critical* item with differing A/B credit (any difference);
  - any *non-critical* item with ``|credit_A - credit_B| > 0.25``;
  - ``|provisional_total_A - provisional_total_B| > 5``.
* Aggregation: no arbiter -> per-item exact mean of A/B; arbiter -> per-item
  median of three. A single Judge (development mode) passes through unchanged.
* Human-review triggers (arbiter/three-Judge path only, §13.1 steps 7-8):
  - any critical item with a three-Judge credit *range* ``> 0.5``;
  - any critical item whose consensus confidence ``< 0.70``;
  - consensus overall confidence ``< 0.65``.
* ``confidence`` only routes to human review; it is never multiplied into item
  credit or totals (§13.1).

v1 effective score (§14): ``effective item credit = Judge consensus credit``.
There is no human-credit override, no ``adjudication.json`` and no
editing/approval surface; this module exposes none of those.
``requires_human_review`` is a status flag with stable frozen reason codes; it
never changes the credit.

All credit/confidence arithmetic uses :class:`decimal.Decimal` so two-Judge
means such as ``0.125`` / ``0.375`` stay exact (§10.1); only the report layer
rounds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from scoring import profiles as prof
from scoring.aggregator import (
    HUMAN_REVIEW_REASONS,
    JUDGE_PROTOCOL,
    RUN_MODES,
    ConsensusInfo,
    ScoreResult,
    VersionMetadata,
    build_score,
)

#: Locked Judge output business version (judge-output.schema.json).
JUDGE_OUTPUT_SCHEMA_VERSION = "judge-output-v1"

#: Frozen single-Judge credit set as exact Decimals (DEC-001 #2). A consensus
#: credit may be an exact mean/median and is NOT restricted to this set.
_FROZEN_CREDITS: frozenset[Decimal] = frozenset(Decimal(str(c)) for c in prof.FROZEN_CREDIT_SET)

# --- Frozen consensus thresholds (DEC-001 #5, §13.1, profiles/common.yaml). ---
# Identical fallbacks used when no common profile is supplied or a key is
# absent; the common profile is the source of truth and overrides these.
_NONCRITICAL_CREDIT_DIFF = Decimal("0.25")
_PROVISIONAL_TOTAL_DIFF = Decimal("5")
_CRITICAL_CREDIT_RANGE = Decimal("0.5")
_CRITICAL_CONSENSUS_CONFIDENCE = Decimal("0.70")
_OVERALL_CONFIDENCE = Decimal("0.65")

#: Arbiter (Judge C) trigger reason codes (§13.1 steps 2-4). Internal/audit;
#: they do not appear in score.json.
ARBITER_TRIGGER_CRITICAL_DISAGREEMENT = "critical_credit_disagreement"
ARBITER_TRIGGER_NONCRITICAL_GAP = "noncritical_credit_gap"
ARBITER_TRIGGER_PROVISIONAL_TOTAL_GAP = "provisional_total_gap"


class ConsensusError(Exception):
    """Raised when Judge outputs cannot be validated or consensus cannot form.

    Covers the acceptance criterion "缺失、非法或协议不一致的 Judge 输出不被静默
    纳入共识": a missing, non-mapping, protocol/profile-mismatched, illegally
    credited, item-mismatched or critical-error-invalid Judge output, plus the
    formal-mode protocol violation where the A/B triggers fire but no arbiter
    (Judge C) was supplied.
    """


# ---------------------------------------------------------------------------
# Internal output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemVerdict:
    """One Judge's normalized per-item verdict."""

    item_id: str
    credit: Decimal
    confidence: Decimal


@dataclass(frozen=True)
class JudgeResult:
    """A validated, normalized single Judge output."""

    label: str
    items: dict[str, ItemVerdict]
    overall_confidence: Decimal
    critical_errors: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ArbiterTrigger:
    """One reason the A/B pair requires the arbiter (Judge C)."""

    code: str
    detail: str


@dataclass(frozen=True)
class ArbiterDecision:
    """The A/B arbiter truth table (§13.1 steps 2-4).

    ``call_arbiter`` is True when any trigger fired. Pure decision: it neither
    aggregates credit nor runs a Judge.
    """

    call_arbiter: bool
    triggers: tuple[ArbiterTrigger, ...]


@dataclass(frozen=True)
class ConsensusOutcome:
    """Per-item consensus credit, consolidated critical errors and review state.

    ``consensus_credits`` feeds the deterministic aggregator directly.
    ``consensus_confidences`` / ``consensus_overall_confidence`` are audit-only
    (§13.1 step 8); they are never multiplied into credit and are not part of
    score.json. ``requires_human_review`` is a status flag (§14); in v1 it never
    alters the credit, which always equals the Judge consensus credit.
    """

    consensus_credits: dict[str, Decimal]
    critical_errors: tuple[Mapping[str, Any], ...]
    mode: str
    judges: int
    arbiter_used: bool
    requires_human_review: bool
    human_review_reasons: tuple[str, ...]
    consensus_confidences: dict[str, Decimal]
    consensus_overall_confidence: Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, *, context: str) -> Decimal:
    """Convert an int/float/Decimal (not bool) to an exact Decimal via ``str()``.

    Mirrors :func:`scoring.aggregator._to_decimal` so credits and confidences
    stay exact end-to-end (§10.1).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConsensusError(f"{context} must be a number, got {type(value).__name__}")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class _Thresholds:
    """Resolved consensus thresholds (from common profile with frozen fallback)."""

    noncritical_credit_diff: Decimal
    provisional_total_diff: Decimal
    critical_credit_range: Decimal
    critical_consensus_confidence: Decimal
    overall_confidence: Decimal
    critical_disagreement_triggers: bool


def _resolve_thresholds(common_profile: dict[str, Any] | None) -> _Thresholds:
    """Resolve consensus thresholds from the common profile (source of truth).

    The frozen constants are identical fallbacks used when no profile is
    supplied or a key is absent (mirroring how the aggregator resolves cap
    values).
    """
    block: Any = common_profile.get("consensus") if isinstance(common_profile, dict) else None

    def _dec(key: str, fallback: Decimal) -> Decimal:
        if isinstance(block, dict) and key in block:
            return _to_decimal(block[key], context=f"consensus.{key}")
        return fallback

    crit_triggers = True
    if isinstance(block, dict) and "critical_item_disagreement_triggers_arbiter" in block:
        crit_triggers = bool(block["critical_item_disagreement_triggers_arbiter"])
    return _Thresholds(
        noncritical_credit_diff=_dec("noncritical_credit_diff_threshold", _NONCRITICAL_CREDIT_DIFF),
        provisional_total_diff=_dec("provisional_total_diff_threshold", _PROVISIONAL_TOTAL_DIFF),
        critical_credit_range=_dec("critical_credit_range_threshold", _CRITICAL_CREDIT_RANGE),
        critical_consensus_confidence=_dec(
            "critical_consensus_confidence_threshold", _CRITICAL_CONSENSUS_CONFIDENCE
        ),
        overall_confidence=_dec("overall_confidence_threshold", _OVERALL_CONFIDENCE),
        critical_disagreement_triggers=crit_triggers,
    )


def _allowed_critical_codes(task_profile: dict[str, Any] | None) -> set[str]:
    """Critical-error codes declared by the task profile (frozen fallback).

    Mirrors :func:`scoring.aggregator._allowed_critical_codes`: an explicit
    ``critical_error_codes: []`` allows no codes; an absent key keeps the frozen
    set.
    """
    if isinstance(task_profile, dict) and "critical_error_codes" in task_profile:
        return set(task_profile["critical_error_codes"])
    return set(prof.FROZEN_CRITICAL_CAP_CODES)


@dataclass(frozen=True)
class _GroundTruthIndex:
    gt_ids: frozenset[str]
    critical_item_ids: frozenset[str]
    points_by_id: dict[str, Decimal]
    scoring_profile: str
    item_order: tuple[str, ...]


def _index_ground_truth(
    ground_truth: Any, task_profile: dict[str, Any] | None
) -> _GroundTruthIndex:
    """Index the ground truth for item ids, critical flags, points and profile."""
    if not isinstance(ground_truth, Mapping):
        raise ConsensusError("ground_truth must be a mapping")
    items_raw = ground_truth.get("rubric_items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ConsensusError("ground truth has no rubric_items")
    scoring_profile = ground_truth.get("scoring_profile")
    if not isinstance(scoring_profile, str) or not scoring_profile:
        raise ConsensusError("ground truth has no scoring_profile")
    gt_ids: set[str] = set()
    critical: set[str] = set()
    points_by_id: dict[str, Decimal] = {}
    order: list[str] = []
    for item in items_raw:
        if not isinstance(item, Mapping):
            raise ConsensusError("ground truth rubric item must be a mapping")
        iid = item.get("id")
        if not isinstance(iid, str) or not iid:
            raise ConsensusError("ground truth rubric item id is missing or empty")
        if iid in gt_ids:
            raise ConsensusError(f"duplicate ground truth item id {iid!r}")
        gt_ids.add(iid)
        order.append(iid)
        if item.get("critical") is True:
            critical.add(iid)
        points = _to_decimal(item.get("points"), context=f"ground truth item {iid!r} points")
        if points <= 0:
            raise ConsensusError(f"ground truth item {iid!r} points must be positive, got {points}")
        points_by_id[iid] = points
    return _GroundTruthIndex(
        gt_ids=frozenset(gt_ids),
        critical_item_ids=frozenset(critical),
        points_by_id=points_by_id,
        scoring_profile=scoring_profile,
        item_order=tuple(order),
    )


def _normalize_judge_output(
    judge_output: Any,
    *,
    gt: _GroundTruthIndex,
    allowed_critical_codes: set[str],
    label: str,
) -> JudgeResult:
    """Validate and normalize one Judge output (§10.2); raise on any violation.

    A missing, non-mapping, protocol/profile-mismatched, illegally credited,
    item-mismatched or critical-error-invalid output is rejected - never
    silently included in the consensus.
    """
    if judge_output is None:
        raise ConsensusError(f"judge {label}: output is missing")
    if not isinstance(judge_output, Mapping):
        raise ConsensusError(
            f"judge {label}: output must be a mapping, got {type(judge_output).__name__}"
        )

    if judge_output.get("schema_version") != JUDGE_OUTPUT_SCHEMA_VERSION:
        raise ConsensusError(
            f"judge {label}: schema_version must be {JUDGE_OUTPUT_SCHEMA_VERSION!r}, "
            f"got {judge_output.get('schema_version')!r}"
        )
    if judge_output.get("judge_protocol") != JUDGE_PROTOCOL:
        raise ConsensusError(
            f"judge {label}: judge_protocol must be {JUDGE_PROTOCOL!r}, "
            f"got {judge_output.get('judge_protocol')!r}"
        )
    if judge_output.get("scoring_profile") != gt.scoring_profile:
        raise ConsensusError(
            f"judge {label}: scoring_profile {judge_output.get('scoring_profile')!r} "
            f"!= ground truth {gt.scoring_profile!r}"
        )

    items_raw = judge_output.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ConsensusError(f"judge {label}: items must be a non-empty list")

    seen: set[str] = set()
    items: dict[str, ItemVerdict] = {}
    for i, item in enumerate(items_raw):
        if not isinstance(item, Mapping):
            raise ConsensusError(f"judge {label}: items[{i}] must be a mapping")
        iid = item.get("item_id")
        if not isinstance(iid, str) or not iid:
            raise ConsensusError(f"judge {label}: items[{i}] item_id is missing or empty")
        if iid in seen:
            raise ConsensusError(f"judge {label}: duplicate item_id {iid!r}")
        if iid not in gt.gt_ids:
            raise ConsensusError(f"judge {label}: item_id {iid!r} is not in the ground truth")
        seen.add(iid)
        credit = _to_decimal(item.get("credit"), context=f"judge {label} item {iid!r} credit")
        if credit not in _FROZEN_CREDITS:
            raise ConsensusError(
                f"judge {label}: item {iid!r} credit {credit} not in frozen credit set "
                f"{prof.FROZEN_CREDIT_SET}"
            )
        confidence = _to_decimal(
            item.get("confidence"), context=f"judge {label} item {iid!r} confidence"
        )
        if not (Decimal(0) <= confidence <= Decimal(1)):
            raise ConsensusError(
                f"judge {label}: item {iid!r} confidence {confidence} out of range [0, 1]"
            )
        items[iid] = ItemVerdict(item_id=iid, credit=credit, confidence=confidence)
    missing = gt.gt_ids - seen
    if missing:
        raise ConsensusError(f"judge {label}: ground truth items not judged: {sorted(missing)}")

    overall = _to_decimal(
        judge_output.get("overall_confidence"), context=f"judge {label} overall_confidence"
    )
    if not (Decimal(0) <= overall <= Decimal(1)):
        raise ConsensusError(f"judge {label}: overall_confidence {overall} out of range [0, 1]")

    crit_raw = judge_output.get("critical_errors")
    if crit_raw is None:
        raise ConsensusError(f"judge {label}: critical_errors is missing or null")
    if not isinstance(crit_raw, list):
        raise ConsensusError(f"judge {label}: critical_errors must be a list")
    crit_errors: list[Mapping[str, Any]] = []
    for ce in crit_raw:
        if not isinstance(ce, Mapping):
            raise ConsensusError(f"judge {label}: critical_errors entry must be a mapping")
        code = ce.get("code")
        if code not in allowed_critical_codes:
            raise ConsensusError(
                f"judge {label}: critical error code {code!r} is not declared by the profile"
            )
        ce_item = ce.get("item_id")
        if ce_item not in gt.critical_item_ids:
            raise ConsensusError(
                f"judge {label}: critical error code {code!r} references "
                f"non-critical or unknown item {ce_item!r}"
            )
        crit_errors.append(ce)

    return JudgeResult(
        label=label,
        items=items,
        overall_confidence=overall,
        critical_errors=tuple(crit_errors),
    )


# ---------------------------------------------------------------------------
# Arbiter decision (§13.1 steps 2-4)
# ---------------------------------------------------------------------------


def _provisional_total(result: JudgeResult, gt: _GroundTruthIndex) -> Decimal:
    """Σ ``points × credit`` for one Judge (the A/B provisional total, §13.1)."""
    return sum(
        (gt.points_by_id[iid] * result.items[iid].credit for iid in gt.item_order),
        Decimal(0),
    )


def _arbiter_triggers(
    a: JudgeResult,
    b: JudgeResult,
    *,
    gt: _GroundTruthIndex,
    thresholds: _Thresholds,
) -> list[ArbiterTrigger]:
    """Evaluate the frozen A/B triggers (§13.1 steps 2-4). Pure decision."""
    triggers: list[ArbiterTrigger] = []

    # Step 2: any *critical* item with differing A/B credit (any difference).
    if thresholds.critical_disagreement_triggers:
        differing = sorted(
            iid for iid in gt.critical_item_ids if a.items[iid].credit != b.items[iid].credit
        )
        if differing:
            triggers.append(
                ArbiterTrigger(
                    code=ARBITER_TRIGGER_CRITICAL_DISAGREEMENT,
                    detail=f"critical items with differing A/B credit: {differing}",
                )
            )

    # Step 3: any *non-critical* item with |credit_A - credit_B| > threshold.
    gap_items = sorted(
        iid
        for iid in gt.item_order
        if iid not in gt.critical_item_ids
        and abs(a.items[iid].credit - b.items[iid].credit) > thresholds.noncritical_credit_diff
    )
    if gap_items:
        triggers.append(
            ArbiterTrigger(
                code=ARBITER_TRIGGER_NONCRITICAL_GAP,
                detail=(
                    f"non-critical items with |A-B| > "
                    f"{thresholds.noncritical_credit_diff}: {gap_items}"
                ),
            )
        )

    # Step 4: |provisional_total_A - provisional_total_B| > threshold.
    total_diff = abs(_provisional_total(a, gt) - _provisional_total(b, gt))
    if total_diff > thresholds.provisional_total_diff:
        triggers.append(
            ArbiterTrigger(
                code=ARBITER_TRIGGER_PROVISIONAL_TOTAL_GAP,
                detail=(
                    f"provisional total diff {total_diff} > {thresholds.provisional_total_diff}"
                ),
            )
        )

    return triggers


def should_call_arbiter(
    judge_a: Mapping[str, Any],
    judge_b: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    *,
    task_profile: dict[str, Any] | None = None,
    common_profile: dict[str, Any] | None = None,
) -> ArbiterDecision:
    """Decide whether the A/B pair requires the arbiter Judge C (§13.1 steps 2-4).

    Validates and normalizes both Judge outputs first (a missing/illegal/
    protocol-inconsistent output raises :class:`ConsensusError`), then evaluates
    the frozen triggers. Pure decision: it never aggregates credit or runs a
    Judge.
    """
    gt = _index_ground_truth(ground_truth, task_profile)
    allowed = _allowed_critical_codes(task_profile)
    thresholds = _resolve_thresholds(common_profile)
    a = _normalize_judge_output(judge_a, gt=gt, allowed_critical_codes=allowed, label="A")
    b = _normalize_judge_output(judge_b, gt=gt, allowed_critical_codes=allowed, label="B")
    triggers = tuple(_arbiter_triggers(a, b, gt=gt, thresholds=thresholds))
    return ArbiterDecision(call_arbiter=bool(triggers), triggers=triggers)


# ---------------------------------------------------------------------------
# Aggregation (§13.1 steps 5-8)
# ---------------------------------------------------------------------------


def _mean2(a: Decimal, b: Decimal) -> Decimal:
    """Exact mean of two Decimals (§13.1 step 5)."""
    return (a + b) / Decimal(2)


def _median3(values: Sequence[Decimal]) -> Decimal:
    """Median of exactly three Decimals (the middle value when sorted)."""
    if len(values) != 3:
        raise ConsensusError(f"_median3 expects 3 values, got {len(values)}")
    return sorted(values)[1]


def _consolidate_critical_errors(
    results: Sequence[JudgeResult],
) -> tuple[Mapping[str, Any], ...]:
    """Consolidate per-Judge critical errors by strict majority vote.

    A ``(item_id, code)`` critical error is consensus when reported by *more
    than half* of the Judges: for one Judge the single report passes; for two
    Judges both must agree; for three Judges at least two must agree. A cap is a
    penalty (§12), so a single contested claim never caps the score on its own.
    The first-seen entry is retained for audit; the result is sorted by
    ``(item_id, code)`` for determinism.
    """
    n = len(results)
    majority = Decimal(n) / Decimal(2)
    counts: dict[tuple[str, str], int] = {}
    first: dict[tuple[str, str], Mapping[str, Any]] = {}
    for result in results:
        seen_this_judge: set[tuple[str, str]] = set()
        for ce in result.critical_errors:
            key = (ce["item_id"], ce["code"])
            if key in seen_this_judge:
                continue  # one Judge reporting the same (item, code) twice: keep first
            seen_this_judge.add(key)
            counts[key] = counts.get(key, 0) + 1
            first.setdefault(key, ce)
    consensus = [key for key, count in counts.items() if Decimal(count) > majority]
    consensus.sort()
    return tuple(first[key] for key in consensus)


def _evaluate_human_review(
    results: Sequence[JudgeResult],
    *,
    gt: _GroundTruthIndex,
    consensus_confidences: Mapping[str, Decimal],
    consensus_overall_confidence: Decimal,
    thresholds: _Thresholds,
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate the three-Judge human-review triggers (§13.1 steps 7-8).

    Only applies in the arbiter (three-Judge) path. Confidence routes to review
    only; it never changes credit. Returns ``(flag, reasons)`` with reasons in
    the frozen :data:`~scoring.aggregator.HUMAN_REVIEW_REASONS` order.
    """
    reasons: list[str] = []

    # Step 7: any critical item with a three-Judge credit range > threshold.
    range_items = sorted(
        iid
        for iid in gt.critical_item_ids
        if (max(r.items[iid].credit for r in results) - min(r.items[iid].credit for r in results))
        > thresholds.critical_credit_range
    )
    if range_items:
        reasons.append("critical_credit_range")

    # Step 8a: any critical item whose consensus confidence < threshold.
    conf_items = sorted(
        iid
        for iid in gt.critical_item_ids
        if consensus_confidences[iid] < thresholds.critical_consensus_confidence
    )
    if conf_items:
        reasons.append("critical_consensus_confidence")

    # Step 8b: consensus overall confidence < threshold.
    if consensus_overall_confidence < thresholds.overall_confidence:
        reasons.append("overall_confidence")

    if not reasons:
        return (False, ())
    ordered = tuple(r for r in HUMAN_REVIEW_REASONS if r in reasons)
    return (True, ordered)


def _single_consensus(result: JudgeResult) -> ConsensusOutcome:
    """Development mode: one Judge passes through unchanged (§13.2)."""
    credits = {iid: v.credit for iid, v in result.items.items()}
    confidences = {iid: v.confidence for iid, v in result.items.items()}
    return ConsensusOutcome(
        consensus_credits=credits,
        critical_errors=result.critical_errors,
        mode="single",
        judges=1,
        arbiter_used=False,
        requires_human_review=False,
        human_review_reasons=(),
        consensus_confidences=confidences,
        consensus_overall_confidence=result.overall_confidence,
    )


def _mean_consensus(a: JudgeResult, b: JudgeResult, *, gt: _GroundTruthIndex) -> ConsensusOutcome:
    """Two-Judge exact mean per item (§13.1 step 5). No human-review triggers."""
    credits = {iid: _mean2(a.items[iid].credit, b.items[iid].credit) for iid in gt.item_order}
    confidences = {
        iid: _mean2(a.items[iid].confidence, b.items[iid].confidence) for iid in gt.item_order
    }
    return ConsensusOutcome(
        consensus_credits=credits,
        critical_errors=_consolidate_critical_errors([a, b]),
        mode="mean",
        judges=2,
        arbiter_used=False,
        requires_human_review=False,
        human_review_reasons=(),
        consensus_confidences=confidences,
        consensus_overall_confidence=_mean2(a.overall_confidence, b.overall_confidence),
    )


def _median_consensus(
    results: Sequence[JudgeResult],
    *,
    gt: _GroundTruthIndex,
    thresholds: _Thresholds,
) -> ConsensusOutcome:
    """Three-Judge median per item (§13.1 step 6) plus human-review evaluation."""
    credits = {iid: _median3([r.items[iid].credit for r in results]) for iid in gt.item_order}
    confidences = {
        iid: _median3([r.items[iid].confidence for r in results]) for iid in gt.item_order
    }
    consensus_overall = _median3([r.overall_confidence for r in results])
    requires_review, reasons = _evaluate_human_review(
        results,
        gt=gt,
        consensus_confidences=confidences,
        consensus_overall_confidence=consensus_overall,
        thresholds=thresholds,
    )
    return ConsensusOutcome(
        consensus_credits=credits,
        critical_errors=_consolidate_critical_errors(results),
        mode="median",
        judges=3,
        arbiter_used=True,
        requires_human_review=requires_review,
        human_review_reasons=reasons,
        consensus_confidences=confidences,
        consensus_overall_confidence=consensus_overall,
    )


def form_consensus(
    judge_outputs: Sequence[Mapping[str, Any]],
    ground_truth: Mapping[str, Any],
    *,
    task_profile: dict[str, Any] | None = None,
    common_profile: dict[str, Any] | None = None,
    run_mode: str = "formal",
) -> ConsensusOutcome:
    """Validate and aggregate Judge outputs into a consensus (§13.1, §13.2).

    * ``development`` mode: exactly one Judge (single pass-through).
    * ``formal`` mode: two Judges (exact mean) or three Judges (median + review).

    Each Judge output is validated against the ground truth and task profile
    (§10.2); a missing, illegal or protocol-inconsistent output raises
    :class:`ConsensusError` and is never silently included. In formal mode with
    two Judges, the A/B arbiter triggers are evaluated: if any fires, Judge C
    was required but not supplied and :class:`ConsensusError` is raised (the
    runner must call C first, or mark the run ``judge_failed`` per §13.5).

    Human review (§13.1 steps 7-8) is evaluated only on the three-Judge path;
    ``requires_human_review`` is a status flag that never changes the credit.
    Development mode must be explicitly identified (task invariant); the default
    is ``formal``.
    """
    if run_mode not in RUN_MODES:
        raise ConsensusError(f"run_mode {run_mode!r} not in {RUN_MODES}")
    if isinstance(judge_outputs, (str, bytes, Mapping)):
        raise ConsensusError("judge_outputs must be a sequence of judge output mappings")
    if not isinstance(judge_outputs, Sequence):
        raise ConsensusError("judge_outputs must be a sequence of judge output mappings")
    outputs = list(judge_outputs)
    n = len(outputs)

    if run_mode == "development":
        if n != 1:
            raise ConsensusError(f"development mode requires exactly 1 judge, got {n}")
    else:  # formal
        if n not in (2, 3):
            raise ConsensusError(f"formal mode requires 2 or 3 judges, got {n}")

    gt = _index_ground_truth(ground_truth, task_profile)
    allowed = _allowed_critical_codes(task_profile)
    thresholds = _resolve_thresholds(common_profile)
    labels = ("A", "B", "C")[:n]
    results = [
        _normalize_judge_output(out, gt=gt, allowed_critical_codes=allowed, label=labels[i])
        for i, out in enumerate(outputs)
    ]

    if n == 1:
        return _single_consensus(results[0])
    if n == 2:
        triggers = _arbiter_triggers(results[0], results[1], gt=gt, thresholds=thresholds)
        if triggers:
            detail = "; ".join(f"{t.code} ({t.detail})" for t in triggers)
            raise ConsensusError(
                f"arbiter (Judge C) required by A/B triggers but not provided: {detail}"
            )
        return _mean_consensus(results[0], results[1], gt=gt)
    return _median_consensus(results, gt=gt, thresholds=thresholds)


# ---------------------------------------------------------------------------
# Effective-score assembly (§14)
# ---------------------------------------------------------------------------


def build_effective_score(
    judge_outputs: Sequence[Mapping[str, Any]],
    ground_truth: Mapping[str, Any],
    *,
    version_metadata: VersionMetadata,
    task_profile: dict[str, Any] | None = None,
    common_profile: dict[str, Any] | None = None,
    run_mode: str = "formal",
) -> ScoreResult:
    """Run consensus then deterministic aggregation to assemble the effective score.

    v1 invariant (§14): ``effective item credit = Judge consensus credit``.
    There is no human-credit override and no adjudication surface; this function
    accepts none. ``requires_human_review`` flows through as a status flag only.
    The returned :class:`~scoring.aggregator.ScoreResult` is serialized to
    ``effective-score.json`` by :func:`scoring.aggregator.score_to_dict`.
    """
    outcome = form_consensus(
        judge_outputs,
        ground_truth,
        task_profile=task_profile,
        common_profile=common_profile,
        run_mode=run_mode,
    )
    consensus_info = ConsensusInfo(
        mode=outcome.mode,
        judges=outcome.judges,
        arbiter_used=outcome.arbiter_used,
        human_review_triggered=outcome.requires_human_review,
    )
    return build_score(
        ground_truth,
        outcome.consensus_credits,
        version_metadata=version_metadata,
        consensus=consensus_info,
        critical_errors=list(outcome.critical_errors),
        requires_human_review=outcome.requires_human_review,
        human_review_reasons=list(outcome.human_review_reasons),
        task_profile=task_profile,
        common_profile=common_profile,
        run_mode=run_mode,
    )
