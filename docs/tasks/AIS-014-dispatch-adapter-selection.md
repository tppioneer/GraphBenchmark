# AIS-014: Configuration-selectable AgentAdapter dispatch

State: READY

## Objective

Extend `runner/experiment_dispatch.py` so executable experiment configurations can select the agent adapter with `runtime.agent_adapter`, defaulting to `claude-code` for backward compatibility and accepting `opencode` for AIS-013. The selected adapter and model must be recorded in each planned run identity and the selected adapter must receive the existing policy-scoped runtime fields.

## Source of truth

- Design: `docs/ai-scoring-design.md` sections 8.6-8.8, 15, 17-18, 20
- Existing dispatch: `runner/experiment_dispatch.py`
- OpenCode implementation: `runner/opencode_adapter.py`
- Reference implementation: `runner/claude_code_adapter.py`
- Base: `d57c732`
- Dependencies: AIS-013 integrated

## Execution envelope

- Executor: Claude Code (`glm-5.2`, automatic permission mode)
- Working directory: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-014-dispatch-adapter`
- Branch: `codex/ais-014-dispatch-adapter`
- Expected HEAD: `d57c732`

## Invariants

- Existing configs without `runtime.agent_adapter` continue selecting Claude Code.
- Accepted adapter values are exactly `claude-code` and `opencode`; unknown values fail validation before any subprocess launch.
- `runtime.agent_model` is passed unchanged to either adapter and recorded in `RunIdentity.agent_model`; the adapter name is recorded in `RunIdentity.agent`.
- Graph/Grep MCP and skill isolation remains identical for both adapters.
- Adapter construction remains injectable through `adapter_factory`; tests never launch a real CLI.
- OpenCode receives only fields supported by `OpenCodeAgentAdapter`; Claude receives its existing permission/plugin fields.
- No dispatcher, Judge, schema, scoring, or live CLI behavior is changed beyond adapter selection.

## Allowed scope

- `runner/experiment_dispatch.py`
- `tests/runner/test_experiment_dispatch.py`

## Excluded scope

- Changes to either adapter implementation
- Experiment YAML, formal config, Case/GT, scoring, Judge, report, or Runner artifact contracts
- Live Claude/OpenCode/provider/MCP execution

## Acceptance criteria

- Parse and validate `runtime.agent_adapter`, defaulting to `claude-code`.
- Build planned identities with the selected adapter name.
- Construct `ClaudeCodeAgentAdapter` or `OpenCodeAgentAdapter` according to the selected value, preserving policy-specific MCP and skill selection.
- Preserve existing Claude behavior and tests.
- Add fake/injected tests for default Claude, explicit Claude, explicit OpenCode, model propagation, unknown adapter rejection, and Graph/Grep isolation.

## Verification

- `.venv\\Scripts\\python.exe -m pytest tests/runner/test_experiment_dispatch.py -q`
- `.venv\\Scripts\\python.exe -m pytest -q`
- `.venv\\Scripts\\python.exe -m ruff check runner/experiment_dispatch.py tests/runner/test_experiment_dispatch.py`
- `.venv\\Scripts\\python.exe -m ruff format --check runner/experiment_dispatch.py tests/runner/test_experiment_dispatch.py`
- `git diff --check d57c732..HEAD`

## Delivery contract

Return a strict `AGENT_RESULT` with base/head, changed files, acceptance evidence, exact checks, deviations, and residual risks. Commit one coherent implementation.
