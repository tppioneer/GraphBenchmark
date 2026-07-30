"""judge-input.schema.json positive and negative tests.

The blind input MUST NOT carry experiment-group-leaking fields; the closed
content set is enforced with additionalProperties=false.
"""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("judge-input").is_valid(ex.FULL_JUDGE_INPUT)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("judge-input").is_valid(ex.MINIMAL_JUDGE_INPUT)


def test_identity_leak_rejected(make_validator) -> None:
    v = make_validator("judge-input")
    bad = ex.judge_input_with_identity_leak()  # adds agent_model
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/" in {json_pointer(e) for e in errors}
    assert any("agent_model" in e.message for e in errors)


def test_bad_digest_rejected(make_validator) -> None:
    v = make_validator("judge-input")
    bad = ex.judge_input_with_bad_digest()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/digests/ground_truth_digest" in {json_pointer(e) for e in errors}


def test_missing_digest_rejected(make_validator) -> None:
    v = make_validator("judge-input")
    bad = dict(ex.MINIMAL_JUDGE_INPUT)
    del bad["digests"]["judge_prompt_digest"]
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("judge_prompt_digest" in e.message and "required" in e.message for e in errors)


def test_wrong_judge_protocol_rejected(make_validator) -> None:
    v = make_validator("judge-input")
    bad = dict(ex.MINIMAL_JUDGE_INPUT)
    bad["judge_protocol"] = "pairwise_v1"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/judge_protocol" in {json_pointer(e) for e in errors}
