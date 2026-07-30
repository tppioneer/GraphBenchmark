"""run-metadata.schema.json positive and negative tests."""

from __future__ import annotations

import copy

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("run-metadata").is_valid(ex.FULL_RUN_METADATA)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("run-metadata").is_valid(ex.MINIMAL_RUN_METADATA)


def test_bad_tool_policy_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["tool_policy"] = "shell"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/tool_policy" in {json_pointer(e) for e in errors}


def test_bad_datetime_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["started_at"] = "not-a-timestamp"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/started_at" in {json_pointer(e) for e in errors}


def test_negative_metric_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = copy.deepcopy(ex.MINIMAL_RUN_METADATA)
    bad["metrics"]["tool_call_count"] = -1
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/metrics/tool_call_count" in {json_pointer(e) for e in errors}


def test_unknown_metric_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = copy.deepcopy(ex.MINIMAL_RUN_METADATA)
    bad["metrics"]["judge_cost_usd"] = 0.01
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/metrics" in {json_pointer(e) for e in errors}


# Strict RFC 3339 date-time validation (R7). datetime.fromisoformat would accept
# date-only and timezone-less values; the strict checker must reject both.


def test_date_only_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["started_at"] = "2025-01-15"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/started_at" in {json_pointer(e) for e in errors}


def test_timezone_less_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["started_at"] = "2025-01-15T10:30:00"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/started_at" in {json_pointer(e) for e in errors}


def test_offset_date_time_valid(make_validator) -> None:
    v = make_validator("run-metadata")
    good = dict(ex.MINIMAL_RUN_METADATA)
    good["started_at"] = "2025-01-15T10:30:00+08:00"
    assert v.is_valid(good)
