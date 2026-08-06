# AIS-012 formal configuration and dry-run

State: INTEGRATED

## Objective

Create the machine-specific formal YAML for the approved QwenPaw case using absolute runtime paths, then validate and dry-run the dispatcher without launching Agent, MCP or Judge processes.

## Frozen runtime inputs

- QwenPaw repository: `F:\develop\codes\QwenPaw\QwenPaw`
- QwenPaw revision: `09fc515c88a5e817870e6b975e66b5be81893e03`
- Graph MCP config: `F:\develop\00-codes\benchmark-runtime\mcp\.mcp.json`
- Graph Skill: `F:\develop\00-codes\benchmark-runtime\skills\gitnexus-guide\SKILL.md`
- Runs root: `F:\develop\00-codes\benchmark-runtime\runs\GraphBenchmark-ai-score-v1`
- Agent model: `glm-5.2`
- Permission mode: `auto`
- Repeats: `3`

## Required boundaries

- Formal YAML uses absolute paths and status `executable`; it contains no credential values.
- Grep has no Graph MCP or Graph Skill input; Graph uses only the specified MCP and Skill.
- Dry-run/validate-only must produce a deterministic plan of 6 runs (3 Graph + 3 Grep) without subprocess launch or artifact output.
- Do not execute the formal experiment or call Judge in this task.

## Acceptance criteria

- Formal YAML passes Case/GT/Profile and runtime validation.
- Dispatcher dry-run yields 6 safe deterministic run IDs and correct Graph/Grep isolation.
- Focused and full tests pass; no unrelated files change.

## Agent evidence

- Implementation commit: `07d6cc4f246eabd5bcd178c2df439421c847502b`
- `dispatch --validate-only`: passed
- `dispatch --dry-run`: passed; 6 planned runs (3 Graph + 3 Grep)
- Focused formal-config tests: 11 passed
- Dispatch/smoke tests: 82 passed
- Full suite: 832 passed
- Ruff and diff checks: passed

## Review note

F2 (resolved): the runtime contract's single global `skill_file` field would have
injected the Graph Skill text into Grep during execution. This is now fixed by
the skill-isolation subtask (integrated as `b4ca65d`): the dispatcher injects the
skill Graph-only and the adapter fail-closes if a skill reaches a Grep run. The
F2 execution gate is closed.

## Independent review

- Review: `docs/reviews/AIS-012-formal-config-review-1.md`
- Verdict: `PASS_WITH_NOTES`
- No blocking implementation finding remains for the dry-run scope.
- F2 (global Graph Skill injection into Grep) resolved by skill-isolation subtask.

## Integration

- Cherry-picked to `ai-score-v1` as `3112fee` (base `bb82f9c`).
- Post-integration: full suite 840 passed; `ruff check` passed; `git diff --check` clean.
- `dispatch --dry-run`: 6 planned runs (3 Graph + 3 Grep), no subprocess launched.
