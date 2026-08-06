# AIS-012 skill isolation review

Verdict: `PASS_WITH_NOTES`

Reviewed commit: `74ab076ba19d8a99a76bac8354a8e25564fc0999`
Expected base: `07d6cc4f246eabd5bcd178c2df439421c847502b`
Executor: Claude Code (`glm-5.2`)
Parent finding: `docs/reviews/AIS-012-formal-config-review-1.md` F2

## Scope

Independent review of the F2 remediation: Graph-only Skill injection so the
configured Graph Skill text cannot contaminate the Grep baseline. Reviewed the
full `07d6cc4..74ab076` diff against the acceptance criteria and invariants in
`docs/tasks/AIS-012-skill-isolation.md`.

## Acceptance-criteria verification

- Graph-only Skill/MCP injection without relying on global resources for Grep:
  PASS. `_default_adapter_factory` sets `skill_text=None` and `skill_file=None`
  for `tool_policy == "grep"`; Graph/Mixed receive `runtime.skill_text`/`skill_file`.
  The config contract keeps one global `skill_file` field; isolation is enforced
  by the dispatcher applying it Graph-only.
- Dispatcher/adapters construct Graph and Grep inputs with required isolation:
  PASS. Factory suppresses skill for Grep; adapter `build_command` fail-closes
  if a skill reaches a Grep run (defense-in-depth).
- Tests cover positive Graph Skill injection and negative Grep Skill absence:
  PASS. 3 adapter tests + 5 dispatch isolation tests + `TestFormalConfig`
  assertions cover positive (Graph/Mixed receive skill), negative (Grep receives
  no skill), inline `skill_text`, and leak defense-in-depth.
- Existing tests and full suite pass: PASS. 840 passed at `74ab076` (matches
  executor evidence); focused adapter+dispatch 115 passed.
- No formal execution occurs: PASS. No subprocess launch, Judge call, or
  artifact generation in the diff.

## Invariant verification

- Graph condition retains configured Graph MCP and Graph Skill: PASS.
- Grep receives neither Graph MCP nor Graph Skill, and no other MCP/Skill: PASS.
  Grep factory path sets both `skill_text` and `skill_file` to `None`; adapter
  guard raises `AgentPolicyConfigError` if `_skill_text` is set on a Grep run.
- Existing public configuration compatibility preserved: PASS. No schema change;
  single global `skill_file`/`skill_text` field retained; isolation enforced at
  dispatcher layer, not by schema.
- Dry-run remains side-effect free and plans six runs: PASS.
  `build_dispatch_plan` is untouched by this diff; only `_default_adapter_factory`
  changed. The 6-run plan structure is preserved.

## Correctness analysis

- `skill_file` is loaded into `_skill_text` at construction time
  (`claude_code_adapter.py:207-214`), and `skill_text`/`skill_file` are mutually
  exclusive. The Grep guard checks `if self._skill_text:`, so it covers both
  sources. A Grep run configured via `skill_file` cannot bypass the guard.
- `benchmark_runner.py:870-871` constructs a `RuntimeFields` override (field
  propagation), not an adapter. The actual adapter construction happens in
  `_default_adapter_factory`, which applies the Grep suppression. No isolation
  gap.
- The guard is placed inside `if self._skill_text:` in `build_command`, so it
  triggers precisely when a skill is present on a Grep run -- the exact F2
  contamination scenario. A skill-free Grep run is unaffected.

## Checks

- `git diff --check 07d6cc4..74ab076`: clean (no whitespace errors).
- `ruff check`: passed.
- Focused tests (`test_claude_code_adapter.py`, `test_experiment_dispatch.py`):
  115 passed.
- Full suite at `74ab076`: 840 passed (independently confirmed by checking out
  the commit's file state and running `pytest tests -q`).

## Findings

- R1 (note, low confidence high): Pre-existing `ruff format --check` violations
  exist in `runner/experiment_dispatch.py`, `tests/runner/test_claude_code_adapter.py`,
  and `tests/runner/test_experiment_dispatch.py`. These are in code from the
  experiment-dispatch task (e.g. `ValidationIssue` call formatting,
  `_safe_load_yaml` signature), not introduced by `74ab076`. The skill-isolation
  additions are properly formatted. Not a regression; pre-existing tech debt
  outside this task's scope.
- R2 (note, low confidence high): The executor created
  `docs/tasks/AIS-012-skill-isolation.md` (72 lines) in commit `74ab076`. The
  orchestration protocol treats task cards as controller-owned. The allowed
  scope permits "relevant task-local documentation only if needed to explain the
  contract"; creating the task card itself is marginally outside that. The
  controller subsequently rewrote the card. Not blocking.

## Integration context (not a finding against the commit)

`74ab076` and its base `07d6cc4` are NOT ancestors of HEAD (`2f5c169`). The
skill-isolation code exists only on a worktree branch; HEAD's
`runner/claude_code_adapter.py` and `runner/experiment_dispatch.py` lack the
Grep skill guard and suppression. The formal config YAML
(`experiments/qwenpaw-corrupt-inbox-formal-v1.yaml`) is likewise absent from
HEAD. After this review, `74ab076` (and `07d6cc4`) must be integrated into
`ai-score-v1` before the formal-config F2 note can be closed and before
AIS-012 e2e-release preflight can pass.

No formal experiment, MCP process, Judge call, or external service was executed
during this review.
