from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandSpec:
    command: list[str]
    cwd: str
    policy_enforced: bool
    notes: list[str]
    stdin_file: str | None


def _has_mcp_server(config_path: str | None, server: str | None) -> bool:
    if not config_path or not server:
        return False
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return isinstance(servers, dict) and server in servers


def build_command(
    run: dict,
    prompt_path: Path,
    *,
    model: str | None,
    mcp_config: str | None,
) -> CommandSpec:
    if run["agent"] != "claude-code":
        raise ValueError(f"unsupported agent: {run['agent']}")
    command = [
        "claude.cmd" if os.name == "nt" else "claude",
        "--print",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
    ]
    if model:
        command.extend(["--model", model])
    allowed = ["Bash", "Read"]
    if run["tool_policy"] == "graph":
        allowed.extend(run["graph_allowed_tools"])
    command.extend(["--allowedTools", ",".join(allowed)])
    enforced = True
    notes = ["Prompt is passed through stdin."]
    if run["tool_policy"] == "graph":
        if _has_mcp_server(mcp_config, run.get("graph_mcp_server")):
            command.extend(["--mcp-config", str(Path(mcp_config).resolve())])
        else:
            enforced = False
            notes.append(
                f"Graph provider {run.get('graph_provider')} requires MCP server "
                f"{run.get('graph_mcp_server')!r} in its config."
            )
    return CommandSpec(command, run["target_project"], enforced, notes, str(prompt_path))
