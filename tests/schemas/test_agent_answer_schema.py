"""agent-answer.schema.json positive and negative tests.

Covers the identity-leak invariant: the agent answer MUST NOT carry
Runner-collected identity, tool, policy or cost fields. additionalProperties=false
at every level enforces that boundary, and the rejection is locatable.
"""

from __future__ import annotations

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
