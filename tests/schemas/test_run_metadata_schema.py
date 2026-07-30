"""run-metadata.schema.json positive and negative tests."""

from __future__ import annotations

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
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["metrics"]["tool_call_count"] = -1
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/metrics/tool_call_count" in {json_pointer(e) for e in errors}


def test_unknown_metric_rejected(make_validator) -> None:
    v = make_validator("run-metadata")
    bad = dict(ex.MINIMAL_RUN_METADATA)
    bad["metrics"]["judge_cost_usd"] = 0.01
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/metrics" in {json_pointer(e) for e in errors}
