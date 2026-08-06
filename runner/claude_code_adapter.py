"""Claude Code AgentAdapter (AIS-012).

Launches the installed Claude Code CLI non-interactively to execute the agent
under test, and observes its tool usage and raw response for the Runner. The
adapter is the Runner's observation channel (design S8.6, S8.7, S15.1): it
constructs the CLI command, parses the stream-json output, classifies observed
tool calls, and returns an AgentRunOutcome. It never calls the Judge, never
inspects secrets, and never relies on the agent's self-report for tool usage.

Graph/Grep isolation (S15.1): the adapter selects MCP configs based on the
declared tool_policy. Graph policy receives only its configured Graph MCP set;
Grep policy fails closed if any Graph MCP configs or Graph tool-name patterns
are configured (a configuration error, not a run). Grep policy also fails
closed if a skill is configured, so no skill text is injected into the Grep
baseline (AIS-012, F2); the default dispatch factory suppresses the skill for
Grep runs, and this guard makes the isolation robust at command-construction
time. The separation is auditable from the constructed command argv (stored
redacted on the instance after each call via last_command).

Skill/plugin inputs: there is no --skill flag in Claude Code. Explicit
skill content is injected via --append-system-prompt (a documented, safe
mechanism that does not read arbitrary global skill directories). Explicit
plugins are loaded via --plugin-dir (repeatable, session-only). Slash commands
(skills) are disabled by default via --disable-slash-commands to prevent
accidental project-global skill loading. CLI 2.1.223+ requires --verbose
when using --print --output-format=stream-json; the adapter passes it
unconditionally.

Credentials (S13.6): the subprocess inherits the parent's environment and global
Claude Code session. No key, token or password is passed as a CLI argument or
environment variable by this adapter. Command argv stored for audit has the
prompt and system-prompt content redacted.

CLI flags used (verified against claude --help v2.1.220): -p/--print, --model,
--output-format stream-json, --permission-mode, --strict-mcp-config,
--mcp-config (when configs are supplied), --disable-slash-commands,
--plugin-dir (when plugin dirs are supplied), --append-system-prompt (when
skill text is supplied). The working directory is set via subprocess.run(cwd=)
(there is no --cwd flag).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .benchmark_runner import AgentRunOutcome
from .policy_validation import RUNNER_OBSERVED_SOURCE, ToolEvent, ToolKind

__all__ = [
    "ClaudeCodeAgentAdapter",
    "ToolNamePatterns",
    "AgentAdapterError",
    "AgentLaunchError",
    "AgentTimeoutError",
    "AgentNonZeroExitError",
    "AgentOutputError",
    "AgentPolicyConfigError",
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_PERMISSION_MODE",
    "DEFAULT_TIMEOUT_SECONDS",
]

DEFAULT_AGENT_MODEL = "glm-5.2"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_TIMEOUT_SECONDS = 600.0

USAGE_UNAVAILABLE = 0


class AgentAdapterError(Exception):
    """Base class for adapter-level errors (auditable, surfaces as failed run)."""


class AgentLaunchError(AgentAdapterError):
    """The CLI process could not be launched (executable not found, etc.)."""


class AgentTimeoutError(AgentAdapterError):
    """The CLI process exceeded the configured timeout."""


class AgentNonZeroExitError(AgentAdapterError):
    """The CLI process exited with a non-zero status code."""


class AgentOutputError(AgentAdapterError):
    """The CLI produced empty, malformed, or unparseable stream-json output."""


class AgentPolicyConfigError(AgentAdapterError):
    """Adapter configuration is inconsistent with the declared tool_policy.

    Raised before any subprocess launch so a misconfigured run fails fast into
    a truthful failed run rather than executing with the wrong MCP set.
    """


@dataclass(frozen=True)
class ToolNamePatterns:
    """Configurable regex patterns for classifying observed tool-use events.

    Each field is a tuple of regex strings; a tool name matching any pattern in
    a field is classified as that ToolKind. Fields are checked in order: graph,
    then search, then file_read; the first match wins. A name matching none
    defaults to ToolKind.OTHER.

    Defaults reflect Claude Code built-in tools and the GitNexus Graph MCP
    (mcp__gitnexus__*). All patterns are configurable so a different Graph MCP
    or tool namespace can be supported without code changes.
    """

    graph: tuple[str, ...] = (r"^mcp__gitnexus",)
    search: tuple[str, ...] = (r"^Grep$", r"^Glob$")
    file_read: tuple[str, ...] = (r"^Read$",)


@dataclass(frozen=True)
class _CompiledPatterns:
    graph: tuple[re.Pattern[str], ...]
    search: tuple[re.Pattern[str], ...]
    file_read: tuple[re.Pattern[str], ...]


def _compile_patterns(patterns: ToolNamePatterns) -> _CompiledPatterns:
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
    result_found: bool


PromptLoader = Callable[[str, str], str]

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(api[_-]?key|token|password|secret|credential)[=:]\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)(Authorization|X-API-Key):\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"\b(eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)\b"), r"<REDACTED>"),
]


class ClaudeCodeAgentAdapter:
    """AgentAdapter that executes the agent under test via Claude Code CLI.

    The adapter instance holds per-run configuration (prompt, identity, model,
    cwd, MCP configs, skill/plugin inputs, permission mode, CLI path, and
    tool-name classification) because AgentAdapter.execute receives only
    case_id/task_type/tool_policy. The tool_policy argument selects which MCP
    configs are passed to the CLI, enforcing Graph/Grep isolation at
    command-construction time. A Grep run also fails closed if a skill is
    configured, so no skill text contaminates the Grep baseline (AIS-012, F2).

    All failures (nonzero exit, timeout, launch failure, malformed/empty output,
    invalid paths, invalid policy config) raise an AgentAdapterError subclass so
    the Runner records a truthful failed run.
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
        plugin_dirs: Sequence[str | Path] = (),
        skill_text: str | None = None,
        skill_file: Path | str | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        cli_path: str | None = None,
        tool_name_patterns: ToolNamePatterns | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        disable_slash_commands: bool = True,
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
        self._graph_mcp_configs = tuple(str(p) for p in graph_mcp_configs)
        self._grep_mcp_configs = tuple(str(p) for p in grep_mcp_configs)
        self._plugin_dirs = tuple(str(p) for p in plugin_dirs)
        self._permission_mode = permission_mode
        self._cli_path = cli_path or self._find_claude()
        self._timeout_seconds = timeout_seconds
        self._disable_slash_commands = disable_slash_commands
        self._extra_args = tuple(extra_args)

        self._skill_text = skill_text
        if skill_file is not None:
            if skill_text is not None:
                raise AgentAdapterError("skill_text and skill_file are mutually exclusive")
            skill_path = Path(skill_file)
            if not skill_path.is_file():
                raise AgentAdapterError(f"skill_file not found: {skill_path}")
            self._skill_text = skill_path.read_text(encoding="utf-8")

        patterns = tool_name_patterns or ToolNamePatterns()
        self._tool_name_patterns = patterns
        self._compiled = _compile_patterns(patterns)

        self.last_command: list[str] = []
        self.last_tool_policy: str = ""
        self.last_mcp_configs: tuple[str, ...] = ()

        self._validate_paths()

    # -- Public API (AgentAdapter protocol) --------------------------------- #

    def execute(
        self, *, case_id: str, task_type: str, tool_policy: str
    ) -> AgentRunOutcome:
        """Run the agent and return the observed outcome.

        Raises an AgentAdapterError subclass on any failure; the Runner catches
        exceptions and records a failed run.
        """
        if self._case_id and case_id != self._case_id:
            raise AgentAdapterError(
                f"case_id mismatch: constructor has {self._case_id!r}, "
                f"execute received {case_id!r}"
            )
        if self._task_type and task_type != self._task_type:
            raise AgentAdapterError(
                f"task_type mismatch: constructor has {self._task_type!r}, "
                f"execute received {task_type!r}"
            )

        mcp_configs = self._select_mcp_configs(tool_policy)

        if self._prompt_loader is not None:
            prompt = self._prompt_loader(case_id, task_type)
        else:
            assert self._prompt is not None
            prompt = self._prompt
        if not prompt:
            raise AgentAdapterError("prompt resolved to an empty string")

        argv = self.build_command(
            tool_policy=tool_policy, prompt=prompt, mcp_configs=mcp_configs
        )

        self.last_command = self._redact_command(argv)
        self.last_tool_policy = tool_policy
        self.last_mcp_configs = mcp_configs

        stdout, stderr, returncode = self._run_subprocess(argv)

        if returncode != 0:
            raise AgentNonZeroExitError(
                f"claude CLI exited with code {returncode}: "
                f"{self._redact_text(stderr[:500])}"
            )

        if not stdout.strip():
            raise AgentOutputError("claude CLI produced empty stdout")

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
        mcp_configs: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Construct the Claude Code CLI argv for the given policy and prompt.

        Returns a list of strings (subprocess argv, never a shell string). The
        prompt is placed after -- so it is not consumed by the variadic
        --mcp-config option.
        """
        if mcp_configs is None:
            mcp_configs = self._select_mcp_configs(tool_policy)

        args: list[str] = [self._cli_path]
        args.extend(["--print", "--output-format", "stream-json", "--verbose"])
        args.extend(["--model", self._agent_model])
        args.extend(["--permission-mode", self._permission_mode])

        # MCP isolation: --strict-mcp-config ensures ONLY the supplied configs
        # are loaded; project-global MCP servers are ignored. Always passed so
        # grep policy never accidentally loads a global Graph MCP server.
        args.append("--strict-mcp-config")

        if mcp_configs:
            args.append("--mcp-config")
            args.extend(mcp_configs)

        if self._disable_slash_commands:
            args.append("--disable-slash-commands")

        for plugin_dir in self._plugin_dirs:
            args.extend(["--plugin-dir", plugin_dir])

        if self._skill_text:
            # Skill isolation (AIS-012, F2): a Grep-baseline run must receive
            # no skill injection. Fail closed if a skill is configured for a
            # Grep run, mirroring the Graph-MCP guard in _select_mcp_configs.
            # The default dispatch factory prevents this by not passing a
            # skill to Grep adapters; this guard enforces isolation at the
            # command-construction layer too, so a misconfigured Grep run
            # fails fast into a truthful failed run rather than executing
            # with a contaminated baseline.
            if tool_policy == "grep":
                raise AgentPolicyConfigError(
                    "Grep policy must not be configured with a skill; "
                    "fail-closed to prevent skill injection into the Grep "
                    "baseline"
                )
            args.extend(["--append-system-prompt", self._skill_text])

        args.extend(self._extra_args)

        # Prompt after -- so it is a positional argument, not consumed by any
        # variadic option (e.g. --mcp-config).
        args.extend(["--", prompt])

        return args

    # -- MCP config selection (Graph/Grep isolation) ------------------------ #

    def _select_mcp_configs(self, tool_policy: str) -> tuple[str, ...]:
        """Select MCP configs for the declared tool_policy (fail-closed).

        graph  -> only graph_mcp_configs.
        grep   -> grep_mcp_configs, but fail closed if any Graph MCP configs or
                  Graph tool-name patterns are configured.
        mixed  -> graph_mcp_configs + grep_mcp_configs.
        """
        if tool_policy == "graph":
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
            return self._graph_mcp_configs + self._grep_mcp_configs
        raise AgentPolicyConfigError(
            f"unknown tool_policy {tool_policy!r}; expected graph/grep/mixed"
        )

    # -- Tool-name classification -------------------------------------------- #

    def _classify_tool_name(self, name: str) -> ToolKind:
        """Classify a tool name to a ToolKind using configured patterns.

        Checked in order: graph, search, file_read; first match wins. A name
        matching none defaults to ToolKind.OTHER. Never raises.
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

    # -- Stream-json parsing ------------------------------------------------- #

    def _parse_stream(self, stdout: str) -> _StreamParseResult:
        """Parse Claude Code stream-json output defensively.

        The stream is newline-delimited JSON. assistant records carry content
        blocks (text and tool_use); the final result record carries the complete
        result text and usage.

        - The final assistant natural-language text is preserved unchanged as
          raw_response bytes. The result record's result field is authoritative;
          if absent, the last assistant text block is used.
        - Tool-use events are extracted from assistant content blocks and
          classified via _classify_tool_name. Each event is stamped with
          RUNNER_OBSERVED_SOURCE.
        - Usage tokens are extracted from the result record's usage field. If
          absent, tokens default to USAGE_UNAVAILABLE (0).

        Malformed individual lines are skipped. An entirely empty stream (no
        valid records) raises AgentOutputError.
        """
        tool_events: list[ToolEvent] = []
        final_text = ""
        input_tokens = USAGE_UNAVAILABLE
        output_tokens = USAGE_UNAVAILABLE
        valid_records = 0
        result_found = False
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
            record_type = record.get("type")

            if record_type == "assistant":
                self._process_assistant_record(record, tool_events)
                text = self._last_assistant_text(record)
                if text is not None:
                    final_text = text

            elif record_type == "result":
                result_found = True
                subtype = record.get("subtype")
                if isinstance(subtype, str) and subtype != "success":
                    raise AgentOutputError(
                        f"claude CLI result subtype is {subtype!r} "
                        f"(expected 'success'); the agent did not "
                        f"complete normally"
                    )
                result = record.get("result")
                if isinstance(result, str):
                    final_text = result
                usage = record.get("usage")
                if isinstance(usage, dict):
                    it = usage.get("input_tokens")
                    ot = usage.get("output_tokens")
                    if isinstance(it, (int, float)):
                        input_tokens = int(it)
                    if isinstance(ot, (int, float)):
                        output_tokens = int(ot)

        if valid_records == 0:
            raise AgentOutputError(
                f"no valid JSON records in stream-json output "
                f"(encountered {total_lines} non-blank line(s))"
            )

        return _StreamParseResult(
            raw_response=final_text.encode("utf-8"),
            tool_events=tuple(tool_events),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            result_found=result_found,
        )

    def _process_assistant_record(
        self, record: dict[str, Any], tool_events: list[ToolEvent]
    ) -> None:
        """Extract tool-use events from an assistant record's content blocks."""
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if isinstance(name, str) and name:
                    kind = self._classify_tool_name(name)
                    tool_events.append(
                        ToolEvent(kind=kind, source=RUNNER_OBSERVED_SOURCE, label=name)
                    )

    @staticmethod
    def _last_assistant_text(record: dict[str, Any]) -> str | None:
        """Return the concatenated text of an assistant record's text blocks."""
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, list):
            return None
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    texts.append(text)
        if not texts:
            return None
        return "".join(texts)

    # -- Subprocess execution ------------------------------------------------ #

    def _run_subprocess(self, argv: list[str]) -> tuple[str, str, int]:
        """Run the CLI subprocess and return (stdout, stderr, returncode).

        Uses subprocess argv (never shell) to prevent shell injection. Raises
        AgentTimeoutError on timeout and AgentLaunchError on launch failure.

        Launch errors: OSError (the base of FileNotFoundError, PermissionError
        and WinError 193 "not a valid Win32 application") is caught and
        converted to AgentLaunchError so an extensionless npm shim or missing
        executable surfaces as a truthful failed run, not an uncaught exception.

        Encoding: Claude Code stream-json is UTF-8. Output is captured as raw
        bytes and decoded as UTF-8 with errors='replace' so a malformed byte
        never crashes the adapter (invalid bytes become U+FFFD and are
        subsequently skipped by the stream parser's malformed-line handling).
        This avoids the locale-dependent cp936 decoding that text=True would
        use on Windows, which raises UnicodeDecodeError on non-ASCII (e.g.
        Chinese) output.
        """
        cwd = str(self._repo_cwd) if self._repo_cwd is not None else None
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self._timeout_seconds,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeoutError(
                f"claude CLI timed out after {self._timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise AgentLaunchError(
                f"cannot launch claude CLI at {self._cli_path!r}: {exc}"
            ) from exc

        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        return stdout, stderr, result.returncode

    # -- Path validation ----------------------------------------------------- #

    def _validate_paths(self) -> None:
        """Validate all configured paths exist (fail fast on misconfiguration)."""
        if self._repo_cwd is not None:
            if not self._repo_cwd.is_dir():
                raise AgentAdapterError(f"repo_cwd is not a directory: {self._repo_cwd}")
        for config in self._graph_mcp_configs:
            if not Path(config).is_file():
                raise AgentAdapterError(
                    f"graph MCP config not found or not a file: {config}"
                )
        for config in self._grep_mcp_configs:
            if not Path(config).is_file():
                raise AgentAdapterError(
                    f"grep MCP config not found or not a file: {config}"
                )
        for plugin_dir in self._plugin_dirs:
            if not Path(plugin_dir).exists():
                raise AgentAdapterError(f"plugin_dir not found: {plugin_dir}")

    # -- CLI detection ------------------------------------------------------- #

    @staticmethod
    def _find_claude() -> str:
        """Locate the claude executable on the system.

        Uses shutil.which, which is PATHEXT-aware on Windows and returns a real
        executable path (e.g. claude.cmd) rather than the first line of
        ``where claude``. On this host the first ``where`` hit is an
        extensionless npm shim that raises WinError 193 when executed;
        shutil.which skips it in favour of a PATHEXT-matching file. Returns
        ``"claude"`` as a fallback when nothing is found, so the eventual
        launch failure surfaces as an AgentLaunchError at run time.
        """
        return shutil.which("claude") or "claude"

    # -- Redaction (S13.6: no prompt/secret content in audit logs) ---------- #

    @staticmethod
    def _redact_text(text: str) -> str:
        """Redact known secret patterns from text for safe error messages."""
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _redact_command(self, argv: list[str]) -> list[str]:
        """Return a redacted copy of argv for audit (prompt/secrets hidden).

        The prompt (after --) and --append-system-prompt value are replaced
        with a length placeholder. MCP config paths, plugin dirs, model, and
        permission mode are non-secret and preserved.
        """
        redacted: list[str] = []
        skip_next = False
        for i, arg in enumerate(argv):
            if skip_next:
                skip_next = False
                redacted.append(f"<redacted:{len(arg)} chars>")
                continue
            if arg == "--append-system-prompt":
                redacted.append(arg)
                skip_next = True
                continue
            if arg == "--":
                redacted.append(arg)
                if i + 1 < len(argv):
                    prompt = argv[i + 1]
                    redacted.append(f"<prompt:{len(prompt)} chars>")
                break
            redacted.append(arg)
        return redacted
