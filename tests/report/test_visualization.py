"""Tests for ``report.visualization`` (design §19).

Verifies that the text rendering separates correctness, compliance, stability
and cost into distinct sections, includes all required case-level fields
(§19), and is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from report.aggregate import aggregate
from report.analysis_input import load_runs
from report.visualization import (
    render_case_report,
    render_compatibility_matrix,
    render_report_bundle,
    render_summary_report,
)
from tests.report import fixtures as fx


@pytest.fixture()
def bundle(tmp_path: Path):
    fx.build_synthetic_experiment(tmp_path / "runs")
    return aggregate(load_runs(tmp_path / "runs"))


class TestSummaryRendering:
    """The summary report has the four separated sections (§19)."""

    def test_summary_has_four_sections(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "Correctness:" in text
        assert "Compliance:" in text
        assert "Stability:" in text
        assert "Cost:" in text

    def test_summary_mentions_paired_diffs(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "paired absolute score differences" in text
        assert "Graph - Grep" in text

    def test_summary_states_no_pairwise(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "Pairwise preference" in text

    def test_summary_has_judge_call_count(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "Judge call count" in text
        assert "0" in text

    def test_summary_has_run_status_counts(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        for label in [
            "scored",
            "version_mismatch",
            "judge_failed",
            "awaiting_judge",
            "valid_zero",
            "invalid",
            "failed",
            "missing_artifact",
        ]:
            assert label in text

    def test_summary_has_review_coverage(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "human review coverage" in text
        assert "arbiter" in text.lower()

    def test_summary_has_cost_metrics(self, bundle) -> None:
        text = render_summary_report(bundle.summary)
        assert "elapsed_ms" in text
        assert "input_tokens" in text
        assert "output_tokens" in text
        assert "tool_call_count" in text
        assert "judge call_count" in text

    def test_compatibility_matrix_renders(self, bundle) -> None:
        text = render_compatibility_matrix(bundle.summary.compatibility)
        assert "Compatibility matrix" in text
        assert "dimensions" in text
        assert "scored" in text


class TestCaseRendering:
    """Each case report shows totals, dimensions, cap, items, consensus (§19)."""

    def _scored_case(self, bundle):
        """Find the first case that has at least one scored run."""
        for case in bundle.cases:
            if any(r.capped_total is not None for r in case.runs):
                return case
        pytest.fail("no scored case found")

    def test_case_report_has_required_fields(self, bundle) -> None:
        case = self._scored_case(bundle)
        text = render_case_report(case)
        assert "CASE:" in text
        assert "capped_total" in text
        assert "dimensions" in text
        assert "consensus" in text
        assert "items" in text

    def test_case_report_shows_paired_diff(self, bundle) -> None:
        case = self._scored_case(bundle)
        if case.paired_diff is not None:
            text = render_case_report(case)
            assert "Paired absolute score difference" in text
            assert "total diff" in text

    def test_case_report_shows_item_verdicts(self, bundle) -> None:
        case = self._scored_case(bundle)
        text = render_case_report(case)
        assert "credit=" in text
        assert "score=" in text

    def test_case_report_shows_cap_when_applied(self, tmp_path: Path) -> None:
        """A run with a critical cap shows the cap in the case report."""
        cap = {"applied": True, "cap_value": 50, "code": "core_correctness_all_zero"}
        fx.build_scored_run(
            tmp_path / "runs" / "capped-run",
            run_id="capped-run",
            case_id="cap-case",
            tool_policy="graph",
            credits=[0, 0, 0, 0, 0, 0, 0, 0, 0, 0],  # all zero -> cap fires
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
            critical_cap=cap,
        )
        b = aggregate(load_runs(tmp_path / "runs"))
        text = render_case_report(b.cases[0])
        assert "critical cap" in text.lower()
        assert "APPLIED" in text


class TestFullBundleRendering:
    """The full bundle renders without error and is deterministic."""

    def test_render_report_bundle(self, bundle) -> None:
        text = render_report_bundle(bundle)
        assert "SUMMARY REPORT" in text
        assert "END OF REPORT" in text

    def test_render_deterministic(self, tmp_path: Path) -> None:
        fx.build_synthetic_experiment(tmp_path / "runs")
        b1 = aggregate(load_runs(tmp_path / "runs"))
        t1 = render_report_bundle(b1)
        fx.build_synthetic_experiment(tmp_path / "runs2")
        b2 = aggregate(load_runs(tmp_path / "runs2"))
        t2 = render_report_bundle(b2)
        assert t1 == t2
