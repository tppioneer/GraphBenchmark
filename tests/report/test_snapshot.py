"""Approved structural snapshot test (AIS-011 verification: "用合成 fixtures
生成报告并与批准的结构快照比较").

The snapshot under ``tests/report/snapshots/synthetic_experiment.json`` is the
approved reference. If the report structure changes intentionally, regenerate
the snapshot and review the diff. The snapshot carries its own canonical digest
so tampering is detectable.
"""

from __future__ import annotations

import json
from pathlib import Path

from report.aggregate import aggregate
from report.analysis_input import load_runs
from report.canonical_audit import report_bundle_digest
from tests.report import fixtures as fx

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "synthetic_experiment.json"


def _load_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _build_bundle_dict(tmp_path: Path) -> dict:
    fx.build_synthetic_experiment(tmp_path / "runs")
    bundle = aggregate(load_runs(tmp_path / "runs"))
    return bundle.to_dict()


class TestSnapshot:
    """The generated report matches the approved structural snapshot."""

    def test_bundle_matches_snapshot(self, tmp_path: Path) -> None:
        snapshot = _load_snapshot()
        generated = _build_bundle_dict(tmp_path)
        assert generated == snapshot["bundle"], (
            "Report bundle does not match the approved snapshot. If the change "
            "is intentional, regenerate tests/report/snapshots/"
            "synthetic_experiment.json."
        )

    def test_snapshot_digest_matches(self, tmp_path: Path) -> None:
        """The snapshot's recorded digest matches a fresh computation."""
        snapshot = _load_snapshot()
        fx.build_synthetic_experiment(tmp_path / "runs2")
        bundle = aggregate(load_runs(tmp_path / "runs2"))
        digest = report_bundle_digest(bundle)
        assert digest == snapshot["digest"]

    def test_snapshot_has_expected_structure(self) -> None:
        snapshot = _load_snapshot()
        bundle = snapshot["bundle"]
        assert "summary" in bundle
        assert "cases" in bundle
        assert bundle["judge_call_count"] == 0
        summary = bundle["summary"]
        assert "run_counts" in summary
        assert "paired_absolute_diffs" in summary
        assert "stability" in summary
        assert "cost" in summary
        assert "compatibility" in summary

    def test_snapshot_has_no_pairwise_preference(self) -> None:
        """The snapshot never contains a Pairwise preference (§16.2)."""
        text = json.dumps(_load_snapshot()).lower()
        assert "pairwise" not in text
        assert "preference" not in text
