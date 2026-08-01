"""Deterministic score aggregation (design §11, §12, §20).

The aggregation layer is the only place a formal score is computed. It consumes
already-validated Ground Truth points and Judge *consensus* credit and produces
item scores, dimension totals, the raw total, the critical cap and the capped
total. The Judge can never change item points, dimension weights, the total
formula or the cap rules (§11); ordinary omissions only deduct per item and
never trigger a cap (§12).

All arithmetic uses :class:`decimal.Decimal` and never rounds - display rounding
happens only at the report layer (§10.1). Every frozen single-Judge credit
(``{0, 0.25, 0.5, 0.75, 1}``) and every two-Judge mean / three-Judge median of
them is an exact binary fraction, so converting the exact Decimal result to a
JSON number for ``score.json`` is lossless; the core nonetheless keeps Decimal
end-to-end so non-standard consensus values stay exact too.

Multi-Judge consensus (§13.1) is out of scope (AIS-005 excluded scope): this
module receives the consensus credit per item and the consolidated critical
errors, it does not run the A/B/C Judge protocol. Empty/refused/irrelevant and
invalid-run handling (§12) is likewise status enforcement that happens before
aggregation; the natural math already yields a 0 total when every credit is 0,
and the cap only ever lowers (§12), so an all-zero answer scores 0 without a
special status input here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from scoring.profiles import (
    FROZEN_CRITICAL_CAP_CODES,
    FROZEN_DIMENSION_NAMES,
)

# ---------------------------------------------------------------------------
# Frozen constants (mirror scoring.profiles / common.yaml; design §5, §12, §20).
# ---------------------------------------------------------------------------

#: Locked business version for the score contract (score.schema.json).
SCORE_SCHEMA_VERSION = "score-v1"
#: Locked benchmark version (design §20).
BENCHMARK_VERSION = "ai-score-v1"
#: Locked Judge output protocol shared by every task profile (design §10).
JUDGE_PROTOCOL = "semantic_outcome_v1"
#: Locked Judge execution provider (design §13.3).
JUDGE_PROVIDER = "claude-code-cli"

#: Frozen critical-failure cap codes (DEC-001 #4, §12).
CORE_CORRECTNESS_ALL_ZERO = "core_correctness_all_zero"
REVERSE_CRITICAL_RELATION_ZERO = "reverse_critical_relation_zero"

#: Frozen cap values (§12): every core_correctness critical item at credit 0
#: caps the total at 50; a Profile-allowed reverse critical relation at credit 0
#: caps it at 60. Multiple caps take the lowest (§12, ``cap_selection: min``).
FROZEN_CAP_VALUES: dict[str, Decimal] = {
    CORE_CORRECTNESS_ALL_ZERO: Decimal(50),
    REVERSE_CRITICAL_RELATION_ZERO: Decimal(60),
}

#: Frozen run modes (§13.2). Reports must state which Judge mode was used.
RUN_MODES: tuple[str, ...] = ("development", "formal")
#: Frozen consensus modes (§13.1, §13.2).
CONSENSUS_MODES: tuple[str, ...] = ("single", "mean", "median")

#: Frozen human-review trigger reason codes (DEC-001 #5, §13.1), matching the
#: score.schema.json ``human_review_reasons`` enum. Confidence routes to human
#: review only; it is never multiplied into credit or totals.
HUMAN_REVIEW_REASONS: tuple[str, ...] = (
    "critical_credit_range",
    "critical_consensus_confidence",
    "overall_confidence",
)

#: Unpinned model sentinels rejected for a formal score (DEC-001 #6, §13.3).
_UNPINNED_MODEL_SENTINELS = frozenset({"auto", "latest"})


class AggregationError(Exception):
    """Raised when the inputs cannot be deterministically aggregated.

    Covers the §10.2 / acceptance-criterion matching failures: an unknown,
    duplicate or missing rubric item; an out-of-range consensus credit; an
    undeclared critical-error code; a critical error referencing a non-critical
    or unknown item; or an inconsistent version-metadata / consensus block.
    """


# ---------------------------------------------------------------------------
# Internal output model (the "scoring output internal model", AIS-005 scope).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemScore:
    """One rubric item's deterministic score (§11: ``points × credit``)."""

    item_id: str
    dimension: str
    points: Decimal
    consensus_credit: Decimal
    item_score: Decimal


@dataclass(frozen=True)
class TriggeredCap:
    """A single cap rule that fired (§12), retained for audit."""

    code: str
    cap_value: Decimal
    reason: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class CriticalCap:
    """The combined critical cap applied to the raw total (§12).

    ``applied`` is True when at least one cap rule fired. ``cap_value`` /
    ``code`` describe the *winning* (lowest, strictest) cap; ``triggered``
    retains every fired cap for audit so the report can show every reason. A cap
    only ever lowers the total (``capped_total = min(raw_total, cap_value)``);
    it never raises it, even when the raw total already sits below the cap.
    """

    applied: bool
    cap_value: Decimal | None
    code: str | None
    reason: str | None
    triggered: tuple[TriggeredCap, ...]


@dataclass(frozen=True)
class AggregationResult:
    """The pure deterministic core: GT + credit -> scores + cap (§11, §12)."""

    items: tuple[ItemScore, ...]
    dimension_totals: dict[str, Decimal]
    raw_total: Decimal
    critical_cap: CriticalCap
    capped_total: Decimal


@dataclass(frozen=True)
class VersionMetadata:
    """The §20 identity block every formal score must carry.

    Scores across incompatible protocols, profiles, models, prompts, GT
    revisions or consensus versions must not be mixed (§20). The requested and
    effective Judge models must both be pinned and must agree; a requested /
    effective mismatch invalidates the run (DEC-001 #6, §13.3, §20).
    """

    benchmark_version: str
    judge_protocol: str
    scoring_profile: str
    judge_provider: str
    judge_requested_model: str
    judge_model: str
    judge_cli_version: str
    judge_prompt_digest: str
    ground_truth_digest: str
    agent_answer_digest: str
    case_id: str
    task_type: str


@dataclass(frozen=True)
class ConsensusInfo:
    """Judge consensus summary (§13.1, §13.2)."""

    mode: str
    judges: int
    arbiter_used: bool
    human_review_triggered: bool = False


@dataclass(frozen=True)
class ScoreResult:
    """The full deterministic score: aggregation + version + consensus (§11, §20)."""

    version_metadata: VersionMetadata
    items: tuple[ItemScore, ...]
    dimension_totals: dict[str, Decimal]
    raw_total: Decimal
    critical_cap: CriticalCap
    capped_total: Decimal
    consensus: ConsensusInfo
    requires_human_review: bool
    human_review_reasons: tuple[str, ...]
    run_mode: str

    @property
    def schema_version(self) -> str:
        """Locked score contract version (score.schema.json)."""
        return SCORE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, *, context: str) -> Decimal:
    """Convert an int/float/Decimal (not bool) to an exact Decimal via ``str()``.

    ``Decimal(str(value))`` avoids binary-float drift so integer points and
    quarter-step credits sum exactly (§10.1). ``context`` names the field for the
    error message.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise AggregationError(f"{context} must be a number, got {type(value).__name__}")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _num(value: Decimal) -> int | float:
    """Convert an exact Decimal to a JSON number (int when integral).

    Every consensus value reachable from the frozen credit set is an exact binary
    fraction, so this conversion is lossless; ``int`` is preferred for integral
    values to keep the score payload compact and match the GT points shape.
    """
    integral = int(value)
    return integral if value == integral else float(value)


def _normalize_credits(
    consensus_credits: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize credit input to a ``item_id -> credit`` dict.

    A :class:`Mapping` is the natural consensus output (unique keys, so no
    duplicate is possible). A sequence of verdict mappings (each carrying
    ``item_id`` and ``credit``, matching the judge-output item shape) is also
    accepted so a repeated item id can be detected and rejected (§10.2,
    acceptance criterion "重复 item 被拒绝").
    """
    credits: dict[str, Any] = {}
    if isinstance(consensus_credits, Mapping):
        for iid, val in consensus_credits.items():
            if not isinstance(iid, str) or not iid:
                raise AggregationError("consensus credit item_id is missing or empty")
            credits[iid] = val
        return credits
    if isinstance(consensus_credits, (str, bytes)):
        raise AggregationError("consensus_credits must be a mapping or sequence of verdicts")
    for entry in consensus_credits:
        if not isinstance(entry, Mapping):
            raise AggregationError("consensus credit entry must be a mapping")
        iid = entry.get("item_id")
        if not isinstance(iid, str) or not iid:
            raise AggregationError("consensus credit entry has no item_id")
        if iid in credits:
            raise AggregationError(f"duplicate consensus credit for item {iid!r}")
        credits[iid] = entry.get("credit")
    return credits


def _resolve_cap_values(common_profile: dict[str, Any] | None) -> dict[str, Decimal]:
    """Cap code -> cap value, from the common profile with frozen fallbacks.

    The common profile is the source of truth for the cap values; the frozen
    constants are an identical fallback used when no profile is supplied (mirrors
    how ``rubric_validator`` resolves dimension weights).
    """
    caps = dict(FROZEN_CAP_VALUES)
    if isinstance(common_profile, dict):
        for entry in common_profile.get("critical_caps", []):
            if isinstance(entry, dict) and "code" in entry and "cap" in entry:
                caps[entry["code"]] = _to_decimal(entry["cap"], context="critical_caps.cap")
    return caps


def _allowed_critical_codes(task_profile: dict[str, Any] | None) -> set[str]:
    """Critical-error codes declared by the task profile (frozen fallback).

    The Judge may only return codes the Profile declares (§12). The frozen set
    is used only when no profile is supplied or the profile omits the
    ``critical_error_codes`` key; an explicit empty declaration (``[]``) is
    honored and allows no critical-error codes.
    """
    if isinstance(task_profile, dict) and "critical_error_codes" in task_profile:
        return set(task_profile["critical_error_codes"])
    return set(FROZEN_CRITICAL_CAP_CODES)


def _validate_version_metadata(vm: VersionMetadata) -> None:
    """Validate the §20 identity block: locked versions, pinned agreeing models."""
    if vm.benchmark_version != BENCHMARK_VERSION:
        raise AggregationError(
            f"benchmark_version must be {BENCHMARK_VERSION!r}, got {vm.benchmark_version!r}"
        )
    if vm.judge_protocol != JUDGE_PROTOCOL:
        raise AggregationError(
            f"judge_protocol must be {JUDGE_PROTOCOL!r}, got {vm.judge_protocol!r}"
        )
    if vm.judge_provider != JUDGE_PROVIDER:
        raise AggregationError(
            f"judge_provider must be {JUDGE_PROVIDER!r}, got {vm.judge_provider!r}"
        )
    for name in ("judge_requested_model", "judge_model"):
        val = getattr(vm, name)
        if not isinstance(val, str) or not val:
            raise AggregationError(f"{name} must be a non-empty string")
        if val.lower() in _UNPINNED_MODEL_SENTINELS:
            raise AggregationError(f"{name} {val!r} is not a pinned model (Auto/latest rejected)")
    if vm.judge_requested_model != vm.judge_model:
        raise AggregationError(
            f"judge_requested_model {vm.judge_requested_model!r} != judge_model {vm.judge_model!r}"
        )
    for name in (
        "scoring_profile",
        "judge_cli_version",
        "judge_prompt_digest",
        "ground_truth_digest",
        "agent_answer_digest",
        "case_id",
        "task_type",
    ):
        val = getattr(vm, name)
        if not isinstance(val, str) or not val:
            raise AggregationError(f"{name} must be a non-empty string")


def _validate_consensus(consensus: ConsensusInfo) -> None:
    """Validate the consensus summary shape (§13.1, §13.2)."""
    if consensus.mode not in CONSENSUS_MODES:
        raise AggregationError(f"consensus.mode {consensus.mode!r} not in {CONSENSUS_MODES}")
    if not isinstance(consensus.judges, int) or isinstance(consensus.judges, bool):
        raise AggregationError(
            f"consensus.judges must be an int, got {type(consensus.judges).__name__}"
        )
    if consensus.judges < 1:
        raise AggregationError(f"consensus.judges must be >= 1, got {consensus.judges}")
    if consensus.mode == "single" and consensus.judges != 1:
        raise AggregationError(f"single-judge mode requires judges=1, got {consensus.judges}")


# ---------------------------------------------------------------------------
# Critical cap determination (§12)
# ---------------------------------------------------------------------------


def _determine_cap(
    *,
    gt_by_id: dict[str, dict[str, Any]],
    credits_dec: dict[str, Decimal],
    crit_errors: list[Mapping[str, Any]],
    cap_values: dict[str, Decimal],
) -> CriticalCap:
    """Determine the critical cap from the frozen §12 rules.

    * ``core_correctness_all_zero`` (cap 50) is computed *deterministically*
      from the GT and consensus credit: it fires when at least one
      core_correctness critical item exists and every one of them has
      consensus_credit 0. The Judge can neither create nor suppress it.
    * ``reverse_critical_relation_zero`` (cap 60) requires the Judge's semantic
      signal (a critical error with that code) AND the referenced critical
      item's consensus_credit being 0. The aggregator never infers a reverse
      relation from credit alone - that is a semantic judgment only the Judge
      can make.

    Multiple fired caps take the lowest ``cap_value`` (§12, ``cap_selection:
    min``). A cap only ever lowers the total; the caller computes
    ``capped_total = min(raw_total, cap_value)``.
    """
    triggered: list[TriggeredCap] = []

    # core_correctness_all_zero: deterministic from credits. The rule implies
    # such items exist, so a profile/GT with no core_correctness critical item
    # does not trigger the cap (no vacuous firing).
    core_crit = [
        iid
        for iid, item in gt_by_id.items()
        if item.get("critical") is True and item.get("dimension") == "core_correctness"
    ]
    if core_crit and all(credits_dec[iid] == Decimal(0) for iid in core_crit):
        triggered.append(
            TriggeredCap(
                code=CORE_CORRECTNESS_ALL_ZERO,
                cap_value=cap_values[CORE_CORRECTNESS_ALL_ZERO],
                reason=(
                    "every core_correctness critical item has consensus_credit 0 "
                    f"({', '.join(core_crit)})"
                ),
                item_ids=tuple(core_crit),
            )
        )

    # reverse_critical_relation_zero: Judge signal + credit == 0.
    reverse_items: list[str] = []
    for ce in crit_errors:
        if ce.get("code") != REVERSE_CRITICAL_RELATION_ZERO:
            continue
        iid = ce.get("item_id")
        # item_id validity (critical + exists) is verified by the caller before
        # this point; here we only enforce the credit == 0 half of the rule.
        if iid in credits_dec and credits_dec[iid] == Decimal(0):
            reverse_items.append(iid)
    if reverse_items:
        triggered.append(
            TriggeredCap(
                code=REVERSE_CRITICAL_RELATION_ZERO,
                cap_value=cap_values[REVERSE_CRITICAL_RELATION_ZERO],
                reason=(
                    "reverse critical relation item(s) at consensus_credit 0 "
                    f"({', '.join(reverse_items)})"
                ),
                item_ids=tuple(reverse_items),
            )
        )

    if not triggered:
        return CriticalCap(
            applied=False,
            cap_value=None,
            code=None,
            reason=None,
            triggered=(),
        )

    # Multiple caps: lowest cap_value wins (strictest). Ties break on code for a
    # deterministic winning code.
    triggered.sort(key=lambda t: (t.cap_value, t.code))
    winner = triggered[0]
    reason = "; ".join(f"{t.code} ({t.cap_value}): {t.reason}" for t in triggered)
    return CriticalCap(
        applied=True,
        cap_value=winner.cap_value,
        code=winner.code,
        reason=reason,
        triggered=tuple(triggered),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def aggregate(
    ground_truth: dict[str, Any],
    consensus_credits: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    critical_errors: list[Mapping[str, Any]] | None = None,
    task_profile: dict[str, Any] | None = None,
    common_profile: dict[str, Any] | None = None,
) -> AggregationResult:
    """Deterministically aggregate validated GT points and consensus credit.

    Computes ``item_score = points × consensus_credit`` per item (§11),
    dimension totals, the raw total, the critical cap (§12) and the capped
    total. All math is exact :class:`~decimal.Decimal`; nothing is rounded.

    Raises :class:`AggregationError` if a rubric item is unknown, duplicated or
    missing from ``consensus_credits`` (§10.2), if a credit is out of
    ``[0, 1]``, or if a critical error is undeclared or references a non-critical
    or unknown item.
    """
    items_raw = ground_truth.get("rubric_items")
    if not isinstance(items_raw, list) or not items_raw:
        raise AggregationError("ground truth has no rubric_items")

    # ---- Build the GT item index and reject duplicate GT item ids. ----------
    gt_by_id: dict[str, dict[str, Any]] = {}
    for item in items_raw:
        if not isinstance(item, dict):
            raise AggregationError("rubric item must be a mapping")
        iid = item.get("id")
        if not isinstance(iid, str) or not iid:
            raise AggregationError("rubric item id is missing or empty")
        if iid in gt_by_id:
            raise AggregationError(f"duplicate rubric item id {iid!r} in ground truth")
        gt_by_id[iid] = item

    # ---- Validate 1:1 matching between GT items and consensus credits. -----
    credits = _normalize_credits(consensus_credits)
    unknown = set(credits) - set(gt_by_id)
    if unknown:
        raise AggregationError(
            f"consensus credit references unknown rubric item(s): {sorted(unknown)}"
        )
    missing = set(gt_by_id) - set(credits)
    if missing:
        raise AggregationError(f"rubric item(s) without consensus credit: {sorted(missing)}")

    # ---- Validate critical errors (§10.2). ----------------------------------
    crit_errors = list(critical_errors or [])
    allowed_codes = _allowed_critical_codes(task_profile)
    critical_item_ids = {iid for iid, it in gt_by_id.items() if it.get("critical") is True}
    for ce in crit_errors:
        if not isinstance(ce, Mapping):
            raise AggregationError("critical_errors entries must be mappings")
        code = ce.get("code")
        if code not in allowed_codes:
            raise AggregationError(f"critical error code {code!r} is not declared by the profile")
        ce_item = ce.get("item_id")
        if ce_item not in critical_item_ids:
            raise AggregationError(
                f"critical error code {code!r} references non-critical or unknown item {ce_item!r}"
            )

    # ---- Item scores and dimension totals (exact Decimal). -----------------
    item_scores: list[ItemScore] = []
    dim_totals: dict[str, Decimal] = {name: Decimal(0) for name in FROZEN_DIMENSION_NAMES}
    credits_dec: dict[str, Decimal] = {}
    raw_total = Decimal(0)
    for item in items_raw:
        iid = item["id"]
        dimension = item.get("dimension")
        if dimension not in dim_totals:
            raise AggregationError(f"item {iid!r} has unknown dimension {dimension!r}")
        points = _to_decimal(item.get("points"), context=f"item {iid!r} points")
        if points <= 0:
            raise AggregationError(f"item {iid!r} points must be positive, got {points}")
        credit = _to_decimal(credits[iid], context=f"item {iid!r} consensus credit")
        if not (Decimal(0) <= credit <= Decimal(1)):
            raise AggregationError(f"item {iid!r} consensus credit {credit} out of range [0, 1]")
        credits_dec[iid] = credit
        score = points * credit
        item_scores.append(
            ItemScore(
                item_id=iid,
                dimension=dimension,
                points=points,
                consensus_credit=credit,
                item_score=score,
            )
        )
        dim_totals[dimension] += score
        raw_total += score

    # ---- Critical cap (§12). ------------------------------------------------
    critical_cap = _determine_cap(
        gt_by_id=gt_by_id,
        credits_dec=credits_dec,
        crit_errors=crit_errors,
        cap_values=_resolve_cap_values(common_profile),
    )
    capped_total = (
        min(raw_total, critical_cap.cap_value)  # type: ignore[arg-type]
        if critical_cap.applied
        else raw_total
    )

    return AggregationResult(
        items=tuple(item_scores),
        dimension_totals=dim_totals,
        raw_total=raw_total,
        critical_cap=critical_cap,
        capped_total=capped_total,
    )


def build_score(
    ground_truth: dict[str, Any],
    consensus_credits: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    version_metadata: VersionMetadata,
    consensus: ConsensusInfo,
    critical_errors: list[Mapping[str, Any]] | None = None,
    task_profile: dict[str, Any] | None = None,
    common_profile: dict[str, Any] | None = None,
    requires_human_review: bool = False,
    human_review_reasons: Sequence[str] | None = None,
    run_mode: str = "development",
) -> ScoreResult:
    """Aggregate and attach version metadata + consensus info (§11, §20).

    Validates the version-metadata invariants (pinned, agreeing models), the
    consensus summary, the run mode and the human-review reasons, then delegates
    to :func:`aggregate` for the deterministic math and cap. The returned
    :class:`ScoreResult` carries everything a formal ``score.json`` needs.
    """
    _validate_version_metadata(version_metadata)
    _validate_consensus(consensus)
    if run_mode not in RUN_MODES:
        raise AggregationError(f"run_mode {run_mode!r} not in {RUN_MODES}")

    reasons = tuple(human_review_reasons or ())
    if requires_human_review:
        if not reasons:
            raise AggregationError(
                "requires_human_review is True but no human_review_reasons given"
            )
        bad = [r for r in reasons if r not in HUMAN_REVIEW_REASONS]
        if bad:
            raise AggregationError(f"unknown human_review_reasons: {bad}")
    elif reasons:
        raise AggregationError("human_review_reasons present but requires_human_review is False")

    agg = aggregate(
        ground_truth,
        consensus_credits,
        critical_errors=critical_errors,
        task_profile=task_profile,
        common_profile=common_profile,
    )
    return ScoreResult(
        version_metadata=version_metadata,
        items=agg.items,
        dimension_totals=agg.dimension_totals,
        raw_total=agg.raw_total,
        critical_cap=agg.critical_cap,
        capped_total=agg.capped_total,
        consensus=consensus,
        requires_human_review=requires_human_review,
        human_review_reasons=reasons,
        run_mode=run_mode,
    )


def critical_cap_to_dict(cap: CriticalCap) -> dict[str, Any] | None:
    """Serialize a :class:`CriticalCap` to the score.schema.json shape.

    Returns ``None`` (JSON ``null``) when no cap fired; otherwise an object with
    ``applied``, ``cap_value``, ``code`` and ``reason`` describing the winning
    (strictest) cap.
    """
    if not cap.applied or cap.cap_value is None or cap.code is None:
        return None
    return {
        "applied": True,
        "cap_value": _num(cap.cap_value),
        "code": cap.code,
        "reason": cap.reason or "",
    }


def score_to_dict(score: ScoreResult) -> dict[str, Any]:
    """Serialize a :class:`ScoreResult` to the ``score-v1`` dict.

    The result is valid against ``schemas/score.schema.json`` (Draft 2020-12):
    the §20 identity block is flattened to top-level fields, ``critical_cap`` is
    ``null`` or the cap object, and ``human_review_reasons`` is present only
    when human review is required (§13.1, score.schema.json allOf).
    """
    vm = score.version_metadata
    result: dict[str, Any] = {
        "schema_version": SCORE_SCHEMA_VERSION,
        "benchmark_version": vm.benchmark_version,
        "judge_protocol": vm.judge_protocol,
        "scoring_profile": vm.scoring_profile,
        "judge_provider": vm.judge_provider,
        "judge_requested_model": vm.judge_requested_model,
        "judge_model": vm.judge_model,
        "judge_cli_version": vm.judge_cli_version,
        "judge_prompt_digest": vm.judge_prompt_digest,
        "ground_truth_digest": vm.ground_truth_digest,
        "agent_answer_digest": vm.agent_answer_digest,
        "case_id": vm.case_id,
        "task_type": vm.task_type,
        "items": [
            {
                "item_id": it.item_id,
                "dimension": it.dimension,
                "points": _num(it.points),
                "consensus_credit": _num(it.consensus_credit),
                "item_score": _num(it.item_score),
            }
            for it in score.items
        ],
        "dimension_totals": {
            name: _num(score.dimension_totals[name]) for name in FROZEN_DIMENSION_NAMES
        },
        "raw_total": _num(score.raw_total),
        "critical_cap": critical_cap_to_dict(score.critical_cap),
        "capped_total": _num(score.capped_total),
        "consensus": {
            "mode": score.consensus.mode,
            "judges": score.consensus.judges,
            "arbiter_used": score.consensus.arbiter_used,
            "human_review_triggered": score.consensus.human_review_triggered,
        },
        "requires_human_review": score.requires_human_review,
        "run_mode": score.run_mode,
    }
    if score.requires_human_review:
        result["human_review_reasons"] = list(score.human_review_reasons)
    return result
