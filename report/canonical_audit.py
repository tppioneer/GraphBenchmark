"""Canonical digest helpers for report bundles (AIS-011 delivery contract).

The delivery contract requires a "sample report and its input digest". The
input digest is the canonical SHA-256 of the report bundle dict, computed via
:func:`judge.canonical.digest_json` so it is stable across platforms, processes
and Python dict orderings (design §9.2, §13.4).

This module is display/audit only; it never calls the Judge.
"""

from __future__ import annotations

from judge.canonical import digest_json
from report.aggregate import ReportBundle


def report_bundle_digest(bundle: ReportBundle) -> str:
    """The canonical sha256 digest of a report bundle (delivery contract).

    The digest is taken over the canonical JSON form of ``bundle.to_dict()``,
    so the same bundle always produces the same digest regardless of dict
    insertion order or platform.
    """
    return digest_json(bundle.to_dict())
