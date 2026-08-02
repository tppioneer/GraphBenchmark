"""Analysis-input layer: load and normalize frozen run artifacts for reporting
(design §15--§20, §17).

This module is the read-only boundary between on-disk run artifacts and the
report aggregation layer (:mod:`report.aggregate`). It loads the frozen
artifact set of a run directory (design §17), validates the minimal shape
needed for reporting, and classifies the run into a *report status* that
the aggregator uses to decide whether the run may enter a formal aggregate.

Frozen invariants enforced here (AIS-011 task card):

* The report never calls the Judge and never re-scores. Every value comes from
  a frozen artifact on disk; nothing is inferred, interpolated or regenerated.
* Correctness is never synthesized from cost, and cost never invalidates a
  complete score (§15.2). Cost metrics are carried as a separate, independent
  block.
* A formal score (``effective-score.json``, ``score-v1``) whose requested and
  effective Judge models disagree is flagged ``version_mismatch`` and excluded
  from every formal aggregate (§13.3, §20, acceptance criterion).
* A ``score-v1``-tagged effective-score artifact with malformed or missing
  fields is isolated as ``invalid`` with a stable detail, rather than raising
  and aborting ``load_runs`` / report generation.
* ``judge_failed`` runs (manifest status ``failed`` for ``judge_score`` /
  ``effective_score``) are isolated with their failure reason; no formal score
  is generated or inferred (§13.5).
* Missing/unreadable artifacts, awaiting-judge runs and invalid runs are all
  explicitly isolated rather than silently dropped (acceptance criterion).

The module depends only on :mod:`scoring.profiles` / :mod:`scoring.aggregator`
for frozen dimension names and version constants (read-only); it never imports
Judge, Runner or scoring-core *logic*, and it performs zero Judge calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from scoring.aggregator import (
    SCORE_SCHEMA_VERSION,
)
from scoring.profiles import FROZEN_DIMENSION_NAMES

# ---------------------------------------------------------------------------
# Frozen constants mirrored from the artifact contracts (design §17, §20).
# ---------------------------------------------------------------------------

#: Artifact filenames within a run directory (design §17).
MANIFEST_FILENAME = "manifest.json"
RUN_METADATA_FILENAME = "run-metadata.json"
POLICY_RESULT_FILENAME = "policy-result.json"
AGENT_ANSWER_FILENAME = "agent-answer.json"
EFFECTIVE_SCORE_FILENAME = "effective-score.json"
JUDGE_SCORE_FILENAME = "judge-score.json"
JUDGE_A_FILENAME = "judge-a.json"
JUDGE_B_FILENAME = "judge-b.json"
JUDGE_C_FILENAME = "judge-c.json"

#: Schema-version constants the loader recognizes on disk.
MANIFEST_SCHEMA_VERSION = "manifest-v1"
RUN_METADATA_SCHEMA_VERSION = "run-metadata-v1"
POLICY_RESULT_SCHEMA_VERSION = "policy-result-v1"
AGENT_ANSWER_SCHEMA_VERSION = "agent-answer-v1"

#: The §20 identity-block fields a formal score carries.
VERSION_IDENTITY_FIELDS: tuple[str, ...] = (
    "benchmark_version",
    "judge_protocol",
    "scoring_profile",
    "judge_provider",
    "judge_requested_model",
    "judge_model",
    "judge_cli_version",
    "judge_prompt_digest",
    "ground_truth_digest",
)

#: The dimensions every formal score reports, in frozen order (§5).
DIMENSION_NAMES: tuple[str, ...] = tuple(FROZEN_DIMENSION_NAMES)


class ReportError(Exception):
    """Raised when the analysis-input layer cannot load a run directory.

    This is a hard loader failure (e.g. the run directory does not exist), not
    a soft isolation. Soft isolation (missing artifact, invalid run, etc.) is
    expressed via :data:`RunReportStatus.ISOLATED` and an ``isolation_reason``.
    """


# ---------------------------------------------------------------------------
# Report status classification
# ---------------------------------------------------------------------------


class RunReportStatus(str, Enum):
    """The report-layer status of one run.

    Only :data:`SCORED` runs may enter a formal aggregate. Every other status
    is isolated with a human-readable ``isolation_reason`` on the
    :class:`RunRecord`. The design run state machine (§15.1) is re-derived
    here purely from frozen artifacts, without calling the Runner.
    """

    SCORED = "scored"
    ISOLATED = "isolated"


# The specific isolation reasons (carried on RunRecord.isolation_reason).
ISOLATION_VERSION_MISMATCH = "version_mismatch"
ISOLATION_JUDGE_FAILED = "judge_failed"
ISOLATION_AWAITING_JUDGE = "awaiting_judge"
ISOLATION_VALID_ZERO = "valid_zero"
ISOLATION_INVALID = "invalid"
ISOLATION_FAILED = "failed"
ISOLATION_MISSING_ARTIFACT = "missing_artifact"

#: The set of valid isolation reasons.
ISOLATION_REASONS: frozenset[str] = frozenset(
    {
        ISOLATION_VERSION_MISMATCH,
        ISOLATION_JUDGE_FAILED,
        ISOLATION_AWAITING_JUDGE,
        ISOLATION_VALID_ZERO,
        ISOLATION_INVALID,
        ISOLATION_FAILED,
        ISOLATION_MISSING_ARTIFACT,
    }
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionIdentity:
    """The §20 identity block extracted from a formal score.

    Two scores may be formally aggregated together only if their compatibility
    key (see :func:`compatibility_key`) is identical. The requested and
    effective Judge models must agree (§13.3, §20); a disagreement is flagged
    upstream and the run is isolated as ``version_mismatch``.
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

    @classmethod
    def from_score(cls, score: Mapping[str, Any]) -> "VersionIdentity":
        """Build a :class:`VersionIdentity` from a ``score-v1`` mapping."""
        return cls(
            benchmark_version=str(score["benchmark_version"]),
            judge_protocol=str(score["judge_protocol"]),
            scoring_profile=str(score["scoring_profile"]),
            judge_provider=str(score["judge_provider"]),
            judge_requested_model=str(score["judge_requested_model"]),
            judge_model=str(score["judge_model"]),
            judge_cli_version=str(score["judge_cli_version"]),
            judge_prompt_digest=str(score["judge_prompt_digest"]),
            ground_truth_digest=str(score["ground_truth_digest"]),
            agent_answer_digest=str(score["agent_answer_digest"]),
            case_id=str(score["case_id"]),
            task_type=str(score["task_type"]),
        )

    @property
    def models_agree(self) -> bool:
        """True when the requested and effective Judge models are identical.

        A disagreement invalidates the run for formal aggregation (§13.3, §20,
        acceptance criterion).
        """
        return self.judge_requested_model == self.judge_model


@dataclass(frozen=True)
class AgentCost:
    """Runner-collected agent cost metrics (run-metadata metrics, §8.6, §15.2).

    Process metrics never enter the correctness total (invariant); they are
    reported as independent cost indicators only.
    """

    elapsed_ms: int
    input_tokens: int
    output_tokens: int
    tool_call_count: int
    files_read_count: int
    graph_query_count: int
    search_query_count: int

    @classmethod
    def from_metrics(cls, metrics: Mapping[str, Any]) -> "AgentCost":
        return cls(
            elapsed_ms=int(metrics["elapsed_ms"]),
            input_tokens=int(metrics["input_tokens"]),
            output_tokens=int(metrics["output_tokens"]),
            tool_call_count=int(metrics["tool_call_count"]),
            files_read_count=int(metrics["files_read_count"]),
            graph_query_count=int(metrics["graph_query_count"]),
            search_query_count=int(metrics["search_query_count"]),
        )


@dataclass(frozen=True)
class JudgeCost:
    """Judge-side cost metrics (§15.2: Judge token, latency, retries).

    These are OPTIONAL: the current v1 artifact set does not yet persist a
    formal Judge-cost artifact. When ``judge-score.json`` carries a
    ``judge_cost`` block the loader reads it; otherwise every field is ``None``
    and the report isolates Judge cost as not available. Cost is always
    independent of correctness: a high Judge cost never invalidates a complete
    score (§15.2).
    """

    judge_call_count: int | None
    total_latency_ms: int | None
    total_retries: int | None
    input_tokens: int | None
    output_tokens: int | None

    @property
    def available(self) -> bool:
        """True when any Judge-cost field was materialized."""
        return self.judge_call_count is not None


@dataclass(frozen=True)
class CostMetrics:
    """All cost metrics for one run, kept strictly independent of correctness."""

    agent: AgentCost | None
    judge: JudgeCost


@dataclass(frozen=True)
class ItemVerdict:
    """One rubric item formal verdict (from effective-score.json)."""

    item_id: str
    dimension: str
    points: Decimal
    consensus_credit: Decimal
    item_score: Decimal


@dataclass(frozen=True)
class ScoreView:
    """The correctness view extracted from a score-v1 effective score.

    All numeric values are exact Decimal; display rounding happens only in the
    visualization layer (§10.1).
    """

    raw_total: Decimal
    capped_total: Decimal
    critical_cap_applied: bool
    critical_cap_value: Decimal | None
    critical_cap_code: str | None
    dimension_totals: dict[str, Decimal]
    items: tuple[ItemVerdict, ...]
    consensus_mode: str
    consensus_judges: int
    arbiter_used: bool
    human_review_triggered: bool
    requires_human_review: bool
    human_review_reasons: tuple[str, ...]
    run_mode: str


@dataclass(frozen=True)
class JudgeDisagreement:
    """Per-run Judge-consensus/stability indicators (§13, §19).

    Derived from the consensus block of the effective score and, when present,
    the individual judge-a/b/c.json verdicts. The report uses these for the
    Judge-disagreement and review-coverage summary sections, never for
    correctness.
    """

    consensus_mode: str
    judges: int
    arbiter_used: bool
    human_review_triggered: bool
    #: Number of rubric items where Judge A and Judge B disagreed, counted with
    #: a uniform |credit_A - credit_B| > 0.25 rule (§13.1 step 3). The full
    #: §13.1 trigger is GT-aware (any non-zero difference on a critical item,
    #: >0.25 on a non-critical item), but criticality is not recoverable from
    #: the judge output alone (the GT carries the critical flag), so the uniform
    #: threshold is applied to all items. This can under-count: a critical item
    #: with a non-zero but sub-threshold (<=0.25) A/B difference is not flagged
    #: here, though the GT-aware trigger would flag it (see
    #: :func:`_count_ab_disagreement`). None when individual judge outputs are
    #: unavailable (e.g. single-judge development mode or missing artifacts).
    ab_disagreement_items: int | None
    #: The number of judges whose per-item verdicts were available.
    available_judge_outputs: int


@dataclass(frozen=True)
class RunRecord:
    """The analyzed, normalized view of one run consumed by the aggregator."""

    run_id: str
    run_dir: Path
    status: RunReportStatus
    #: Populated only for ISOLATED runs; one of ISOLATION_REASONS.
    isolation_reason: str | None
    #: Free-text detail expanding on isolation_reason.
    isolation_detail: str
    #: The §20 identity block; None when no formal score is available.
    version_identity: VersionIdentity | None
    #: The formal correctness view; None for non-SCORED runs.
    score: ScoreView | None
    #: The Judge-consensus/stability indicators; None for non-SCORED runs.
    judge_disagreement: JudgeDisagreement | None
    #: Agent identity carried by run-metadata.
    agent: str | None
    agent_model: str | None
    tool_policy: str | None
    #: Cost metrics (agent always present for a loadable run; judge optional).
    cost: CostMetrics
    #: Policy-compliance verdict.
    policy_valid: bool | None
    policy_violation_count: int
    #: Agent-answer status string.
    answer_status: str | None
    #: The case id from the agent-answer (fallback when no version identity).
    answer_case_id: str | None
    #: Artifact status map from the manifest (name -> status).
    artifact_status: dict[str, str]
    #: The raw effective-score document; kept for audit/re-rendering.
    raw_score: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def is_scored(self) -> bool:
        """True when this run carries a version-consistent formal score."""
        return self.status is RunReportStatus.SCORED

    @property
    def case_id(self) -> str | None:
        """The case id: from the version identity, falling back to the
        agent-answer's ``case_id`` when no formal score is available."""
        if self.version_identity is not None:
            return self.version_identity.case_id
        return self.answer_case_id


# ---------------------------------------------------------------------------
# Compatibility key (design §20)
# ---------------------------------------------------------------------------

#: The dimensions across which formal scores must NOT be mixed (§20). The
#: per-instance digests (judge_prompt_digest, ground_truth_digest,
#: agent_answer_digest) are identity/audit fields that naturally differ per
#: case/answer and are NOT aggregation-blocking; the prompt major version, GT
#: revision and consensus-algorithm major version are frozen at v1 and encoded
#: in benchmark_version + judge_protocol. The effective Judge model is the
#: blocking model dimension (requested must equal effective; verified
#: upstream). CLI version is included because it affects reproducibility.
COMPATIBILITY_DIMENSIONS: tuple[str, ...] = (
    "benchmark_version",
    "judge_protocol",
    "scoring_profile",
    "judge_provider",
    "judge_model",
    "judge_cli_version",
)


def compatibility_key(identity: VersionIdentity) -> tuple[str, ...]:
    """The aggregation-compatibility key for a version identity (§20).

    Runs with different keys cannot be formally aggregated together.
    """
    return tuple(getattr(identity, name) for name in COMPATIBILITY_DIMENSIONS)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from path; return None if absent/unreadable."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _artifact_status_map(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    """Build a name->status map from a manifest (empty when no manifest)."""
    if not manifest:
        return {}
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return {}
    result: dict[str, str] = {}
    for entry in artifacts:
        if isinstance(entry, Mapping):
            name = entry.get("name")
            status = entry.get("status")
            if isinstance(name, str) and isinstance(status, str):
                result[name] = status
    return result


def _manifest_status(artifact_status: dict[str, str], name: str) -> str | None:
    """The manifest status for name (present/absent/failed/not_applicable)."""
    return artifact_status.get(name)


def _build_score_view(score: Mapping[str, Any]) -> ScoreView:
    """Extract the correctness view from a score-v1 mapping."""
    cap = score.get("critical_cap")
    cap_applied = isinstance(cap, Mapping) and bool(cap.get("applied"))
    cap_value: Decimal | None = None
    cap_code: str | None = None
    if cap_applied and isinstance(cap, Mapping):
        cap_value = Decimal(str(cap.get("cap_value")))
        cap_code = str(cap.get("code")) if cap.get("code") else None

    dim_totals = {name: Decimal(str(score["dimension_totals"][name])) for name in DIMENSION_NAMES}
    items = tuple(
        ItemVerdict(
            item_id=str(it["item_id"]),
            dimension=str(it["dimension"]),
            points=Decimal(str(it["points"])),
            consensus_credit=Decimal(str(it["consensus_credit"])),
            item_score=Decimal(str(it["item_score"])),
        )
        for it in score.get("items", [])
    )
    consensus = score.get("consensus", {})
    reasons = score.get("human_review_reasons") or []
    return ScoreView(
        raw_total=Decimal(str(score["raw_total"])),
        capped_total=Decimal(str(score["capped_total"])),
        critical_cap_applied=cap_applied,
        critical_cap_value=cap_value,
        critical_cap_code=cap_code,
        dimension_totals=dim_totals,
        items=items,
        consensus_mode=str(consensus.get("mode", "")),
        consensus_judges=int(consensus.get("judges", 0)),
        arbiter_used=bool(consensus.get("arbiter_used", False)),
        human_review_triggered=bool(consensus.get("human_review_triggered", False)),
        requires_human_review=bool(score.get("requires_human_review", False)),
        human_review_reasons=tuple(str(r) for r in reasons),
        run_mode=str(score.get("run_mode", "")),
    )


def _count_ab_disagreement(
    judge_outputs: list[Mapping[str, Any]],
) -> int | None:
    """Count rubric items where Judge A and Judge B disagreed (§13.1 steps 2-3).

    Returns None when fewer than two judge outputs are available. A critical
    item counts as disagreement on any A/B credit difference; a non-critical
    item counts when |credit_A - credit_B| > 0.25 (§13.1). Whether an item is
    critical is not recoverable from the judge output alone (the GT carries the
    critical flag), so the uniform §13.1 step-3 threshold (>0.25) is applied to
    all items. This **under-counts** relative to the full GT-aware trigger:
    whenever a critical item has a non-zero but sub-threshold (<=0.25) A/B
    difference, the GT-aware trigger (any difference on a critical item) would
    flag it as disagreement, but the uniform >0.25 rule does not. The
    under-count is one-directional -- the uniform rule never flags an item the
    GT-aware trigger would skip -- so it is acceptable for a reporting-only
    indicator.
    """
    if len(judge_outputs) < 2:
        return None
    a = {str(it["item_id"]): Decimal(str(it["credit"])) for it in judge_outputs[0].get("items", [])}
    b = {str(it["item_id"]): Decimal(str(it["credit"])) for it in judge_outputs[1].get("items", [])}
    threshold = Decimal("0.25")
    count = 0
    for iid, credit_a in a.items():
        credit_b = b.get(iid)
        if credit_b is None:
            continue
        if abs(credit_a - credit_b) > threshold:
            count += 1
    return count


def _build_judge_disagreement(
    score: Mapping[str, Any],
    judge_outputs: list[Mapping[str, Any]],
) -> JudgeDisagreement:
    """Build the per-run Judge-disagreement view (§13)."""
    consensus = score.get("consensus", {})
    return JudgeDisagreement(
        consensus_mode=str(consensus.get("mode", "")),
        judges=int(consensus.get("judges", 0)),
        arbiter_used=bool(consensus.get("arbiter_used", False)),
        human_review_triggered=bool(consensus.get("human_review_triggered", False)),
        ab_disagreement_items=_count_ab_disagreement(judge_outputs),
        available_judge_outputs=len(judge_outputs),
    )


def _build_judge_cost(judge_score: Mapping[str, Any] | None) -> JudgeCost:
    """Read the OPTIONAL Judge-cost block from judge-score.json (§15.2).

    The block is optional and defensively parsed: any missing/non-int field
    becomes None. When the whole file is absent, every field is None and the
    report isolates Judge cost as not available.
    """
    block = judge_score.get("judge_cost") if judge_score else None
    if not isinstance(block, Mapping):
        return JudgeCost(
            judge_call_count=None,
            total_latency_ms=None,
            total_retries=None,
            input_tokens=None,
            output_tokens=None,
        )

    def _opt_int(key: str) -> int | None:
        val = block.get(key)
        if isinstance(val, bool) or not isinstance(val, int):
            return None
        return val

    return JudgeCost(
        judge_call_count=_opt_int("judge_call_count"),
        total_latency_ms=_opt_int("total_latency_ms"),
        total_retries=_opt_int("total_retries"),
        input_tokens=_opt_int("input_tokens"),
        output_tokens=_opt_int("output_tokens"),
    )


def _classify_isolated_run(
    *,
    artifact_status: dict[str, str],
    agent_answer: Mapping[str, Any] | None,
    policy_result: Mapping[str, Any] | None,
    run_metadata: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Classify a non-SCORED run into a specific isolation reason + detail.

    Returns (isolation_reason, isolation_detail). Precedence follows the design
    run state machine: a failed judge attempt dominates, then missing required
    artifacts, then execution failure, then policy invalidity, then the answer
    state.
    """
    for name in ("effective_score", "judge_score"):
        if _manifest_status(artifact_status, name) == "failed":
            return ISOLATION_JUDGE_FAILED, f"artifact {name!r} status is failed"

    if run_metadata is None or policy_result is None:
        missing = [
            n
            for n, doc in (
                (RUN_METADATA_FILENAME, run_metadata),
                (POLICY_RESULT_FILENAME, policy_result),
            )
            if doc is None
        ]
        return ISOLATION_MISSING_ARTIFACT, f"unreadable/absent: {', '.join(missing)}"

    if agent_answer is None:
        return ISOLATION_FAILED, "no agent-answer artifact (execution failed)"

    answer_status = str(agent_answer.get("status", ""))
    policy_valid = bool(policy_result.get("valid", False))

    if not policy_valid or answer_status == "invalid":
        detail = f"answer_status={answer_status!r}, policy_valid={policy_valid}"
        return ISOLATION_INVALID, detail

    if answer_status in ("empty", "refused"):
        return (
            ISOLATION_VALID_ZERO,
            f"answer_status={answer_status!r} (deterministic 0, not materialized)",
        )

    return (
        ISOLATION_AWAITING_JUDGE,
        f"answer_status={answer_status!r}, no effective-score.json",
    )


def load_run(run_dir: Path) -> RunRecord:
    """Load and classify one run directory into a :class:`RunRecord`.

    Reads the frozen artifact set (design §17), validates the minimal shape
    needed for reporting, and classifies the run. A :class:`ReportError` is
    raised only for a hard loader failure (missing/unreadable directory); soft
    isolation is expressed via :data:`RunReportStatus.ISOLATED`.

    The loader performs zero Judge calls and never re-scores.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise ReportError(f"run directory does not exist: {run_dir}")
    run_id = run_dir.name

    manifest = _read_json(run_dir / MANIFEST_FILENAME)
    run_metadata = _read_json(run_dir / RUN_METADATA_FILENAME)
    policy_result = _read_json(run_dir / POLICY_RESULT_FILENAME)
    agent_answer = _read_json(run_dir / AGENT_ANSWER_FILENAME)
    effective_score = _read_json(run_dir / EFFECTIVE_SCORE_FILENAME)
    judge_score = _read_json(run_dir / JUDGE_SCORE_FILENAME)
    judge_a = _read_json(run_dir / JUDGE_A_FILENAME)
    judge_b = _read_json(run_dir / JUDGE_B_FILENAME)
    judge_c = _read_json(run_dir / JUDGE_C_FILENAME)

    artifact_status = _artifact_status_map(manifest)

    agent: str | None = None
    agent_model: str | None = None
    tool_policy: str | None = None
    agent_cost: AgentCost | None = None
    if run_metadata is not None:
        agent = str(run_metadata.get("agent", "")) or None
        agent_model = str(run_metadata.get("agent_model", "")) or None
        tool_policy = str(run_metadata.get("tool_policy", "")) or None
        metrics = run_metadata.get("metrics")
        if isinstance(metrics, Mapping):
            try:
                agent_cost = AgentCost.from_metrics(metrics)
            except (KeyError, TypeError, ValueError):
                agent_cost = None

    policy_valid: bool | None = None
    policy_violation_count = 0
    if policy_result is not None:
        policy_valid = bool(policy_result.get("valid", False))
        violations = policy_result.get("violations")
        if isinstance(violations, list):
            policy_violation_count = len(violations)

    answer_status = str(agent_answer.get("status", "")) if agent_answer else None
    answer_case_id = str(agent_answer.get("case_id", "")) or None if agent_answer else None
    judge_cost = _build_judge_cost(judge_score)
    cost = CostMetrics(agent=agent_cost, judge=judge_cost)

    if (
        effective_score is not None
        and effective_score.get("schema_version") == SCORE_SCHEMA_VERSION
    ):
        # A score-v1-tagged artifact with malformed/missing fields is isolated
        # as ``invalid`` with a stable detail rather than raising and aborting
        # ``load_runs`` / report generation (R2). The shape exceptions a
        # malformed document can trigger (missing key, wrong type, bad Decimal
        # coercion, non-mapping nested block) are all caught here.
        try:
            identity = VersionIdentity.from_score(effective_score)
            if not identity.models_agree:
                detail = (
                    f"judge_requested_model={identity.judge_requested_model!r} != "
                    f"judge_model={identity.judge_model!r}"
                )
                return RunRecord(
                    run_id=run_id,
                    run_dir=run_dir,
                    status=RunReportStatus.ISOLATED,
                    isolation_reason=ISOLATION_VERSION_MISMATCH,
                    isolation_detail=detail,
                    version_identity=identity,
                    score=None,
                    judge_disagreement=None,
                    agent=agent,
                    agent_model=agent_model,
                    tool_policy=tool_policy,
                    cost=cost,
                    policy_valid=policy_valid,
                    policy_violation_count=policy_violation_count,
                    answer_status=answer_status,
                    answer_case_id=answer_case_id,
                    artifact_status=artifact_status,
                    raw_score=effective_score,
                )

            judge_outputs = [j for j in (judge_a, judge_b, judge_c) if j is not None]
            score_view = _build_score_view(effective_score)
            disagreement = _build_judge_disagreement(effective_score, judge_outputs)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
            return RunRecord(
                run_id=run_id,
                run_dir=run_dir,
                status=RunReportStatus.ISOLATED,
                isolation_reason=ISOLATION_INVALID,
                isolation_detail=f"malformed score-v1 effective-score artifact: {exc}",
                version_identity=None,
                score=None,
                judge_disagreement=None,
                agent=agent,
                agent_model=agent_model,
                tool_policy=tool_policy,
                cost=cost,
                policy_valid=policy_valid,
                policy_violation_count=policy_violation_count,
                answer_status=answer_status,
                answer_case_id=answer_case_id,
                artifact_status=artifact_status,
                raw_score=effective_score,
            )
        return RunRecord(
            run_id=run_id,
            run_dir=run_dir,
            status=RunReportStatus.SCORED,
            isolation_reason=None,
            isolation_detail="",
            version_identity=identity,
            score=score_view,
            judge_disagreement=disagreement,
            agent=agent,
            agent_model=agent_model,
            tool_policy=tool_policy,
            cost=cost,
            policy_valid=policy_valid,
            policy_violation_count=policy_violation_count,
            answer_status=answer_status,
            answer_case_id=answer_case_id,
            artifact_status=artifact_status,
            raw_score=effective_score,
        )

    reason, detail = _classify_isolated_run(
        artifact_status=artifact_status,
        agent_answer=agent_answer,
        policy_result=policy_result,
        run_metadata=run_metadata,
    )
    return RunRecord(
        run_id=run_id,
        run_dir=run_dir,
        status=RunReportStatus.ISOLATED,
        isolation_reason=reason,
        isolation_detail=detail,
        version_identity=None,
        score=None,
        judge_disagreement=None,
        agent=agent,
        agent_model=agent_model,
        tool_policy=tool_policy,
        cost=cost,
        policy_valid=policy_valid,
        policy_violation_count=policy_violation_count,
        answer_status=answer_status,
        answer_case_id=answer_case_id,
        artifact_status=artifact_status,
        raw_score=None,
    )


def load_runs(runs_root: Path) -> list[RunRecord]:
    """Load every run directory directly under ``runs_root``.

    Each immediate sub-directory is treated as a run directory (its name is the
    run id). Directories without a loadable manifest are still loaded (they
    classify as ``missing_artifact``); the loader never raises for a soft
    isolation. Sort by run id for deterministic ordering.
    """
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        raise ReportError(f"runs root does not exist: {runs_root}")
    records: list[RunRecord] = []
    for child in sorted(runs_root.iterdir()):
        if child.is_dir():
            records.append(load_run(child))
    return records
