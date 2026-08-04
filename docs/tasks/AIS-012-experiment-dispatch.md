# AIS-012 configuration-driven experiment dispatch

State: IMPLEMENTING

## Objective

Add a configuration-driven dispatch layer that loads an experiment YAML, validates its Case/GT and execution conditions, constructs `ClaudeCodeAgentAdapter` instances, and invokes the existing `execute_run` lifecycle for declared Graph/Grep conditions and repeats.

## Fixed base

- Base: `8a227d795d1a9468838d47129c43afb0804a41c9`
- Worktree: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-012-experiment-dispatch`
- Executor: Claude Code (`glm-5.2`, automatic permission mode)

## Required boundaries

- Keep the current `qwenpaw-corrupt-inbox-smoke-v1.yaml` explicitly non-executable (`status: smoke_only`, no adapter/repo credentials). It must be loadable for validation but refused for real execution unless an explicit, documented execution mode and complete runtime fields are supplied.
- Runtime fields must be config-driven: case prompt/path, task identity, agent model, repo cwd, Graph/Grep MCP JSON paths, skill/plugin inputs, permission mode, repeats and output root. No hard-coded user or repository paths.
- Validate Case schema and production Ground Truth/Profile before any subprocess launch. Reject missing/invalid repo cwd, MCP/skill paths, unsupported policy/condition, missing required runtime fields and unsafe output/run IDs deterministically.
- Graph/Grep conditions must construct the existing Adapter with the matching MCP/skill inputs and call `execute_run`; do not bypass Runner artifact/policy validation.
- Provide validate-only/dry-run behavior and an explicit guard against accidentally running smoke-only configs. Tests must never call real Claude, Judge, MCP or external repositories.
- Do not implement Judge calls, scoring, report aggregation, credentials, or formal-release freezing in this task.

## Acceptance criteria

- Reusable dispatcher module plus a CLI subcommand or documented callable entry point can load a config, validate it, produce a deterministic dispatch plan, and execute only explicitly runnable configs.
- Unit/integration tests use fake Adapter/subprocess and cover Graph/Grep pairing, repeats, run IDs, validation failures, smoke-only refusal, dry-run planning, and `execute_run` artifact handoff.
- Existing smoke input tests and Adapter/Runner contracts remain green.
- Focused tests, full `.venv\Scripts\python.exe -m pytest tests -q`, Ruff and diff check pass.

## Delivery contract

- Scoped commit and strict `AGENT_RESULT`; no real Claude session, Judge call, credential access or external repository mutation.
