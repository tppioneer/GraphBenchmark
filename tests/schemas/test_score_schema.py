"""score.schema.json positive and negative tests.

Covers the digest-missing negative category: every formal score must carry
version and input digests (invariant). Also covers requested/effective Judge
model pinning and agreement (R2) and constrained human-review reasons (R3).
"""

from __future__ import annotations

import pytest

from . import examples as ex
from ._validators import json_pointer, validate_requested_effective_model


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


# Requested and effective Judge models (R2).

FORBIDDEN_MODEL_VALUES = ["Auto", "auto", "AUTO", "latest", "Latest", "LATEST"]


def test_requested_effective_model_agree_valid(make_validator) -> None:
    v = make_validator("score")
    assert v.is_valid(ex.FULL_SCORE)
    validate_requested_effective_model(ex.FULL_SCORE)  # no raise


def test_missing_requested_model_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = {k: val for k, val in ex.MINIMAL_SCORE.items() if k != "judge_requested_model"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("judge_requested_model" in e.message and "required" in e.message for e in errors)


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODEL_VALUES)
def test_forbidden_requested_model_rejected(make_validator, forbidden: str) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["judge_requested_model"] = forbidden
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/judge_requested_model" in {json_pointer(e) for e in errors}


@pytest.mark.parametrize("forbidden", FORBIDDEN_MODEL_VALUES)
def test_forbidden_effective_model_rejected(make_validator, forbidden: str) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["judge_model"] = forbidden
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/judge_model" in {json_pointer(e) for e in errors}


def test_model_mismatch_rejected() -> None:
    # A formal score with different requested/effective models is rejected.
    bad = ex.score_with_model_mismatch()
    # The schema itself is satisfied (both are pinned), but the cross-field
    # consistency checker rejects the mismatch.
    assert bad["judge_requested_model"] != bad["judge_model"]
    from ._validators import ContractError

    with pytest.raises(ContractError) as exc_info:
        validate_requested_effective_model(bad)
    assert exc_info.value.pointer == "/judge_requested_model"
    assert "!=" in exc_info.value.message


# Human review reasons (R3).


def test_human_review_true_with_reasons_valid(make_validator) -> None:
    v = make_validator("score")
    assert v.is_valid(ex.score_with_human_review())
    # reasons are constrained to the frozen trigger codes
    assert set(ex.score_with_human_review()["human_review_reasons"]) <= {
        "critical_credit_range",
        "critical_consensus_confidence",
        "overall_confidence",
    }


def test_human_review_true_missing_reasons_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["requires_human_review"] = True
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("human_review_reasons" in e.message and "required" in e.message for e in errors)


def test_human_review_true_empty_reasons_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["requires_human_review"] = True
    bad["human_review_reasons"] = []
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/human_review_reasons" in {json_pointer(e) for e in errors}


def test_human_review_false_with_reasons_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["requires_human_review"] = False
    bad["human_review_reasons"] = ["overall_confidence"]
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/human_review_reasons" in {json_pointer(e) for e in errors}


def test_human_review_false_empty_reasons_valid(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["requires_human_review"] = False
    bad["human_review_reasons"] = []
    assert v.is_valid(bad)


def test_human_review_bad_reason_code_rejected(make_validator) -> None:
    v = make_validator("score")
    bad = dict(ex.MINIMAL_SCORE)
    bad["requires_human_review"] = True
    bad["human_review_reasons"] = ["bogus_reason"]
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/human_review_reasons/0" in {json_pointer(e) for e in errors}
