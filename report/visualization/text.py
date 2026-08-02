"""Deterministic text/Markdown rendering of report views (design §19).

Every renderer is a pure function of its dataclass input; the output is stable
across runs on the same input (acceptance criterion: deterministic reporting).
Display rounding to 2 decimal places happens here and only here (§10.1); the
aggregation core carries exact :class:`~decimal.Decimal` values throughout.

The renderers separate correctness, compliance, stability and cost into
distinct sections (AIS-011 invariant: "分开呈现结果质量、合规性、稳定性与成本").
"""

from __future__ import annotations

from decimal import Decimal

from report.aggregate import (
    CaseReport,
    CaseRunDetail,
    CompatibilityMatrix,
    ReportBundle,
    SummaryReport,
)
from report.analysis_input import DIMENSION_NAMES

#: Display rounding precision (§10.1).
_PLACES = Decimal("0.01")


def _fmt(value: Decimal | int | float | None) -> str:
    """Format a numeric value for display (2 dp, None -> "n/a")."""
    if value is None:
        return "n/a"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(d.quantize(_PLACES))


def _fmt_int(value: int | None) -> str:
    """Format an integer value for display (None -> "n/a")."""
    return str(value) if value is not None else "n/a"


def _section_header(title: str) -> str:
    """A visible section separator + title."""
    return f"\n{'=' * 72}\n{title}\n{'=' * 72}\n"


def render_case_report(case: CaseReport) -> str:
    """Render one per-case report as text (§19).

    Shows every run with its correctness (total, five dimensions, cap, item
    verdicts), consensus/arbiter/human-review state, compliance, cost, and the
    paired Graph/Grep absolute score difference when a pair exists.
    """
    lines: list[str] = []
    lines.append(_section_header(f"CASE: {case.case_id}"))
    lines.append(f"runs: {len(case.runs)} | pairing: {case.pairing_note}")

    if case.paired_diff is not None:
        pd = case.paired_diff
        lines.append("")
        lines.append("--- Paired absolute score difference (Graph - Grep) ---")
        lines.append(f"  graph run: {pd.graph_run_id} (total={_fmt(pd.graph_total)})")
        lines.append(f"  grep  run: {pd.grep_run_id} (total={_fmt(pd.grep_total)})")
        lines.append(f"  total diff: {_fmt(pd.total_diff)}")
        lines.append("  dimension diffs:")
        for name in DIMENSION_NAMES:
            lines.append(f"    {name}: {_fmt(pd.dimension_diffs.get(name, Decimal(0)))}")
        if pd.graph_cap_applied or pd.grep_cap_applied:
            lines.append(f"  cap applied: graph={pd.graph_cap_applied}, grep={pd.grep_cap_applied}")

    for run in case.runs:
        lines.append("")
        lines.append(_render_run_detail(run))

    return "\n".join(lines) + "\n"


def _render_run_detail(run: CaseRunDetail) -> str:
    """Render one run within a case report."""
    lines: list[str] = []
    status_tag = run.status
    if run.isolation_reason:
        status_tag = f"{run.status} ({run.isolation_reason})"
    lines.append(f"  run: {run.run_id} | policy={run.tool_policy or '?'} | status={status_tag}")
    if run.isolation_detail:
        lines.append(f"    isolation: {run.isolation_detail}")

    if run.capped_total is not None:
        lines.append(
            f"    correctness: capped_total={_fmt(run.capped_total)} "
            f"raw_total={_fmt(run.raw_total)}"
        )
        if run.critical_cap_applied:
            lines.append(f"    critical cap: APPLIED (code={run.critical_cap_code})")
        if run.dimension_totals:
            dim_str = ", ".join(
                f"{name}={_fmt(run.dimension_totals[name])}" for name in DIMENSION_NAMES
            )
            lines.append(f"    dimensions: {dim_str}")
        if run.items:
            lines.append(f"    items ({len(run.items)}):")
            for it in run.items:
                lines.append(
                    f"      {it['item_id']} [{it['dimension']}] "
                    f"credit={_fmt(it['consensus_credit'])} "
                    f"points={_fmt(it['points'])} "
                    f"score={_fmt(it['item_score'])}"
                )
    if run.consensus_mode is not None:
        lines.append(
            f"    consensus: mode={run.consensus_mode} judges={_fmt_int(run.consensus_judges)} "
            f"arbiter={run.arbiter_used} review_triggered={run.human_review_triggered}"
        )
        if run.requires_human_review:
            lines.append(
                f"    requires_human_review: True reasons={list(run.human_review_reasons)}"
            )
        if run.ab_disagreement_items is not None:
            lines.append(f"    A/B disagreement items: {run.ab_disagreement_items}")
        if run.run_mode:
            lines.append(f"    run_mode: {run.run_mode}")

    lines.append(
        f"    compliance: policy_valid={run.policy_valid} "
        f"violations={run.policy_violation_count} answer_status={run.answer_status}"
    )

    if run.agent_cost:
        ac = run.agent_cost
        lines.append(
            f"    cost (agent): elapsed_ms={ac['elapsed_ms']} "
            f"tokens={ac['input_tokens']}+{ac['output_tokens']} "
            f"tools={ac['tool_call_count']} files={ac['files_read_count']} "
            f"graph_q={ac['graph_query_count']} search_q={ac['search_query_count']}"
        )
    if run.judge_cost_available:
        lines.append(
            f"    cost (judge): calls={_fmt_int(run.judge_call_count)} "
            f"latency_ms={_fmt_int(run.judge_total_latency_ms)} "
            f"retries={_fmt_int(run.judge_total_retries)}"
        )
    return "\n".join(lines)


def render_summary_report(summary: SummaryReport) -> str:
    """Render the cross-case summary as text (§19).

    Separates correctness (paired absolute diffs), compliance (status counts),
    stability (Judge consensus / review coverage) and cost (independent
    metrics). No dimension is synthesized into an opaque total.
    """
    lines: list[str] = []
    lines.append(_section_header("SUMMARY REPORT"))

    # --- Correctness: paired absolute score differences -------------------
    lines.append("--- Correctness: paired absolute score differences (Graph - Grep) ---")
    rc = summary.run_counts
    lines.append(f"  total runs: {rc.total} (scored={rc.scored}, isolated={rc.isolated})")
    lines.append(f"  paired pairs: {len(summary.paired_diffs)}")
    if summary.median_total_diff is not None:
        lines.append(f"  median total diff: {_fmt(summary.median_total_diff)}")
    if summary.mean_total_diff is not None:
        lines.append(f"  mean total diff: {_fmt(summary.mean_total_diff)}")
    for pd in summary.paired_diffs:
        lines.append(
            f"    {pd.case_id}: graph={_fmt(pd.graph_total)} grep={_fmt(pd.grep_total)} "
            f"diff={_fmt(pd.total_diff)} (graph={pd.graph_run_id}, grep={pd.grep_run_id})"
        )
    lines.append("  (no Pairwise preference is generated or displayed; §16.2)")

    # --- Compliance: run status + compatibility matrix --------------------
    lines.append("")
    lines.append("--- Compliance: run status & version compatibility ---")
    lines.append(f"  scored: {rc.scored}")
    lines.append(f"  version_mismatch: {rc.version_mismatch} (isolated, excluded from aggregation)")
    lines.append(f"  judge_failed: {rc.judge_failed} (isolated, no inferred score)")
    lines.append(f"  awaiting_judge: {rc.awaiting_judge}")
    lines.append(f"  valid_zero: {rc.valid_zero} (deterministic 0, not materialized)")
    lines.append(f"  invalid: {rc.invalid}")
    lines.append(f"  failed: {rc.failed}")
    lines.append(f"  missing_artifact: {rc.missing_artifact}")
    lines.append("")
    lines.append(render_compatibility_matrix(summary.compatibility))

    # --- Stability: Judge consensus, arbiter, review coverage -------------
    lines.append("")
    lines.append("--- Stability: Judge consensus & review coverage ---")
    st = summary.stability
    lines.append(f"  scored runs with consensus data: {st.scored_run_count}")
    lines.append(f"  arbiter (Judge C) used: {st.arbiter_used_count}")
    lines.append(f"  human review required: {st.human_review_required_count}")
    lines.append(f"  human review coverage: {_fmt(st.human_review_coverage)}")
    lines.append(f"  consensus modes: {st.consensus_mode_counts}")
    lines.append(f"  total A/B disagreement items: {st.total_ab_disagreement_items}")
    lines.append(f"  runs with A/B disagreement: {st.runs_with_ab_disagreement}")

    # --- Cost: independent metrics (§15.2) --------------------------------
    lines.append("")
    lines.append("--- Cost: independent metrics (never folded into correctness) ---")
    cost = summary.cost
    lines.append(f"  agent elapsed_ms (sum): {_fmt_int(cost.agent_elapsed_ms_total)}")
    lines.append(f"  agent input_tokens (sum): {_fmt_int(cost.agent_input_tokens_total)}")
    lines.append(f"  agent output_tokens (sum): {_fmt_int(cost.agent_output_tokens_total)}")
    lines.append(f"  agent tool_call_count (sum): {_fmt_int(cost.agent_tool_call_count_total)}")
    lines.append(f"  agent files_read (sum): {_fmt_int(cost.agent_files_read_count_total)}")
    lines.append(f"  agent graph_query (sum): {_fmt_int(cost.agent_graph_query_count_total)}")
    lines.append(f"  agent search_query (sum): {_fmt_int(cost.agent_search_query_count_total)}")
    lines.append(f"  judge cost available runs: {cost.judge_cost_available_runs}")
    lines.append(f"  judge call_count (sum): {_fmt_int(cost.judge_call_count_total)}")
    lines.append(f"  judge latency_ms (sum): {_fmt_int(cost.judge_total_latency_ms)}")
    lines.append(f"  judge retries (sum): {_fmt_int(cost.judge_total_retries)}")
    lines.append(f"  judge input_tokens (sum): {_fmt_int(cost.judge_input_tokens_total)}")
    lines.append(f"  judge output_tokens (sum): {_fmt_int(cost.judge_output_tokens_total)}")

    lines.append("")
    lines.append(f"  Judge call count (this report): {summary.judge_call_count}")
    lines.append("  (a complete score is never invalidated by high cost; §15.2)")

    return "\n".join(lines) + "\n"


def render_compatibility_matrix(matrix: CompatibilityMatrix) -> str:
    """Render the version-compatibility matrix as text (§20)."""
    lines: list[str] = []
    lines.append("  Compatibility matrix:")
    lines.append(f"  dimensions: {list(matrix.dimensions)}")
    for g in matrix.groups:
        lines.append(
            f"    [{g.scoring_profile} | judge={g.judge_model} | cli={g.judge_cli_version}] "
            f"scored={len(g.scored_run_ids)} isolated={len(g.isolated_run_ids)}"
        )
        if g.scored_run_ids:
            lines.append(f"      scored: {list(g.scored_run_ids)}")
        if g.isolated_run_ids:
            lines.append(f"      isolated: {list(g.isolated_run_ids)}")
    if not matrix.groups:
        lines.append("    (no runs)")
    return "\n".join(lines)


def render_report_bundle(bundle: ReportBundle) -> str:
    """Render the full report bundle: summary + all case reports (§19)."""
    lines: list[str] = []
    lines.append(render_summary_report(bundle.summary))
    for case in bundle.cases:
        lines.append(render_case_report(case))
    lines.append(_section_header("END OF REPORT"))
    lines.append(f"Judge call count: {bundle.judge_call_count}")
    return "\n".join(lines) + "\n"
