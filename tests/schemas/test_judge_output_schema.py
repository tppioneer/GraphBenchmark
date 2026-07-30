"""judge-output.schema.json positive and negative tests.

Covers the illegal-credit negative category: single-Judge credit must belong
to the frozen set {0, 0.25, 0.5, 0.75, 1}.
"""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("judge-output").is_valid(ex.FULL_JUDGE_OUTPUT)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("judge-output").is_valid(ex.MINIMAL_JUDGE_OUTPUT)


def test_illegal_credit_rejected(make_validator) -> None:
    v = make_validator("judge-output")
    bad = ex.judge_output_with_illegal_credit()  # credit 0.6
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/items/0/credit" in {json_pointer(e) for e in errors}


def test_confidence_out_of_range_rejected(make_validator) -> None:
    v = make_validator("judge-output")
    bad = dict(ex.MINIMAL_JUDGE_OUTPUT)
    bad["items"][0]["confidence"] = 1.5
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/items/0/confidence" in {json_pointer(e) for e in errors}


def test_bad_json_pointer_format_rejected(make_validator) -> None:
    v = make_validator("judge-output")
    bad = dict(ex.MINIMAL_JUDGE_OUTPUT)
    bad["items"][0]["answer_evidence"] = [{"json_pointer": "answer/summary", "quote": "..."}]
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/items/0/answer_evidence/0/json_pointer" in {json_pointer(e) for e in errors}


def test_missing_scoring_profile_rejected(make_validator) -> None:
    v = make_validator("judge-output")
    bad = {k: val for k, val in ex.MINIMAL_JUDGE_OUTPUT.items() if k != "scoring_profile"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("scoring_profile" in e.message and "required" in e.message for e in errors)
