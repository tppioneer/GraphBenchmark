"""agent-answer.schema.json positive and negative tests.

Covers the identity-leak invariant: the agent answer MUST NOT carry
Runner-collected identity, tool, policy or cost fields. additionalProperties=false
at every level enforces that boundary, and the rejection is locatable.
"""

from __future__ import annotations

import copy

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("agent-answer").is_valid(ex.FULL_AGENT_ANSWER)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("agent-answer").is_valid(ex.MINIMAL_AGENT_ANSWER)


def test_root_identity_leak_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = ex.agent_answer_with_identity_leak()  # adds agent_model at root
    errors = list(v.iter_errors(bad))
    assert errors
    pointers = {json_pointer(e) for e in errors}
    assert "/" in pointers
    assert any("agent_model" in e.message for e in errors)


def test_answer_identity_leak_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = ex.agent_answer_with_tool_policy_leak()  # adds tool_policy in answer
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/answer" in {json_pointer(e) for e in errors}
    assert any("tool_policy" in e.message for e in errors)


def test_bad_finding_kind_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = ex.agent_answer_with_bad_finding_kind()  # entrypoint under bug_localization
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/answer/findings/0/kind" in {json_pointer(e) for e in errors}


def test_missing_explanation_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = dict(ex.MINIMAL_AGENT_ANSWER)
    bad["answer"] = {"summary": "only summary"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("explanation" in e.message and "required" in e.message for e in errors)


def test_bad_status_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = dict(ex.MINIMAL_AGENT_ANSWER)
    bad["status"] = "pending"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/status" in {json_pointer(e) for e in errors}


# Status-conditional answer representation (design §8.8, R1).


def test_schema_warning_fallback_valid(make_validator) -> None:
    # Empty summary is allowed; explanation carries the raw Markdown (non-empty).
    assert make_validator("agent-answer").is_valid(ex.agent_answer_schema_warning_fallback())


def test_empty_run_valid(make_validator) -> None:
    # empty status has a legal representation without fabricated answer text.
    assert make_validator("agent-answer").is_valid(ex.agent_answer_empty())


def test_refused_run_valid(make_validator) -> None:
    # refused status has a legal representation without fabricated answer text.
    assert make_validator("agent-answer").is_valid(ex.agent_answer_refused())


def test_invalid_run_valid(make_validator) -> None:
    # invalid status shares the same no-fabrication rule as empty/refused.
    assert make_validator("agent-answer").is_valid(ex.agent_answer_invalid())


def test_schema_warning_empty_explanation_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = ex.agent_answer_schema_warning_empty_explanation()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/answer/explanation" in {json_pointer(e) for e in errors}


def test_completed_empty_summary_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = ex.agent_answer_completed_empty_summary()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/answer/summary" in {json_pointer(e) for e in errors}


def test_completed_empty_explanation_rejected(make_validator) -> None:
    v = make_validator("agent-answer")
    bad = copy.deepcopy(ex.MINIMAL_AGENT_ANSWER)
    bad["answer"]["explanation"] = ""
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/answer/explanation" in {json_pointer(e) for e in errors}
