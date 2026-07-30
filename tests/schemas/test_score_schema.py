"""score.schema.json positive and negative tests.

Covers the digest-missing negative category: every formal score must carry
version and input digests (invariant).
"""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("score").is_valid(ex.FULL_SCORE)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("score").is_valid(ex.MINIMAL_SCORE)


def test_missing_digest_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = ex.score_missing_digest()  # drops ground_truth_digest
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/" in {json_pointer(e) for e in errors}
    assert any("ground_truth_digest" in e.message and "required" in e.message for e in errors)


def test_bad_benchmark_version_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = ex.score_with_bad_benchmark_version()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/benchmark_version" in {json_pointer(e) for e in errors}


def test_bad_judge_provider_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["judge_provider"] = "openai-api"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/judge_provider" in {json_pointer(e) for e in errors}


def test_bad_digest_pattern_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["agent_answer_digest"] = "sha256:short"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/agent_answer_digest" in {json_pointer(e) for e in errors}


def test_critical_cap_structure(make_validator) -> None:
    v = make_validator("score")
    capped = dict(ex.MINIMAL_SCORE)
    capped["raw_total"] = 35.0
    capped["critical_cap"] = {
        "applied": True,
        "cap_value": 50,
        "code": "core_correctness_all_zero",
        "reason": "all core critical items zero",
    }
    capped["capped_total"] = 35.0
    assert v.is_valid(capped)
