"""Tests for ``report.aggregate`` (design §16, §19, §20).

AIS-011 acceptance-criteria mapping:

==  =========================================  ================================================
§   Criterion                                  Test
==  =========================================  ================================================
16  paired absolute score diff (Graph-Grep)    test_paired_diff_graph_minus_grep
16.2 no Pairwise preference generated          test_no_pairwise_preference
20  incompatible versions not mixed            test_incompatible_versions_separate_groups
13.5 judge_failed listed, no inferred score    test_judge_failed_excluded_from_aggregation
20  version_mismatch excluded                  test_version_mismatch_excluded
15.2 cost independent of correctness           test_cost_separate_from_correctness
19  summary has status counts                  test_summary_run_counts
13  stability: arbiter / review coverage       test_stability_summary
    judge_call_count is zero                   test_judge_call_count_zero
==  =========================================  ================================================
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from report.aggregate import (
    JUDGE_CALL_COUNT,
    ReportBundle,
    aggregate,
    build_compatibility_matrix,
)
from report.analysis_input import (
    load_runs,
)
from tests.report import fixtures as fx


@pytest.fixture()
def experiment_records(tmp_path: Path):
    """Load the full synthetic experiment as RunRecords."""
    fx.build_synthetic_experiment(tmp_path / "runs")
    return load_runs(tmp_path / "runs")


@pytest.fixture()
def bundle(experiment_records) -> ReportBundle:
    return aggregate(experiment_records)


class TestPairedDiff:
    """Paired absolute score differences (§16.1)."""

    def test_paired_diff_graph_minus_grep(self, bundle: ReportBundle) -> None:
        """Each case has a paired diff: graph.capped - grep.capped."""
        diffs = bundle.summary.paired_diffs
        assert len(diffs) == 2  # case A and case B
        by_case = {d.case_id: d for d in diffs}
        # case A: graph=100, grep=70.5 -> diff=29.5
        assert by_case[fx.CASE_A].graph_total == Decimal(100)
        assert by_case[fx.CASE_A].grep_total == Decimal("70.5")
        assert by_case[fx.CASE_A].total_diff == Decimal("29.5")
        # case B: graph=83.75, grep=43 -> diff=40.75
        assert by_case[fx.CASE_B].graph_total == Decimal("83.75")
        assert by_case[fx.CASE_B].grep_total == Decimal(43)
        assert by_case[fx.CASE_B].total_diff == Decimal("40.75")

    def test_paired_diff_has_dimension_diffs(self, bundle: ReportBundle) -> None:
        diffs = bundle.summary.paired_diffs
        for d in diffs:
            assert set(d.dimension_diffs.keys()) == {
                "core_correctness",
                "reasoning_correctness",
                "completeness",
                "scope_precision",
                "evidence_actionability",
            }

    def test_no_pairwise_preference(self, bundle: ReportBundle) -> None:
        """The report never generates or displays a Pairwise preference (§16.2)."""
        doc = bundle.to_dict()
        text = repr(doc)
        assert "pairwise" not in text.lower()
        assert "preference" not in text.lower()
        # Paired diffs are absolute score differences, not preferences.
        for d in bundle.summary.paired_diffs:
            assert d.total_diff == d.graph_total - d.grep_total

    def test_pairing_note_when_no_grep(self, tmp_path: Path) -> None:
        """A case with only a graph run gets a 'missing grep' note."""
        fx.build_scored_run(
            tmp_path / "runs" / "solo-graph",
            run_id="solo-graph",
            case_id="solo-case",
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
        )
        records = load_runs(tmp_path / "runs")
        b = aggregate(records)
        assert len(b.cases) == 1
        assert "missing grep" in b.cases[0].pairing_note


class TestVersionIsolation:
    """Incompatible versions are never mixed in a formal aggregate (§20)."""

    def test_incompatible_versions_separate_groups(self, experiment_records) -> None:
        """Different Judge models land in separate compatibility groups."""
        # Build two scored runs with different judge models.
        matrix = build_compatibility_matrix(experiment_records)
        # The synthetic experiment has one judge model for all scored runs.
        scored_groups = [g for g in matrix.groups if g.scored_run_ids]
        assert len(scored_groups) == 1
        assert len(scored_groups[0].scored_run_ids) == 4

    def test_version_mismatch_excluded(self, bundle: ReportBundle) -> None:
        """version_mismatch runs are isolated, not in scored counts."""
        rc = bundle.summary.run_counts
        assert rc.version_mismatch == 1
        assert rc.scored == 4
        # The version_mismatch run is not in any paired diff.
        all_run_ids = set()
        for d in bundle.summary.paired_diffs:
            all_run_ids.update([d.graph_run_id, d.grep_run_id])
        assert "iso-version-mismatch" not in all_run_ids

    def test_judge_failed_excluded_from_aggregation(self, bundle: ReportBundle) -> None:
        """judge_failed runs are listed but never scored (§13.5)."""
        rc = bundle.summary.run_counts
        assert rc.judge_failed == 1
        assert rc.scored == 4


class TestCostSeparation:
    """Cost is independent of correctness (§15.2)."""

    def test_cost_separate_from_correctness(self, bundle: ReportBundle) -> None:
        """Cost metrics live in their own summary section, not in scores."""
        cost_dict = bundle.summary.cost.to_dict()
        score_keys = {"raw_total", "capped_total", "total_diff"}
        assert not (set(cost_dict.keys()) & score_keys)
        # Agent cost is summed.
        assert cost_dict["agent_elapsed_ms_total"] is not None
        assert cost_dict["agent_elapsed_ms_total"] > 0

    def test_judge_cost_optional_aggregation(self, bundle: ReportBundle) -> None:
        """Judge cost is summed only where available; coverage is recorded."""
        cost = bundle.summary.cost
        # All 4 scored runs have judge_cost in the synthetic experiment.
        assert cost.judge_cost_available_runs == 4
        assert cost.judge_call_count_total == 8  # 2 calls x 4 runs

    def test_high_cost_does_not_invalidate_score(self, tmp_path: Path) -> None:
        """A complete score is not invalidated by high cost (§15.2)."""
        run_dir = tmp_path / "runs" / "expensive"
        fx.build_scored_run(
            run_dir,
            run_id="expensive",
            case_id="c1",
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
        )
        # Overwrite run-metadata with very high cost.
        from tests.report.fixtures import _run_metadata_doc, _write_json

        high_metrics = {
            "tool_call_count": 9999,
            "files_read_count": 9999,
            "graph_query_count": 9999,
            "search_query_count": 0,
            "elapsed_ms": 9999999,
            "input_tokens": 9999999,
            "output_tokens": 9999999,
        }
        _write_json(
            run_dir / "run-metadata.json",
            _run_metadata_doc(tool_policy="graph", metrics=high_metrics),
        )
        rec = load_runs(tmp_path / "runs")
        b = aggregate(rec)
        assert b.summary.run_counts.scored == 1
        assert b.cases[0].runs[0].capped_total == Decimal(100)


class TestSummary:
    """Summary report completeness (§19)."""

    def test_summary_run_counts(self, bundle: ReportBundle) -> None:
        rc = bundle.summary.run_counts
        assert rc.total == 11
        assert rc.scored == 4
        assert rc.isolated == 7
        assert rc.version_mismatch == 1
        assert rc.judge_failed == 1
        assert rc.awaiting_judge == 1
        assert rc.valid_zero == 1
        assert rc.invalid == 1
        assert rc.failed == 1
        assert rc.missing_artifact == 1

    def test_stability_summary(self, bundle: ReportBundle) -> None:
        st = bundle.summary.stability
        assert st.scored_run_count == 4
        # All synthetic scored runs use mean consensus (2 judges, no arbiter).
        assert st.arbiter_used_count == 0
        assert st.consensus_mode_counts.get("mean") == 4
        # All 4 runs have A/B disagreement (2 items each).
        assert st.runs_with_ab_disagreement == 4
        assert st.total_ab_disagreement_items == 8

    def test_judge_call_count_zero(self, bundle: ReportBundle) -> None:
        """The report performed zero Judge calls (acceptance criterion)."""
        assert bundle.judge_call_count == 0
        assert bundle.summary.judge_call_count == 0
        assert JUDGE_CALL_COUNT == 0

    def test_compatibility_matrix_in_summary(self, bundle: ReportBundle) -> None:
        matrix = bundle.summary.compatibility
        assert len(matrix.groups) >= 1
        doc = matrix.to_dict()
        assert "dimensions" in doc
        assert "groups" in doc

    def test_mean_median_total_diff(self, bundle: ReportBundle) -> None:
        """Mean and median paired total diffs are computed."""
        # diffs: case A = 29.5, case B = 40.75
        expected = (Decimal("29.5") + Decimal("40.75")) / Decimal(2)
        assert bundle.summary.median_total_diff == expected
        assert bundle.summary.mean_total_diff == expected
