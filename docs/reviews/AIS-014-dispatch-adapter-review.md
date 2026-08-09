# AIS-014 Dispatch Adapter Selection Review

- Verdict: `PASS_WITH_NOTES`
- Reviewer: Claude Code (`glm-5.2`)
- Base: `c2ca454b1b2ba52d13438ce109180a87587b6f01`
- Head: `85f5842b01ed3dbd0179f24d12849be1494be82b`
- Scope: `runner/experiment_dispatch.py`, `tests/runner/test_experiment_dispatch.py`

## Evidence

- `runtime.agent_adapter` defaults to `claude-code` and accepts `opencode`; unknown values fail validation before launch.
- Adapter name and model propagate into `RunIdentity`.
- Claude and OpenCode constructors receive only supported runtime fields.
- Graph/Grep MCP and Skill isolation is preserved for both adapters.
- Focused tests: 83 passed; full suite: 950 passed.
- Ruff check, format check, and `git diff --check` passed.
- No live CLI/provider/MCP calls were made.

## Non-blocking notes

- Direct programmatic `RuntimeFields(agent_adapter="opencode")` construction without an explicit model uses the Claude default; config-file parsing applies adapter-specific defaults correctly.
- A private factory uses an assertion for an internal precondition. These are deferred and do not affect the supported dispatch path.
