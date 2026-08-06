# AIS-013: OpenCode AgentAdapter

State: INTEGRATED

## Objective

Implement a concrete `OpenCodeAgentAdapter` for the Runner so an experiment can execute a Case through the locally installed OpenCode CLI while preserving Runner-observed raw response, tool events, token usage, model selection, failure evidence, and Graph/Grep policy isolation.

## Source of truth

- Design: `docs/ai-scoring-design.md` §§8.6–8.8, §13.6, §15, §17–§18, §20
- Existing contract: `runner/benchmark_runner.py::AgentAdapter` and `AgentRunOutcome`
- Reference implementation: `runner/claude_code_adapter.py` (behavioral reference, not a requirement to duplicate Claude-specific flags)
- Installed OpenCode: `1.18.13`
- Base: `09ef16d4367857960732d4cda1af3a3e7fb4f352`
- Dependencies: AIS-009 and AIS-012 Claude Code AgentAdapter are INTEGRATED

## Execution envelope

- Executor: Claude Code (`glm-5.2`, automatic permission mode)
- Working directory: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-013-opencode-adapter`
- Branch: `codex/ais-013-opencode-adapter`
- Expected HEAD: `09ef16d4367857960732d4cda1af3a3e7fb4f352`
- Return channel: strict terminal `AGENT_RESULT`

## Invariants

- Launch OpenCode non-interactively using locally supported flags: `opencode run --format json --model <provider/model> --dir <repo> --auto`; default model is `ark-plan-qlw/deepseek-v4-flash` and auto approval is enabled by default but configurable.
- Discover Windows npm shims safely, including `opencode.ps1`, and invoke them without shell-string interpolation.
- Parse OpenCode NDJSON events independently: final text becomes `raw_response`; completed tool parts become Runner-observed `ToolEvent`s; step-finish token data supplies input/output usage. Missing usage is explicitly `0`, never invented.
- Classify OpenCode built-ins (`read`, `grep`, `glob`, `list`, `bash`) and MCP tool names (`<server>_<tool>`) through configurable patterns.
- Graph/Grep separation fails closed before launch. A Grep run cannot receive or invoke Graph MCP tools; a Graph run must receive an explicit Graph MCP configuration.
- Per-run OpenCode configuration is injected through a subprocess-only environment override such as `OPENCODE_CONFIG_CONTENT`. It uses deny-by-default permissions and only enables the current condition's built-ins/MCP namespace. Global/project configuration must not silently authorize an undeclared MCP tool.
- Support the existing Claude-style `mcpServers` JSON shape and native OpenCode `mcp` configuration shape by normalizing them into a runtime-only OpenCode config. Never persist resolved credential values or place them in command arguments/audit fields.
- The child process inherits the user's global OpenCode provider authentication; the adapter does not inspect, print, copy, or write credential values.
- `--pure` should be used when compatible to prevent undeclared external plugins. Explicit skill text/file may be prepended to the prompt, while the native global/project skill tool remains denied unless explicitly supported in a later task.
- Timeouts, launch failures, non-zero exit, malformed/empty NDJSON, missing final text, invalid paths/config, duplicate MCP server names, and error events are deterministic auditable adapter errors.
- No real OpenCode model call, MCP launch, Judge call, credential probe, experiment dispatch, or external repository mutation is allowed in this task.

## Allowed scope

- `runner/opencode_adapter.py`
- `tests/runner/test_opencode_adapter.py`
- Minimal package export changes only if required for importability

## Excluded scope

- `runner/experiment_dispatch.py`, experiment YAML, formal configuration, Case/GT, scoring, Judge, report, schemas, profiles
- Refactoring or changing `ClaudeCodeAgentAdapter`
- Live OpenCode/Provider/MCP execution

## Acceptance criteria

- `OpenCodeAgentAdapter` implements the existing `AgentAdapter` protocol without changing Runner artifact contracts.
- Command construction matches local OpenCode `1.18.13` help and redacts prompt/config/secret-bearing data from audit and exception text.
- Fake-CLI tests cover final text, multiple text/tool events, tool classification, token summation, missing usage, malformed/error streams, timeout/non-zero/launch failures, UTF-8, Windows shim discovery, model/dir/auto/pure flags, and identity mismatch.
- Tests prove Graph/Grep isolation and normalization of both supported MCP config shapes, including duplicate/invalid/secret-bearing configurations without leaking values.
- `.venv\Scripts\python.exe -m pytest tests/runner/test_opencode_adapter.py -q`, the full test suite, Ruff, and `git diff --check` pass.

## Delivery contract

- One scoped implementation commit.
- Strict `AGENT_RESULT` with base/head, changed files, acceptance evidence, exact checks, deviations, open questions, and residual risks.

## Implementation result

- Commit: `425caa27316bf5bcce47607a2ce6bff03ecd440a` based on the fixed `09ef16d4367857960732d4cda1af3a3e7fb4f352` revision.
- Scope: only `runner/opencode_adapter.py` and `tests/runner/test_opencode_adapter.py` changed; the worktree is clean.
- Verification reported by the executor: 83 focused tests and 904 full-suite tests passed; Ruff and diff checks passed.
- No live OpenCode model, MCP, Provider authentication, or Judge call occurred.

## Review 1 result

- Verdict: `PASS_WITH_NOTES`, treated as `CHANGES_REQUIRED` because four findings are reproducible implementation defects.
- Accepted remediation: preserve validated remote MCP headers; count distinct id-less tool calls; ignore tool parts from non-assistant messages; redact the adapter-owned prompt separator correctly; set the supported project-config-disable environment guard for stronger isolation.
- Deferred live-contract risks: real OpenCode NDJSON shapes, per-step versus cumulative token semantics, and actual permission behavior under `--auto` require a separately authorized bounded live model probe.

## Remediation 1 result

- Commit: `cff1a88890229bcfb39aab7ba21c61f8fac94ac3`, based on `425caa27316bf5bcce47607a2ce6bff03ecd440a`.
- AIS013-R1 through AIS013-R5 resolved with regression tests; only the two allowed files changed.
- Verification: 96 focused tests, 917 full-suite tests, Ruff format/check, and diff check passed. Worktree clean.

## Review 3 result

- Verdict: `PASS_WITH_NOTES` from an independent Claude Code (`glm-5.2`) review of the cumulative range `09ef16d4367857960732d4cda1af3a3e7fb4f352..cff1a88890229bcfb39aab7ba21c61f8fac94ac3`.
- Protocol conformance, Windows subprocess handling, JSON parsing, tool/token accounting, MCP isolation, R1-R5 remediation, strict scope, focused/full tests, Ruff, and diff checks all passed.
- Deferred note: stderr redaction may not cover every JSON-quoted or Bearer-token form if OpenCode echoes `OPENCODE_CONFIG_CONTENT`; this is reserved for authorized live testing and is not an integration blocker.

## Integration result

- Integrated commits: `d11db0c` (implementation) and `2f222a0` (R1-R5 remediation).
- Main-branch verification: 96 focused tests and 938 full-suite tests passed.
- No live OpenCode/provider/model call was performed; remaining runtime-contract risks are reserved for user-authorized measurement.
