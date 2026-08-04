# AIS-012 Claude Code AgentAdapter

State: INTEGRATED

## Objective

Implement a parameterized Claude Code CLI adapter for the Runner so a Case can be executed against a local repository under a declared Graph or Grep condition while preserving Runner-observed raw output, tool events, token usage and elapsed time.

## Fixed base

- Base: `12e8077` (`ai-score-v1`)
- Worktree: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-012-agent-adapter`
- Executor: Claude Code (`glm-5.2`, automatic permission mode)

## Required configuration contract

- Case prompt source/path and case/task identity are configuration inputs.
- Agent model is an explicit constructor/config value; do not confuse it with the Judge model.
- Target repository is an explicit local `cwd`/workspace path.
- MCP JSON paths are explicit inputs. Graph runs use `--mcp-config` plus `--strict-mcp-config`; Grep runs must not receive Graph MCP configuration.
- Skill/plugin paths are explicit inputs. A `.skill` file may be supplied through the adapter's documented prompt/plugin mechanism; do not silently load project-global skills.
- Tool-name classification is configurable (Graph/MCP patterns, Grep/search patterns, file-read patterns), not guessed from answer text.

## Invariants

- Launch Claude Code non-interactively with the configured prompt/model/cwd and version-compatible flags; never put credentials in arguments or artifacts.
- Prefer `--output-format stream-json` so the adapter can preserve the final assistant text and parse observed tool-use/usage events. If the installed CLI cannot provide a required field, record an explicit unavailable value rather than inventing it.
- Every emitted `ToolEvent` uses the Runner observation source and derives from parsed CLI events, never agent self-report.
- Graph/Grep policy separation is enforced by configuration and strict MCP loading; the adapter must fail closed for invalid configuration.
- Natural-language final output is allowed and is passed unchanged as `raw_response` to existing `runner.execution`; the adapter must not fabricate `agent-answer.json`.
- Process failures, malformed stream records, unavailable token usage and non-zero exit are auditable exceptions/results, not silent success.
- No real formal case, Judge call, credential check, or external repository mutation is part of this task.

## Acceptance criteria

- A concrete `ClaudeCodeAgentAdapter` implements `runner.benchmark_runner.AgentAdapter` without changing existing Runner artifact contracts.
- Unit tests use a fake Claude executable/stream and cover command construction, prompt/cwd/model propagation, Graph-vs-Grep MCP isolation, skill/plugin propagation, stream parsing, tool event classification, final text, token usage and failure paths.
- The adapter rejects unsafe/missing paths and invalid policy configuration deterministically.
- Focused tests, full suite, Ruff and diff check pass.

## Delivery contract

- Scoped commit plus strict `AGENT_RESULT` with command/flag evidence, parser limitations, and explicit statement that no real Claude session or Judge call was made.

## Execution result

- Executor result: `READY_FOR_REVIEW` at `3fd2e04744e222b21cba6fbbec80ba58b8153840` (base `12e8077f58e5f90721e610cdccbcb0538e9f92b3`).
- Checks: Adapter tests 54 passed; full suite 760 passed; Ruff and diff check passed.
- Real Claude service was not invoked. The adapter is not yet wired to a CLI subcommand or formal experiment dispatch.

## Review 1 result

- Verdict: `CHANGES_REQUIRED` for `3fd2e04744e222b21cba6fbbec80ba58b8153840`.
- Blocking findings: Windows executable discovery may select the extensionless npm shim (WinError 193); CLI output is decoded with the locale instead of explicit UTF-8, so Chinese output fails under cp936.
- Required remediation: use Windows-safe executable discovery, force UTF-8 decoding with a documented error policy, catch launch `OSError` as an adapter error, and add regression tests for default discovery, non-ASCII output and Grep MCP path validation.

## Remediation result

- Executor result: `READY_FOR_REVIEW` at `65012a6475baa8f6eb578d572f6fd6a34432c689` (base `3fd2e04744e222b21cba6fbbec80ba58b8153840`).
- Fixed: PATHEXT-aware `shutil.which` discovery, explicit UTF-8 byte decoding, launch `OSError` conversion, default discovery/Grep-path regressions, and non-success stream subtype handling.
- Checks: focused adapter suite 60 passed; full suite 766 passed; Ruff and diff check passed. No real Claude or Judge call was made.

## Review 2 result

- Verdict: `PASS` for `3fd2e04744e222b21cba6fbbec80ba58b8153840..65012a6475baa8f6eb578d572f6fd6a34432c689`.
- No blocking findings. Windows executable discovery, UTF-8 decoding, launch error conversion and stream subtype handling were independently verified; prior Graph/Grep isolation and Runner contracts remain intact.
- Residual P3 risks: permissive missing subtype handling, deliberate `errors="replace"` behavior for corrupted bytes, and a cosmetic CLI version reference in the module docstring.
- Integration: remediation files integrated to `ai-score-v1` as `4a2bd6f`; post-integration full suite `766 passed`, Ruff and diff checks passed.
