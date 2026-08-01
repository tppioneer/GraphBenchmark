"""AIS-009: Runner lifecycle coordination and run artifact production.

Covers the acceptance criteria using a fake Agent:

* each run has manifest, raw response, answer, metadata and policy result;
* the Runner collects start/end time, tokens, tool/file/Graph/Search counts;
* tool events have a verifiable source and the agent cannot forge compliance;
* run status distinguishes valid / invalid / awaiting-judge / failed;
* interrupt-restart never treats a half-done run as success, and a run id is
  never silently reused for a different input;
* correctness and cost/policy fields are isolated at the storage layer.

The fake Agent is the Runner's observation channel: it returns the model's raw
bytes plus the tool events the Runner observed. The agent never self-reports
identity, tools, cost or violations.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from runner import benchmark_runner as br
from runner.execution import AgentAnswerStatus
from runner.policy_validation import RUNNER_OBSERVED_SOURCE, ToolEvent, ToolKind

from . import fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"

RAW = "raw-response.txt"
ANSWER = "agent-answer.json"
METADATA = "run-metadata.json"
POLICY = "policy-result.json"
MANIFEST = "manifest.json"
RUN_INPUT = "run-input.json"

_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _load_schema(name: str) -> dict:
    with (SCHEMA_DIR / f"{name}.schema.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _format_checker() -> FormatChecker:
    fc = FormatChecker()

    @fc.checks("date-time")
    def _is_strict_rfc3339(value: object) -> bool:
        if not isinstance(value, str):
            return True
        if not _RFC3339_DATE_TIME.match(value):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return False
        return True

    return fc


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_load_schema(name), format_checker=_format_checker())


# --------------------------------------------------------------------------- #
# Fake agent and builders
# --------------------------------------------------------------------------- #


class FakeAgent:
    """A minimal AgentAdapter that returns a predetermined outcome or raises."""

    def __init__(
        self,
        outcome: br.AgentRunOutcome | None = None,
        *,
        raises: BaseException | None = None,
    ):
        self._outcome = outcome
        self._raises = raises
        self.call_count = 0

    def execute(self, *, case_id: str, task_type: str, tool_policy: str) -> br.AgentRunOutcome:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        assert self._outcome is not None
        return self._outcome


def _identity(tool_policy: str = "graph") -> br.RunIdentity:
    return br.RunIdentity(
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        tool_policy=tool_policy,
        agent="claude-code",
        agent_model="glm-5.2",
    )


def _event(kind: ToolKind) -> ToolEvent:
    return ToolEvent(kind=kind, source=RUNNER_OBSERVED_SOURCE, label=kind.value)


def _outcome(
    raw: bytes,
    events: tuple[ToolEvent, ...] = (),
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> br.AgentRunOutcome:
    return br.AgentRunOutcome(
        raw_response=raw,
        tool_events=events,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _run(
    tmp_path: Path,
    agent: FakeAgent,
    *,
    run_id: str = "run-1",
    tool_policy: str = "graph",
) -> br.RunResult:
    return br.execute_run(
        runs_root=tmp_path,
        run_id=run_id,
        identity=_identity(tool_policy),
        agent=agent,
    )


# --------------------------------------------------------------------------- #
# Run state machine: the six required fake-Agent scenarios
# --------------------------------------------------------------------------- #


def test_graph_compliance_awaiting_judge(tmp_path: Path) -> None:
    agent = FakeAgent(
        _outcome(
            fx.completed_answer_bytes(),
            (_event(ToolKind.GRAPH), _event(ToolKind.FILE_READ)),
        )
    )
    result = _run(tmp_path, agent)
    assert result.status is br.RunStatus.AWAITING_JUDGE
    assert result.policy_valid
    assert result.agent_answer_status is AgentAnswerStatus.COMPLETED
    assert agent.call_count == 1
    # Every required artifact is present.
    run_dir = tmp_path / "run-1"
    assert (run_dir / RAW).exists()
    assert (run_dir / ANSWER).exists()
    assert (run_dir / METADATA).exists()
    assert (run_dir / POLICY).exists()
    assert (run_dir / MANIFEST).exists()


def test_graph_missing_invalid(tmp_path: Path) -> None:
    agent = FakeAgent(
        _outcome(fx.completed_answer_bytes(), (_event(ToolKind.FILE_READ), _event(ToolKind.SEARCH)))
    )
    result = _run(tmp_path, agent, run_id="graph-missing")
    assert result.status is br.RunStatus.INVALID
    assert not result.policy_valid
    policy_doc = json.loads((tmp_path / "graph-missing" / POLICY).read_text(encoding="utf-8"))
    assert "graph_policy_no_graph_query" in [v["code"] for v in policy_doc["violations"]]


def test_grep_overreach_invalid(tmp_path: Path) -> None:
    agent = FakeAgent(
        _outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH), _event(ToolKind.SEARCH)))
    )
    result = _run(tmp_path, agent, run_id="grep-overreach", tool_policy="grep")
    assert result.status is br.RunStatus.INVALID
    policy_doc = json.loads((tmp_path / "grep-overreach" / POLICY).read_text(encoding="utf-8"))
    assert "grep_policy_graph_query" in [v["code"] for v in policy_doc["violations"]]


def test_schema_warning_awaiting_judge(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.MARKDOWN_BYTES, (_event(ToolKind.GRAPH),)))
    result = _run(tmp_path, agent, run_id="schema-warning")
    assert result.status is br.RunStatus.AWAITING_JUDGE
    assert result.agent_answer_status is AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.policy_valid


def test_empty_response_valid(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.WHITESPACE_BYTES, (_event(ToolKind.GRAPH),)))
    result = _run(tmp_path, agent, run_id="empty")
    assert result.status is br.RunStatus.VALID
    assert result.agent_answer_status is AgentAnswerStatus.EMPTY
    assert result.policy_valid


def test_refused_response_valid(tmp_path: Path) -> None:
    doc = fx.completed_answer_doc()
    doc["status"] = "refused"
    doc["answer"]["summary"] = ""
    doc["answer"]["explanation"] = ""
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    agent = FakeAgent(_outcome(raw, (_event(ToolKind.GRAPH),)))
    result = _run(tmp_path, agent, run_id="refused")
    assert result.status is br.RunStatus.VALID
    assert result.agent_answer_status is AgentAnswerStatus.REFUSED


def test_execution_failure_failed(tmp_path: Path) -> None:
    agent = FakeAgent(raises=RuntimeError("agent crashed"))
    result = _run(tmp_path, agent, run_id="failed")
    assert result.status is br.RunStatus.FAILED
    assert result.agent_answer_status is None
    assert not result.policy_valid
    run_dir = tmp_path / "failed"
    # No raw response / answer produced, but metadata + policy + manifest exist.
    assert not (run_dir / RAW).exists()
    assert not (run_dir / ANSWER).exists()
    assert (run_dir / METADATA).exists()
    assert (run_dir / POLICY).exists()
    assert (run_dir / MANIFEST).exists()
    policy_doc = json.loads((run_dir / POLICY).read_text(encoding="utf-8"))
    assert policy_doc["valid"] is False
    assert [v["code"] for v in policy_doc["violations"]] == ["execution_failed"]


def test_all_four_run_statuses_are_distinct(tmp_path: Path) -> None:
    statuses = {
        _run(
            tmp_path,
            FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),))),
            run_id="s1",
        ).status,
        _run(
            tmp_path,
            FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.FILE_READ),))),
            run_id="s2",
        ).status,
        _run(
            tmp_path,
            FakeAgent(_outcome(fx.WHITESPACE_BYTES, (_event(ToolKind.GRAPH),))),
            run_id="s3",
        ).status,
        _run(tmp_path, FakeAgent(raises=RuntimeError("boom")), run_id="s4").status,
    }
    assert statuses == {
        br.RunStatus.AWAITING_JUDGE,
        br.RunStatus.INVALID,
        br.RunStatus.VALID,
        br.RunStatus.FAILED,
    }


# --------------------------------------------------------------------------- #
# Metrics collection (§8.6)
# --------------------------------------------------------------------------- #


def test_metrics_collected_from_observed_events(tmp_path: Path) -> None:
    events = (
        _event(ToolKind.GRAPH),
        _event(ToolKind.GRAPH),
        _event(ToolKind.SEARCH),
        _event(ToolKind.FILE_READ),
        _event(ToolKind.OTHER),
    )
    agent = FakeAgent(
        _outcome(
            fx.completed_answer_bytes(),
            events,
            input_tokens=12000,
            output_tokens=2300,
        )
    )
    result = _run(tmp_path, agent, run_id="metrics")
    assert result.metrics == {
        "tool_call_count": 5,
        "files_read_count": 1,
        "graph_query_count": 2,
        "search_query_count": 1,
        "elapsed_ms": result.metrics["elapsed_ms"],  # non-negative int
        "input_tokens": 12000,
        "output_tokens": 2300,
    }
    assert isinstance(result.metrics["elapsed_ms"], int)
    assert result.metrics["elapsed_ms"] >= 0
    metadata = json.loads((tmp_path / "metrics" / METADATA).read_text(encoding="utf-8"))
    assert metadata["metrics"] == result.metrics


def test_start_end_times_are_rfc3339_and_ordered(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    result = _run(tmp_path, agent, run_id="times")
    assert _RFC3339_DATE_TIME.match(result.started_at)
    assert _RFC3339_DATE_TIME.match(result.ended_at)
    start = datetime.fromisoformat(result.started_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(result.ended_at.replace("Z", "+00:00"))
    assert end >= start


# --------------------------------------------------------------------------- #
# Manifest correctness and digests (§17)
# --------------------------------------------------------------------------- #


def test_manifest_schema_valid_and_lists_all_artifacts(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="manifest")
    manifest = json.loads((tmp_path / "manifest" / MANIFEST).read_text(encoding="utf-8"))
    assert not list(_validator("manifest").iter_errors(manifest))
    by_name = {a["name"]: a for a in manifest["artifacts"]}
    # The Runner-produced artifacts are present.
    for name in ("raw_response", "agent_answer", "run_metadata", "policy_result"):
        assert by_name[name]["status"] == "present"
    # Judge artifacts are absent; adjudication is not_applicable (§14, §17).
    for name in ("blind_input", "judge_a", "judge_b", "judge_c", "judge_score", "effective_score"):
        assert by_name[name]["status"] == "absent"
    assert by_name["adjudication"]["status"] == "not_applicable"


def test_manifest_digests_match_file_bytes(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="digests")
    run_dir = tmp_path / "digests"
    manifest = json.loads((run_dir / MANIFEST).read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        if entry["status"] != "present":
            continue
        # The manifest path is run-relative; resolve against runs_root.
        artifact_path = tmp_path / entry["path"]
        actual = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert entry["sha256"] == actual, entry["name"]


def test_manifest_paths_are_run_relative(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="paths")
    manifest = json.loads((tmp_path / "paths" / MANIFEST).read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        if entry["status"] == "present":
            assert entry["path"].startswith("paths/")
            assert "\\" not in entry["path"]


def test_all_artifact_documents_are_schema_valid(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="schemas")
    run_dir = tmp_path / "schemas"
    answer = json.loads((run_dir / ANSWER).read_text(encoding="utf-8"))
    metadata = json.loads((run_dir / METADATA).read_text(encoding="utf-8"))
    policy = json.loads((run_dir / POLICY).read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / MANIFEST).read_text(encoding="utf-8"))
    assert not list(_validator("agent-answer").iter_errors(answer))
    assert not list(_validator("run-metadata").iter_errors(metadata))
    assert not list(_validator("policy-result").iter_errors(policy))
    assert not list(_validator("manifest").iter_errors(manifest))


# --------------------------------------------------------------------------- #
# Storage-layer isolation of correctness / cost / policy fields
# --------------------------------------------------------------------------- #


def test_correctness_cost_policy_fields_isolated(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="isolation")
    run_dir = tmp_path / "isolation"
    answer = json.loads((run_dir / ANSWER).read_text(encoding="utf-8"))
    metadata = json.loads((run_dir / METADATA).read_text(encoding="utf-8"))
    policy = json.loads((run_dir / POLICY).read_text(encoding="utf-8"))

    # agent-answer carries no Runner-collected identity/tool/cost/policy fields.
    forbidden_in_answer = {
        "agent", "agent_model", "tool_policy", "metrics", "violations", "valid",
    }
    assert not (set(answer) & forbidden_in_answer)

    # run-metadata carries only identity + cost; no answer/correctness/policy.
    forbidden_in_metadata = {
        "answer", "findings", "evidence", "violations", "valid", "case_id", "task_type",
    }
    assert not (set(metadata) & forbidden_in_metadata)
    assert set(metadata["metrics"]) == {
        "tool_call_count", "files_read_count", "graph_query_count",
        "search_query_count", "elapsed_ms", "input_tokens", "output_tokens",
    }

    # policy-result carries only compliance; no identity/cost/answer content.
    forbidden_in_policy = {"agent", "agent_model", "metrics", "answer", "case_id", "task_type"}
    assert not (set(policy) & forbidden_in_policy)


# --------------------------------------------------------------------------- #
# Verifiable source: the agent cannot forge compliance
# --------------------------------------------------------------------------- #


def test_agent_cannot_forge_compliance(tmp_path: Path) -> None:
    """The agent's answer text cannot affect policy; only observed events matter.

    The answer is a valid completed doc whose explanation claims Graph usage,
    but the Runner observed no Graph call -> the run is invalid. The agent-answer
    schema also forbids tool/policy fields, so there is no self-report to honour.
    """
    doc = fx.completed_answer_doc()
    doc["answer"]["explanation"] = "I used the Graph tool extensively to trace the call chain."
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    agent = FakeAgent(_outcome(raw, (_event(ToolKind.FILE_READ),)))  # no Graph observed
    result = _run(tmp_path, agent, run_id="forge")
    assert result.status is br.RunStatus.INVALID
    assert not result.policy_valid


def test_failed_run_has_no_judge_placeholder_files(tmp_path: Path) -> None:
    agent = FakeAgent(raises=RuntimeError("boom"))
    _run(tmp_path, agent, run_id="no-placeholder")
    run_dir = tmp_path / "no-placeholder"
    names = {p.name for p in run_dir.iterdir()}
    # Only the guard's run-input record + metadata + policy + manifest are
    # produced for a failed run (run-input.json is guard state, not a manifest
    # artifact). No raw_response/agent_answer/judge placeholder files.
    assert names == {RUN_INPUT, METADATA, POLICY, MANIFEST}
    assert RAW not in names
    assert ANSWER not in names


# --------------------------------------------------------------------------- #
# Interrupt/restart and no-silent-overwrite
# --------------------------------------------------------------------------- #


def test_half_done_run_is_rerun_not_treated_as_success(tmp_path: Path) -> None:
    """An interrupted run (metadata written, no manifest) is re-executed.

    Simulates a crash after run-metadata was written but before the manifest
    (the atomic completion marker): the restart re-executes and completes the
    run rather than treating the half-done artifacts as a success.
    """
    run_dir = tmp_path / "halfdone"
    run_dir.mkdir(parents=True)
    # Write a partial run-metadata (no manifest) from a previous interrupted run.
    partial_metadata = {
        "schema_version": "run-metadata-v1",
        "agent": "claude-code",
        "agent_model": "glm-5.2",
        "tool_policy": "graph",
        "policy_enforced": True,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:01Z",
        "metrics": {k: 0 for k in (
            "tool_call_count", "files_read_count", "graph_query_count",
            "search_query_count", "elapsed_ms", "input_tokens", "output_tokens",
        )},
    }
    (run_dir / METADATA).write_text(json.dumps(partial_metadata), encoding="utf-8")

    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    result = br.execute_run(
        runs_root=tmp_path, run_id="halfdone", identity=_identity("graph"), agent=agent
    )
    # The run was re-executed to a successful awaiting-judge state, not treated
    # as a success from the half-done artifacts.
    assert result.status is br.RunStatus.AWAITING_JUDGE
    assert agent.call_count == 1
    assert (run_dir / MANIFEST).exists()


def test_complete_identical_run_is_idempotent(tmp_path: Path) -> None:
    """A complete identical run is returned without re-executing the agent."""
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    first = _run(tmp_path, agent, run_id="idem")
    assert agent.call_count == 1
    # Re-run with the identical input: idempotent, agent not called again.
    second = _run(tmp_path, agent, run_id="idem")
    assert agent.call_count == 1
    assert second.status is first.status
    assert second.started_at == first.started_at


def test_same_run_id_different_input_rejected(tmp_path: Path) -> None:
    """Reusing a run id with a different tool_policy is rejected (no overwrite)."""
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="conflict", tool_policy="graph")
    # Reuse the same run id with a different tool_policy.
    grep_agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.SEARCH),)))
    with pytest.raises(br.RunConflictError, match="different input"):
        br.execute_run(
            runs_root=tmp_path,
            run_id="conflict",
            identity=_identity("grep"),
            agent=grep_agent,
        )
    # The grep agent was never executed and the original artifacts are intact.
    assert grep_agent.call_count == 0
    metadata = json.loads((tmp_path / "conflict" / METADATA).read_text(encoding="utf-8"))
    assert metadata["tool_policy"] == "graph"


def test_load_run_result_reconstructs_status(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="reload")
    reloaded = br.load_run_result(runs_root=tmp_path, run_id="reload")
    assert reloaded.status is br.RunStatus.AWAITING_JUDGE
    assert reloaded.agent_answer_status is AgentAnswerStatus.COMPLETED
    assert reloaded.policy_valid


def test_load_run_result_reconstructs_failed_status(tmp_path: Path) -> None:
    agent = FakeAgent(raises=RuntimeError("boom"))
    _run(tmp_path, agent, run_id="reload-failed")
    reloaded = br.load_run_result(runs_root=tmp_path, run_id="reload-failed")
    assert reloaded.status is br.RunStatus.FAILED
    assert reloaded.agent_answer_status is None


# --------------------------------------------------------------------------- #
# AIS009-R1: rejectable policy inputs fail before artifact writes
# --------------------------------------------------------------------------- #


def test_policy_validation_error_leaves_no_artifacts_and_is_truthful(tmp_path: Path) -> None:
    """A rejectable tool-event source fails BEFORE raw-response/answer/metadata.

    The agent returned an outcome with an untrusted tool-event source; the
    Runner validates rejectable inputs before any artifact write, so the failed
    run's terminal persistence is truthful (absent raw_response/agent_answer and
    zero metrics reflect reality - nothing was written) and execute_run agrees
    with load_run_result on FAILED (AIS009-R1).
    """
    forged = ToolEvent(kind=ToolKind.GRAPH, source="agent:self_report", label="graph")
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (forged,)))
    result = _run(tmp_path, agent, run_id="r1-source")
    assert result.status is br.RunStatus.FAILED
    run_dir = tmp_path / "r1-source"
    # No raw-response/agent-answer written: pre-validation failed first.
    assert not (run_dir / RAW).exists()
    assert not (run_dir / ANSWER).exists()
    # Metadata metrics are zero (no execution artifacts were produced/recorded).
    metadata = json.loads((run_dir / METADATA).read_text(encoding="utf-8"))
    assert metadata["metrics"]["tool_call_count"] == 0
    assert metadata["metrics"]["input_tokens"] == 0
    # The policy-result records the cause (audit behavior preserved).
    policy_doc = json.loads((run_dir / POLICY).read_text(encoding="utf-8"))
    assert policy_doc["valid"] is False
    assert [v["code"] for v in policy_doc["violations"]] == ["execution_failed"]
    assert "policy_validation_error" in policy_doc["observations"][0]
    # Manifest truthfully marks raw_response/agent_answer absent.
    manifest = json.loads((run_dir / MANIFEST).read_text(encoding="utf-8"))
    by_name = {a["name"]: a for a in manifest["artifacts"]}
    assert by_name["raw_response"]["status"] == "absent"
    assert by_name["agent_answer"]["status"] == "absent"
    # execute_run and load_run_result agree on FAILED (was INVALID before R1).
    assert br.load_run_result(runs_root=tmp_path, run_id="r1-source").status is br.RunStatus.FAILED


def test_unknown_tool_policy_fails_before_artifact_writes(tmp_path: Path) -> None:
    """An unknown tool_policy is rejected before artifact writes (AIS009-R1)."""
    identity = br.RunIdentity(
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        tool_policy="bogus",
        agent="claude-code",
        agent_model="glm-5.2",
    )
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    result = br.execute_run(
        runs_root=tmp_path, run_id="r1-policy", identity=identity, agent=agent
    )
    assert result.status is br.RunStatus.FAILED
    run_dir = tmp_path / "r1-policy"
    assert not (run_dir / RAW).exists()
    assert not (run_dir / ANSWER).exists()
    # execute_run and load_run_result agree on FAILED.
    assert br.load_run_result(runs_root=tmp_path, run_id="r1-policy").status is br.RunStatus.FAILED


# --------------------------------------------------------------------------- #
# AIS009-R2: immutable input identity checked for every terminal run
# --------------------------------------------------------------------------- #


def _identity_with(
    *,
    case_id: str = fx.CASE_ID,
    task_type: str = fx.TASK_TYPE,
    tool_policy: str = "graph",
) -> br.RunIdentity:
    return br.RunIdentity(
        case_id=case_id,
        task_type=task_type,
        tool_policy=tool_policy,
        agent="claude-code",
        agent_model="glm-5.2",
    )


def test_run_input_records_full_identity(tmp_path: Path) -> None:
    """run-input.json records the full immutable identity (AIS009-R2)."""
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="r2-input")
    recorded = json.loads((tmp_path / "r2-input" / RUN_INPUT).read_text(encoding="utf-8"))
    assert recorded == {
        "schema_version": "run-input-v1",
        "case_id": fx.CASE_ID,
        "task_type": fx.TASK_TYPE,
        "tool_policy": "graph",
        "agent": "claude-code",
        "agent_model": "glm-5.2",
    }


def test_failed_run_different_case_id_rejected(tmp_path: Path) -> None:
    """A failed run's case_id is checked on reuse even with no agent-answer (R2)."""
    _run(tmp_path, FakeAgent(raises=RuntimeError("boom")), run_id="r2-failed")
    # Reuse the same run id with a different case_id; the run has no
    # agent-answer, but run-input.json still lets the guard detect the conflict.
    with pytest.raises(br.RunConflictError, match="different input"):
        br.execute_run(
            runs_root=tmp_path,
            run_id="r2-failed",
            identity=_identity_with(case_id="different-case"),
            agent=FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),))),
        )


def test_failed_run_different_task_type_rejected(tmp_path: Path) -> None:
    """A failed run's task_type is checked on reuse (AIS009-R2)."""
    _run(tmp_path, FakeAgent(raises=RuntimeError("boom")), run_id="r2-task")
    with pytest.raises(br.RunConflictError, match="different input"):
        br.execute_run(
            runs_root=tmp_path,
            run_id="r2-task",
            identity=_identity_with(task_type="impact_analysis"),
            agent=FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),))),
        )


def test_failed_run_same_input_is_idempotent(tmp_path: Path) -> None:
    """A failed run reused with the identical input is returned idempotently (R2)."""
    agent = FakeAgent(raises=RuntimeError("boom"))
    first = _run(tmp_path, agent, run_id="r2-idem")
    assert first.status is br.RunStatus.FAILED
    assert agent.call_count == 1
    second = _run(tmp_path, agent, run_id="r2-idem")
    assert second.status is br.RunStatus.FAILED
    # The agent was not re-executed (idempotent, not a stale re-run).
    assert agent.call_count == 1


def test_complete_run_different_case_id_rejected(tmp_path: Path) -> None:
    """A complete run reused with a different case_id is rejected via the sidecar (R2)."""
    _run(
        tmp_path,
        FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),))),
        run_id="r2-complete",
    )
    with pytest.raises(br.RunConflictError, match="different input"):
        br.execute_run(
            runs_root=tmp_path,
            run_id="r2-complete",
            identity=_identity_with(case_id="different-case"),
            agent=FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),))),
        )


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_invalid_run_id_rejected(tmp_path: Path) -> None:
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    for bad in ("", "a/b", "a\\b", ".", ".."):
        with pytest.raises(br.RunnerError):
            br.execute_run(
                runs_root=tmp_path, run_id=bad, identity=_identity(), agent=agent
            )


# --------------------------------------------------------------------------- #
# AIS009-N1: policy_enforced is mandatory (always enforced, always true)
# --------------------------------------------------------------------------- #


def test_policy_enforced_false_rejected(tmp_path: Path) -> None:
    """policy_enforced=False is rejected: enforcement is mandatory (AIS009-N1)."""
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    with pytest.raises(br.RunnerError, match="policy_enforced"):
        br.execute_run(
            runs_root=tmp_path,
            run_id="n1",
            identity=_identity(),
            agent=agent,
            policy_enforced=False,
        )
    # Rejected before any run directory is created.
    assert not (tmp_path / "n1").exists()


def test_policy_enforced_always_true_in_metadata(tmp_path: Path) -> None:
    """A persisted run's metadata always records policy_enforced=true (AIS009-N1)."""
    agent = FakeAgent(_outcome(fx.completed_answer_bytes(), (_event(ToolKind.GRAPH),)))
    _run(tmp_path, agent, run_id="n1-true")
    metadata = json.loads((tmp_path / "n1-true" / METADATA).read_text(encoding="utf-8"))
    assert metadata["policy_enforced"] is True


def test_failed_run_metadata_policy_enforced_true(tmp_path: Path) -> None:
    """Even a failed run records policy_enforced=true (AIS009-N1)."""
    _run(tmp_path, FakeAgent(raises=RuntimeError("boom")), run_id="n1-failed")
    metadata = json.loads((tmp_path / "n1-failed" / METADATA).read_text(encoding="utf-8"))
    assert metadata["policy_enforced"] is True


def test_cli_main_remains_callable() -> None:
    """The module entry still exposes a callable main (smoke contract)."""
    assert br.main([]) == 0
