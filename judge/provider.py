"""Judge Provider interface and Claude Code CLI adapter
(docs/ai-scoring-design.md §13.3, §13.4, §13.5, §13.6).

The provider layer abstracts Judge execution so the runner never depends on a
specific model or CLI. Production uses ``ClaudeCodeCliProvider``; tests use
:class:`FakeCliProvider` (see ``tests/judge/test_provider.py``).

The adapter inspects the installed ``claude --help`` once at init time to
select only flags the current CLI version actually supports (acceptance:
"adapter 在实现环境中读取 claude --help 后选择非交互参数"). Generation
parameters that are requested but unsupported by the current CLI are logged
and skipped, never silently assumed to work.

Credentials (§13.6): the CLI process inherits the global Claude Code session;
no key, token or password is passed as a CLI argument or environment variable
by this adapter. All stdout/stderr/exception text is redacted before
persistence.
"""

from __future__ import annotations

import abc
import dataclasses
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

# --- Constants ------------------------------------------------------------- #

#: Default judge model (design §13.3).
DEFAULT_JUDGE_MODEL = "glm-5.2"

#: Default Judge prompt digest placeholder (replaced by the prompt layer).
DEFAULT_PROMPT_DIGEST = "sha256:" + "d" * 64

#: Default generation parameters.
DEFAULT_GENERATION_PARAMS: dict[str, Any] = {
    "temperature": 0.0,
    "seed": 42,
    "top_p": 1.0,
}

#: Default timeout per Judge call (milliseconds).
DEFAULT_JUDGE_TIMEOUT_MS = 300000

#: Sentinel value for "effective model cannot be verified" (R1).
UNVERIFIABLE_MODEL = "unverifiable"

#: Regex to extract the real ``.exe`` path from a ``.CMD``/``.BAT`` wrapper.
_CMD_EXE_RE = re.compile(r'"([^"]*\.exe)"', re.IGNORECASE)

#: Patterns whose matches in output are replaced with ``<REDACTED>``.
#: Patterns whose matches in output are replaced with ``<REDACTED>``.
#: Each entry is ``(pattern, replacement_template)``.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(api[_-]?key|token|password|secret|credential)[=:]\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)(Authorization|X-API-Key):\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"\b(eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)\b"), r"<REDACTED>"),
]


# --- Exceptions ------------------------------------------------------------ #

class JudgeProviderError(Exception):
    """Provider-level error (CLI not found, timeout, non-zero exit, etc.)."""


class JudgeAuthenticationError(JudgeProviderError):
    """The provider is not authenticated or credentials are invalid (§13.6)."""


class JudgeTimeoutError(JudgeProviderError):
    """The Judge call exceeded its timeout (§13.5)."""


class JudgeOutputError(JudgeProviderError):
    """The Judge returned non-JSON or invalid JSON output."""


# --- Data model ------------------------------------------------------------ #

@dataclasses.dataclass(frozen=True)
class JudgeProviderConfig:
    """Provider configuration; all fields are non-secret (§13.6)."""

    judge_model: str = DEFAULT_JUDGE_MODEL
    timeout_ms: int = DEFAULT_JUDGE_TIMEOUT_MS
    generation_params: Mapping[str, Any] = dataclasses.field(
        default_factory=lambda: dict(DEFAULT_GENERATION_PARAMS)
    )


@dataclasses.dataclass(frozen=True)
class CliInfo:
    """Information about the installed ``claude`` CLI."""

    version: str
    supported_flags: frozenset[str]
    unsupported_params: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class JudgeCallResult:
    """The result of a single Judge invocation."""

    success: bool
    label: str
    judge_output: dict[str, Any] | None
    raw_stdout: str
    raw_stderr: str
    cli_version: str
    requested_model: str
    effective_model: str
    generation_params: Mapping[str, Any]
    prompt_digest: str
    elapsed_ms: int
    retry_count: int
    failed: bool
    failure_reason: str | None
    retry_exhausted: bool


@dataclasses.dataclass(frozen=True)
class JudgeCallParams:
    """Parameters for a single Judge call."""

    label: str
    blind_input: Mapping[str, Any]
    prompt_text: str
    prompt_digest: str
    judge_model: str
    generation_params: Mapping[str, Any]
    timeout_ms: int


# --- Provider interface ---------------------------------------------------- #

class JudgeProvider(abc.ABC):
    """Abstract Judge provider; every provider must be stateless and reusable."""

    @abc.abstractmethod
    def call(self, params: JudgeCallParams) -> JudgeCallResult:
        """Execute one Judge call and return the result."""

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Provider identifier (e.g. ``"claude-code-cli"``)."""

    @property
    @abc.abstractmethod
    def cli_version(self) -> str:
        """Installed CLI version string."""

    @property
    @abc.abstractmethod
    def cli_info(self) -> CliInfo:
        """CLI detection result (version, supported flags)."""

    @property
    @abc.abstractmethod
    def effective_model(self) -> str:
        """The actual model that will be used (must match requested_model)."""


# --- Secret redaction ------------------------------------------------------ #

def redact_secrets(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --- Claude Code CLI provider ---------------------------------------------- #

class ClaudeCodeCliProvider(JudgeProvider):
    """Production Judge provider that calls ``claude`` as a subprocess.

    The adapter inspects the installed CLI's ``--help`` output once at init
    time. Flags that the current CLI does not advertise are never used; the
    caller is warned via ``unsupported_params`` but the call still proceeds
    with the subset it supports (acceptance: "仅传入当前 CLI/模型实际支持的生成
    参数，并记录不支持项").

    Credentials (§13.6): the subprocess inherits the parent's environment and
    global Claude Code session. No secrets are passed as CLI arguments.
    """

    #: Known flags that control generation. Only those present in --help are used.
    _KNOWN_GEN_FLAGS: dict[str, str] = {
        "temperature": "--temperature",
        "seed": "--seed",
        "top_p": "--top-p",
    }

    def __init__(self, config: JudgeProviderConfig | None = None) -> None:
        self._config = config or JudgeProviderConfig()
        self._cli_version: str = ""
        self._supported_flags: frozenset[str] = frozenset()
        self._unsupported_params: tuple[str, ...] = ()
        self._effective_model: str = self._config.judge_model
        self._probe_cli()

    def _probe_cli(self) -> None:
        try:
            result = subprocess.run(
                [self._find_claude(), "--help"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            help_text = result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JudgeProviderError(
                f"cannot probe claude CLI: {exc}"
            ) from exc

        supported: set[str] = set()
        for line in help_text.splitlines():
            line = line.strip()
            if line.startswith("--") and not line.startswith("---"):
                flag = line.split()[0]
                supported.add(flag)

        self._supported_flags = frozenset(supported)

        unsupported: list[str] = []
        for name, flag in self._KNOWN_GEN_FLAGS.items():
            if flag not in supported:
                unsupported.append(name)
        self._unsupported_params = tuple(unsupported)

        self._cli_version = self._detect_version(help_text)

    def _detect_version(self, help_text: str) -> str:
        try:
            result = subprocess.run(
                [self._find_claude(), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            version = (result.stdout or result.stderr).strip()
            if version:
                return version.splitlines()[0].strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    @staticmethod
    def _find_claude() -> str:
        """Locate the claude executable.

        Uses ``shutil.which`` (PATHEXT-aware on Windows) instead of the raw
        ``where`` command so the extensionless npm shim that raises WinError
        193 is skipped.  When a ``.CMD``/``.BAT`` wrapper is found, resolve
        the real ``.exe`` it wraps so ``cmd.exe`` does not interpret special
        characters in CLI arguments (e.g. the Judge prompt).
        """
        found = shutil.which("claude")
        if not found:
            return "claude"
        if found.lower().endswith((".cmd", ".bat")):
            resolved = ClaudeCodeCliProvider._resolve_cmd_wrapper(found)
            if resolved:
                return resolved
        return found

    @staticmethod
    def _resolve_cmd_wrapper(cmd_path: str) -> str | None:
        """Parse a ``.CMD``/``.BAT`` wrapper to find the real ``.exe`` path."""
        try:
            text = Path(cmd_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        wrapper_dir = Path(cmd_path).resolve().parent
        for line in text.splitlines():
            match = _CMD_EXE_RE.search(line)
            if match:
                spec = match.group(1)
                spec = spec.replace("%dp0%", str(wrapper_dir))
                spec = spec.replace("%~dp0", str(wrapper_dir))
                exe_path = Path(spec)
                if not exe_path.is_absolute():
                    exe_path = wrapper_dir / exe_path
                if exe_path.is_file():
                    return str(exe_path)
        return None

    def _build_cli_args(self, params: JudgeCallParams) -> list[str]:
        args = [self._find_claude(), "--print", "--output-format", "json"]

        requested = params.judge_model
        if "--model" in self._supported_flags:
            args.extend(["--model", requested])
            self._effective_model = requested
        else:
            self._effective_model = UNVERIFIABLE_MODEL

        if "--json-schema" in self._supported_flags:
            schema = _build_output_schema()
            args.extend(["--json-schema", json.dumps(schema)])

        if "--dangerously-skip-permissions" in self._supported_flags:
            args.append("--dangerously-skip-permissions")

        if "--permission-mode" in self._supported_flags:
            args.extend(["--permission-mode", "bypassPermissions"])

        gen = dict(params.generation_params)
        for name, flag in self._KNOWN_GEN_FLAGS.items():
            if name in gen and flag in self._supported_flags:
                value = gen.pop(name)
                if isinstance(value, float):
                    args.extend([flag, str(value)])
                else:
                    args.extend([flag, str(value)])

        if "--max-budget-usd" in self._supported_flags:
            args.extend(["--max-budget-usd", "1"])

        args.append(params.prompt_text)
        return args

    def call(self, params: JudgeCallParams) -> JudgeCallResult:
        start = time.monotonic()
        retry_count = 0
        last_error: str | None = None

        for attempt in range(2):
            try:
                cli_args = self._build_cli_args(params)

                proc = subprocess.run(
                    cli_args,
                    capture_output=True,
                    text=True,
                    timeout=params.timeout_ms / 1000,
                    env={**os.environ, "CLAUDE_CODE_SIMPLE": "1"},
                )

                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                elapsed = int((time.monotonic() - start) * 1000)

                if proc.returncode != 0:
                    stderr_lower = stderr.lower()
                    auth_keywords = ("auth" in stderr_lower or "login" in stderr_lower
                        or "credential" in stderr_lower)
                    if auth_keywords:
                        raise JudgeAuthenticationError(
                            f"CLI authentication error: {redact_secrets(stderr[:500])}"
                        )
                    if attempt == 0:
                        retry_count += 1
                        last_error = f"non-zero exit {proc.returncode}"
                        continue
                    return JudgeCallResult(
                        success=False,
                        label=params.label,
                        judge_output=None,
                        raw_stdout=redact_secrets(stdout),
                        raw_stderr=redact_secrets(stderr),
                        cli_version=self._cli_version,
                        requested_model=params.judge_model,
                        effective_model=self._effective_model,
                        generation_params=dict(params.generation_params),
                        prompt_digest=params.prompt_digest,
                        elapsed_ms=elapsed,
                        retry_count=retry_count,
                        failed=True,
                        failure_reason=f"non-zero exit {proc.returncode}",
                        retry_exhausted=attempt > 0,
                    )

                output = self._parse_output(stdout)
                return JudgeCallResult(
                    success=True,
                    label=params.label,
                    judge_output=output,
                    raw_stdout=redact_secrets(stdout),
                    raw_stderr=redact_secrets(stderr),
                    cli_version=self._cli_version,
                    requested_model=params.judge_model,
                    effective_model=self._effective_model,
                    generation_params=dict(params.generation_params),
                    prompt_digest=params.prompt_digest,
                    elapsed_ms=elapsed,
                    retry_count=retry_count,
                    failed=False,
                    failure_reason=None,
                    retry_exhausted=False,
                )

            except subprocess.TimeoutExpired:
                elapsed = int((time.monotonic() - start) * 1000)
                if attempt == 0:
                    retry_count += 1
                    last_error = "timeout"
                    continue
                return JudgeCallResult(
                    success=False,
                    label=params.label,
                    judge_output=None,
                    raw_stdout="",
                    raw_stderr="",
                    cli_version=self._cli_version,
                    requested_model=params.judge_model,
                    effective_model=self._effective_model,
                    generation_params=dict(params.generation_params),
                    prompt_digest=params.prompt_digest,
                    elapsed_ms=elapsed,
                    retry_count=retry_count,
                    failed=True,
                    failure_reason="timeout",
                    retry_exhausted=True,
                )

            except JudgeAuthenticationError:
                elapsed = int((time.monotonic() - start) * 1000)
                return JudgeCallResult(
                    success=False,
                    label=params.label,
                    judge_output=None,
                    raw_stdout="",
                    raw_stderr="",
                    cli_version=self._cli_version,
                    requested_model=params.judge_model,
                    effective_model=self._effective_model,
                    generation_params=dict(params.generation_params),
                    prompt_digest=params.prompt_digest,
                    elapsed_ms=elapsed,
                    retry_count=0,
                    failed=True,
                    failure_reason="judge_unavailable",
                    retry_exhausted=False,
                )

            except (subprocess.CalledProcessError, OSError) as exc:
                if attempt == 0:
                    retry_count += 1
                    last_error = str(exc)
                    continue
                return JudgeCallResult(
                    success=False,
                    label=params.label,
                    judge_output=None,
                    raw_stdout="",
                    raw_stderr=str(exc),
                    cli_version=self._cli_version,
                    requested_model=params.judge_model,
                    effective_model=self._effective_model,
                    generation_params=dict(params.generation_params),
                    prompt_digest=params.prompt_digest,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    retry_count=retry_count,
                    failed=True,
                    failure_reason=str(exc),
                    retry_exhausted=True,
                )

        return JudgeCallResult(
            success=False,
            label=params.label,
            judge_output=None,
            raw_stdout="",
            raw_stderr="",
            cli_version=self._cli_version,
            requested_model=params.judge_model,
            effective_model=self._effective_model,
            generation_params=dict(params.generation_params),
            prompt_digest=params.prompt_digest,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            retry_count=retry_count,
            failed=True,
            failure_reason=last_error or "retry_exhausted",
            retry_exhausted=True,
        )

    def _parse_output(self, stdout: str) -> dict[str, Any]:
        stdout = stdout.strip()
        if not stdout:
            raise JudgeOutputError("empty stdout from CLI")
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise JudgeOutputError(f"invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise JudgeOutputError(f"expected JSON object, got {type(parsed).__name__}")
        return parsed

    @property
    def provider_name(self) -> str:
        return "claude-code-cli"

    @property
    def cli_version(self) -> str:
        return self._cli_version

    @property
    def cli_info(self) -> CliInfo:
        return CliInfo(
            version=self._cli_version,
            supported_flags=self._supported_flags,
            unsupported_params=self._unsupported_params,
        )

    @property
    def effective_model(self) -> str:
        return self._effective_model


# --- Fake CLI provider (for tests) ----------------------------------------- #

class FakeCliProvider(JudgeProvider):
    """Fake provider for tests; never calls a real CLI.

    Pass ``judge_output`` to return a fixed Judge response, or ``fail_mode``
    to simulate error conditions.
    """

    def __init__(
        self,
        judge_output: dict[str, Any] | None = None,
        *,
        fail_mode: str | None = None,
        cli_version: str = "2.1.220",
        effective_model: str = "glm-5.2",
    ) -> None:
        self._judge_output = judge_output
        self._fail_mode = fail_mode
        self._cli_version = cli_version
        self._effective_model = effective_model
        self._calls: list[JudgeCallParams] = []

    @property
    def calls(self) -> list[JudgeCallParams]:
        return list(self._calls)

    def call(self, params: JudgeCallParams) -> JudgeCallResult:
        self._calls.append(params)
        start = time.monotonic()
        elapsed = int((time.monotonic() - start) * 1000)

        if self._fail_mode == "auth":
            return JudgeCallResult(
                success=False,
                label=params.label,
                judge_output=None,
                raw_stdout="",
                raw_stderr="authentication required",
                cli_version=self._cli_version,
                requested_model=params.judge_model,
                effective_model=self._effective_model,
                generation_params=dict(params.generation_params),
                prompt_digest=params.prompt_digest,
                elapsed_ms=elapsed,
                retry_count=0,
                failed=True,
                failure_reason="judge_unavailable",
                retry_exhausted=False,
            )

        if self._fail_mode == "timeout":
            return JudgeCallResult(
                success=False,
                label=params.label,
                judge_output=None,
                raw_stdout="",
                raw_stderr="",
                cli_version=self._cli_version,
                requested_model=params.judge_model,
                effective_model=self._effective_model,
                generation_params=dict(params.generation_params),
                prompt_digest=params.prompt_digest,
                elapsed_ms=elapsed + 300000,
                retry_count=1,
                failed=True,
                failure_reason="timeout",
                retry_exhausted=True,
            )

        if self._fail_mode == "retry_exhausted":
            return JudgeCallResult(
                success=False,
                label=params.label,
                judge_output=None,
                raw_stdout="",
                raw_stderr="CLI error",
                cli_version=self._cli_version,
                requested_model=params.judge_model,
                effective_model=self._effective_model,
                generation_params=dict(params.generation_params),
                prompt_digest=params.prompt_digest,
                elapsed_ms=elapsed,
                retry_count=1,
                failed=True,
                failure_reason="non-zero exit 1",
                retry_exhausted=True,
            )

        if self._fail_mode == "invalid_json":
            return JudgeCallResult(
                success=True,
                label=params.label,
                judge_output=None,
                raw_stdout="not json at all",
                raw_stderr="",
                cli_version=self._cli_version,
                requested_model=params.judge_model,
                effective_model=self._effective_model,
                generation_params=dict(params.generation_params),
                prompt_digest=params.prompt_digest,
                elapsed_ms=elapsed,
                retry_count=0,
                failed=True,
                failure_reason="invalid_json",
                retry_exhausted=False,
            )

        if self._fail_mode == "non_dict_output":
            return JudgeCallResult(
                success=True,
                label=params.label,
                judge_output=None,
                raw_stdout='"just a string"',
                raw_stderr="",
                cli_version=self._cli_version,
                requested_model=params.judge_model,
                effective_model=self._effective_model,
                generation_params=dict(params.generation_params),
                prompt_digest=params.prompt_digest,
                elapsed_ms=elapsed,
                retry_count=0,
                failed=True,
                failure_reason="expected JSON object",
                retry_exhausted=False,
            )

        return JudgeCallResult(
            success=True,
            label=params.label,
            judge_output=self._judge_output,
            raw_stdout=json.dumps(self._judge_output) if self._judge_output else "{}",
            raw_stderr="",
            cli_version=self._cli_version,
            requested_model=params.judge_model,
            effective_model=self._effective_model,
            generation_params=dict(params.generation_params),
            prompt_digest=params.prompt_digest,
            elapsed_ms=elapsed,
            retry_count=0,
            failed=False,
            failure_reason=None,
            retry_exhausted=False,
        )

    @property
    def provider_name(self) -> str:
        return "fake-cli"

    @property
    def cli_version(self) -> str:
        return self._cli_version

    @property
    def cli_info(self) -> CliInfo:
        return CliInfo(
            version=self._cli_version,
            supported_flags=frozenset({
                "--print", "--model", "--json-schema", "--output-format",
                "--dangerously-skip-permissions", "--permission-mode",
                "--max-budget-usd",
            }),
        )

    @property
    def effective_model(self) -> str:
        return self._effective_model


# --- Output schema helper -------------------------------------------------- #

def _build_output_schema() -> dict[str, Any]:
    """Build the JSON Schema that constrains Judge output to the frozen format."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version", "judge_protocol", "scoring_profile",
            "items", "unsupported_claims", "critical_errors",
            "overall_confidence", "requires_human_review",
        ],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": "judge-output-v1"},
            "judge_protocol": {"const": "semantic_outcome_v1"},
            "scoring_profile": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["item_id", "credit", "verdict", "reason", "confidence"],
                    "properties": {
                        "item_id": {"type": "string"},
                        "credit": {"enum": [0, 0.25, 0.5, 0.75, 1]},
                        "verdict": {"type": "string"},
                        "answer_evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["json_pointer", "quote"],
                                "properties": {
                                    "json_pointer": {"type": "string"},
                                    "quote": {"type": "string"},
                                },
                            },
                        },
                        "reason": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "unsupported_claims": {"type": "array", "items": {"type": "object"}},
            "critical_errors": {"type": "array", "items": {"type": "object"}},
            "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "requires_human_review": {"type": "boolean"},
        },
    }