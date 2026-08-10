"""OpenCode AgentAdapter (AIS-013).

Launches the locally installed OpenCode CLI non-interactively to execute the
agent under test, and independently observes its raw response, tool events and
token usage for the Runner. The adapter is the Runner's observation channel
(design S8.6, S8.7, S15.1): it constructs the CLI command, parses the NDJSON
event stream emitted by ``opencode run --format json``, classifies observed
tool calls, and returns an :class:`AgentRunOutcome`. It never calls the Judge,
never inspects credential values, and never relies on the agent's self-report.

Graph/Grep isolation (S15.1, invariants): the adapter selects MCP configs and
allowed built-ins based on the declared ``tool_policy`` and fails closed BEFORE
launch. A Grep run cannot receive Graph MCP configs or Graph tool-name patterns;
a Graph run (and a Mixed run) MUST receive an explicit Graph MCP configuration.
The separation is auditable from the redacted ``last_command`` and the
``last_mcp_servers`` server-name tuple (names only, never credential content).

Per-run configuration (invariants): the runtime OpenCode config (normalized MCP
servers + deny-by-default permissions) is injected through a subprocess-only
environment override (``OPENCODE_CONFIG_CONTENT``); it is never persisted to
disk and never placed in command arguments or audit fields. Both the
Claude-style ``mcpServers`` shape and the native OpenCode ``mcp`` shape are
normalized into a single runtime config, preserving native remote MCP
``headers`` (credential-bearing, delivered only via the env override) without
exposing their values in audit/error output. ``OPENCODE_DISABLE_PROJECT_CONFIG``
is set to ``"true"`` in the child environment so project-local config cannot
silently authorize an undeclared MCP tool; the child still inherits the user's
global OpenCode provider authentication. ``--pure`` prevents undeclared external
plugins. This adapter does not inspect, print, copy or write credential values.
``OPENCODE_CONFIG_CONTENT`` is the task-card-prescribed injection mechanism (not
discoverable from ``opencode run --help`` alone); it is a module constant so it
can be retargeted if the CLI renames it.

NDJSON event parsing (invariants): the last assistant ``text`` event's
``part.text`` becomes ``raw_response``; completed ``tool_use`` events become
Runner-observed :class:`ToolEvent`s; ``step_finish`` token data
(``part.tokens.input``/``output``) supplies input/output usage (summed across
steps). Missing usage is explicitly ``0``, never invented. Missing final
text, malformed/empty streams, ``error`` events, non-zero exit, timeout,
launch failure, invalid paths/config and duplicate MCP server names are
deterministic, auditable adapter errors.

CLI flags used (verified against ``opencode run --help`` v1.18.15): ``run``,
``--format json``, ``--model <provider/model>``, ``--dir <repo>``, ``--auto``,
``--pure``. The prompt is passed as the ``[message..]`` positional after ``--``
so it is never consumed by an option. There is no ``--mcp-config`` flag on
``opencode run``, so MCP configuration is delivered via the env override rather
than argv.

``--format json`` event shapes (verified against opencode v1.18.15 live
output):

* ``{"type": "step_start", "part": {"type": "step-start", ...}}``
* ``{"type": "text", "part": {"type": "text", "text": "..."}}``
* ``{"type": "tool_use", "part": {"type": "tool", "tool": "<name>",
   "callID": "...", "state": {"status": "completed"|"error"}}}``
* ``{"type": "step_finish", "part": {"type": "step-finish",
   "tokens": {"input": N, "output": N, ...}}}``
* ``{"type": "error", "error": {"message": "..."}}``
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .benchmark_runner import AgentRunOutcome
from .policy_validation import RUNNER_OBSERVED_SOURCE, ToolEvent, ToolKind

__all__ = [
    "OpenCodeAgentAdapter",
    "OpenCodeToolNamePatterns",
    "AgentAdapterError",
    "AgentLaunchError",
    "AgentTimeoutError",
    "AgentNonZeroExitError",
    "AgentOutputError",
    "AgentPolicyConfigError",
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "OPENCODE_CONFIG_ENV",
    "OPENCODE_DISABLE_PROJECT_CONFIG_ENV",
    "DEFAULT_ALLOWED_BUILTINS",
    "KNOWN_BUILTINS",
]

#: Default model (invariant). ``provider/model`` form per ``--model`` help.
#: Provider name matches the global OpenCode config's ``ark-plan-lmm`` provider.
DEFAULT_AGENT_MODEL = "ark-plan-lmm/deepseek-v4-flash"

DEFAULT_TIMEOUT_SECONDS = 600.0

#: Subprocess-only env override carrying the per-run OpenCode config. The value
#: is the JSON runtime config (normalized MCP + deny-by-default permissions). It
#: is never persisted and never placed in argv/audit. Prescribed by the task
#: card; kept as a constant so it can be retargeted if the CLI renames it.
OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"

#: Subprocess-only env guard (R5): set to ``"true"`` in the child environment so
#: OpenCode ignores project-local config (``opencode.json``/``.opencode.d/``)
#: and only the injected :data:`OPENCODE_CONFIG_CONTENT` runtime config
#: authorizes tools/MCP. The child still inherits the user's global Provider
#: authentication; this adapter never clears, inspects, prints, copies, or writes
#: credential values. Set in the child env only — the parent environment is never
#: modified.
OPENCODE_DISABLE_PROJECT_CONFIG_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"

USAGE_UNAVAILABLE = 0

#: OpenCode built-in tools the permission map is aware of (deny-by-default).
#: ``read``/``grep``/``glob``/``list``/``bash`` are the agent built-ins named in
#: the invariants; ``edit``/``webfetch`` are dangerous transports that are
#: always denied by default to prevent bypassing Graph/Grep isolation.
KNOWN_BUILTINS: tuple[str, ...] = ("read", "grep", "glob", "list", "bash", "edit", "webfetch")

#: Default allowed built-ins per tool_policy. Graph allows only ``read`` (Graph
#: queries come from the Graph MCP namespace); Grep/Mixed allow the code-search
#: built-ins. ``bash``/``edit``/``webfetch`` are denied for all policies by
#: default so an agent cannot bypass MCP isolation via a shell or network hop.
DEFAULT_ALLOWED_BUILTINS: Mapping[str, tuple[str, ...]] = {
    "graph": ("read",),
    "grep": ("read", "grep", "glob", "list"),
    "mixed": ("read", "grep", "glob", "list"),
}


class AgentAdapterError(Exception):
    """Base class for adapter-level errors (auditable, surfaces as failed run)."""


class AgentLaunchError(AgentAdapterError):
    """The CLI process could not be launched (executable not found, etc.)."""


class AgentTimeoutError(AgentAdapterError):
    """The CLI process exceeded the configured timeout."""


class AgentNonZeroExitError(AgentAdapterError):
    """The CLI process exited with a non-zero status code."""


class AgentOutputError(AgentAdapterError):
    """The CLI produced empty, malformed, or error-bearing NDJSON output."""


class AgentPolicyConfigError(AgentAdapterError):
    """Adapter configuration is inconsistent with the declared tool_policy.

    Raised before any subprocess launch so a misconfigured run fails fast into a
    truthful failed run rather than executing with the wrong MCP set.
    """


@dataclass(frozen=True)
class OpenCodeToolNamePatterns:
    """Configurable regex patterns for classifying observed OpenCode tool calls.

    Each field is a tuple of regex strings; a tool name matching any pattern in
    a field is classified as that ToolKind. Fields are checked in order: graph,
    then search, then file_read; the first match wins. A name matching none
    defaults to :attr:`ToolKind.OTHER`.

    Defaults reflect OpenCode built-ins (lowercase) and the GitNexus Graph MCP
    server namespace. OpenCode exposes MCP tools as ``<server>_<tool>`` (single
    underscore), so a GitNexus query is ``gitnexus_query``; the default graph
    pattern matches the ``gitnexus_`` server namespace. All patterns are
    configurable so a different Graph MCP or tool namespace can be supported
    without code changes.
    """

    graph: tuple[str, ...] = (r"^gitnexus_",)
    search: tuple[str, ...] = (r"^grep$", r"^glob$")
    file_read: tuple[str, ...] = (r"^read$",)


@dataclass(frozen=True)
class _CompiledPatterns:
    graph: tuple[re.Pattern[str], ...]
    search: tuple[re.Pattern[str], ...]
    file_read: tuple[re.Pattern[str], ...]


def _compile_patterns(patterns: OpenCodeToolNamePatterns) -> _CompiledPatterns:
    return _CompiledPatterns(
        graph=tuple(re.compile(p) for p in patterns.graph),
        search=tuple(re.compile(p) for p in patterns.search),
        file_read=tuple(re.compile(p) for p in patterns.file_read),
    )


@dataclass(frozen=True)
class _StreamParseResult:
    raw_response: bytes
    tool_events: tuple[ToolEvent, ...]
    input_tokens: int
    output_tokens: int


PromptLoader = Callable[[str, str], str]

# Secret patterns redacted from stderr/exception text before audit (S13.6).
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(api[_-]?key|token|password|secret|credential)[=:]\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)(Authorization|X-API-Key):\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"\b(eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)\b"), r"<REDACTED>"),
]


def _find_executable_in_path(name: str) -> str | None:
    """Search PATH directories for an exact-named file (no PATHEXT expansion).

    Used to discover ``opencode.ps1``, which is not in the default Windows
    PATHEXT and so is invisible to :func:`shutil.which`.
    """
    path_env = os.environ.get("PATH", "")
    seen: set[str] = set()
    for directory in path_env.split(os.pathsep):
        if not directory or directory in seen:
            continue
        seen.add(directory)
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


class OpenCodeAgentAdapter:
    """AgentAdapter that executes the agent under test via the OpenCode CLI.

    The adapter instance holds per-run configuration (prompt, identity, model,
    cwd, MCP configs, skill text, CLI prefix, tool-name classification, allowed
    built-ins) because :class:`AgentAdapter.execute` receives only
    case_id/task_type/tool_policy. The ``tool_policy`` argument selects which
    MCP configs and built-ins are enabled, enforcing Graph/Grep isolation at
    command/config-construction time (before launch).

    All failures (nonzero exit, timeout, launch failure, malformed/empty output,
    missing final text, invalid paths/config, duplicate MCP names, policy
    misconfiguration) raise an :class:`AgentAdapterError` subclass so the Runner
    records a truthful failed run.
    """

    def __init__(
        self,
        *,
        prompt: str | None = None,
        prompt_loader: PromptLoader | None = None,
        case_id: str = "",
        task_type: str = "",
        agent_model: str = DEFAULT_AGENT_MODEL,
        repo_cwd: Path | str | None = None,
        graph_mcp_configs: Sequence[str | Path] = (),
        grep_mcp_configs: Sequence[str | Path] = (),
        skill_text: str | None = None,
        skill_file: Path | str | None = None,
        cli_path: str | None = None,
        tool_name_patterns: OpenCodeToolNamePatterns | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        auto_approve: bool = True,
        pure: bool = True,
        allowed_builtins: Mapping[str, Sequence[str]] | None = None,
        extra_args: Sequence[str] = (),
    ) -> None:
        if prompt is None and prompt_loader is None:
            raise AgentAdapterError("either prompt or prompt_loader must be provided")
        if prompt is not None and prompt_loader is not None:
            raise AgentAdapterError("prompt and prompt_loader are mutually exclusive")
        self._prompt = prompt
        self._prompt_loader = prompt_loader

        self._case_id = case_id
        self._task_type = task_type
        self._agent_model = agent_model
        self._repo_cwd = Path(repo_cwd) if repo_cwd is not None else None
        self._graph_mcp_configs = tuple(Path(p) for p in graph_mcp_configs)
        self._grep_mcp_configs = tuple(Path(p) for p in grep_mcp_configs)
        self._auto_approve = auto_approve
        self._pure = pure
        self._extra_args = tuple(extra_args)

        self._skill_text = skill_text
        if skill_file is not None:
            if skill_text is not None:
                raise AgentAdapterError("skill_text and skill_file are mutually exclusive")
            skill_path = Path(skill_file)
            if not skill_path.is_file():
                raise AgentAdapterError(f"skill_file not found: {skill_path}")
            self._skill_text = skill_path.read_text(encoding="utf-8")

        if not isinstance(agent_model, str) or not agent_model.strip():
            raise AgentAdapterError("agent_model must be a non-empty string")

        # CLI argv prefix. A .ps1 shim is invoked via ``powershell -File`` so it
        # is a multi-element prefix; a .cmd/.exe is a single-element prefix.
        if cli_path is not None:
            self._cli_prefix: list[str] = [cli_path]
        else:
            self._cli_prefix = self._find_opencode()

        patterns = tool_name_patterns or OpenCodeToolNamePatterns()
        self._tool_name_patterns = patterns
        self._compiled = _compile_patterns(patterns)

        if allowed_builtins is not None:
            self._allowed_builtins: Mapping[str, tuple[str, ...]] = {
                k: tuple(v) for k, v in allowed_builtins.items()
            }
        else:
            self._allowed_builtins = DEFAULT_ALLOWED_BUILTINS

        self._timeout_seconds = timeout_seconds

        # Audit state. ``last_command`` is the redacted argv; ``last_mcp_servers``
        # carries server NAMES only (never credential-bearing config content).
        self.last_command: list[str] = []
        self.last_tool_policy: str = ""
        self.last_mcp_servers: tuple[str, ...] = ()

        self._validate_paths()

    # -- Public API (AgentAdapter protocol) --------------------------------- #

    def execute(self, *, case_id: str, task_type: str, tool_policy: str) -> AgentRunOutcome:
        """Run the agent and return the observed outcome.

        Raises an :class:`AgentAdapterError` subclass on any failure; the Runner
        catches exceptions and records a failed run.
        """
        if self._case_id and case_id != self._case_id:
            raise AgentAdapterError(
                f"case_id mismatch: constructor has {self._case_id!r}, execute received {case_id!r}"
            )
        if self._task_type and task_type != self._task_type:
            raise AgentAdapterError(
                f"task_type mismatch: constructor has {self._task_type!r}, "
                f"execute received {task_type!r}"
            )

        if self._prompt_loader is not None:
            prompt = self._prompt_loader(case_id, task_type)
        else:
            assert self._prompt is not None
            prompt = self._prompt
        if not prompt:
            raise AgentAdapterError("prompt resolved to an empty string")

        # Fail closed BEFORE launch: isolation + config normalization errors
        # surface here (no subprocess is started for a misconfigured run).
        runtime_config = self.build_runtime_config(tool_policy)
        argv = self.build_command(tool_policy=tool_policy, prompt=prompt)

        self.last_command = self._redact_command(argv)
        self.last_tool_policy = tool_policy
        self.last_mcp_servers = tuple(sorted(runtime_config["mcp"].keys()))

        env_extra = {
            OPENCODE_CONFIG_ENV: _json_dumps(runtime_config),
            # R5: disable project-local config so only the injected runtime
            # config authorizes tools/MCP. Child-only; Provider auth inherited.
            OPENCODE_DISABLE_PROJECT_CONFIG_ENV: "true",
        }
        stdout, stderr, returncode = self._run_subprocess(argv, env_extra)

        if returncode != 0:
            raise AgentNonZeroExitError(
                f"opencode CLI exited with code {returncode}: {self._redact_text(stderr[:500])}"
            )

        if not stdout.strip():
            raise AgentOutputError("opencode CLI produced empty stdout")

        parsed = self._parse_stream(stdout)

        return AgentRunOutcome(
            raw_response=parsed.raw_response,
            tool_events=parsed.tool_events,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
        )

    # -- Command construction (testable without subprocess) ----------------- #

    def build_command(
        self,
        *,
        tool_policy: str,
        prompt: str,
    ) -> list[str]:
        """Construct the OpenCode CLI argv for the given policy and prompt.

        Returns a list of strings (subprocess argv, never a shell string). MCP
        configuration is NOT placed in argv; it is delivered via the
        ``OPENCODE_CONFIG_CONTENT`` env override (see :meth:`build_runtime_config`).
        The prompt (with any skill text prepended) is placed after ``--`` so it
        is treated as the ``[message..]`` positional, not consumed by an option.
        """
        args: list[str] = list(self._cli_prefix)
        args.extend(["run", "--format", "json"])
        args.extend(["--model", self._agent_model])
        if self._repo_cwd is not None:
            args.extend(["--dir", str(self._repo_cwd)])
        if self._auto_approve:
            args.append("--auto")
        if self._pure:
            args.append("--pure")
        args.extend(self._extra_args)

        composed = self._compose_prompt(prompt)
        args.extend(["--", composed])
        return args

    def _compose_prompt(self, prompt: str) -> str:
        """Prepend explicit skill text to the prompt (invariant).

        There is no ``--append-system-prompt``/``--skill`` flag on
        ``opencode run``; explicit skill content is prepended to the message.
        The native global/project skill tool remains denied (``--pure``) unless
        explicitly supported in a later task.
        """
        if self._skill_text:
            return self._skill_text + "\n\n" + prompt
        return prompt

    # -- Runtime config (Graph/Grep isolation + normalization) -------------- #

    def build_runtime_config(self, tool_policy: str) -> dict[str, Any]:
        """Build the runtime OpenCode config dict for the declared tool_policy.

        Reads and normalizes the selected MCP config files (both
        ``mcpServers`` and ``mcp`` shapes) into a single OpenCode-native
        ``mcp`` map, and builds a deny-by-default ``permission`` map that only
        allows the current condition's built-ins. The returned dict is serialized
        into ``OPENCODE_CONFIG_CONTENT``; it is never persisted to disk and never
        placed in argv/audit. Raises an :class:`AgentAdapterError` for an unknown
        policy, an isolation violation, an unreadable/invalid config, or a
        duplicate MCP server name.
        """
        selected = self._select_mcp_configs(tool_policy)
        mcp_servers = self._normalize_mcp_configs(selected)
        allowed = self._allowed_builtins_for(tool_policy)
        permission = self._build_permission(allowed)
        return {"mcp": mcp_servers, "permission": permission}

    def _select_mcp_configs(self, tool_policy: str) -> tuple[Path, ...]:
        """Select MCP config file paths for the declared policy (fail-closed).

        graph  -> graph_mcp_configs (MUST be non-empty: explicit Graph MCP).
        grep   -> grep_mcp_configs; fail closed if any Graph MCP configs or
                  Graph tool-name patterns are configured.
        mixed  -> graph_mcp_configs (MUST be non-empty) + grep_mcp_configs.
        """
        if tool_policy == "graph":
            if not self._graph_mcp_configs:
                raise AgentPolicyConfigError(
                    "Graph policy requires an explicit Graph MCP configuration; "
                    "none provided (fail-closed)"
                )
            return self._graph_mcp_configs
        if tool_policy == "grep":
            if self._graph_mcp_configs:
                raise AgentPolicyConfigError(
                    "Grep policy must not be configured with Graph MCP configs; "
                    "fail-closed to prevent Graph tool access"
                )
            if self._tool_name_patterns.graph:
                raise AgentPolicyConfigError(
                    "Grep policy must not be configured with Graph tool-name "
                    "patterns; fail-closed to prevent Graph tool classification"
                )
            return self._grep_mcp_configs
        if tool_policy == "mixed":
            if not self._graph_mcp_configs:
                raise AgentPolicyConfigError(
                    "Mixed policy requires an explicit Graph MCP configuration; "
                    "none provided (fail-closed)"
                )
            return self._graph_mcp_configs + self._grep_mcp_configs
        raise AgentPolicyConfigError(
            f"unknown tool_policy {tool_policy!r}; expected graph/grep/mixed"
        )

    def _normalize_mcp_configs(self, paths: tuple[Path, ...]) -> dict[str, dict[str, Any]]:
        """Read & normalize MCP config files into an OpenCode-native ``mcp`` map.

        Accepts both the Claude-style ``{"mcpServers": {...}}`` shape and the
        native OpenCode ``{"mcp": {...}}`` shape. Each server is normalized to
        ``{"type": "local"|"remote", "command": [...], "env": {...}, "enabled":
        True}`` (local) or ``{"type": "remote", "url": ..., "headers": {...},
        "enabled": True}`` (remote, when headers are present). Duplicate server
        names across configs raise :class:`AgentAdapterError`.
        Credential-bearing ``env``/``headers`` values are preserved in the
        returned dict (delivered only via the env override) but never appear in
        argv/audit.
        """
        servers: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentAdapterError(f"cannot read MCP config {path!r}: {exc}") from exc
            if not isinstance(raw, dict):
                raise AgentAdapterError(f"MCP config {path!r} must be a JSON object")
            if "mcpServers" in raw:
                section = raw["mcpServers"]
                shape = "mcpServers"
            elif "mcp" in raw:
                section = raw["mcp"]
                shape = "mcp"
            else:
                raise AgentAdapterError(
                    f"MCP config {path!r} has neither 'mcpServers' nor 'mcp' key"
                )
            if not isinstance(section, dict):
                raise AgentAdapterError(
                    f"MCP config {path!r} '{shape}' section must be a JSON object"
                )
            for name, spec in section.items():
                if name in servers:
                    raise AgentAdapterError(f"duplicate MCP server name {name!r} across configs")
                servers[name] = self._normalize_server(name, spec, path)
        return servers

    def _normalize_server(self, name: str, spec: Any, path: Path) -> dict[str, Any]:
        """Normalize one MCP server spec to the OpenCode-native shape.

        Both Claude-style ``{"type": "stdio", "command": "...", "args": [...],
        "env": {...}}`` and OpenCode-native ``{"type": "local", "command":
        [...], "environment": {...}}`` are normalized to the OpenCode-native
        shape. Claude's ``"stdio"`` type is mapped to ``"local"`` (the
        OpenCode schema value); ``env`` (Claude) and ``environment`` (OpenCode)
        are both accepted and emitted as ``environment``.
        """
        if not isinstance(spec, dict):
            raise AgentAdapterError(f"MCP server {name!r} in {path!r} must be a JSON object")
        normalized: dict[str, Any] = {"enabled": True}
        if "command" in spec:
            command = spec["command"]
            args = spec.get("args", [])
            if isinstance(command, str):
                cmd_list: list[str] = [command]
            elif isinstance(command, list):
                cmd_list = [str(c) for c in command]
            else:
                raise AgentAdapterError(f"MCP server {name!r} command must be a string or list")
            if not isinstance(args, list):
                raise AgentAdapterError(f"MCP server {name!r} args must be a list")
            cmd_list.extend(str(a) for a in args)
            if not cmd_list or not cmd_list[0]:
                raise AgentAdapterError(f"MCP server {name!r} has no command")
            # Claude-style configs use "stdio"; OpenCode schema requires "local".
            raw_type = spec.get("type", "local")
            normalized["type"] = "local" if raw_type == "stdio" else raw_type
            normalized["command"] = cmd_list
        elif "url" in spec:
            normalized["type"] = "remote"
            url = spec["url"]
            if not isinstance(url, str) or not url:
                raise AgentAdapterError(f"MCP server {name!r} url must be a non-empty string")
            normalized["url"] = url
            # Preserve native remote MCP headers (R1). Values are credential-
            # bearing (e.g. Authorization); they travel only via the env override
            # and never appear in argv/audit. Validate the mapping shape without
            # echoing values in the error message.
            headers = spec.get("headers")
            if headers is not None:
                if not isinstance(headers, dict):
                    raise AgentAdapterError(f"MCP server {name!r} headers must be a JSON object")
                normalized["headers"] = {str(k): str(v) for k, v in headers.items()}
        else:
            raise AgentAdapterError(f"MCP server {name!r} in {path!r} has no 'command' or 'url'")
        # Accept both Claude-style "env" and OpenCode-native "environment";
        # always emit as "environment" per OpenCode schema.
        env = spec.get("env")
        if env is None:
            env = spec.get("environment")
        if env:
            if not isinstance(env, dict):
                raise AgentAdapterError(f"MCP server {name!r} env must be a JSON object")
            normalized["environment"] = {str(k): str(v) for k, v in env.items()}
        return normalized

    def _allowed_builtins_for(self, tool_policy: str) -> tuple[str, ...]:
        return tuple(self._allowed_builtins.get(tool_policy, ()))

    def _build_permission(self, allowed_builtins: tuple[str, ...]) -> dict[str, str]:
        """Deny-by-default permission map: only allowed built-ins are 'allow'."""
        return {b: ("allow" if b in allowed_builtins else "deny") for b in KNOWN_BUILTINS}

    # -- Tool-name classification -------------------------------------------- #

    def _classify_tool_name(self, name: str) -> ToolKind:
        """Classify a tool name to a ToolKind using configured patterns.

        Checked in order: graph, search, file_read; first match wins. A name
        matching none defaults to :attr:`ToolKind.OTHER`. Never raises.
        """
        for pattern in self._compiled.graph:
            if pattern.search(name):
                return ToolKind.GRAPH
        for pattern in self._compiled.search:
            if pattern.search(name):
                return ToolKind.SEARCH
        for pattern in self._compiled.file_read:
            if pattern.search(name):
                return ToolKind.FILE_READ
        return ToolKind.OTHER

    # -- NDJSON stream parsing ---------------------------------------------- #

    def _parse_stream(self, stdout: str) -> _StreamParseResult:
        """Parse OpenCode ``--format json`` NDJSON output defensively.

        Actual event format (verified against opencode v1.18.15 ``--format
        json``):

        * ``{"type": "step_start", "part": {"type": "step-start", ...}}``
          — step begin, no payload of interest.
        * ``{"type": "text", "part": {"type": "text", "text": "..."}}``
          — assistant text; the last ``text`` event's ``part.text`` is the
          final response.
        * ``{"type": "tool_use", "part": {"type": "tool", "tool": "<name>",
           "callID": "...", "state": {"status": "completed"|"error"}}}``
           — completed tool calls become :class:`ToolEvent`s.
        * ``{"type": "step_finish", "part": {"type": "step-finish",
           "tokens": {"input": N, "output": N}}}``
           — token usage summed across steps.
        * ``{"type": "error", "error": {"message": "..."}}``
           — raises :class:`AgentOutputError`.

        - Completed ``tool_use`` parts become Runner-observed
          :class:`ToolEvent`s, classified via :meth:`_classify_tool_name` and
          stamped with RUNNER_OBSERVED_SOURCE. Tools with a real ``callID``
          are deduplicated by that ID; ID-less calls get unique per-occurrence
          fallback identities so distinct same-name calls are not collapsed
          (R2).
        - ``step_finish`` token data (``part.tokens.input``/``output``) is
          summed across steps. Missing usage is explicitly ``0``, never
          invented.
        - A top-level ``error`` event raises :class:`AgentOutputError`.

        Malformed individual lines are skipped. An entirely empty stream (no
        valid records) or a stream with no ``text`` event raises
        :class:`AgentOutputError`.
        """
        # callID -> (tool_name, completed); insertion-ordered. A tool with a
        # real callID is deduplicated (last write wins). ID-less calls get
        # unique per-occurrence fallback keys so distinct same-name calls are
        # not collapsed (R2).
        tools: dict[str, tuple[str, bool]] = {}
        noid_seq: list[int] = [0]  # mutable counter for per-occurrence fallback IDs
        text_parts: list[str] = []
        input_tokens = USAGE_UNAVAILABLE
        output_tokens = USAGE_UNAVAILABLE
        valid_records = 0
        total_lines = 0

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            valid_records += 1
            rtype = record.get("type")

            if rtype == "error":
                raise AgentOutputError(
                    "opencode stream reported an error event: "
                    + self._redact_text(_extract_error_message(record))
                )
            elif rtype == "text":
                part = record.get("part")
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            elif rtype == "tool_use":
                self._record_tool(record, tools, noid_seq)
            elif rtype == "step_finish":
                it, ot = _extract_usage(record)
                input_tokens += it
                output_tokens += ot

        if valid_records == 0:
            raise AgentOutputError(
                f"no valid JSON records in opencode output "
                f"(encountered {total_lines} non-blank line(s))"
            )

        if text_parts:
            final_text = text_parts[-1]
        else:
            final_text = ""

        if not final_text:
            raise AgentOutputError("opencode stream produced no final assistant text")

        tool_events: list[ToolEvent] = []
        for name, completed in tools.values():
            if not completed:
                continue
            kind = self._classify_tool_name(name)
            tool_events.append(ToolEvent(kind=kind, source=RUNNER_OBSERVED_SOURCE, label=name))

        return _StreamParseResult(
            raw_response=final_text.encode("utf-8"),
            tool_events=tuple(tool_events),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _record_tool(
        self,
        record: dict[str, Any],
        tools: dict[str, tuple[str, bool]],
        noid_seq: list[int],
    ) -> None:
        """Record a ``tool_use`` event's tool name + completed state.

        Extracts tool info from ``record["part"]``. Tools with a real
        ``callID`` are deduplicated by that ID (last write wins). An ID-less
        call gets a unique per-occurrence fallback key so distinct same-name
        calls are not collapsed into one (R2).
        """
        part = record.get("part")
        if not isinstance(part, dict):
            return
        name = part.get("tool")
        if not isinstance(name, str) or not name:
            return
        tid = part.get("callID")
        if not isinstance(tid, str) or not tid:
            tid = _noid_key(noid_seq)
        tools[tid] = (name, _tool_completed(part))

    # -- Subprocess execution ------------------------------------------------ #

    def _run_subprocess(
        self, argv: list[str], env_extra: Mapping[str, str]
    ) -> tuple[str, str, int]:
        """Run the CLI subprocess and return (stdout, stderr, returncode).

        Inherits the parent environment (global OpenCode provider auth) and adds
        the per-run config override. Uses subprocess argv (never shell) to
        prevent shell injection. Raises :class:`AgentTimeoutError` on timeout and
        :class:`AgentLaunchError` on launch failure (OSError covers
        FileNotFoundError, PermissionError and WinError 193 from an extensionless
        shim). Output is captured as raw bytes and decoded UTF-8 with
        errors='replace' so a malformed byte never crashes the adapter.
        """
        env = os.environ.copy()
        env.update(env_extra)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self._timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeoutError(
                f"opencode CLI timed out after {self._timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise AgentLaunchError(
                f"cannot launch opencode CLI at {self._cli_prefix[0]!r}: {exc}"
            ) from exc

        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        return stdout, stderr, result.returncode

    # -- Path validation ----------------------------------------------------- #

    def _validate_paths(self) -> None:
        """Validate configured paths exist (fail fast on misconfiguration)."""
        if self._repo_cwd is not None and not self._repo_cwd.is_dir():
            raise AgentAdapterError(f"repo_cwd is not a directory: {self._repo_cwd}")
        for config in self._graph_mcp_configs:
            if not config.is_file():
                raise AgentAdapterError(f"graph MCP config not found or not a file: {config}")
        for config in self._grep_mcp_configs:
            if not config.is_file():
                raise AgentAdapterError(f"grep MCP config not found or not a file: {config}")

    # -- CLI detection (Windows npm shim safe discovery) -------------------- #

    @staticmethod
    def _find_opencode() -> list[str]:
        """Locate the opencode executable, returning a subprocess argv prefix.

        Prefers a PATHEXT-matching shim (``opencode.cmd`` / ``opencode.exe``) via
        :func:`shutil.which`, which on Windows skips the extensionless npm shim
        that raises WinError 193. If no PATHEXT match is found, discovers
        ``opencode.ps1`` in PATH (``.ps1`` is not in the default PATHEXT) and
        invokes it via ``powershell -NoProfile -ExecutionPolicy Bypass -File``
        using subprocess argv (never a shell string). Returns ``["opencode"]``
        as a fallback so a missing executable surfaces as
        :class:`AgentLaunchError` at run time.
        """
        found = shutil.which("opencode")
        if found:
            return [found]
        ps1 = _find_executable_in_path("opencode.ps1")
        if ps1:
            powershell = shutil.which("powershell") or "powershell.exe"
            return [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps1,
            ]
        return ["opencode"]

    # -- Redaction (S13.6: no prompt/secret content in audit logs) ---------- #

    @staticmethod
    def _redact_text(text: str) -> str:
        """Redact known secret patterns from text for safe error messages."""
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _redact_command(self, argv: list[str]) -> list[str]:
        """Return a redacted copy of argv for audit (prompt content hidden).

        The adapter-owned prompt separator is the LAST ``--`` in argv:
        :meth:`build_command` always appends ``["--", composed_prompt]`` as the
        final elements, so a ``--`` supplied via ``extra_args`` earlier in argv
        is not mistaken for the prompt separator (R4). The prompt is replaced
        with a length placeholder; model, ``--dir`` path, and flags are
        non-secret and preserved. MCP config content is never in argv (it travels
        via the env override), so it cannot leak here.
        """
        last_dash = -1
        for i, arg in enumerate(argv):
            if arg == "--":
                last_dash = i
        if last_dash == -1:
            return list(argv)
        prompt_len = sum(len(a) for a in argv[last_dash + 1 :])
        return list(argv[: last_dash + 1]) + [f"<prompt:{prompt_len} chars>"]


# --------------------------------------------------------------------------- #
# Module-private helpers
# --------------------------------------------------------------------------- #


def _json_dumps(doc: dict[str, Any]) -> str:
    """Stable JSON serialization for the env override (no ASCII escaping)."""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True)


def _noid_key(noid_seq: list[int]) -> str:
    """Return a unique per-occurrence fallback key for an ID-less tool call.

    Uses a sentinel-prefixed counter so distinct ID-less calls are never
    collapsed into one while real-ID deduplication is preserved (R2). The key is
    internal to stream parsing and never surfaces in Runner artifacts.
    """
    key = f"__noid_{noid_seq[0]}__"
    noid_seq[0] += 1
    return key


def _tool_completed(part: dict[str, Any]) -> bool:
    """Whether a ``tool_use`` part represents a completed tool call.

    In the real event format, completion status is in
    ``part["state"]["status"]`` (``"completed"`` or ``"error"``).
    """
    state = part.get("state")
    if isinstance(state, dict):
        status = state.get("status")
        if isinstance(status, str):
            return status == "completed"
    # No explicit state: treat as completed unless an error is present.
    return not part.get("error")


def _extract_usage(record: dict[str, Any]) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a ``step_finish`` record.

    Token usage is in ``record["part"]["tokens"]`` with keys ``input`` and
    ``output`` (verified against opencode v1.18.15). Missing usage
    contributes 0 (never invented).
    """
    part = record.get("part")
    if not isinstance(part, dict):
        return 0, 0
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        return 0, 0
    it = tokens.get("input")
    ot = tokens.get("output")
    input_tokens = int(it) if isinstance(it, (int, float)) else 0
    output_tokens = int(ot) if isinstance(ot, (int, float)) else 0
    return input_tokens, output_tokens


def _extract_error_message(record: dict[str, Any]) -> str:
    """Extract a short, safe message from an ``error`` event (no structured dump)."""
    error = record.get("error")
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str):
            return msg[:300]
    elif isinstance(error, str):
        return error[:300]
    return "unknown error"
