"""Report package: renders outcome, cost and stability views.

Responsibility boundary (see docs/ai-scoring-design.md §4, §18.1):

- ``report`` does NOT call or re-run the Judge.
- ``report`` consumes only frozen artifacts and produces deterministic,
  artifact-only views that separate correctness, compliance, stability and
  cost (AIS-011).
"""

from report.aggregate import JUDGE_CALL_COUNT, ReportBundle, aggregate
from report.analysis_input import RunRecord, load_run, load_runs
from report.canonical_audit import report_bundle_digest

__all__ = (
    "JUDGE_CALL_COUNT",
    "ReportBundle",
    "RunRecord",
    "aggregate",
    "load_run",
    "load_runs",
    "report_bundle_digest",
)
