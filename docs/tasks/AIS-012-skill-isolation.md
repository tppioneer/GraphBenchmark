# AIS-012 skill isolation remediation

State: VERIFIED

## Objective

Ensure condition-specific resource injection is isolated: Graph runs may receive the configured Graph Skill and Graph MCP, while Grep runs must receive no Skill and no MCP of any kind.

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
- Grep condition receives neither Graph MCP nor Graph Skill, and no other MCP or Skill input.
- Existing public configuration compatibility is preserved unless a narrowly scoped schema change is required.
- Dry-run remains side-effect free and plans exactly six runs for the formal config.

## Allowed scope

- `runner/`
- `experiments/`
- `tests/runner/`
- Relevant task-local documentation only if needed to explain the contract.

## Acceptance criteria

- The formal YAML expresses Graph-only Skill/MCP injection without relying on global resources for Grep.
- Dispatcher/adapters construct Graph and Grep inputs with the required isolation.
- Tests cover both positive Graph Skill injection and negative Grep Skill absence.
- Existing tests and the full suite pass.
- No formal execution occurs.

## Delivery contract

Return one `AGENT_RESULT` block with commit SHA, changed files, acceptance results, exact checks, deviations, open questions, and risks.

## Agent evidence

- Implementation commit: `74ab076ba19d8a99a76bac8354a8e25564fc0999`
- Dispatcher and adapter now enforce Graph-only Skill injection; Grep receives no Skill and no MCP.
- Focused tests: 134 passed
- Full suite: 840 passed
- Formal validate-only and dry-run: passed; six runs planned, no formal execution
- Ruff check and formatting checks: passed

## Independent review

- Review: `docs/reviews/AIS-012-skill-isolation-review-1.md`
- Verdict: `PASS_WITH_NOTES`
- F2 fully resolved: Graph-only Skill injection enforced at the dispatcher
  factory; Grep receives no `skill_text`/`skill_file`; adapter fail-closes at
  command-construction time if a skill reaches a Grep run (defense-in-depth).
- `skill_file` is loaded into `_skill_text` at construction, so the Grep guard
  covers both skill sources. No bypass path found.
- Controller independently confirmed: full suite 840 passed at `74ab076`;
  `git diff --check` clean; `ruff check` passed.
- R1/R2 are non-blocking notes (pre-existing `ruff format` debt; task-card
  creation by executor). Neither blocks VERIFIED.
- Integration pending: `74ab076` and base `07d6cc4` are not ancestors of HEAD
  (`2f5c169`). The code must be integrated into `ai-score-v1` (cherry-pick or
  merge) before the formal-config F2 note can be closed.
