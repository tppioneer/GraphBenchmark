"""Report aggregation: case, paired and summary views from frozen artifacts
(design §16, §19, §20).

This module turns a list of :class:`~report.analysis_input.RunRecord` objects
into the four report views required by AIS-011:

* **Correctness** -- per-case outcome totals, five dimensions, critical cap,
  item verdicts, consensus/arbiter and human-review state; cross-case paired
  *absolute* score differences (Graph minus Grep) for runs sharing the same
  compatibility key (benchmark version, Judge protocol, scoring profile,
  Judge provider/model/CLI version) as well as case and agent identity
  (§16.1, §20).
* **Compliance** -- artifact validity, policy-compliance verdicts and the
  version-compatibility matrix (§20).
* **Stability** -- Judge consensus modes, arbiter usage, A/B disagreement
  counts and human-review coverage (§13, §19).
* **Cost** -- independent agent + Judge cost metrics, never folded into a
  correctness total (§15.2).

Invariants (AIS-011 task card):

* The report consumes only absolute scores and paired score differences. It
  never generates or displays a Pairwise preference (§16.2).
* Incompatible versions never enter the same formal aggregate (§20). Runs with
  a requested/effective Judge-model mismatch are isolated as ``version_mismatch``
  and excluded from every formal aggregate.
* ``judge_failed`` runs are listed separately with their failure reason; no
  formal score is generated or inferred (§13.5).
* Correctness is never synthesized from cost (§15.2). A complete score is never
  invalidated by high cost.
* The aggregation is a pure function of the input records; it performs **zero**
  Judge calls. :data:`JUDGE_CALL_COUNT` is always 0 and is carried on every
  report bundle as auditable evidence (acceptance criterion).

All score arithmetic uses :class:`~decimal.Decimal`; display rounding happens
only in :mod:`report.visualization`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from report.analysis_input import (
    DIMENSION_NAMES,
    ISOLATION_AWAITING_JUDGE,
    ISOLATION_FAILED,
    ISOLATION_INVALID,
    ISOLATION_JUDGE_FAILED,
    ISOLATION_MISSING_ARTIFACT,
    ISOLATION_REASONS,
    ISOLATION_VALID_ZERO,
    ISOLATION_VERSION_MISMATCH,
    RunRecord,
    compatibility_key,
)

#: Auditable proof that the report performed zero Judge calls (acceptance
#: criterion: "相同 artifact 重算产生稳定结果且 Judge 调用计数为零").
JUDGE_CALL_COUNT: int = 0

#: The tool policies that form a Graph/Grep comparison pair (§16.1).
GRAPH_POLICY = "graph"
GREP_POLICY = "grep"

#: Decimal rounding precision for display-only values (§10.1). The report core
#: keeps exact Decimals; this is used only by the ``to_dict`` serializers.
_DISPLAY_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _d(value: Any) -> Decimal:
    """Coerce to Decimal via str (lossless for the frozen credit/point set)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _num(value: Decimal) -> int | float:
    """Convert an exact Decimal to a JSON number (int when integral)."""
    integral = int(value)
    return integral if value == integral else float(value)


def _round2(value: Decimal) -> Decimal:
    """Round to 2 decimal places (display only; core stays exact)."""
    return value.quantize(_DISPLAY_PLACES)


# ---------------------------------------------------------------------------
# Compatibility matrix (§20)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatibilityGroup:
    """One version-compatible group of runs (§20).

    All runs in a group share the same compatibility key (benchmark version,
    Judge protocol, Profile, Judge model, CLI version). Only SCORED runs in a
    group may be formally aggregated; isolated runs are listed for audit.
    """

    key: tuple[str, ...]
    benchmark_version: str
    judge_protocol: str
    scoring_profile: str
    judge_provider: str
    judge_model: str
    judge_cli_version: str
    scored_run_ids: tuple[str, ...]
    isolated_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompatibilityMatrix:
    """The full version-compatibility matrix across all loaded runs (§20).

    Each group is independently aggregatable. Runs in different groups are
    never mixed in a formal aggregate. The matrix is the delivery-contract
    "聚合兼容性矩阵" (aggregation compatibility matrix).
    """

    groups: tuple[CompatibilityGroup, ...]
    dimensions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "groups": [
                {
                    "key": list(g.key),
                    "benchmark_version": g.benchmark_version,
                    "judge_protocol": g.judge_protocol,
                    "scoring_profile": g.scoring_profile,
                    "judge_provider": g.judge_provider,
                    "judge_model": g.judge_model,
                    "judge_cli_version": g.judge_cli_version,
                    "scored_run_count": len(g.scored_run_ids),
                    "isolated_run_count": len(g.isolated_run_ids),
                    "scored_run_ids": list(g.scored_run_ids),
                    "isolated_run_ids": list(g.isolated_run_ids),
                }
                for g in self.groups
            ],
        }


def build_compatibility_matrix(records: list[RunRecord]) -> CompatibilityMatrix:
    """Build the version-compatibility matrix from run records (§20).

    Only SCORED runs contribute a compatibility key (they carry a version
    identity). Isolated runs are attributed to the group whose key matches
    their version identity when available (e.g. ``version_mismatch`` runs
    still carry an identity); otherwise they are listed under a synthetic
    ``isolated-no-identity`` group for audit.
    """
    from report.analysis_input import COMPATIBILITY_DIMENSIONS

    groups: dict[tuple[str, ...], dict[str, Any]] = {}

    for rec in records:
        if rec.version_identity is not None:
            key = compatibility_key(rec.version_identity)
        else:
            key = ("__isolated_no_identity__",)
        if key not in groups:
            ident = rec.version_identity
            groups[key] = {
                "key": key,
                "benchmark_version": ident.benchmark_version if ident else "n/a",
                "judge_protocol": ident.judge_protocol if ident else "n/a",
                "scoring_profile": ident.scoring_profile if ident else "n/a",
                "judge_provider": ident.judge_provider if ident else "n/a",
                "judge_model": ident.judge_model if ident else "n/a",
                "judge_cli_version": ident.judge_cli_version if ident else "n/a",
                "scored": [],
                "isolated": [],
            }
        if rec.is_scored:
            groups[key]["scored"].append(rec.run_id)
        else:
            groups[key]["isolated"].append(rec.run_id)

    sorted_keys = sorted(groups.keys())
    group_objs = tuple(
        CompatibilityGroup(
            key=tuple(g["key"]),
            benchmark_version=g["benchmark_version"],
            judge_protocol=g["judge_protocol"],
            scoring_profile=g["scoring_profile"],
            judge_provider=g["judge_provider"],
            judge_model=g["judge_model"],
            judge_cli_version=g["judge_cli_version"],
            scored_run_ids=tuple(sorted(g["scored"])),
            isolated_run_ids=tuple(sorted(g["isolated"])),
        )
        for g in (groups[k] for k in sorted_keys)
    )
    return CompatibilityMatrix(
        groups=group_objs,
        dimensions=COMPATIBILITY_DIMENSIONS,
    )


# ---------------------------------------------------------------------------
# Paired score difference (§16.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedScoreDiff:
    """The paired absolute score difference for one Graph/Grep pair (§16.1).

    The main statistic is ``total_diff = graph.capped_total - grep.capped_total``
    (an absolute score difference, never a Pairwise preference, §16.2).
    Per-dimension and per-item differences are carried alongside for detail.
    All values are exact Decimal; display rounding happens in visualization.
    """

    case_id: str
    agent: str
    agent_model: str
    scoring_profile: str
    judge_model: str
    graph_run_id: str
    grep_run_id: str
    graph_total: Decimal
    grep_total: Decimal
    total_diff: Decimal
    dimension_diffs: dict[str, Decimal]
    graph_raw_total: Decimal
    grep_raw_total: Decimal
    #: True when either side has a critical cap applied (§12).
    graph_cap_applied: bool
    grep_cap_applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "agent": self.agent,
            "agent_model": self.agent_model,
            "scoring_profile": self.scoring_profile,
            "judge_model": self.judge_model,
            "graph_run_id": self.graph_run_id,
            "grep_run_id": self.grep_run_id,
            "graph_total": _num(self.graph_total),
            "grep_total": _num(self.grep_total),
            "total_diff": _num(self.total_diff),
            "dimension_diffs": {k: _num(v) for k, v in self.dimension_diffs.items()},
            "graph_raw_total": _num(self.graph_raw_total),
            "grep_raw_total": _num(self.grep_raw_total),
            "graph_cap_applied": self.graph_cap_applied,
            "grep_cap_applied": self.grep_cap_applied,
        }


def _pairing_key(rec: RunRecord) -> tuple[str, ...]:
    """The within-group pairing key: compatibility dimensions + (case, agent).

    Runs may be paired only when they share the *full* §20 compatibility key
    (benchmark version, Judge protocol, scoring profile, Judge provider, Judge
    model, CLI version) as well as case and agent identity (§16.1). This
    prevents a paired absolute aggregate from crossing any available
    compatibility dimension (R1): two same-case runs judged by different Judge
    models -- or differing on any other compatibility dimension -- never enter
    the same formal paired aggregate.

    The ``repeat`` dimension is not yet carried by any v1 artifact, so it is
    not included; when multiple candidates exist for the same policy the
    earliest run id is paired deterministically.
    """
    ident = rec.version_identity
    assert ident is not None  # scored runs always carry a version identity
    return (
        *compatibility_key(ident),
        ident.case_id,
        rec.agent or "",
        rec.agent_model or "",
    )


def _build_paired_diff(graph: RunRecord, grep: RunRecord) -> PairedScoreDiff:
    """Build a :class:`PairedScoreDiff` from a scored Graph/Grep pair."""
    assert graph.score is not None and grep.score is not None
    g_score = graph.score
    p_score = grep.score
    dim_diffs = {
        name: g_score.dimension_totals[name] - p_score.dimension_totals[name]
        for name in DIMENSION_NAMES
    }
    ident = graph.version_identity
    assert ident is not None
    return PairedScoreDiff(
        case_id=ident.case_id,
        agent=graph.agent or "",
        agent_model=graph.agent_model or "",
        scoring_profile=ident.scoring_profile,
        judge_model=ident.judge_model,
        graph_run_id=graph.run_id,
        grep_run_id=grep.run_id,
        graph_total=g_score.capped_total,
        grep_total=p_score.capped_total,
        total_diff=g_score.capped_total - p_score.capped_total,
        dimension_diffs=dim_diffs,
        graph_raw_total=g_score.raw_total,
        grep_raw_total=p_score.raw_total,
        graph_cap_applied=g_score.critical_cap_applied,
        grep_cap_applied=p_score.critical_cap_applied,
    )


# ---------------------------------------------------------------------------
# Case report (§19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseRunDetail:
    """One run within a case report, carrying its correctness/compliance/cost
    summary for display alongside its peers."""

    run_id: str
    tool_policy: str | None
    status: str
    isolation_reason: str | None
    isolation_detail: str
    #: Correctness (None for non-SCORED runs).
    capped_total: Decimal | None
    raw_total: Decimal | None
    dimension_totals: dict[str, Decimal] | None
    critical_cap_applied: bool
    critical_cap_code: str | None
    items: tuple[dict[str, Any], ...]
    #: Consensus / arbiter / human-review state (§13, §19).
    consensus_mode: str | None
    consensus_judges: int | None
    arbiter_used: bool
    human_review_triggered: bool
    requires_human_review: bool
    human_review_reasons: tuple[str, ...]
    ab_disagreement_items: int | None
    run_mode: str | None
    #: Compliance.
    policy_valid: bool | None
    policy_violation_count: int
    answer_status: str | None
    #: Cost (independent, §15.2).
    agent_cost: dict[str, int] | None
    judge_cost_available: bool
    judge_call_count: int | None
    judge_total_latency_ms: int | None
    judge_total_retries: int | None


@dataclass(frozen=True)
class CaseReport:
    """The per-case report (§19): all runs for one case + any paired diff.

    A case is identified by ``case_id``. The report lists every run for the
    case (scored and isolated), the paired Graph/Grep absolute score
    difference when a pair exists, and flags when pairing is incomplete.
    """

    case_id: str
    runs: tuple[CaseRunDetail, ...]
    paired_diff: PairedScoreDiff | None
    #: Why no pair was formed (e.g. "missing grep-policy run").
    pairing_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_count": len(self.runs),
            "paired_diff": self.paired_diff.to_dict() if self.paired_diff else None,
            "pairing_note": self.pairing_note,
            "runs": [
                {
                    "run_id": r.run_id,
                    "tool_policy": r.tool_policy,
                    "status": r.status,
                    "isolation_reason": r.isolation_reason,
                    "isolation_detail": r.isolation_detail,
                    "capped_total": _num(r.capped_total) if r.capped_total is not None else None,
                    "raw_total": _num(r.raw_total) if r.raw_total is not None else None,
                    "dimension_totals": (
                        {k: _num(v) for k, v in r.dimension_totals.items()}
                        if r.dimension_totals
                        else None
                    ),
                    "critical_cap_applied": r.critical_cap_applied,
                    "critical_cap_code": r.critical_cap_code,
                    "items": list(r.items),
                    "consensus_mode": r.consensus_mode,
                    "consensus_judges": r.consensus_judges,
                    "arbiter_used": r.arbiter_used,
                    "human_review_triggered": r.human_review_triggered,
                    "requires_human_review": r.requires_human_review,
                    "human_review_reasons": list(r.human_review_reasons),
                    "ab_disagreement_items": r.ab_disagreement_items,
                    "run_mode": r.run_mode,
                    "policy_valid": r.policy_valid,
                    "policy_violation_count": r.policy_violation_count,
                    "answer_status": r.answer_status,
                    "agent_cost": r.agent_cost,
                    "judge_cost_available": r.judge_cost_available,
                    "judge_call_count": r.judge_call_count,
                    "judge_total_latency_ms": r.judge_total_latency_ms,
                    "judge_total_retries": r.judge_total_retries,
                }
                for r in self.runs
            ],
        }


def _run_detail(rec: RunRecord) -> CaseRunDetail:
    """Build a :class:`CaseRunDetail` from a :class:`RunRecord`."""
    score = rec.score
    items: tuple[dict[str, Any], ...] = ()
    dim_totals: dict[str, Decimal] | None = None
    capped: Decimal | None = None
    raw: Decimal | None = None
    cap_applied = False
    cap_code: str | None = None
    consensus_mode: str | None = None
    consensus_judges: int | None = None
    arbiter = False
    hr_triggered = False
    requires_hr = False
    hr_reasons: tuple[str, ...] = ()
    ab_disagree: int | None = None
    run_mode: str | None = None

    if score is not None:
        capped = score.capped_total
        raw = score.raw_total
        dim_totals = dict(score.dimension_totals)
        cap_applied = score.critical_cap_applied
        cap_code = score.critical_cap_code
        consensus_mode = score.consensus_mode
        consensus_judges = score.consensus_judges
        arbiter = score.arbiter_used
        hr_triggered = score.human_review_triggered
        requires_hr = score.requires_human_review
        hr_reasons = score.human_review_reasons
        run_mode = score.run_mode
        items = tuple(
            {
                "item_id": it.item_id,
                "dimension": it.dimension,
                "points": _num(it.points),
                "consensus_credit": _num(it.consensus_credit),
                "item_score": _num(it.item_score),
            }
            for it in score.items
        )

    if rec.judge_disagreement is not None:
        ab_disagree = rec.judge_disagreement.ab_disagreement_items

    agent_cost: dict[str, int] | None = None
    if rec.cost.agent is not None:
        ac = rec.cost.agent
        agent_cost = {
            "elapsed_ms": ac.elapsed_ms,
            "input_tokens": ac.input_tokens,
            "output_tokens": ac.output_tokens,
            "tool_call_count": ac.tool_call_count,
            "files_read_count": ac.files_read_count,
            "graph_query_count": ac.graph_query_count,
            "search_query_count": ac.search_query_count,
        }

    jc = rec.cost.judge
    return CaseRunDetail(
        run_id=rec.run_id,
        tool_policy=rec.tool_policy,
        status=rec.status.value,
        isolation_reason=rec.isolation_reason,
        isolation_detail=rec.isolation_detail,
        capped_total=capped,
        raw_total=raw,
        dimension_totals=dim_totals,
        critical_cap_applied=cap_applied,
        critical_cap_code=cap_code,
        items=items,
        consensus_mode=consensus_mode,
        consensus_judges=consensus_judges,
        arbiter_used=arbiter,
        human_review_triggered=hr_triggered,
        requires_human_review=requires_hr,
        human_review_reasons=hr_reasons,
        ab_disagreement_items=ab_disagree,
        run_mode=run_mode,
        policy_valid=rec.policy_valid,
        policy_violation_count=rec.policy_violation_count,
        answer_status=rec.answer_status,
        agent_cost=agent_cost,
        judge_cost_available=jc.available,
        judge_call_count=jc.judge_call_count,
        judge_total_latency_ms=jc.total_latency_ms,
        judge_total_retries=jc.total_retries,
    )


def _build_case_reports(
    records: list[RunRecord],
) -> tuple[list[CaseReport], list[PairedScoreDiff]]:
    """Build per-case reports and collect paired diffs (§16.1, §19).

    Runs are grouped by case_id (from the version identity; isolated runs
    without an identity are grouped under a synthetic ``__no_case__`` key).
    Within each case, scored runs are further grouped by the within-group
    pairing key and tool_policy to form Graph/Grep pairs.
    """
    # Group all runs by case_id.
    by_case: dict[str, list[RunRecord]] = defaultdict(list)
    for rec in records:
        by_case[rec.case_id or "__no_case__"].append(rec)

    case_reports: list[CaseReport] = []
    paired_diffs: list[PairedScoreDiff] = []

    for case_id in sorted(by_case.keys()):
        case_runs = sorted(by_case[case_id], key=lambda r: r.run_id)
        details = tuple(_run_detail(r) for r in case_runs)

        # Among scored runs, form Graph/Grep pairs by pairing key.
        scored = [r for r in case_runs if r.is_scored]
        by_pair_key: dict[tuple[str, ...], dict[str, list[RunRecord]]] = defaultdict(
            lambda: {GRAPH_POLICY: [], GREP_POLICY: []}
        )
        for r in scored:
            pk = _pairing_key(r)
            policy = r.tool_policy or ""
            if policy in (GRAPH_POLICY, GREP_POLICY):
                by_pair_key[pk][policy].append(r)

        paired: PairedScoreDiff | None = None
        note = ""

        if not scored:
            note = "no scored runs"
        elif not by_pair_key:
            note = "no graph/grep scored runs"
        else:
            # Use the first (deterministic) pairing key that yields a pair.
            pair_keys = sorted(by_pair_key.keys())
            for pk in pair_keys:
                graphs = sorted(by_pair_key[pk][GRAPH_POLICY], key=lambda r: r.run_id)
                greps = sorted(by_pair_key[pk][GREP_POLICY], key=lambda r: r.run_id)
                if graphs and greps:
                    paired = _build_paired_diff(graphs[0], greps[0])
                    paired_diffs.append(paired)
                    extra = len(graphs) - 1 + len(greps) - 1
                    if extra > 0:
                        note = f"paired (first of {len(graphs)} graph, {len(greps)} grep runs)"
                    else:
                        note = "paired"
                    break
                if graphs and not greps:
                    note = "missing grep-policy run"
                elif greps and not graphs:
                    note = "missing graph-policy run"
                else:
                    note = "no graph/grep scored runs"

        case_reports.append(
            CaseReport(
                case_id=case_id,
                runs=details,
                paired_diff=paired,
                pairing_note=note,
            )
        )

    return case_reports, paired_diffs


# ---------------------------------------------------------------------------
# Summary report (§19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStatusCounts:
    """Counts of runs by report status / isolation reason (§19)."""

    total: int
    scored: int
    isolated: int
    version_mismatch: int
    judge_failed: int
    awaiting_judge: int
    valid_zero: int
    invalid: int
    failed: int
    missing_artifact: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scored": self.scored,
            "isolated": self.isolated,
            "by_isolation_reason": {
                "version_mismatch": self.version_mismatch,
                "judge_failed": self.judge_failed,
                "awaiting_judge": self.awaiting_judge,
                "valid_zero": self.valid_zero,
                "invalid": self.invalid,
                "failed": self.failed,
                "missing_artifact": self.missing_artifact,
            },
        }


@dataclass(frozen=True)
class StabilitySummary:
    """Judge-consensus / stability / review-coverage summary (§13, §19)."""

    scored_run_count: int
    arbiter_used_count: int
    human_review_required_count: int
    human_review_coverage: Decimal  #: fraction requires_human_review / scored
    consensus_mode_counts: dict[str, int]
    total_ab_disagreement_items: int
    runs_with_ab_disagreement: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored_run_count": self.scored_run_count,
            "arbiter_used_count": self.arbiter_used_count,
            "human_review_required_count": self.human_review_required_count,
            "human_review_coverage": _num(self.human_review_coverage),
            "consensus_mode_counts": dict(self.consensus_mode_counts),
            "total_ab_disagreement_items": self.total_ab_disagreement_items,
            "runs_with_ab_disagreement": self.runs_with_ab_disagreement,
        }


@dataclass(frozen=True)
class CostSummary:
    """Independent cost metrics aggregated across scored runs (§15.2).

    Agent cost is summed where available; Judge cost is summed only where the
    optional Judge-cost block was present. Cost is never folded into a
    correctness total. ``judge_cost_available_runs`` records how many runs
    carried Judge-cost data so the consumer knows the coverage.
    """

    agent_elapsed_ms_total: int | None
    agent_input_tokens_total: int | None
    agent_output_tokens_total: int | None
    agent_tool_call_count_total: int | None
    agent_files_read_count_total: int | None
    agent_graph_query_count_total: int | None
    agent_search_query_count_total: int | None
    judge_cost_available_runs: int
    judge_call_count_total: int | None
    judge_total_latency_ms: int | None
    judge_total_retries: int | None
    judge_input_tokens_total: int | None
    judge_output_tokens_total: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_elapsed_ms_total": self.agent_elapsed_ms_total,
            "agent_input_tokens_total": self.agent_input_tokens_total,
            "agent_output_tokens_total": self.agent_output_tokens_total,
            "agent_tool_call_count_total": self.agent_tool_call_count_total,
            "agent_files_read_count_total": self.agent_files_read_count_total,
            "agent_graph_query_count_total": self.agent_graph_query_count_total,
            "agent_search_query_count_total": self.agent_search_query_count_total,
            "judge_cost_available_runs": self.judge_cost_available_runs,
            "judge_call_count_total": self.judge_call_count_total,
            "judge_total_latency_ms": self.judge_total_latency_ms,
            "judge_total_retries": self.judge_total_retries,
            "judge_input_tokens_total": self.judge_input_tokens_total,
            "judge_output_tokens_total": self.judge_output_tokens_total,
        }


@dataclass(frozen=True)
class SummaryReport:
    """The cross-case summary (§19).

    Separates correctness (paired absolute diffs), compliance (status counts +
    compatibility matrix), stability (Judge consensus / review coverage) and
    cost (independent metrics). No dimension is synthesized into an opaque
    total.
    """

    run_counts: RunStatusCounts
    paired_diffs: tuple[PairedScoreDiff, ...]
    stability: StabilitySummary
    cost: CostSummary
    compatibility: CompatibilityMatrix
    judge_call_count: int
    #: Median paired total diff (robust central tendency across pairs).
    median_total_diff: Decimal | None
    mean_total_diff: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_counts": self.run_counts.to_dict(),
            "paired_absolute_diffs": [d.to_dict() for d in self.paired_diffs],
            "stability": self.stability.to_dict(),
            "cost": self.cost.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "judge_call_count": self.judge_call_count,
            "median_total_diff": (
                _num(self.median_total_diff) if self.median_total_diff is not None else None
            ),
            "mean_total_diff": (
                _num(self.mean_total_diff) if self.mean_total_diff is not None else None
            ),
        }


@dataclass(frozen=True)
class ReportBundle:
    """The complete report: summary + per-case reports (§19).

    This is the top-level artifact produced by :func:`aggregate`. It carries
    the four separated views (correctness, compliance, stability, cost) and
    is a pure, deterministic function of its inputs.
    """

    summary: SummaryReport
    cases: tuple[CaseReport, ...]
    judge_call_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "cases": [c.to_dict() for c in self.cases],
            "judge_call_count": self.judge_call_count,
        }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _median(values: list[Decimal]) -> Decimal | None:
    """Exact median of a list of Decimals (None for an empty list)."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


def _mean(values: list[Decimal]) -> Decimal | None:
    """Exact mean of a list of Decimals (None for an empty list)."""
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _build_run_counts(records: list[RunRecord]) -> RunStatusCounts:
    scored = sum(1 for r in records if r.is_scored)
    isolated = len(records) - scored
    counts = {reason: 0 for reason in ISOLATION_REASONS}
    for r in records:
        if r.isolation_reason in counts:
            counts[r.isolation_reason] += 1
    return RunStatusCounts(
        total=len(records),
        scored=scored,
        isolated=isolated,
        version_mismatch=counts[ISOLATION_VERSION_MISMATCH],
        judge_failed=counts[ISOLATION_JUDGE_FAILED],
        awaiting_judge=counts[ISOLATION_AWAITING_JUDGE],
        valid_zero=counts[ISOLATION_VALID_ZERO],
        invalid=counts[ISOLATION_INVALID],
        failed=counts[ISOLATION_FAILED],
        missing_artifact=counts[ISOLATION_MISSING_ARTIFACT],
    )


def _build_stability(records: list[RunRecord]) -> StabilitySummary:
    scored_recs = [r for r in records if r.is_scored and r.judge_disagreement is not None]
    n = len(scored_recs)
    arbiter_count = sum(
        1 for r in scored_recs if r.judge_disagreement and r.judge_disagreement.arbiter_used
    )
    hr_count = sum(1 for r in scored_recs if r.score and r.score.requires_human_review)
    coverage = Decimal(hr_count) / Decimal(n) if n else Decimal(0)
    mode_counts: dict[str, int] = defaultdict(int)
    total_ab = 0
    runs_with_ab = 0
    for r in scored_recs:
        jd = r.judge_disagreement
        assert jd is not None
        mode_counts[jd.consensus_mode] += 1
        if jd.ab_disagreement_items is not None:
            total_ab += jd.ab_disagreement_items
            if jd.ab_disagreement_items > 0:
                runs_with_ab += 1
    return StabilitySummary(
        scored_run_count=n,
        arbiter_used_count=arbiter_count,
        human_review_required_count=hr_count,
        human_review_coverage=coverage,
        consensus_mode_counts=dict(sorted(mode_counts.items())),
        total_ab_disagreement_items=total_ab,
        runs_with_ab_disagreement=runs_with_ab,
    )


def _build_cost(records: list[RunRecord]) -> CostSummary:
    """Aggregate independent cost metrics across scored runs (§15.2).

    Agent cost is summed where available; Judge cost is summed only where the
    optional Judge-cost block was present. Cost is never folded into a
    correctness total, and a high cost never invalidates a complete score.
    """
    scored = [r for r in records if r.is_scored]

    def _sum_agent(attr: str) -> int | None:
        vals = [getattr(r.cost.agent, attr) for r in scored if r.cost.agent is not None]
        return sum(vals) if vals else None

    jc_runs = [r for r in scored if r.cost.judge.available]

    def _sum_judge(attr: str) -> int | None:
        vals = [getattr(r.cost.judge, attr) for r in jc_runs]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    return CostSummary(
        agent_elapsed_ms_total=_sum_agent("elapsed_ms"),
        agent_input_tokens_total=_sum_agent("input_tokens"),
        agent_output_tokens_total=_sum_agent("output_tokens"),
        agent_tool_call_count_total=_sum_agent("tool_call_count"),
        agent_files_read_count_total=_sum_agent("files_read_count"),
        agent_graph_query_count_total=_sum_agent("graph_query_count"),
        agent_search_query_count_total=_sum_agent("search_query_count"),
        judge_cost_available_runs=len(jc_runs),
        judge_call_count_total=_sum_judge("judge_call_count"),
        judge_total_latency_ms=_sum_judge("total_latency_ms"),
        judge_total_retries=_sum_judge("total_retries"),
        judge_input_tokens_total=_sum_judge("input_tokens"),
        judge_output_tokens_total=_sum_judge("output_tokens"),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def aggregate(records: list[RunRecord]) -> ReportBundle:
    """Aggregate run records into a :class:`ReportBundle` (§16, §19, §20).

    A pure, deterministic function of ``records``. It performs **zero** Judge
    calls (:data:`JUDGE_CALL_COUNT` is 0 and is stamped on the bundle). The
    four views -- correctness, compliance, stability, cost -- are kept
    strictly separate; no cost metric ever enters a correctness total, and no
    incompatible versions are mixed in a formal aggregate.

    The report consumes only absolute scores and paired absolute score
    differences; it never generates or displays a Pairwise preference (§16.2).
    """
    matrix = build_compatibility_matrix(records)
    case_reports, paired_diffs = _build_case_reports(records)

    # Sort paired diffs by case_id for deterministic ordering.
    paired_diffs.sort(key=lambda d: d.case_id)
    total_diffs = [d.total_diff for d in paired_diffs]

    summary = SummaryReport(
        run_counts=_build_run_counts(records),
        paired_diffs=tuple(paired_diffs),
        stability=_build_stability(records),
        cost=_build_cost(records),
        compatibility=matrix,
        judge_call_count=JUDGE_CALL_COUNT,
        median_total_diff=_median(total_diffs),
        mean_total_diff=_mean(total_diffs),
    )
    return ReportBundle(
        summary=summary,
        cases=tuple(case_reports),
        judge_call_count=JUDGE_CALL_COUNT,
    )
