# AIS-012 skill isolation remediation

State: READY

## Objective

Ensure condition-specific Skill injection is isolated: Graph runs may receive the configured Graph Skill, while Grep runs must receive no Graph Skill and no Graph MCP.

## Source of truth

- Parent task: `docs/tasks/AIS-012-formal-config.md`
- Review: `docs/reviews/AIS-012-formal-config-review-1.md` (F2)
- Base commit: `07d6cc4f246eabd5bcd178c2df439421c847502b`

## Execution envelope

- Executor: Claude Code
- Model: `glm-5.2`
- Permission mode: `auto`
- Working directory: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-012-skill-isolation`
- No formal experiment, MCP process, Judge call, or external service

## Invariants

- Graph condition retains its configured Graph MCP and Graph Skill.
- Grep condition receives neither Graph MCP nor Graph Skill.
- Existing public configuration compatibility is preserved unless a narrowly scoped schema change is required.
- Dry-run remains side-effect free and plans exactly six runs for the formal config.

## Allowed scope

- `runner/`
- `experiments/`
- `tests/runner/`
- Relevant task-local documentation only if needed to explain the contract.

## Acceptance criteria

- The formal YAML expresses Graph-only Skill injection without relying on a global Skill field.
- Dispatcher/adapters construct Graph and Grep inputs with the required isolation.
- Tests cover both positive Graph Skill injection and negative Grep Skill absence.
- Existing tests and the full suite pass.
- No formal execution occurs.

## Delivery contract

Return one `AGENT_RESULT` block with commit SHA, changed files, acceptance results, exact checks, deviations, open questions, and risks.
