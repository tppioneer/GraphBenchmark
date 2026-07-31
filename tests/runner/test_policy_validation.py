"""AIS-009: tool-policy and artifact-compliance validation (design §8.7, §15.1).

Covers the policy truth table required by the task card:

* Graph policy + Graph queries          -> valid;
* Graph policy + no Graph queries        -> invalid (graph_policy_no_graph_query);
* Grep policy + Graph query              -> invalid (grep_policy_graph_query);
* Grep policy + no Graph queries         -> valid;
* Mixed policy                           -> valid (no count-based violation);
* unreadable agent-answer artifact       -> invalid (artifact_unreadable);
* empty / refused / completed / schema-warning -> admissible.

Also covers the invariants: tool events must carry the Runner's verifiable
source (the agent cannot forge compliance), and the policy result document is
schema-valid (policy-result.schema.json).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runner.policy_validation import (
    RUNNER_OBSERVED_SOURCE,
    PolicyResult,
    PolicyValidationError,
    ToolEvent,
    ToolKind,
    derive_metrics,
    validate_policy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"


def _load_schema(name: str) -> dict:
    with (SCHEMA_DIR / f"{name}.schema.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _policy_result_validator() -> Draft202012Validator:
    return Draft202012Validator(_load_schema("policy-result"))


def _event(kind: ToolKind, *, source: str = RUNNER_OBSERVED_SOURCE) -> ToolEvent:
    return ToolEvent(kind=kind, source=source, label=kind.value)


# --------------------------------------------------------------------------- #
# Policy truth table
# --------------------------------------------------------------------------- #


def test_graph_policy_with_graph_queries_valid() -> None:
    result = validate_policy(
        tool_policy="graph",
        tool_events=(_event(ToolKind.GRAPH), _event(ToolKind.FILE_READ)),
        agent_answer_status="completed",
    )
    assert result.valid
    assert result.violations == []
    assert any("verified Graph query" in obs for obs in result.observations)


def test_graph_policy_without_graph_queries_invalid() -> None:
    result = validate_policy(
        tool_policy="graph",
        tool_events=(_event(ToolKind.FILE_READ), _event(ToolKind.SEARCH)),
        agent_answer_status="completed",
    )
    assert not result.valid
    codes = [v.code for v in result.violations]
    assert "graph_policy_no_graph_query" in codes
    assert all(v.severity == "error" for v in result.violations)


def test_grep_policy_without_graph_queries_valid() -> None:
    result = validate_policy(
        tool_policy="grep",
        tool_events=(_event(ToolKind.SEARCH), _event(ToolKind.FILE_READ)),
        agent_answer_status="completed",
    )
    assert result.valid
    assert result.violations == []


def test_grep_policy_with_graph_query_invalid() -> None:
    result = validate_policy(
        tool_policy="grep",
        tool_events=(_event(ToolKind.GRAPH), _event(ToolKind.SEARCH)),
        agent_answer_status="completed",
    )
    assert not result.valid
    codes = [v.code for v in result.violations]
    assert "grep_policy_graph_query" in codes


def test_mixed_policy_valid_regardless_of_graph_usage() -> None:
    # Mixed policy with graph queries: no count-based violation.
    with_graph = validate_policy(
        tool_policy="mixed",
        tool_events=(_event(ToolKind.GRAPH), _event(ToolKind.SEARCH)),
        agent_answer_status="completed",
    )
    assert with_graph.valid
    # Mixed policy without graph queries: also valid.
    without_graph = validate_policy(
        tool_policy="mixed",
        tool_events=(_event(ToolKind.SEARCH),),
        agent_answer_status="completed",
    )
    assert without_graph.valid


def test_unreadable_artifact_invalid() -> None:
    """An unreadable agent-answer (status invalid) is not admissible (§12)."""
    result = validate_policy(
        tool_policy="graph",
        tool_events=(_event(ToolKind.GRAPH),),
        agent_answer_status="invalid",
    )
    assert not result.valid
    assert "artifact_unreadable" in [v.code for v in result.violations]


def test_empty_refused_completed_schema_warning_admissible() -> None:
    """empty/refused/completed/completed_with_schema_warning are admissible."""
    for status in ("empty", "refused", "completed", "completed_with_schema_warning"):
        result = validate_policy(
            tool_policy="graph",
            tool_events=(_event(ToolKind.GRAPH),),
            agent_answer_status=status,
        )
        assert result.valid, f"status {status!r} should be admissible"
        assert [v.code for v in result.violations] == []


# --------------------------------------------------------------------------- #
# Verifiable source: the agent cannot forge compliance
# --------------------------------------------------------------------------- #


def test_untrusted_tool_event_source_rejected() -> None:
    """A tool event whose source is not the Runner's channel is rejected.

    The agent cannot forge compliance by self-reporting tool events: the
    validator trusts only the Runner's verifiable observation source.
    """
    forged = ToolEvent(kind=ToolKind.GRAPH, source="agent:self_report", label="graph")
    with pytest.raises(PolicyValidationError, match="untrusted source"):
        validate_policy(
            tool_policy="graph",
            tool_events=(forged,),
            agent_answer_status="completed",
        )


def test_policy_ignores_agent_answer_for_tool_usage() -> None:
    """Compliance is decided solely by Runner-observed events, never the answer.

    Even if the agent's answer text claimed Graph usage, the policy result
    follows the observed events: graph policy with no observed Graph query is
    invalid regardless of any claim the agent might make (the agent-answer
    schema also forbids tool/policy fields, so there is nothing to honour).
    """
    result = validate_policy(
        tool_policy="graph",
        tool_events=(),  # Runner observed no Graph calls.
        agent_answer_status="completed",
    )
    assert not result.valid
    assert "graph_policy_no_graph_query" in [v.code for v in result.violations]


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_unknown_tool_policy_rejected() -> None:
    with pytest.raises(PolicyValidationError, match="tool_policy must be one of"):
        validate_policy(tool_policy="unknown", tool_events=(), agent_answer_status="completed")


def test_empty_tool_events_valid_for_grep() -> None:
    """Grep policy with no tool events at all is valid (0 Graph queries)."""
    result = validate_policy(
        tool_policy="grep", tool_events=(), agent_answer_status="completed"
    )
    assert result.valid


# --------------------------------------------------------------------------- #
# Metrics derivation
# --------------------------------------------------------------------------- #


def test_derive_metrics_counts() -> None:
    events = (
        _event(ToolKind.GRAPH),
        _event(ToolKind.GRAPH),
        _event(ToolKind.SEARCH),
        _event(ToolKind.FILE_READ),
        _event(ToolKind.FILE_READ),
        _event(ToolKind.FILE_READ),
        _event(ToolKind.OTHER),
    )
    metrics = derive_metrics(
        tool_events=events, elapsed_ms=12345, input_tokens=1000, output_tokens=200
    )
    assert metrics == {
        "tool_call_count": 7,
        "files_read_count": 3,
        "graph_query_count": 2,
        "search_query_count": 1,
        "elapsed_ms": 12345,
        "input_tokens": 1000,
        "output_tokens": 200,
    }


def test_derive_metrics_empty_events() -> None:
    metrics = derive_metrics(tool_events=(), elapsed_ms=0, input_tokens=0, output_tokens=0)
    assert metrics == {
        "tool_call_count": 0,
        "files_read_count": 0,
        "graph_query_count": 0,
        "search_query_count": 0,
        "elapsed_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


# --------------------------------------------------------------------------- #
# Document schema conformance
# --------------------------------------------------------------------------- #


def test_policy_result_doc_schema_valid() -> None:
    result = validate_policy(
        tool_policy="graph",
        tool_events=(_event(ToolKind.GRAPH),),
        agent_answer_status="completed",
    )
    doc = result.to_doc()
    assert doc["schema_version"] == "policy-result-v1"
    assert not list(_policy_result_validator().iter_errors(doc))


def test_policy_result_doc_invalid_case_schema_valid() -> None:
    """An invalid policy result is still a schema-valid policy-result document."""
    result = validate_policy(
        tool_policy="grep",
        tool_events=(_event(ToolKind.GRAPH),),
        agent_answer_status="completed",
    )
    assert not result.valid
    doc = result.to_doc()
    assert doc["valid"] is False
    assert doc["violations"]
    assert not list(_policy_result_validator().iter_errors(doc))


def test_policy_result_is_frozen() -> None:
    result = PolicyResult(valid=True)
    # A frozen dataclass cannot be mutated; this guards against accidental
    # post-construction mutation of the verdict.
    with pytest.raises((AttributeError, Exception)):
        result.valid = False  # type: ignore[misc]
