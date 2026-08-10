"""Tests for runner.opencode_adapter (AIS-013).

Covers the acceptance criteria using a mocked subprocess (never the real
OpenCode service, never a real MCP launch, never a provider call):

* argv propagation of run/--format/--model/--dir/--auto/--pure and the prompt
  positional, plus skill-text prepend and Windows shim prefix handling;
* Graph vs Grep MCP isolation, required Graph config, and fail-closed;
* runtime-config normalization of both ``mcpServers`` and ``mcp`` shapes,
  deny-by-default permissions, duplicate/invalid/secret-bearing configs (no
  leakage), and config injection via the env override (not argv);
* parsing final text (last ``text`` event wins), multiple
  text/tool events, tool classification, dedup, errored-tool skipping, token
  summation, missing usage, malformed/error streams, missing final text;
* timeout / non-zero exit / launch failure / identity mismatch / invalid paths;
* command redaction (prompt content hidden; config content never in audit);
* UTF-8 decoding and Windows npm shim discovery (cmd preference, ps1 fallback);
* Runner compatibility via focused execute_run integration tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from runner import benchmark_runner as br
from runner.execution import AgentAnswerStatus
from runner.opencode_adapter import (
    DEFAULT_AGENT_MODEL,
    OPENCODE_CONFIG_ENV,
    OPENCODE_DISABLE_PROJECT_CONFIG_ENV,
    AgentAdapterError,
    AgentLaunchError,
    AgentNonZeroExitError,
    AgentOutputError,
    AgentPolicyConfigError,
    AgentTimeoutError,
    OpenCodeAgentAdapter,
    OpenCodeToolNamePatterns,
)
from runner.policy_validation import RUNNER_OBSERVED_SOURCE, ToolKind

from . import fixtures as fx

PROMPT = "Locate the bug in the corrupt inbox recovery path."
MODEL = DEFAULT_AGENT_MODEL
CASE_ID = fx.CASE_ID
TASK_TYPE = fx.TASK_TYPE

# Short alias for the subprocess.run patch target.
_RUN = "runner.opencode_adapter.subprocess.run"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _claude_mcp(
    name: str = "gitnexus",
    command: str = "node",
    args: tuple[str, ...] = ("srv.js",),
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "stdio", "command": command, "args": list(args)}
    if env:
        spec["env"] = env
    return {"mcpServers": {name: spec}}


def _opencode_mcp(
    name: str = "gitnexus",
    command: tuple[str, ...] = ("node", "srv.js"),
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "remote" if url else "local"}
    if url:
        spec["url"] = url
    else:
        spec["command"] = list(command)
    if env:
        spec["environment"] = env
    if headers:
        spec["headers"] = headers
    return {"mcp": {name: spec}}


def _mcp_file(tmp_path: Path, name: str, doc: dict[str, Any]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _completed(
    stdout: str, stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[bytes]:
    """Build a CompletedProcess with UTF-8 encoded bytes output."""
    return subprocess.CompletedProcess(
        args=["opencode"],
        returncode=returncode,
        stdout=stdout.encode("utf-8"),
        stderr=stderr.encode("utf-8"),
    )


def _text_event(text: str, pid: str = "prt-t1") -> str:
    """Build a ``text`` event (assistant text in ``part.text``)."""
    return json.dumps({
        "type": "text",
        "timestamp": 1,
        "sessionID": "ses_test",
        "part": {"id": pid, "messageID": "msg_test", "sessionID": "ses_test",
                  "type": "text", "text": text},
    })


def _tool_use_event(
    tool: str,
    call_id: str = "call_1",
    status: str = "completed",
    error: Any = None,
) -> str:
    """Build a ``tool_use`` event (tool call in ``part.tool``/``callID``/``state``)."""
    state: dict[str, Any] = {"status": status}
    if error is not None:
        state["error"] = error
    return json.dumps({
        "type": "tool_use",
        "timestamp": 1,
        "sessionID": "ses_test",
        "part": {
            "type": "tool",
            "tool": tool,
            "callID": call_id,
            "state": state,
            "id": "prt_tool_1",
            "messageID": "msg_test",
            "sessionID": "ses_test",
        },
    })


def _step_finish_event(
    input_tokens: int | None = 100,
    output_tokens: int | None = 20,
    reason: str = "stop",
) -> str:
    """Build a ``step_finish`` event (token usage in ``part.tokens``)."""
    tokens: dict[str, Any] = {"total": 0, "reasoning": 0, "cache": {"write": 0, "read": 0}}
    if input_tokens is not None:
        tokens["input"] = input_tokens
    if output_tokens is not None:
        tokens["output"] = output_tokens
    return json.dumps({
        "type": "step_finish",
        "timestamp": 1,
        "sessionID": "ses_test",
        "part": {
            "id": "prt_sf1",
            "messageID": "msg_test",
            "sessionID": "ses_test",
            "type": "step-finish",
            "reason": reason,
            "tokens": tokens,
            "cost": 0,
        },
    })


def _step_start_event() -> str:
    """Build a ``step_start`` event (no payload of interest)."""
    return json.dumps({
        "type": "step_start",
        "timestamp": 1,
        "sessionID": "ses_test",
        "part": {
            "id": "prt_ss1",
            "messageID": "msg_test",
            "sessionID": "ses_test",
            "type": "step-start",
            "snapshot": "abc",
        },
    })


def _error_event(message: str = "boom") -> str:
    return json.dumps({"type": "error", "error": {"message": message}})


def _stream(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _make_adapter(
    tmp_path: Path,
    *,
    prompt: str = PROMPT,
    case_id: str = CASE_ID,
    task_type: str = TASK_TYPE,
    agent_model: str = MODEL,
    cli_path: str = "opencode",
    timeout_seconds: float = 30.0,
    **kwargs: Any,
) -> OpenCodeAgentAdapter:
    """Build an adapter with safe defaults for testing."""
    return OpenCodeAgentAdapter(
        prompt=prompt,
        case_id=case_id,
        task_type=task_type,
        agent_model=agent_model,
        repo_cwd=tmp_path,
        cli_path=cli_path,
        timeout_seconds=timeout_seconds,
        **kwargs,
    )


def _graph_adapter(tmp_path: Path, **kwargs: Any) -> OpenCodeAgentAdapter:
    """Adapter configured for a Graph run (with a Graph MCP config)."""
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    return _make_adapter(tmp_path, graph_mcp_configs=(gcfg,), **kwargs)


# --------------------------------------------------------------------------- #
# 1. argv propagation (model/dir/auto/pure flags, prompt positional, skill)
# --------------------------------------------------------------------------- #


def test_argv_has_required_run_format_model_dir_auto_pure(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert argv[argv.index("--format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--dir") + 1] == str(tmp_path)
    assert "--auto" in argv
    assert "--pure" in argv


def test_argv_prompt_after_double_dash(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--" in argv
    dash_idx = argv.index("--")
    assert argv[dash_idx + 1] == PROMPT


def test_argv_default_model_is_prescribed(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    assert (
        adapter.build_command(tool_policy="graph", prompt=PROMPT)[
            adapter.build_command(tool_policy="graph", prompt=PROMPT).index("--model") + 1
        ]
        == "ark-plan-lmm/deepseek-v4-flash"
    )


def test_argv_custom_model(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, agent_model="openai/gpt-5")
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[argv.index("--model") + 1] == "openai/gpt-5"


def test_argv_auto_disabled_when_configured(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, auto_approve=False)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--auto" not in argv


def test_argv_pure_disabled_when_configured(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, pure=False)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--pure" not in argv


def test_argv_dir_omitted_when_no_repo_cwd(tmp_path: Path) -> None:
    adapter = OpenCodeAgentAdapter(prompt=PROMPT, cli_path="opencode", repo_cwd=None)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--dir" not in argv


def test_argv_extra_args_propagated(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, extra_args=("--thinking",))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert "--thinking" in argv


def test_argv_skill_text_prepended_to_prompt(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, skill_text="Always cite evidence.")
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    composed = argv[argv.index("--") + 1]
    assert composed == "Always cite evidence.\n\n" + PROMPT


def test_skill_file_read_at_init(tmp_path: Path) -> None:
    skill = tmp_path / "skill.md"
    skill.write_text("Skill instructions here.", encoding="utf-8")
    adapter = _make_adapter(tmp_path, skill_file=skill)
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[argv.index("--") + 1] == "Skill instructions here.\n\n" + PROMPT


def test_argv_uses_subprocess_argv_not_shell(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    argv = adapter.build_command(tool_policy="graph", prompt="hello; rm -rf /")
    assert isinstance(argv, list)
    assert "hello; rm -rf /" in argv  # single element, not shell-split


def test_no_prompt_source_raises() -> None:
    with pytest.raises(AgentAdapterError, match="either prompt or prompt_loader"):
        OpenCodeAgentAdapter(case_id="x", task_type="y", cli_path="opencode")


def test_both_prompt_sources_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="mutually exclusive"):
        OpenCodeAgentAdapter(
            prompt="p",
            prompt_loader=lambda c, t: "p",
            case_id="x",
            task_type="y",
            repo_cwd=tmp_path,
            cli_path="opencode",
        )


def test_invalid_repo_cwd_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="repo_cwd is not a directory"):
        OpenCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path / "nonexistent_dir",
            cli_path="opencode",
        )


def test_invalid_graph_mcp_config_path_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="graph MCP config not found"):
        OpenCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            graph_mcp_configs=(tmp_path / "missing.json",),
            cli_path="opencode",
        )


def test_invalid_skill_file_raises_at_init(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="skill_file not found"):
        OpenCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            skill_file=tmp_path / "missing.md",
            cli_path="opencode",
        )


def test_empty_agent_model_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentAdapterError, match="agent_model must be a non-empty string"):
        OpenCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            repo_cwd=tmp_path,
            cli_path="opencode",
            agent_model="  ",
        )


# --------------------------------------------------------------------------- #
# 2. Graph vs Grep MCP isolation and fail-closed
# --------------------------------------------------------------------------- #


def test_graph_policy_requires_explicit_graph_config(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentPolicyConfigError, match="Graph policy requires an explicit"):
        adapter._select_mcp_configs("graph")


def test_graph_policy_gets_only_graph_configs(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    assert adapter._select_mcp_configs("graph") == (gcfg,)


def test_grep_policy_fail_closed_with_graph_mcp_configs(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    with pytest.raises(AgentPolicyConfigError, match="Grep policy.*Graph MCP"):
        adapter._select_mcp_configs("grep")


def test_grep_policy_fail_closed_with_graph_patterns(tmp_path: Path) -> None:
    adapter = _make_adapter(
        tmp_path, tool_name_patterns=OpenCodeToolNamePatterns(graph=(r"^graph_",))
    )
    with pytest.raises(AgentPolicyConfigError, match="Grep policy.*Graph tool-name"):
        adapter._select_mcp_configs("grep")


def test_grep_policy_succeeds_with_no_graph_config(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, tool_name_patterns=OpenCodeToolNamePatterns(graph=()))
    assert adapter._select_mcp_configs("grep") == ()


def test_grep_policy_uses_grep_mcp_configs(tmp_path: Path) -> None:
    gpcfg = _mcp_file(tmp_path, "grep_tools.json", _claude_mcp("ripgrep"))
    adapter = _make_adapter(
        tmp_path,
        grep_mcp_configs=(gpcfg,),
        tool_name_patterns=OpenCodeToolNamePatterns(graph=()),
    )
    assert adapter._select_mcp_configs("grep") == (gpcfg,)


def test_mixed_policy_requires_explicit_graph_config(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentPolicyConfigError, match="Mixed policy requires an explicit"):
        adapter._select_mcp_configs("mixed")


def test_mixed_policy_gets_both_configs(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    gpcfg = _mcp_file(tmp_path, "grep_tools.json", _claude_mcp("ripgrep"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,), grep_mcp_configs=(gpcfg,))
    assert adapter._select_mcp_configs("mixed") == (gcfg, gpcfg)


def test_unknown_policy_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentPolicyConfigError, match="unknown tool_policy"):
        adapter._select_mcp_configs("bogus")


def test_grep_execute_fail_closed_raises_and_does_not_launch(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    with patch(_RUN) as mock_run:
        with pytest.raises(AgentPolicyConfigError):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="grep")
    mock_run.assert_not_called()


# --------------------------------------------------------------------------- #
# 3. Runtime config normalization, deny-by-default, duplicate/invalid/secret
# --------------------------------------------------------------------------- #


def test_normalize_claude_mcp_servers_shape(tmp_path: Path) -> None:
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _claude_mcp("gitnexus", command="node", args=("a.js", "b.js"), env={"K": "v"}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    cfg = adapter.build_runtime_config("graph")
    srv = cfg["mcp"]["gitnexus"]
    assert srv["type"] == "local"
    assert srv["command"] == ["node", "a.js", "b.js"]
    assert srv["environment"] == {"K": "v"}
    assert srv["enabled"] is True


def test_normalize_opencode_mcp_shape(tmp_path: Path) -> None:
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _opencode_mcp("gitnexus", command=("node", "srv.js"), env={"K": "v"}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    cfg = adapter.build_runtime_config("graph")
    srv = cfg["mcp"]["gitnexus"]
    assert srv["type"] == "local"
    assert srv["command"] == ["node", "srv.js"]
    assert srv["environment"] == {"K": "v"}
    assert srv["enabled"] is True


def test_normalize_remote_url_server(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _opencode_mcp("gitnexus", url="https://x/sse"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    srv = adapter.build_runtime_config("graph")["mcp"]["gitnexus"]
    assert srv["type"] == "remote"
    assert srv["url"] == "https://x/sse"
    assert "command" not in srv


def test_graph_config_has_only_graph_servers_not_grep(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    gpcfg = _mcp_file(tmp_path, "grep_tools.json", _claude_mcp("ripgrep"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,), grep_mcp_configs=(gpcfg,))
    cfg = adapter.build_runtime_config("graph")
    assert set(cfg["mcp"]) == {"gitnexus"}
    cfg_mixed = adapter.build_runtime_config("mixed")
    assert set(cfg_mixed["mcp"]) == {"gitnexus", "ripgrep"}


def test_permission_deny_by_default_graph(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    perm = adapter.build_runtime_config("graph")["permission"]
    assert perm["read"] == "allow"
    for denied in ("grep", "glob", "list", "bash", "edit", "webfetch"):
        assert perm[denied] == "deny"


def test_permission_deny_by_default_grep(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path, tool_name_patterns=OpenCodeToolNamePatterns(graph=()))
    perm = adapter.build_runtime_config("grep")["permission"]
    for allowed in ("read", "grep", "glob", "list"):
        assert perm[allowed] == "allow"
    for denied in ("bash", "edit", "webfetch"):
        assert perm[denied] == "deny"


def test_custom_allowed_builtins_override(tmp_path: Path) -> None:
    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = _make_adapter(
        tmp_path,
        graph_mcp_configs=(gcfg,),
        allowed_builtins={"graph": ("read", "bash")},
    )
    perm = adapter.build_runtime_config("graph")["permission"]
    assert perm["bash"] == "allow"
    assert perm["grep"] == "deny"


def test_duplicate_server_names_raise(tmp_path: Path) -> None:
    g1 = _mcp_file(tmp_path, "g1.json", _claude_mcp("gitnexus"))
    g2 = _mcp_file(tmp_path, "g2.json", _opencode_mcp("gitnexus"))
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(g1, g2))
    with pytest.raises(AgentAdapterError, match="duplicate MCP server name 'gitnexus'"):
        adapter.build_runtime_config("graph")


def test_config_missing_mcp_keys_raises(tmp_path: Path) -> None:
    bad = _mcp_file(tmp_path, "bad.json", {"foo": {}})
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(bad,))
    with pytest.raises(AgentAdapterError, match="neither 'mcpServers' nor 'mcp'"):
        adapter.build_runtime_config("graph")


def test_config_not_object_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(p,))
    with pytest.raises(AgentAdapterError, match="must be a JSON object"):
        adapter.build_runtime_config("graph")


def test_config_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(p,))
    with pytest.raises(AgentAdapterError, match="cannot read MCP config"):
        adapter.build_runtime_config("graph")


def test_server_spec_no_command_or_url_raises(tmp_path: Path) -> None:
    bad = _mcp_file(tmp_path, "bad.json", {"mcp": {"gitnexus": {"type": "local"}}})
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(bad,))
    with pytest.raises(AgentAdapterError, match="no 'command' or 'url'"):
        adapter.build_runtime_config("graph")


def test_server_command_not_string_or_list_raises(tmp_path: Path) -> None:
    bad = _mcp_file(tmp_path, "bad.json", {"mcpServers": {"gitnexus": {"command": 5}}})
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(bad,))
    with pytest.raises(AgentAdapterError, match="command must be a string or list"):
        adapter.build_runtime_config("graph")


def test_config_injected_via_env_not_argv(tmp_path: Path) -> None:
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _claude_mcp("gitnexus", env={"API_KEY": "sk-secret-xyz"}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)) as mock_run:
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    env = mock_run.call_args.kwargs["env"]
    assert OPENCODE_CONFIG_ENV in env
    injected = json.loads(env[OPENCODE_CONFIG_ENV])
    assert "gitnexus" in injected["mcp"]
    assert injected["mcp"]["gitnexus"]["environment"]["API_KEY"] == "sk-secret-xyz"
    # The config content and the secret are NOT in the argv ...
    argv = mock_run.call_args.args[0]
    assert "sk-secret-xyz" not in argv
    assert OPENCODE_CONFIG_ENV not in argv
    # ... nor in the redacted audit command.
    assert "sk-secret-xyz" not in " ".join(adapter.last_command)
    assert OPENCODE_CONFIG_ENV not in " ".join(adapter.last_command)


def test_secret_in_mcp_config_not_leaked_in_exception(tmp_path: Path) -> None:
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _claude_mcp("gitnexus", env={"API_KEY": "sk-secret-xyz"}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    with patch(_RUN, return_value=_completed("", "boom api_key=sk-secret-xyz", returncode=2)):
        with pytest.raises(AgentNonZeroExitError) as exc_info:
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert "sk-secret-xyz" not in str(exc_info.value)
    assert "<REDACTED>" in str(exc_info.value)


def test_last_mcp_servers_audits_names_only(tmp_path: Path) -> None:
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _claude_mcp("gitnexus", env={"API_KEY": "sk-secret-xyz"}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert adapter.last_mcp_servers == ("gitnexus",)
    assert adapter.last_tool_policy == "graph"


# --------------------------------------------------------------------------- #
# 4. Parsing final text, tool events, classification, tokens
# --------------------------------------------------------------------------- #


def test_parse_final_text_last_text_event_wins(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _text_event("Let me check."),
            _text_event("The root cause is _load_events."),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"The root cause is _load_events."


def test_parse_final_text_single_text_event(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_text_event("Fallback answer.")])
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"Fallback answer."


def test_parse_multiple_text_events_last_wins(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _text_event("intermediate"),
            _text_event("final answer"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"final answer"


def test_parse_tool_events_from_tool_use(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
            _tool_use_event("read", call_id="c2"),
            _tool_use_event("grep", call_id="c3"),
            _tool_use_event("bash", call_id="c4"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    kinds = [e.kind for e in parsed.tool_events]
    labels = [e.label for e in parsed.tool_events]
    assert kinds == [ToolKind.GRAPH, ToolKind.FILE_READ, ToolKind.SEARCH, ToolKind.OTHER]
    assert labels == ["gitnexus_query", "read", "grep", "bash"]


def test_parse_tool_use_dedup_by_callID(tmp_path: Path) -> None:
    """Two tool_use events with the same callID are counted once."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="same-id"),
            _tool_use_event("gitnexus_query", call_id="same-id"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 1
    assert parsed.tool_events[0].kind is ToolKind.GRAPH


def test_parse_errored_tool_not_counted(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("bash", call_id="c1", status="error", error="boom"),
            _text_event("recovered"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.tool_events == ()
    assert parsed.raw_response == b"recovered"


def test_parse_token_summation_across_steps(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _step_finish_event(100, 20),
            _step_finish_event(250, 30),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.input_tokens == 350
    assert parsed.output_tokens == 50


def test_parse_missing_usage_defaults_to_zero(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _step_finish_event(None, None),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.input_tokens == 0
    assert parsed.output_tokens == 0


def test_parse_no_step_finish_zero_tokens(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream([_text_event("done")])
    parsed = adapter._parse_stream(stream)
    assert parsed.input_tokens == 0
    assert parsed.output_tokens == 0


def test_parse_malformed_lines_skipped(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = (
        "not json at all\n"
        + _text_event("answer")
        + "\n"
        + "{bad"
    )
    parsed = adapter._parse_stream(stream)
    assert parsed.raw_response == b"answer"


def test_parse_all_malformed_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    with pytest.raises(AgentOutputError, match="no valid JSON records"):
        adapter._parse_stream("not json\neither not json\n")


def test_parse_error_event_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_error_event("model provider unavailable")])
    with patch(_RUN, return_value=_completed(stream)):
        with pytest.raises(AgentOutputError, match="error event"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_parse_error_event_redacts_secret(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_error_event("api_key=sk-secret-xyz failed")])
    with patch(_RUN, return_value=_completed(stream)):
        with pytest.raises(AgentOutputError) as exc_info:
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert "sk-secret-xyz" not in str(exc_info.value)


def test_parse_missing_final_text_raises(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
            _step_finish_event(10, 5),
        ]
    )
    with pytest.raises(AgentOutputError, match="no final assistant text"):
        adapter._parse_stream(stream)


def test_parse_tool_only_no_text_raises(tmp_path: Path) -> None:
    """A stream with only tool_use events (no text) is missing final text."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
        ]
    )
    with pytest.raises(AgentOutputError, match="no final assistant text"):
        adapter._parse_stream(stream)


def test_parse_custom_classification_patterns(tmp_path: Path) -> None:
    patterns = OpenCodeToolNamePatterns(
        graph=(r"^custom_graph_",),
        search=(r"^mysearch$",),
        file_read=(r"^cat$",),
    )
    adapter = _make_adapter(tmp_path, tool_name_patterns=patterns)
    stream = _stream(
        [
            _tool_use_event("custom_graph_q", call_id="c1"),
            _tool_use_event("mysearch", call_id="c2"),
            _tool_use_event("cat", call_id="c3"),
            _tool_use_event("gitnexus_query", call_id="c4"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    kinds = [e.kind for e in parsed.tool_events]
    assert kinds == [ToolKind.GRAPH, ToolKind.SEARCH, ToolKind.FILE_READ, ToolKind.OTHER]


def test_default_classification_patterns(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    assert adapter._classify_tool_name("gitnexus_query") is ToolKind.GRAPH
    assert adapter._classify_tool_name("gitnexus_context") is ToolKind.GRAPH
    assert adapter._classify_tool_name("grep") is ToolKind.SEARCH
    assert adapter._classify_tool_name("glob") is ToolKind.SEARCH
    assert adapter._classify_tool_name("read") is ToolKind.FILE_READ
    assert adapter._classify_tool_name("list") is ToolKind.OTHER
    assert adapter._classify_tool_name("bash") is ToolKind.OTHER
    assert adapter._classify_tool_name("write") is ToolKind.OTHER


def test_tool_events_stamped_with_runner_source(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    for event in parsed.tool_events:
        assert event.source == RUNNER_OBSERVED_SOURCE
        assert event.label == "gitnexus_query"


def test_execute_returns_agent_run_outcome(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
            _text_event("The bug is in _load_events."),
            _step_finish_event(300, 50),
        ]
    )
    with patch(_RUN, return_value=_completed(stream)):
        outcome = adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert outcome.raw_response == b"The bug is in _load_events."
    assert len(outcome.tool_events) == 1
    assert outcome.tool_events[0].kind is ToolKind.GRAPH
    assert outcome.input_tokens == 300
    assert outcome.output_tokens == 50


def test_prompt_loader_called_with_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def loader(case_id: str, task_type: str) -> str:
        calls.append((case_id, task_type))
        return "Loaded prompt."

    gcfg = _mcp_file(tmp_path, "graph.json", _claude_mcp("gitnexus"))
    adapter = OpenCodeAgentAdapter(
        prompt_loader=loader,
        case_id=CASE_ID,
        task_type=TASK_TYPE,
        agent_model=MODEL,
        repo_cwd=tmp_path,
        cli_path="opencode",
        graph_mcp_configs=(gcfg,),
        timeout_seconds=30.0,
        tool_name_patterns=OpenCodeToolNamePatterns(graph=()),
    )
    # graph policy still needs the graph config; patterns cleared just to keep
    # the adapter grep-capable in other tests (not exercised here).
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert calls == [(CASE_ID, TASK_TYPE)]


# --------------------------------------------------------------------------- #
# 5. Failure modes: timeout, non-zero, launch, identity mismatch
# --------------------------------------------------------------------------- #


def test_empty_stdout_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    with patch(_RUN, return_value=_completed("")):
        with pytest.raises(AgentOutputError, match="empty stdout"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_nonzero_exit_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    with patch(_RUN, return_value=_completed("", "error", returncode=1)):
        with pytest.raises(AgentNonZeroExitError, match="exited with code 1"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_timeout_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path, timeout_seconds=0.01)
    with patch(_RUN, side_effect=subprocess.TimeoutExpired(cmd=["opencode"], timeout=0.01)):
        with pytest.raises(AgentTimeoutError, match="timed out"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_launch_failure_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path, cli_path="/nonexistent/opencode")
    with patch(_RUN, side_effect=FileNotFoundError("not found")):
        with pytest.raises(AgentLaunchError, match="cannot launch"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_plain_oserror_converted_to_launch_error(tmp_path: Path) -> None:
    """A plain OSError (e.g. WinError 193 from an extensionless shim) is
    converted to AgentLaunchError, not left uncaught."""
    adapter = _graph_adapter(tmp_path, cli_path="/bad/shim/opencode")
    with patch(_RUN, side_effect=OSError("[WinError 193] not a valid Win32 application")):
        with pytest.raises(AgentLaunchError, match="cannot launch"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")


def test_identity_mismatch_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path, case_id="case-A")
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        with pytest.raises(AgentAdapterError, match="case_id mismatch"):
            adapter.execute(case_id="case-B", task_type=TASK_TYPE, tool_policy="graph")


def test_empty_resolved_prompt_raises(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path, prompt="")
    with patch(_RUN) as mock_run:
        with pytest.raises(AgentAdapterError, match="prompt resolved to an empty string"):
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    mock_run.assert_not_called()


# --------------------------------------------------------------------------- #
# 6. Command redaction
# --------------------------------------------------------------------------- #


def test_last_command_redacts_prompt(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    cmd_str = " ".join(adapter.last_command)
    assert PROMPT not in cmd_str
    assert "<prompt:" in cmd_str


def test_last_command_preserves_non_secret_flags(tmp_path: Path) -> None:
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    cmd = adapter.last_command
    assert "--model" in cmd
    assert MODEL in cmd
    assert "--dir" in cmd
    assert "--auto" in cmd
    assert "--pure" in cmd


# --------------------------------------------------------------------------- #
# 7. UTF-8 decoding
# --------------------------------------------------------------------------- #


def test_chinese_text_decoded_as_utf8(tmp_path: Path) -> None:
    chinese = "根因是 _load_events 函数中的空指针解引用。"
    adapter = _graph_adapter(tmp_path)
    stream = _stream(
        [
            _text_event(chinese),
            _step_finish_event(10, 20),
        ]
    )
    with patch(_RUN, return_value=_completed(stream)):
        outcome = adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    assert outcome.raw_response == chinese.encode("utf-8")
    assert outcome.input_tokens == 10
    assert outcome.output_tokens == 20


# --------------------------------------------------------------------------- #
# 8. Windows npm shim discovery
# --------------------------------------------------------------------------- #


def test_find_opencode_prefers_pathtext_match(tmp_path: Path) -> None:
    """shutil.which (PATHEXT-aware) finds opencode.cmd; returned as a 1-element prefix."""
    with patch("runner.opencode_adapter.shutil.which", return_value="C:/npm/opencode.cmd"):
        prefix = OpenCodeAgentAdapter._find_opencode()
    assert prefix == ["C:/npm/opencode.cmd"]


def test_find_opencode_fallback_when_not_found(tmp_path: Path) -> None:
    with (
        patch("runner.opencode_adapter.shutil.which", return_value=None),
        patch("runner.opencode_adapter._find_executable_in_path", return_value=None),
    ):
        prefix = OpenCodeAgentAdapter._find_opencode()
    assert prefix == ["opencode"]


def test_find_opencode_ps1_via_powershell(tmp_path: Path) -> None:
    """When no PATHEXT match exists, opencode.ps1 is invoked via powershell -File."""

    def fake_which(name: str, **_kwargs: Any) -> str | None:
        if name == "powershell":
            return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        return None

    with (
        patch("runner.opencode_adapter.shutil.which", side_effect=fake_which),
        patch(
            "runner.opencode_adapter._find_executable_in_path",
            return_value="C:/npm/opencode.ps1",
        ),
    ):
        prefix = OpenCodeAgentAdapter._find_opencode()
    assert prefix == [
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "C:/npm/opencode.ps1",
    ]


def test_ps1_prefix_flows_into_build_command(tmp_path: Path) -> None:
    """A discovered .ps1 prefix is used as the argv head before 'run'."""
    ps1_prefix = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "C:/npm/opencode.ps1",
    ]
    with patch.object(OpenCodeAgentAdapter, "_find_opencode", return_value=list(ps1_prefix)):
        adapter = OpenCodeAgentAdapter(
            prompt=PROMPT,
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            agent_model=MODEL,
            repo_cwd=tmp_path,
        )
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    assert argv[: len(ps1_prefix)] == ps1_prefix
    assert argv[len(ps1_prefix)] == "run"
    assert "--format" in argv


def test_find_executable_in_path_searches_path_dirs(tmp_path: Path) -> None:
    """_find_executable_in_path locates an exact-named file in a PATH directory."""
    from runner.opencode_adapter import _find_executable_in_path

    ndir = tmp_path / "npmbin"
    ndir.mkdir()
    (ndir / "opencode.ps1").write_text("# shim", encoding="utf-8")
    env = {"PATH": str(ndir) + os.pathsep + "C:/other"}
    with patch.dict("os.environ", env, clear=True):
        assert _find_executable_in_path("opencode.ps1") == str(ndir / "opencode.ps1")
    with patch.dict("os.environ", {"PATH": "C:/other"}, clear=True):
        assert _find_executable_in_path("opencode.ps1") is None


# --------------------------------------------------------------------------- #
# 9. Runner compatibility (execute_run integration)
# --------------------------------------------------------------------------- #


def test_execute_run_integration_graph_compliant(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _graph_adapter(tmp_path)

    answer_json = fx.completed_answer_bytes().decode("utf-8")
    stream = _stream(
        [
            _tool_use_event("gitnexus_query", call_id="c1"),
            _text_event(answer_json),
            _step_finish_event(500, 100),
        ]
    )

    with patch(_RUN, return_value=_completed(stream)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="opencode-run-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="graph",
                agent="opencode",
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
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _make_adapter(tmp_path, tool_name_patterns=OpenCodeToolNamePatterns(graph=()))

    answer_json = fx.completed_answer_bytes().decode("utf-8")
    stream = _stream(
        [
            _tool_use_event("grep", call_id="c1"),
            _tool_use_event("read", call_id="c2"),
            _text_event(answer_json),
            _step_finish_event(200, 50),
        ]
    )

    with patch(_RUN, return_value=_completed(stream)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="opencode-grep-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="grep",
                agent="opencode",
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
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    adapter = _graph_adapter(tmp_path)

    with patch(_RUN, return_value=_completed("", "error", returncode=1)):
        result = br.execute_run(
            runs_root=runs_root,
            run_id="opencode-fail-1",
            identity=br.RunIdentity(
                case_id=CASE_ID,
                task_type=TASK_TYPE,
                tool_policy="graph",
                agent="opencode",
                agent_model=MODEL,
            ),
            agent=adapter,
        )

    assert result.status is br.RunStatus.FAILED
    assert result.agent_answer_status is None
    assert not result.policy_valid


# --------------------------------------------------------------------------- #
# 10. AIS-013 Review 1 remediation regression tests (R1-R5)
# --------------------------------------------------------------------------- #


# -- R1: preserve and validate native remote MCP headers -------------------- #


def test_r1_remote_mcp_headers_preserved(tmp_path: Path) -> None:
    """Remote MCP headers are preserved in the normalized runtime config."""
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _opencode_mcp(
            "gitnexus",
            url="https://graph.example.com/sse",
            headers={"Authorization": "Bearer secret-token", "X-Api-Key": "key123"},
        ),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    srv = adapter.build_runtime_config("graph")["mcp"]["gitnexus"]
    assert srv["type"] == "remote"
    assert srv["headers"] == {"Authorization": "Bearer secret-token", "X-Api-Key": "key123"}


def test_r1_remote_mcp_headers_delivered_via_env_not_argv(tmp_path: Path) -> None:
    """Header values travel via the env override, never in argv or audit."""
    secret = "Bearer sk-secret-header-xyz"
    gcfg = _mcp_file(
        tmp_path,
        "graph.json",
        _opencode_mcp("gitnexus", url="https://x/sse", headers={"Authorization": secret}),
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(gcfg,))
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)) as mock_run:
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    env = mock_run.call_args.kwargs["env"]
    injected = json.loads(env[OPENCODE_CONFIG_ENV])
    assert injected["mcp"]["gitnexus"]["headers"]["Authorization"] == secret
    argv = mock_run.call_args.args[0]
    assert secret not in " ".join(argv)
    assert secret not in " ".join(adapter.last_command)
    assert adapter.last_mcp_servers == ("gitnexus",)


def test_r1_invalid_headers_type_raises_without_value_leak(tmp_path: Path) -> None:
    """Non-dict headers raise an error that does not echo header content."""
    bad = _mcp_file(
        tmp_path,
        "bad.json",
        {
            "mcp": {
                "gitnexus": {
                    "type": "remote",
                    "url": "https://x/sse",
                    "headers": "not-a-dict",
                }
            }
        },
    )
    adapter = _make_adapter(tmp_path, graph_mcp_configs=(bad,))
    with pytest.raises(AgentAdapterError, match="headers must be a JSON object"):
        adapter.build_runtime_config("graph")


# -- R2: per-occurrence fallback identities for id-less tool calls ---------- #


def test_r2_idless_same_name_tools_counted_separately(tmp_path: Path) -> None:
    """Multiple ID-less same-name tool calls are counted as distinct events."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("read", call_id=""),
            _tool_use_event("read", call_id=""),
            _tool_use_event("read", call_id=""),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 3
    assert all(e.kind is ToolKind.FILE_READ for e in parsed.tool_events)
    assert all(e.label == "read" for e in parsed.tool_events)


def test_r2_idless_tools_counted_separately(tmp_path: Path) -> None:
    """ID-less tool_use events (empty callID) are each counted separately."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("read", call_id=""),
            _tool_use_event("read", call_id=""),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 2
    assert all(e.label == "read" for e in parsed.tool_events)


def test_r2_real_id_dedup_preserved(tmp_path: Path) -> None:
    """Tools with a real callID are deduplicated across tool_use events."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("read", call_id="real-1"),
            _tool_use_event("read", call_id="real-1"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 1


# -- R3: all tool_use events are agent-initiated (no role filtering needed) -- #


def test_r3_tool_use_events_always_counted(tmp_path: Path) -> None:
    """In the real event format, all tool_use events are agent-initiated."""
    adapter = _make_adapter(tmp_path)
    stream = _stream(
        [
            _tool_use_event("read", call_id="c1"),
            _text_event("done"),
        ]
    )
    parsed = adapter._parse_stream(stream)
    assert len(parsed.tool_events) == 1
    assert parsed.tool_events[0].label == "read"


# -- R4: redact using the adapter-owned final prompt separator -------------- #


def test_r4_redact_uses_last_separator_with_extra_args_dash(tmp_path: Path) -> None:
    """When extra_args contains '--', the real prompt is still redacted."""
    adapter = _make_adapter(tmp_path, extra_args=("--", "intermediate-value"))
    argv = adapter.build_command(tool_policy="graph", prompt=PROMPT)
    redacted = adapter._redact_command(argv)
    redacted_str = " ".join(redacted)
    assert PROMPT not in redacted_str
    assert "intermediate-value" in redacted_str
    assert "<prompt:" in redacted_str
    assert f"<prompt:{len(PROMPT)} chars>" in redacted_str


def test_r4_redact_prompt_hidden_in_execute_with_extra_args_dash(tmp_path: Path) -> None:
    """End-to-end: the prompt stays hidden in last_command when extra_args has '--'."""
    adapter = _graph_adapter(tmp_path, extra_args=("--", "passthrough"))
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)):
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    cmd_str = " ".join(adapter.last_command)
    assert PROMPT not in cmd_str
    assert "passthrough" in cmd_str
    assert "<prompt:" in cmd_str


# -- R5: OPENCODE_DISABLE_PROJECT_CONFIG in child-only env ------------------ #


def test_r5_disable_project_config_set_in_child_env(tmp_path: Path) -> None:
    """OPENCODE_DISABLE_PROJECT_CONFIG=true is set in the child env."""
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)) as mock_run:
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    env = mock_run.call_args.kwargs["env"]
    assert env[OPENCODE_DISABLE_PROJECT_CONFIG_ENV] == "true"


def test_r5_child_env_inherits_parent_and_does_not_modify_it(tmp_path: Path) -> None:
    """Child env inherits parent env (Provider auth) and parent is not written back."""
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_text_event("answer")])
    marker = "AIS013_R5_MARKER"
    with patch.dict(os.environ, {marker: "inherited"}, clear=False):
        assert OPENCODE_DISABLE_PROJECT_CONFIG_ENV not in os.environ
        with patch(_RUN, return_value=_completed(stream)) as mock_run:
            adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
        # Parent env was not modified by the adapter.
        assert OPENCODE_DISABLE_PROJECT_CONFIG_ENV not in os.environ
        env = mock_run.call_args.kwargs["env"]
    # Child env inherited the parent marker (Provider auth inheritance proof).
    assert env[marker] == "inherited"
    assert env[OPENCODE_DISABLE_PROJECT_CONFIG_ENV] == "true"


def test_r5_deny_by_default_permissions_retained(tmp_path: Path) -> None:
    """The inline deny-by-default permission policy is still in the config."""
    adapter = _graph_adapter(tmp_path)
    stream = _stream([_text_event("answer")])
    with patch(_RUN, return_value=_completed(stream)) as mock_run:
        adapter.execute(case_id=CASE_ID, task_type=TASK_TYPE, tool_policy="graph")
    env = mock_run.call_args.kwargs["env"]
    injected = json.loads(env[OPENCODE_CONFIG_ENV])
    perm = injected["permission"]
    assert perm["read"] == "allow"
    for denied in ("grep", "glob", "list", "bash", "edit", "webfetch"):
        assert perm[denied] == "deny"
