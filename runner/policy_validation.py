"""Tool-policy and artifact-compliance validation (design §8.7, §15.1).

The Runner independently observes tool events (§8.6) and this validator
determines whether the observed tool usage satisfies the declared
``tool_policy`` and whether the produced agent-answer artifact is admissible.
The agent never self-reports tools, cost or violations (invariant): the
agent-answer schema (``additionalProperties: false`` at every level) forbids
those fields, and this validator consumes only Runner-observed tool events -
never anything the agent emitted. The agent's textual answer therefore cannot
forge compliance (acceptance criterion: "工具事件有可核验来源，Agent 输出不能
伪造合规").

Admission rules enforced here (§15.1):

* ``graph`` policy with no real Graph query              -> run invalid.
* ``grep`` policy with any Graph query                   -> run invalid.
* unreadable agent-answer artifact (status ``invalid``)  -> run invalid (§12).

Ground-Truth/Profile scorability and Judge-protocol completeness are later
scoring-layer concerns (AIS-004+, §15.1) and are intentionally NOT checked
here: the Runner does not score and never falls back to a string scorer
(invariant: "不可评分状态不能回退到旧 scorer"). Process metrics derived from
tool events never enter the correctness total (invariant: "过程指标不进入
correctness total").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "policy-result-v1"

#: The only source trusted for a tool event. The Runner's observation channel
#: stamps every event with this source; the agent's textual answer cannot
#: produce tool events (the agent-answer schema forbids tool/policy fields), so
#: an event carrying any other source is an adapter/programming error and is
#: rejected rather than honoured (invariant: "Agent 输出不能伪造合规").
RUNNER_OBSERVED_SOURCE = "runner:tool_call"

#: The agent-answer status value that marks an unreadable artifact (§8.8).
#: Kept as a string so this module depends only on the agent-answer contract,
#: not on :mod:`runner.execution`.
UNREADABLE_ANSWER_STATUS = "invalid"


class ToolKind(str, Enum):
    """Classifies an observed tool call for policy and metric accounting."""

    GRAPH = "graph"
    SEARCH = "search"
    FILE_READ = "file_read"
    OTHER = "other"


class ToolPolicy(str, Enum):
    """The declared experimental tool condition (run-metadata ``tool_policy``)."""

    GRAPH = "graph"
    GREP = "grep"
    MIXED = "mixed"


@dataclass(frozen=True)
class ToolEvent:
    """A single tool call observed by the Runner.

    ``source`` must be :data:`RUNNER_OBSERVED_SOURCE`; the validator rejects any
    event whose source is not the Runner's verifiable observation channel, so
    the agent cannot forge compliance by self-reporting tool events. ``label``
    is an optional human-readable detail (e.g. the tool name) for auditability;
    it does not affect policy decisions.
    """

    kind: ToolKind
    source: str = RUNNER_OBSERVED_SOURCE
    label: str = ""


class PolicyValidationError(ValueError):
    """Raised for invalid policy-validation inputs (a programming/adapter error).

    This is distinct from a :class:`PolicyResult` with ``valid=False``: an
    invalid input (unknown ``tool_policy`` or an untrusted tool-event source)
    cannot be evaluated at all and is rejected, whereas a policy violation is a
    valid evaluation that the run is not admissible.
    """


@dataclass(frozen=True)
class Violation:
    """A single policy violation entry (policy-result.schema.json)."""

    code: str
    message: str
    severity: str = "error"

    def to_doc(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(frozen=True)
class PolicyResult:
    """The tool-policy and artifact-compliance verdict (policy-result-v1)."""

    valid: bool
    violations: list[Violation] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "valid": self.valid,
            "violations": [v.to_doc() for v in self.violations],
            "observations": list(self.observations),
        }


def _verify_sources(tool_events: tuple[ToolEvent, ...]) -> None:
    """Reject any tool event whose source is not the Runner's observation channel.

    The agent cannot produce tool events (its answer schema forbids tool/policy
    fields), so an untrusted source is always an adapter/programming error, not
    agent behaviour, and is rejected rather than silently ignored.
    """
    for i, event in enumerate(tool_events):
        if event.source != RUNNER_OBSERVED_SOURCE:
            raise PolicyValidationError(
                f"tool_events[{i}] has untrusted source {event.source!r}; only "
                f"{RUNNER_OBSERVED_SOURCE!r} is accepted (the agent cannot forge "
                f"compliance by self-reporting tool events)"
            )


def validate_policy(
    *,
    tool_policy: str,
    tool_events: tuple[ToolEvent, ...] = (),
    agent_answer_status: str,
) -> PolicyResult:
    """Validate tool-policy and artifact compliance (§8.7, §15.1).

    Returns a :class:`PolicyResult` whose ``valid`` is ``False`` when an
    admission rule is violated. Tool events are trusted only from the Runner's
    verifiable observation channel; the agent's answer is never consulted for
    tool usage. ``agent_answer_status`` is the agent-answer ``status`` string
    (e.g. ``"completed"``, ``"invalid"``); only the unreadable ``"invalid"``
    status is an admission violation - empty/refused answers are admissible
    (they score 0, §12) and schema-warning answers are admissible (the Judge may
    still evaluate them, §8.8).
    """
    try:
        policy = ToolPolicy(tool_policy)
    except ValueError as exc:
        raise PolicyValidationError(
            f"tool_policy must be one of {[p.value for p in ToolPolicy]}, "
            f"got {tool_policy!r}"
        ) from exc

    _verify_sources(tool_events)

    graph_queries = sum(1 for e in tool_events if e.kind is ToolKind.GRAPH)
    search_queries = sum(1 for e in tool_events if e.kind is ToolKind.SEARCH)
    files_read = sum(1 for e in tool_events if e.kind is ToolKind.FILE_READ)

    violations: list[Violation] = []
    observations: list[str] = [
        f"Runner observed {len(tool_events)} tool call(s): "
        f"{graph_queries} graph, {search_queries} search, {files_read} file_read."
    ]

    # Tool-policy admission rules (§15.1).
    if policy is ToolPolicy.GRAPH:
        if graph_queries == 0:
            violations.append(
                Violation(
                    code="graph_policy_no_graph_query",
                    message="Graph policy requires at least one real Graph query; none observed.",
                )
            )
            observations.append("Graph policy produced 0 verified Graph queries.")
        else:
            observations.append(
                f"Graph policy produced {graph_queries} verified Graph query(ies)."
            )
    elif policy is ToolPolicy.GREP:
        if graph_queries > 0:
            violations.append(
                Violation(
                    code="grep_policy_graph_query",
                    message="Grep policy forbids Graph queries; a Graph query was observed.",
                )
            )
            observations.append(
                f"Grep policy observed {graph_queries} Graph query(ies) (must be 0)."
            )
        else:
            observations.append("Grep policy observed 0 Graph queries.")
    else:  # ToolPolicy.MIXED
        observations.append(
            f"Mixed policy observed {graph_queries} graph and {search_queries} search queries."
        )

    # Artifact compliance (§15.1, §12): an unreadable agent-answer artifact
    # (status ``invalid``) is not admissible. Empty/refused answers are
    # admissible (they score 0, §12); schema-warning answers are admissible
    # (the Judge may still evaluate them, §8.8).
    if agent_answer_status == UNREADABLE_ANSWER_STATUS:
        violations.append(
            Violation(
                code="artifact_unreadable",
                message="Agent-answer artifact is unreadable (status invalid); run not admissible.",
            )
        )

    return PolicyResult(
        valid=not violations,
        violations=violations,
        observations=observations,
    )


def derive_metrics(
    *,
    tool_events: tuple[ToolEvent, ...],
    elapsed_ms: int,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, int]:
    """Build the run-metadata ``metrics`` dict from Runner-observed tool events.

    Every count is derived from verifiable tool events, not self-reported by the
    agent. Process metrics never enter the correctness total (invariant); they
    are reported separately (§15.2). The returned dict conforms to the
    ``metrics`` sub-schema of ``run-metadata.schema.json``.
    """
    return {
        "tool_call_count": len(tool_events),
        "files_read_count": sum(1 for e in tool_events if e.kind is ToolKind.FILE_READ),
        "graph_query_count": sum(1 for e in tool_events if e.kind is ToolKind.GRAPH),
        "search_query_count": sum(1 for e in tool_events if e.kind is ToolKind.SEARCH),
        "elapsed_ms": int(elapsed_ms),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }
