# AIS-012 Claude Code AgentAdapter

State: IMPLEMENTING

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
