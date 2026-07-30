"""Cross-document contract tests (§10.2).

These cover the negative categories that a single schema cannot enforce on its
own because they span two artifacts:

* unknown item      - judge-output item_id not in the ground-truth rubric;
* bad reference     - finding.evidence_ids pointing at missing evidence, and
                      judge-output answer_evidence.json_pointer not resolving
                      in the answer payload.

The validators live in ``_validators`` and are test-only (rubric business
validation is out of scope for AIS-002). Each violation raises a
``ContractError`` carrying an RFC 6901 JSON Pointer.
"""

from __future__ import annotations

import pytest

from . import examples as ex
from ._validators import (
    ContractError,
    validate_answer_evidence_pointers,
    validate_evidence_references,
    validate_judge_output_items,
)


def test_valid_judge_output_matches_ground_truth() -> None:
    validate_judge_output_items(ex.FULL_JUDGE_OUTPUT, ex.FULL_GT)  # no raise


def test_valid_evidence_references() -> None:
    validate_evidence_references(ex.FULL_AGENT_ANSWER)  # no raise


def test_valid_answer_evidence_pointers() -> None:
    validate_answer_evidence_pointers(ex.FULL_JUDGE_OUTPUT, ex.FULL_AGENT_ANSWER)  # no raise


def test_unknown_item_rejected() -> None:
    bad = ex.judge_output_with_unknown_item()
    with pytest.raises(ContractError) as exc_info:
        validate_judge_output_items(bad, ex.FULL_GT)
    assert exc_info.value.pointer == "/items/0/item_id"
    assert "unknown" in exc_info.value.message


def test_missing_gt_item_rejected() -> None:
    import copy

    bad = copy.deepcopy(ex.FULL_JUDGE_OUTPUT)
    bad["items"].pop()  # one GT item no longer judged
    with pytest.raises(ContractError) as exc_info:
        validate_judge_output_items(bad, ex.FULL_GT)
    assert exc_info.value.pointer == "/items"
    assert "not judged" in exc_info.value.message


def test_bad_evidence_reference_rejected() -> None:
    bad = ex.agent_answer_with_bad_evidence_reference()
    with pytest.raises(ContractError) as exc_info:
        validate_evidence_references(bad)
    assert exc_info.value.pointer == "/answer/findings/0/evidence_ids/0"
    assert "evidence-missing" in exc_info.value.message


def test_bad_answer_evidence_pointer_rejected() -> None:
    bad = ex.judge_output_with_bad_answer_pointer()
    with pytest.raises(ContractError) as exc_info:
        validate_answer_evidence_pointers(bad, ex.FULL_AGENT_ANSWER)
    assert exc_info.value.pointer == "/items/0/answer_evidence/0/json_pointer"
    assert "no-such-field" in exc_info.value.message


def test_mismatched_answer_evidence_quote_rejected() -> None:
    # Correct json_pointer (resolves to /answer/summary) but the quote is absent
    # from the referenced text (R5).
    bad = ex.judge_output_with_mismatched_quote()
    with pytest.raises(ContractError) as exc_info:
        validate_answer_evidence_pointers(bad, ex.FULL_AGENT_ANSWER)
    assert exc_info.value.pointer == "/items/0/answer_evidence/0/quote"
    assert "not found" in exc_info.value.message


def test_non_text_answer_evidence_pointer_rejected() -> None:
    # A json_pointer that resolves to a non-text node is not referenceable text (R5).
    bad = ex.judge_output_with_non_text_pointer()
    with pytest.raises(ContractError) as exc_info:
        validate_answer_evidence_pointers(bad, ex.FULL_AGENT_ANSWER)
    assert exc_info.value.pointer == "/items/0/answer_evidence/0/json_pointer"
    assert "non-text" in exc_info.value.message
