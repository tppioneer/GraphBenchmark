# AIS-012 formal configuration review

Verdict: `PASS_WITH_NOTES`

Reviewed commit: `07d6cc4f246eabd5bcd178c2df439421c847502b`
Expected base: `b9710aa30f0329b25f6639c423d8cbb1d5286e48`
Executor: Claude Code (`glm-5.2`)

## Findings

- F1 (closed): the reviewer requested confirmation that `permission_mode: auto` is accepted. Controller verification of the installed Claude CLI help confirms `auto` is listed as a valid mode, and the value is user-approved.
- F2 (note): the current contract has one global `skill_file`; the Graph Skill text would also be injected into Grep during execution. Graph MCP/tool access remains isolated, but the skill text may contaminate the Grep baseline. This is a known execution-gate decision, not a dry-run defect.
- F3 (note): `RuntimeFields` documentation says paths are resolved, while the implementation parses absolute paths without canonicalizing them. No functional impact for the approved absolute-path configuration.
- F4 (note): adapter selection is fixed to Claude Code in this AIS-012 scope. Multi-adapter selection is outside this task.

## Evidence

- Code inspection covered the formal YAML, dispatcher, Claude adapter, CLI wiring, case/ground truth, profile validation, and tests.
- Agent reported: validate-only passed; dry-run planned six runs; 832-test full suite, focused tests, Ruff, and diff checks passed.
- Controller independently confirmed the installed Claude CLI advertises `--permission-mode auto`.

No formal experiment, MCP process, Judge call, or external service was executed.
