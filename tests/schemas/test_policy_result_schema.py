"""policy-result.schema.json positive and negative tests."""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("policy-result").is_valid(ex.FULL_POLICY_RESULT)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("policy-result").is_valid(ex.MINIMAL_POLICY_RESULT)


def test_unknown_field_rejected(make_validator) -> None:
    v = make_validator("policy-result")
    bad = {**ex.MINIMAL_POLICY_RESULT, "policy_id": "leak"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/" in {json_pointer(e) for e in errors}
    assert any("policy_id" in e.message for e in errors)


def test_bad_severity_rejected(make_validator) -> None:
    v = make_validator("policy-result")
    bad = {
        **ex.MINIMAL_POLICY_RESULT,
        "violations": [{"code": "graph_misuse", "message": "bad", "severity": "fatal"}],
    }
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/violations/0/severity" in {json_pointer(e) for e in errors}
