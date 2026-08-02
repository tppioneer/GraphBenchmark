"""Determinism and zero-Judge-call tests (AIS-011 acceptance criterion: "相同
artifact 重算产生稳定结果且 Judge 调用计数为零").

The report is a pure function of frozen artifacts. Re-aggregating the same
artifacts must produce byte-identical output, and the Judge call count must be
zero on every run.
"""

from __future__ import annotations

from pathlib import Path

from report.aggregate import JUDGE_CALL_COUNT, aggregate
from report.analysis_input import load_runs
from report.canonical_audit import report_bundle_digest
from tests.report import fixtures as fx


def _build_experiment(tmp_path: Path) -> Path:
    runs_root = tmp_path / "runs"
    fx.build_synthetic_experiment(runs_root)
    return runs_root


class TestDeterminism:
    """Re-aggregating the same artifacts produces identical output."""

    def test_recompute_identical_dict(self, tmp_path: Path) -> None:
        runs_root = _build_experiment(tmp_path)
        b1 = aggregate(load_runs(runs_root))
        b2 = aggregate(load_runs(runs_root))
        assert b1.to_dict() == b2.to_dict()

    def test_recompute_identical_text(self, tmp_path: Path) -> None:
        from report.visualization import render_report_bundle

        runs_root = _build_experiment(tmp_path)
        t1 = render_report_bundle(aggregate(load_runs(runs_root)))
        t2 = render_report_bundle(aggregate(load_runs(runs_root)))
        assert t1 == t2

    def test_judge_call_count_always_zero(self, tmp_path: Path) -> None:
        runs_root = _build_experiment(tmp_path)
        bundle = aggregate(load_runs(runs_root))
        assert bundle.judge_call_count == 0
        assert bundle.summary.judge_call_count == 0
        assert JUDGE_CALL_COUNT == 0

    def test_bundle_digest_stable(self, tmp_path: Path) -> None:
        """The canonical digest of the report bundle is stable across runs."""
        runs_root = _build_experiment(tmp_path)
        d1 = report_bundle_digest(aggregate(load_runs(runs_root)))
        d2 = report_bundle_digest(aggregate(load_runs(runs_root)))
        assert d1 == d2
        assert d1.startswith("sha256:")

    def test_no_judge_imports(self) -> None:
        """The report package does not import any Judge execution logic."""
        import inspect

        import report.aggregate
        import report.analysis_input
        import report.visualization.text

        for mod in (report.aggregate, report.analysis_input, report.visualization.text):
            src = inspect.getsource(mod)
            # The report may reference "Judge" in docstrings/constants, but must
            # not import judge_runner/provider/consensus execution modules.
            assert "from judge.judge_runner" not in src
            assert "from judge.provider" not in src
            assert "import judge.judge_runner" not in src
            assert "import judge.provider" not in src
