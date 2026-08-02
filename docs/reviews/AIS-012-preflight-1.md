# AIS-012 Preflight 1

Result: BLOCKED

Executor: Claude Code (`glm-5.2`)

Base: `23731d7424a776a55c2ccaf152820d3e3ee4fdcc`

## Confirmed gates

- The isolated worktree matched the fixed base exactly.
- `.venv\\Scripts\\python.exe -m pytest tests -q` passed: 690 tests.
- Claude CLI was globally available (`2.1.220`) and the judge code pins and verifies the requested `glm-5.2` identity.
- No files changed, no artifacts were generated, and no Judge call was made.

## Blocking gates

1. The repository has no approved formal `cases/` / ground-truth set. Only synthetic test fixtures are present; AIS-010 remains DRAFT and does not supply a formal dataset.
2. No frozen formal manifest/config exists to define the case set, repeats, paired Graph/Grep conditions, and output root.
3. No concrete AgentAdapter is wired for executing the Graph/Grep agent under test; the current runner defines only its protocol/minimal parser.
4. The supported Claude health check reports inactive authentication, so a real Judge call would be `judge_unavailable` and could not generate a formal score.

## Required resolution

Provide or authorize creation of an approved formal cases/GT set and frozen formal manifest, decide the scope/owner for concrete AgentAdapter wiring, and restore the globally configured Claude authentication. Then restart AIS-012 from a newly fixed base.
