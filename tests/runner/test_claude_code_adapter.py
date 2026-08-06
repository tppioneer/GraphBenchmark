"""Tests for runner.claude_code_adapter (AIS-012).

Covers the acceptance criteria using a mocked subprocess (never the real Claude
service):

* argv propagation of prompt, model, cwd, permission mode, --mcp-config,
  --strict-mcp-config, and the explicit skill/plugin mechanism;
* Graph vs Grep MCP isolation and invalid-config fail-closed;
* parsing final text, tool events and usage from representative stream-json;
* configurable classification and Runner source stamping;
* missing usage, malformed stream, empty output, nonzero exit, timeout and
  unsafe path/error handling;
* command redaction (prompt/secret content hidden in audit);
* existing Runner compatibility via a focused execute_run integration test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from runner import benchmark_runner as br
from runner.claude_code_adapter import (
    AgentAdapterError,
    AgentLaunchError,
    AgentNonZeroExitError,
    AgentOutputError,
    AgentPolicyConfigError,
    AgentTimeoutError,
    ClaudeCodeAgentAdapter,
    ToolNamePatterns,
)
from runner.execution import AgentAnswerStatus
from runner.policy_validation import RUNNER_OBSERVED_SOURCE, ToolKind

from . import fixtures as fx

PROMPT = "Locate the bug in the corrupt inbox recovery path."
MODEL = "glm-5.2"
CASE_ID = fx.CASE_ID
TASK_TYPE = fx.TASK_TYPE

# Short alias for the subprocess.run patch target.
_RUN = "runner.claude_code_adapter.subprocess.run"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _mcp_config(tmp_path: Path, name: str = "gitnexus.json") -> Path:
    """Create a minimal MCP config JSON file and return its path."""
    p = tmp_path / name
    p.write_text(json.dumps({"mcpServers": {"gitnexus": {}}}), encoding="utf-8")
    return p


def _completed(
    stdout: str, stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[bytes]:
    """Build a CompletedProcess with UTF-8 encoded bytes output.

    The adapter captures raw bytes from subprocess.run and decodes them as
    UTF-8, so the mock must return bytes to match the real subprocess contract.
    """
    return subprocess.CompletedProcess(
        args=["claude"],
        returncode=returncode,
        stdout=stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"),
    )


def _assistant_record(
    text: str | None = None,
    tool_uses: list[dict[str, Any]] | None = None,
) -> str:
    """Build a stream-json assistant record line."""
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({
            "type": "tool_use",
            "id": tu.get("id", "toolu_1"),
            "name": tu["name"],
            "input": tu.get("input", {}),
        })
    return json.dumps({"type": "assistant", "message": {"content": content}})


def _result_record(
    result: str = "",
    input_tokens: int | None = 1000,
    output_tokens: int | None = 200,
) -> str:
    """Build a stream-json result record line."""
    usage: dict[str, Any] = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    return json.dumps({"type": "result", "subtype": "success", "result": result, "usage": usage})


def _stream(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _make_adapter(
    tmp_path: Path,
    *,
    prompt: str = PROMPT,
    case_id: str = CASE_ID,
    task_type: str = TASK_TYPE,
    agent_model: str = MODEL,
    cli_path: str = "claude",
    timeout_seconds: float = 30.0,
    **kwargs: Any,
) -> ClaudeCodeAgentAdapter:
    """Build an adapter with safe defaults for testing."""
    return ClaudeCodeAgentAdapter(
        prompt=prompt,
        case_id=case_id,
        task_type=task_type,
        agent_model=agent_model,
        repo_cwd=tmp_path,
        cli_path=cli_path,
        timeout_seconds=timeout_seconds,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. argv propagation
# --------------------------------------------------------------------------- #


def test_argv_has_required_non_interactive_flags(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--print" in argv
    assert "--output-format" in argv
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == MODEL
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_argv_always_has_strict_mcp_config(tmp_path: Path) -> None:
    """--strict-mcp-config is always present so global MCPs are never loaded."""
    # Graph policy with graph configs:
    gcfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--strict-mcp-config" in argv
    # Grep policy with no graph configs/patterns:
    grep_adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=()))
    argv = grep_adapter.build_command(tool_policy="grep", prompt=PROMPT)
    assert "--strict-mcp-config" in argv


def test_argv_mcp_config_for_graph_policy(tmp_path: Path) -> None:
    cfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(cfg,))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--mcp-config" in argv
    idx = argv.index("--mcp-config")
    assert str(cfg) in argv[idx + 1 : idx + 2]


def test_argv_no_mcp_config_for_grep_policy(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=()))
    argv = adapter.build_command(tool_policy="grep", prompt=PROMPT)
    assert "--mcp-config" not in argv


def test_prompt_after_double_dash_not_consumed_by_mcp_config(tmp_path: Path) -> None:
    """The prompt is after -- so --mcp-config's variadic does not eat it."""
    cfg = _mcp_config(tmp_path, "g.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(cfg,))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--" in argv
    dash_idx = argv.index("--")
    # The prompt is the element right after --.
    assert argv[dash_idx + 1] == PROMPT
    # --mcp-config value(s) are before --.
    mcp_idx = argv.index("--mcp-config")
    assert mcp_idx < dash_idx


def test_argv_propagates_custom_permission_mode(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, permission_mode="acceptEdits")
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_argv_propagates_custom_model(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, agent_model="claude-sonnet-4")
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4"


def test_argv_has_skill_text_via_append_system_prompt(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, skill_text="Always cite evidence.")
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--append-system-prompt" in argv
    idx = argv.index("--append-system-prompt")
    assert argv[idx + 1] == "Always cite evidence."


def test_argv_has_plugin_dirs(tmp_path: Path) -> None:
    pdir = tmp_path / "myplugin"
    pdir.mkdir()
    adapter = _make_adapter(tmp_path, plugin_dirs=(pdir,))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--plugin-dir" in argv
    idx = argv.index("--plugin-dir")
    assert argv[idx + 1] == str(pdir)


def test_argv_has_disable_slash_commands_by_default(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--disable-slash-commands" in argv


def test_argv_can_disable_disable_slash_commands(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, disable_slash_commands=False)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--disable-slash-commands" not in argv


def test_skill_file_read_at_init(tmp_path: Path) -> None:
    skill = tmp_path / "skill.md"
    skill.write_text("Skill instructions here.", encoding="utf-8")
    adapter = _make_adapter(tmp_path, skill_file=skill)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    idx = argv.index("--append-system-prompt")
    assert argv[idx + 1] == "Skill instructions here."


def test_prompt_loader_called_with_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def loader(case_id: str, task_type: str) -> str:
        calls.append((case_id, task_type))
        return "Loaded prompt."

    adapter = ClaudeCodeAgentAdapter(
        prompt_loader=loader,
        case_id=CASE_ID,
        task_type=TASK_TYPE,
        agent_model=MODEL,
        repo_cwd=tmp_path,
        cli_path="claude",
        timeout_seconds=30.0,
        tool_name_patterns=ToolNamePatterns(graph=()),
    )
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="grep")
    assert calls == [(CASE_ID, TASK_TYPE)]


def test_cwd_passed_to_subprocess(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)) as mock_run:
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)


# --------------------------------------------------------------------------- #
# 2. Graph vs Grep MCP isolation and fail-closed
# --------------------------------------------------------------------------- #


def test_graph_policy_gets_only_graph_configs(tmp_path: Path) -> None:
    gcfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    configs = adapter._select_mcp_configs("graph")
    assert configs == (str(gcfg),)


def test_grep_policy_fail_closed_with_graph_mcp_configs(tmp_path: Path) -> None:
    gcfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    with pytest.raises(AgentPolicyConfigError, match="Grep policy.*Graph MCP"):
        adapter._select_mcp_configs("grep")


def test_grep_policy_fail_closed_with_graph_patterns(tmp_path: Path) -> None:
    """Grep with Graph tool-name patterns configured is also fail-closed."""
    adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=(r"^mcp__graph",)))
    with pytest.raises(AgentPolicyConfigError, match="Grep policy.*Graph tool-name"):
        adapter._select_mcp_configs("grep")


def test_grep_policy_fail_closed_with_skill(tmp_path: Path) -> None:
    """Grep with a configured skill fails closed: no skill injection (F2)."""
    adapter = _make_adapter(
        tmp_path,
        skill_text="Graph skill text.",
        tool_name_patterns=ToolNamePatterns(graph=()),
    )
    with pytest.raises(AgentPolicyConfigError, match="Grep policy.*skill"):
        adapter.build_command(tool_policy="grep", prompt=PROMPT)


def test_grep_execute_with_skill_fail_closed_no_launch(tmp_path: Path) -> None:
    """Grep + skill raises before any subprocess launch (F2)."""
    adapter = _make_adapter(
        tmp_path,
        skill_text="Graph skill text.",
        tool_name_patterns=ToolNamePatterns(graph=()),
    )
    with patch(_RUN) as mock_run:
        with pytest.raises(AgentPolicyConfigError, match="skill"):
            adapter.execute(
                case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="grep"
            )
    mock_run.assert_not_called()


def test_grep_policy_no_skill_has_no_append_system_prompt(
    tmp_path: Path,
) -> None:
    """A Grep run with no skill has no --append-system-prompt in argv (F2)."""
    adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=()))
    argv = adapter.build_command(tool_policy="grep", prompt=PROMPT)
    assert "--append-system-prompt" not in argv


def test_grep_policy_succeeds_with_no_graph_config(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=()))
    configs = adapter._select_mcp_configs("grep")
    assert configs == ()


def test_grep_policy_uses_grep_mcp_configs(tmp_path: Path) -> None:
    gcfg = _mcp_config(tmp_path, "grep_tools.json")
    adapter = _make_adapter(
        tmp_path,
        grep_mcp_configs=(gcfg,),
        tool_name_patterns=ToolNamePatterns(graph=()),
    )
    configs = adapter._select_mcp_configs("grep")
    assert configs == (str(gcfg),)


def test_mixed_policy_gets_both_configs(tmp_path: Path) -> None:
    gcfg = _mcp_config(tmp_path, "graph.json")
    gpcfg = _mcp_config(tmp_path, "grep_tools.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,), grep_mcp_configs=(gpcfg,))
    configs = adapter._select_mcp_configs("mixed")
    assert configs == (str(gcfg), str(gpcfg))


def test_unknown_policy_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentPolicyConfigError, match="unknown tool_policy"):
        adapter._select_mcp_configs("bogus")


def test_last_command_auditable_with_mcp_configs(tmp_path: Path) -> None:
    """The Graph/Grep separation is visible in the redacted command after execute."""
    gcfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    # The redacted command preserves MCP config paths (non-secret) and shows
    # --strict-mcp-config + --mcp-config, but the prompt is redacted.
    cmd = adapter.last_command
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd
    assert str(gcfg) in cmd
    assert PROMPT not in cmd  # prompt is redacted
    assert adapter.last_tool_policy == "graph"
    assert adapter.last_mcp_configs == (str(gcfg),)


def test_grep_execute_fail_closed_raises_and_does_not_launch(tmp_path: Path) -> None:
    """Grep policy with graph configs raises before any subprocess launch."""
    gcfg = _mcp_config(tmp_path, "graph.json")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    with patch(_RUN) as mock_run:
        with pytest.raises(AgentPolicyConfigError):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="grep")
    mock_run.assert_not_called()


# --------------------------------------------------------------------------- #
# 3. Parsing final text, tool events and usage
# --------------------------------------------------------------------------- #


def test_parse_final_text_from_result_record(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(text="Let me check."),
        _result_record(result="The root cause is _load_events."),
    ])
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"The root cause is _load_events."


def test_parse_tool_events_from_assistant_records(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(text="Investigating.", tool_uses=[{"name": "mcp__gitnexus__query"}]),
        _assistant_record(tool_uses=[{"name": "Read"}, {"name": "Grep"}]),
        _result_record(result="Done."),
    ])
    parsed = adapter._parse_stream(stream)
    kinds = [e.kind for e in parsed.tool_events]
    labels = [e.label for e in parsed.tool_events]
    assert kinds == [ToolKind.GRAPH, ToolKind.FILE_READ, ToolKind.SEARCH]
    assert labels == ["mcp__gitnexus__query", "Read", "Grep"]


def test_parse_usage_from_result_record(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_result_record(result="answer", input_tokens=5000, output_tokens=800)])
    parsed = adapter._parse_stream(stream)
    assert parsed.input_tokens == 5000
    assert parsed.output_tokens == 800


def test_parse_fallback_to_assistant_text_without_result(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_assistant_record(text="Fallback answer.")])
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"Fallback answer."
    assert parsed.result_found is False


def test_parse_multiple_tool_use_in_one_message(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(tool_uses=[
            {"name": "mcp__gitnexus__query"},
            {"name": "mcp__gitnexus__context"},
            {"name": "Glob"},
        ]),
        _result_record(result="done"),
    ])
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 3
    assert parsed.tool_events[0].kind is ToolKind.GRAPH
    assert parsed.tool_events[1].kind is ToolKind.GRAPH
    assert parsed.tool_events[2].kind is ToolKind.SEARCH


def test_execute_returns_agent_run_outcome(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(tool_uses=[{"name": "mcp__gitnexus__query"}]),
        _result_record(result="The bug is in _load_events.", input_tokens=300, output_tokens=50),
    ])
    with patch(_RUN, return_value=_completed(stream)):
        outcome = adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert outcome.raw_response == b"The bug is in _load_events."
    assert len(outcome.tool_events) == 1
    assert outcome.tool_events[0].kind is ToolKind.GRAPH
    assert outcome.input_tokens == 300
    assert outcome.output_tokens == 50


# --------------------------------------------------------------------------- #
# 4. Configurable classification and Runner source stamping
# --------------------------------------------------------------------------- #


def test_default_classification_patterns(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    assert adapter._classify_tool_name("mcp__gitnexus__query") is ToolKind.GRAPH
    assert adapter._classify_tool_name("mcp__gitnexus__context") is ToolKind.GRAPH
    assert adapter._classify_tool_name("Grep") is ToolKind.SEARCH
    assert adapter._classify_tool_name("Glob") is ToolKind.SEARCH
    assert adapter._classify_tool_name("Read") is ToolKind.FILE_READ
    assert adapter._classify_tool_name("Bash") is ToolKind.OTHER
    assert adapter._classify_tool_name("Write") is ToolKind.OTHER
    assert adapter._classify_tool_name("WebFetch") is ToolKind.OTHER


def test_custom_classification_patterns(tmp_path: Path) -> None:
    patterns = ToolNamePatterns(
        graph=(r"^custom_graph_tool$",),
        search=(r"^my_search$",),
        file_read=(r"^cat$",),
    )
    adapter = _make_adapter(tmp_path, tool_name_patterns=patterns)
    assert adapter._classify_tool_name("custom_graph_tool") is ToolKind.GRAPH
    assert adapter._classify_tool_name("my_search") is ToolKind.SEARCH
    assert adapter._classify_tool_name("cat") is ToolKind.FILE_READ
    # Default patterns no longer apply.
    assert adapter._classify_tool_name("mcp__gitnexus__query") is ToolKind.OTHER
    assert adapter._classify_tool_name("Read") is ToolKind.OTHER


def test_all_tool_events_stamped_with_runner_source(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(tool_uses=[
            {"name": "mcp__gitnexus__query"},
            {"name": "Read"},
            {"name": "Bash"},
        ]),
        _result_record(result="done"),
    ])
    parsed = adapter._parse_stream(stream)
    for event in parsed.tool_events:
        assert event.source == RUNNER_OBSERVED_SOURCE


def test_tool_event_label_is_tool_name(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(tool_uses=[{"name": "mcp__gitnexus__query"}]),
        _result_record(result="done"),
    ])
    parsed = adapter._parse_stream(stream)
    assert parsed.tool_events[0].label == "mcp__gitnexus__query"


# --------------------------------------------------------------------------- #
# 5. Missing usage, malformed stream, empty output, nonzero exit, timeout
# --------------------------------------------------------------------------- #


def test_missing_usage_defaults_to_zero(tmp_path: Path) -> None:
    """When usage is absent, tokens default to 0 (documented unavailable policy)."""
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(text="answer"),
        json.dumps({"type": "result", "subtype": "success", "result": "answer"}),
    ])
    parsed = adapter._parse_stream(stream)
    assert parsed.input_tokens == 0
    assert parsed.output_tokens == 0


def test_malformed_lines_skipped_valid_parsed(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = (
        "not json at all\n"
        + _assistant_record(text="answer")
        + "\n"
        + _result_record(result="answer")
        + "\n{bad"
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"answer"


def test_all_malformed_raises_output_error(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentOutputError, match="no valid JSON records"):
        adapter._parse_stream("not json\neither not json\n")


def test_non_success_result_subtype_raises_output_error(tmp_path: Path) -> None:
    """A result record with a non-success subtype raises AgentOutputError.

    The CLI may exit 0 but still report a non-success subtype (e.g.
    error_max_turns); the adapter must surface this as a failed run rather
    than treating the partial result as a completed answer.
    """
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(text="partial"),
        json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "result": "partial",
            "usage": {},
        }),
    ])
    with patch(_RUN, return_value=_completed(stream)):
        with pytest.raises(AgentOutputError, match="error_max_turns"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_empty_stdout_raises_output_error(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with patch(_RUN, return_value=_completed("")):
        with pytest.raises(AgentOutputError, match="empty stdout"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_nonzero_exit_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with patch(_RUN, return_value=_completed("", "error", returncode=1)):
        with pytest.raises(AgentNonZeroExitError, match="exited with code 1"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_timeout_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, timeout_seconds=0.01)
    with patch(
        _RUN,
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=0.01),
    ):
        with pytest.raises(AgentTimeoutError, match="timed out"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_launch_failure_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, cli_path="/nonexistent/claude")
    with patch(_RUN, side_effect=FileNotFoundError("not found")):
        with pytest.raises(AgentLaunchError, match="cannot launch"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_plain_oserror_converted_to_launch_error(tmp_path: Path) -> None:
    """A plain OSError (e.g. WinError 193 from an extensionless shim) is
    converted to AgentLaunchError, not left as an uncaught exception."""
    adapter = _make_adapter(tmp_path, cli_path="/bad/shim/claude")
    with patch(_RUN, side_effect=OSError("[WinError 193] not a valid Win32 application")):
        with pytest.raises(AgentLaunchError, match="cannot launch"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_invalid_repo_cwd_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="repo_cwd is not a directory"):
        ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path / "nonexistent_dir",
            cli_path="claude",
        )


def test_invalid_graph_mcp_config_path_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="graph MCP config not found"):
        ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            graph_mcp_configs=(tmp_path / "missing.json",),
            cli_path="claude",
        )


def test_invalid_grep_mcp_config_path_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="grep MCP config not found"):
        ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            grep_mcp_configs=(tmp_path / "missing_grep.json",),
            cli_path="claude",
        )


def test_invalid_plugin_dir_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="plugin_dir not found"):
        ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            plugin_dirs=(tmp_path / "missing_plugin",),
            cli_path="claude",
        )


def test_invalid_skill_file_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="skill_file not found"):
        ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            skill_file=tmp_path / "missing.md",
            cli_path="claude",
        )


def test_no_prompt_source_raises() -> None:
    with pytest.raises(AgentAdapterError, match="either prompt or prompt_loader"):
        ClaudeCodeAgentAdapter(case_id="x", task_type="y", cli_path="claude")


def test_both_prompt_sources_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="mutually exclusive"):
        ClaudeCodeAgentAdapter(
            prompt="p",
            prompt_loader=lambda c, t: "p",
            case_id="x",
            task_type="y",
            repo_cwd=tmp_path,
            cli_path="claude",
        )


def test_identity_mismatch_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, case_id="case-A")
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        with pytest.raises(AgentAdapterError, match="case_id mismatch"):
            adapter.execute(case_id="case-B", task_type=TASK_TYPE, tool_policy="graph")


def test_nonzero_exit_error_message_redacts_secrets(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    secret_stderr = "Error: api_key=sk-secret123 in config"
    with patch(_RUN, return_value=_completed("", secret_stderr, returncode=2)):
        with pytest.raises(AgentNonZeroExitError) as exc_info:
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert "sk-secret123" not in str(exc_info.value)
    assert "<REDACTED>" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 6. Command redaction
# --------------------------------------------------------------------------- #


def test_last_command_redacts_prompt(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    cmd_str = " ".join(adapter.last_command)
    assert PROMPT not in cmd_str
    assert "<prompt:" in cmd_str


def test_last_command_redacts_skill_text(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, skill_text="Secret skill content.")
    stream = _stream([_result_record("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    cmd_str = " ".join(adapter.last_command)
    assert "Secret skill content." not in cmd_str
    assert "<redacted:" in cmd_str


def test_argv_uses_subprocess_argv_not_shell(tmp_path: Path) -> None:
    """build_command returns a list, never a shell string (no injection)."""
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt="hello; rm -rf /")
    assert isinstance(argv, list)
    # The dangerous prompt is a single element, not split into shell tokens.
    assert "hello; rm -rf /" in argv


# --------------------------------------------------------------------------- #
# 7. Runner compatibility (execute_run integration)
# --------------------------------------------------------------------------- #


def test_execute_run_integration_graph_compliant(tmp_path: Path) -> None:
    """The adapter plugs into execute_run and produces a compliant awaiting-judge run."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _make_adapter(tmp_path)

    # Build a stream that returns a schema-valid completed answer as the result
    # text, with a Graph tool-use event observed.
    answer_json = fx.completed_answer_bytes().decode("utf-8")
    stream = _stream([
        _assistant_record(tool_uses=[{"name": "mcp__gitnexus__query"}]),
        _result_record(result=answer_json, input_tokens=500, output_tokens=100),
    ])

    with patch(_RUN, return_value=_completed(stream)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="adapter-run-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="graph",
                agent="claude-code",
                agent_model=MODEL,
            ),
            agent=adapter,
        )

    assert result.status is br.RunStatus.AWAITING_JUDGE
    assert result.policy_valid
    assert result.agent_answer_status is AgentAnswerStatus.COMPLETED
    assert result.metrics["graph_query_count"] == 1
    assert result.metrics["tool_call_count"] == 1
    assert result.metrics["input_tokens"] == 500
    assert result.metrics["output_tokens"] == 100


def test_execute_run_integration_grep_compliant(tmp_path: Path) -> None:
    """A grep-policy run with only search tools is compliant."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _make_adapter(tmp_path, tool_name_patterns=ToolNamePatterns(graph=()))

    answer_json = fx.completed_answer_bytes().decode("utf-8")
    stream = _stream([
        _assistant_record(tool_uses=[{"name": "Grep"}, {"name": "Read"}]),
        _result_record(result=answer_json, input_tokens=200, output_tokens=50),
    ])

    with patch(_RUN, return_value=_completed(stream)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="adapter-grep-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="grep",
                agent="claude-code",
                agent_model=MODEL,
            ),
            agent=adapter,
        )

    assert result.status is br.RunStatus.AWAITING_JUDGE
    assert result.policy_valid
    assert result.metrics["search_query_count"] == 1
    assert result.metrics["files_read_count"] == 1
    assert result.metrics["graph_query_count"] == 0


def test_execute_run_integration_failure_is_failed(tmp_path: Path) -> None:
    """A nonzero CLI exit surfaces as a failed run through execute_run."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _make_adapter(tmp_path)

    with patch(_RUN, return_value=_completed("", "error", returncode=1)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="adapter-fail-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="graph",
                agent="claude-code",
                agent_model=MODEL,
            ),
            agent=adapter,
        )

    assert result.status is br.RunStatus.FAILED
    assert result.agent_answer_status is None
    assert not result.policy_valid


# --------------------------------------------------------------------------- #
# 8. CLI discovery and UTF-8 encoding
# --------------------------------------------------------------------------- #


def test_default_find_claude_uses_shutil_which(tmp_path: Path) -> None:
    """Default CLI discovery uses shutil.which (PATHEXT-aware), not `where`.

    shutil.which respects PATHEXT on Windows and returns a real executable
    (e.g. claude.cmd) rather than the extensionless npm shim that `where`
    would surface first and that fails with WinError 193.
    """
    with patch(
        "runner.claude_code_adapter.shutil.which",
        return_value="/usr/local/bin/claude",
    ):
        adapter = ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
        )
    assert adapter._cli_path == "/usr/local/bin/claude"


def test_default_find_claude_fallback_when_not_found(tmp_path: Path) -> None:
    """When shutil.which returns None, fall back to 'claude'.

    The fallback lets construction succeed; the missing executable surfaces
    as an AgentLaunchError at run time (via OSError -> AgentLaunchError).
    """
    with patch("runner.claude_code_adapter.shutil.which", return_value=None):
        adapter = ClaudeCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
        )
    assert adapter._cli_path == "claude"


def test_chinese_result_text_decoded_as_utf8(tmp_path: Path) -> None:
    """Non-ASCII (Chinese) result text is decoded as UTF-8, not cp936.

    The mock returns UTF-8 bytes (matching the real subprocess contract where
    capture_output=True without text=True yields bytes); the adapter must
    decode them as UTF-8 so Chinese text round-trips correctly. This is a
    regression test for the text=True/cp936 UnicodeDecodeError on Windows.

    No real Claude service call is made; this verifies the adapter's decode
    path, not the real CLI's output encoding.
    """
    chinese = "根因是 _load_events 函数中的空指针解引用。"
    adapter = _make_adapter(tmp_path)
    stream = _stream([
        _assistant_record(text=chinese),
        _result_record(result=chinese, input_tokens=10, output_tokens=20),
    ])
    with patch(_RUN, return_value=_completed(stream)):
        outcome = adapter.execute(
            case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph"
        )
    assert outcome.raw_response == chinese.encode("utf-8")
    assert outcome.input_tokens == 10
    assert outcome.output_tokens == 20
