"""Tests for ``report.analysis_input`` (design §15--§20, §17).

AIS-011 acceptance-criteria mapping:

==  =========================================  ================================================
§   Criterion                                  Test
==  =========================================  ================================================
17  scored run loads effective score           test_scored_run_classified_scored
20  requested/effective model mismatch         test_version_mismatch_isolated
    -> isolated, not in aggregation
13.5 judge_failed -> isolated, no score        test_judge_failed_isolated
    awaiting-judge -> isolated                 test_awaiting_judge_isolated
    invalid -> isolated                        test_invalid_isolated
    failed -> isolated                         test_failed_isolated
    missing artifact -> isolated               test_missing_artifact_isolated
    valid_zero -> isolated (no inferred score) test_valid_zero_isolated
    malformed score-v1 -> isolated invalid      TestMalformedScoreV1.test_missing_dimension_totals
15.2 cost is independent of correctness        test_agent_cost_loaded / test_judge_cost_optional
13  judge disagreement from judge-a/b          test_ab_disagreement_counted
13  A/B under-count on critical sub-threshold  test_critical_subthreshold_diff_under_counted
20  compatibility key groups by version        test_compatibility_key_groups
==  =========================================  ================================================
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from report.aggregate import aggregate
from report.analysis_input import (
    DIMENSION_NAMES,
    ISOLATION_AWAITING_JUDGE,
    ISOLATION_FAILED,
    ISOLATION_INVALID,
    ISOLATION_JUDGE_FAILED,
    ISOLATION_MISSING_ARTIFACT,
    ISOLATION_VALID_ZERO,
    ISOLATION_VERSION_MISMATCH,
    ReportError,
    RunReportStatus,
    compatibility_key,
    load_run,
    load_runs,
)
from tests.report import fixtures as fx


@pytest.fixture()
def scored_run(tmp_path: Path) -> Path:
    """A single scored graph run directory."""
    run_dir = tmp_path / "caseA-graph-r1"
    fx.build_scored_run(
        run_dir,
        run_id="caseA-graph-r1",
        case_id=fx.CASE_A,
        tool_policy="graph",
        credits=fx._CREDITS_GRAPH_A,
        answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
    )
    return run_dir


class TestScoredRunLoading:
    """A scored run loads its effective score and classifies as SCORED."""

    def test_scored_run_classified_scored(self, scored_run: Path) -> None:
        rec = load_run(scored_run)
        assert rec.status is RunReportStatus.SCORED
        assert rec.is_scored is True
        assert rec.isolation_reason is None
        assert rec.score is not None
        assert rec.version_identity is not None

    def test_scored_run_carries_correctness_view(self, scored_run: Path) -> None:
        rec = load_run(scored_run)
        assert rec.score is not None
        # All credits are 1.0 -> raw/capped total = 100.
        assert rec.score.raw_total == Decimal(100)
        assert rec.score.capped_total == Decimal(100)
        assert rec.score.critical_cap_applied is False
        assert len(rec.score.items) == 10
        assert set(rec.score.dimension_totals.keys()) == set(DIMENSION_NAMES)

    def test_scored_run_carries_agent_identity(self, scored_run: Path) -> None:
        rec = load_run(scored_run)
        assert rec.agent == fx.AGENT
        assert rec.agent_model == fx.AGENT_MODEL
        assert rec.tool_policy == "graph"

    def test_scored_run_carries_consensus_and_review(self, scored_run: Path) -> None:
        rec = load_run(scored_run)
        assert rec.score is not None
        assert rec.score.consensus_mode == "mean"
        assert rec.score.consensus_judges == 2
        assert rec.score.requires_human_review is False

    def test_scored_run_ab_disagreement_counted(self, scored_run: Path) -> None:
        """Judge A/B disagreement items are counted from judge-a/b.json (§13)."""
        rec = load_run(scored_run)
        assert rec.judge_disagreement is not None
        assert rec.judge_disagreement.available_judge_outputs == 2
        # _JUDGE_A_CREDITS vs _JUDGE_B_CREDITS differ by 0.5 on items 2 and 7
        # (>0.25 uniform threshold, §13.1 step 3).
        assert rec.judge_disagreement.ab_disagreement_items == 2

    def test_agent_cost_loaded(self, scored_run: Path) -> None:
        """Agent cost metrics are loaded from run-metadata (§15.2)."""
        rec = load_run(scored_run)
        assert rec.cost.agent is not None
        assert rec.cost.agent.tool_call_count == 10
        assert rec.cost.agent.graph_query_count == 7
        assert rec.cost.agent.input_tokens == 11000

    def test_judge_cost_optional(self, scored_run: Path) -> None:
        """Judge cost is loaded from judge-score.json when present."""
        rec = load_run(scored_run)
        assert rec.cost.judge.available is True
        assert rec.cost.judge.judge_call_count == 2
        assert rec.cost.judge.total_latency_ms == 45000

    def test_judge_cost_absent_when_no_judge_score(self, tmp_path: Path) -> None:
        """When judge-score.json is absent, Judge cost is not available."""
        run_dir = tmp_path / "no-jcost"
        fx.build_scored_run(
            run_dir,
            run_id="no-jcost",
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
            judge_cost=False,
        )
        rec = load_run(run_dir)
        assert rec.cost.judge.available is False
        assert rec.cost.judge.judge_call_count is None


class TestIsolatedRuns:
    """Each isolation reason is correctly classified from frozen artifacts."""

    def test_version_mismatch_isolated(self, tmp_path: Path) -> None:
        """requested != effective model -> version_mismatch (§13.3, §20)."""
        run_dir = tmp_path / "vmm"
        fx.build_version_mismatch_run(run_dir, run_id="vmm", case_id=fx.CASE_A)
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_VERSION_MISMATCH
        assert rec.score is None
        # The version identity is still carried (for the compatibility matrix).
        assert rec.version_identity is not None
        assert rec.version_identity.models_agree is False

    def test_judge_failed_isolated(self, tmp_path: Path) -> None:
        """judge_failed -> isolated, no inferred score (§13.5)."""
        run_dir = tmp_path / "jf"
        fx.build_judge_failed_run(run_dir, run_id="jf", case_id=fx.CASE_A)
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_JUDGE_FAILED
        assert rec.score is None

    def test_awaiting_judge_isolated(self, tmp_path: Path) -> None:
        """awaiting-judge -> isolated (substantive answer, no score)."""
        run_dir = tmp_path / "aj"
        fx.build_awaiting_judge_run(run_dir, run_id="aj", case_id=fx.CASE_A)
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_AWAITING_JUDGE
        assert rec.score is None

    def test_valid_zero_isolated(self, tmp_path: Path) -> None:
        """empty/refused answer -> valid_zero, no inferred score (§12)."""
        run_dir = tmp_path / "vz"
        fx.build_valid_zero_run(run_dir, run_id="vz", case_id=fx.CASE_A)
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_VALID_ZERO
        assert rec.score is None

    def test_invalid_isolated(self, tmp_path: Path) -> None:
        """policy violation -> invalid, isolated."""
        run_dir = tmp_path / "inv"
        fx.build_invalid_run(run_dir, run_id="inv", case_id=fx.CASE_A)
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_INVALID
        assert rec.policy_valid is False

    def test_failed_isolated(self, tmp_path: Path) -> None:
        """execution failure -> failed, isolated."""
        run_dir = tmp_path / "fld"
        fx.build_failed_run(run_dir, run_id="fld")
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_FAILED
        assert rec.answer_status is None

    def test_missing_artifact_isolated(self, tmp_path: Path) -> None:
        """missing required artifacts -> missing_artifact, isolated."""
        run_dir = tmp_path / "ma"
        fx.build_missing_artifact_run(run_dir, run_id="ma")
        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_MISSING_ARTIFACT


class TestMalformedScoreV1:
    """A malformed score-v1 artifact is isolated as invalid, not raised (R2).

    A ``score-v1``-tagged ``effective-score.json`` with missing or malformed
    fields must isolate the run as ``invalid`` with a stable detail instead of
    raising and aborting ``load_runs`` / report generation.
    """

    @staticmethod
    def _overwrite_score(run_dir: Path, doc: dict) -> None:
        """Build a valid scored run then overwrite effective-score.json."""
        fx.build_scored_run(
            run_dir,
            run_id=run_dir.name,
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
        )
        fx._write_json(run_dir / "effective-score.json", doc)

    @staticmethod
    def _valid_score_doc() -> dict:
        return fx._score_doc(
            case_id=fx.CASE_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
            gt_digest=fx.DIGEST_GT_A,
            credits=fx._CREDITS_GRAPH_A,
        )

    def test_missing_dimension_totals_isolated_invalid(self, tmp_path: Path) -> None:
        """A score-v1 doc missing dimension_totals isolates as invalid (R2)."""
        doc = self._valid_score_doc()
        del doc["dimension_totals"]
        run_dir = tmp_path / "runs" / "malformed-dt"
        self._overwrite_score(run_dir, doc)

        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_INVALID
        assert "malformed score-v1" in rec.isolation_detail
        assert rec.score is None
        assert rec.version_identity is None

    def test_missing_identity_field_isolated_invalid(self, tmp_path: Path) -> None:
        """A score-v1 doc missing a version-identity field isolates as invalid (R2)."""
        doc = self._valid_score_doc()
        del doc["benchmark_version"]
        run_dir = tmp_path / "runs" / "malformed-id"
        self._overwrite_score(run_dir, doc)

        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_INVALID
        assert "malformed score-v1" in rec.isolation_detail

    def test_non_numeric_dimension_total_isolated_invalid(self, tmp_path: Path) -> None:
        """A score-v1 doc with a non-numeric dimension total isolates as invalid (R2)."""
        doc = self._valid_score_doc()
        doc["dimension_totals"]["core_correctness"] = None
        run_dir = tmp_path / "runs" / "malformed-num"
        self._overwrite_score(run_dir, doc)

        rec = load_run(run_dir)
        assert rec.status is RunReportStatus.ISOLATED
        assert rec.isolation_reason == ISOLATION_INVALID
        assert "malformed score-v1" in rec.isolation_detail

    def test_malformed_run_does_not_abort_load_runs_or_report(self, tmp_path: Path) -> None:
        """A malformed run alongside a valid run does not abort load_runs or
        report generation (R2)."""
        runs = tmp_path / "runs"
        # A valid scored run.
        fx.build_scored_run(
            runs / "valid-graph",
            run_id="valid-graph",
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
        )
        # A malformed run (valid structure then field removed).
        doc = self._valid_score_doc()
        del doc["dimension_totals"]
        self._overwrite_score(runs / "malformed-run", doc)

        # load_runs must not raise.
        records = load_runs(runs)
        by_id = {r.run_id: r for r in records}
        assert by_id["valid-graph"].is_scored
        assert by_id["malformed-run"].status is RunReportStatus.ISOLATED
        assert by_id["malformed-run"].isolation_reason == ISOLATION_INVALID

        # Report generation must not raise.
        bundle = aggregate(records)
        assert bundle.summary.run_counts.scored == 1
        assert bundle.summary.run_counts.invalid == 1


class TestAbDisagreementUnderCount:
    """The uniform >0.25 A/B threshold under-counts critical sub-threshold
    disagreement relative to the GT-aware trigger (R3, §13.1)."""

    def test_critical_subthreshold_diff_under_counted(self, tmp_path: Path) -> None:
        """A critical item with a sub-threshold (<=0.25) A/B difference is NOT
        counted by the uniform >0.25 rule, even though the GT-aware trigger
        (any non-zero difference on a critical item) would flag it (R3).

        Rubric item ``outcome.root-cause`` is critical (see
        :func:`tests.report.fixtures._rubric_items`). Judge A credits 1.0,
        Judge B credits 0.9 -> |diff| = 0.1 < 0.25, so the uniform rule counts
        0 items. The GT-aware trigger would count 1.
        """
        run_dir = tmp_path / "runs" / "under-count"
        fx.build_scored_run(
            run_dir,
            run_id="under-count",
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
            judge_a_credits=[1.0] * 10,
            judge_b_credits=[0.9] + [1.0] * 9,  # 0.1 gap on critical item 0
        )
        rec = load_run(run_dir)
        assert rec.judge_disagreement is not None
        # The uniform >0.25 rule under-counts: 0 instead of the GT-aware 1.
        assert rec.judge_disagreement.ab_disagreement_items == 0


class TestCompatibilityKey:
    """The §20 compatibility key groups runs by version-level identity."""

    def test_compatibility_key_groups_by_version(self, scored_run: Path) -> None:
        rec = load_run(scored_run)
        assert rec.version_identity is not None
        key = compatibility_key(rec.version_identity)
        assert key == (
            "ai-score-v1",
            "semantic_outcome_v1",
            "bug_localization_v1",
            "claude-code-cli",
            "glm-5.2",
            "2.1.220",
        )

    def test_different_judge_model_different_key(self, tmp_path: Path) -> None:
        """A different Judge model produces a different compatibility key."""
        run_a = tmp_path / "a"
        fx.build_scored_run(
            run_a,
            run_id="a",
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
        )
        run_b = tmp_path / "b"
        fx.build_scored_run(
            run_b,
            run_id="b",
            case_id=fx.CASE_A,
            tool_policy="graph",
            credits=fx._CREDITS_GRAPH_A,
            answer_digest=fx.DIGEST_ANSWER_GRAPH_A,
            judge_model="claude-sonnet-4",
            judge_requested_model="claude-sonnet-4",
        )
        key_a = compatibility_key(load_run(run_a).version_identity)
        key_b = compatibility_key(load_run(run_b).version_identity)
        assert key_a != key_b


class TestLoadRuns:
    """``load_runs`` loads every sub-directory deterministically."""

    def test_load_runs_sorted(self, tmp_path: Path) -> None:
        fx.build_synthetic_experiment(tmp_path / "runs")
        records = load_runs(tmp_path / "runs")
        ids = [r.run_id for r in records]
        assert ids == sorted(ids)
        assert len(records) == 11  # 4 scored + 7 isolated

    def test_load_run_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ReportError):
            load_run(tmp_path / "does-not-exist")
