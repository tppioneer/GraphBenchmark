"""Report visualization: deterministic text rendering of report views.

Responsibility boundary (design §4, §18.1):

- ``report.visualization`` renders already-aggregated views to text/Markdown.
- It never calls the Judge, never re-scores, and never reads run artifacts
  directly (it consumes :mod:`report.aggregate` dataclasses only).
"""

from report.visualization.text import (
    render_case_report,
    render_compatibility_matrix,
    render_report_bundle,
    render_summary_report,
)

__all__ = (
    "render_case_report",
    "render_compatibility_matrix",
    "render_report_bundle",
    "render_summary_report",
)
