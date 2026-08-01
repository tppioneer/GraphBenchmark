# AIS-009 Review 1

Reviewed range: `bc43a7b5c87e93fee474aacc14df0d61a5d7ed9b..55ff91a6dd310fe9f2f6890d018e05151d7c1439`

Verdict: `CHANGES_REQUIRED`

## Findings

### AIS009-R1 — Medium — Policy validation failure leaves a contradictory persisted run

- Confidence: high
- Location: `runner/benchmark_runner.py:230-266`, `:320-387`, `:453-498`
- Evidence: policy validation occurs after raw-response, Agent Answer and metadata writes. A `PolicyValidationError` reaches the execution-failure finalizer, which declares those existing files absent, replaces observed metrics with zeroes, and returns `FAILED`; reloading the persisted run derives `INVALID` instead.
- Violated: AIS-009 acceptance criteria for accurate artifacts, independent metric collection and distinct deterministic terminal states; design §17 manifest contract.
- Expected: validate rejectable tool-policy inputs and tool-event sources before artifact writes, or preserve already-written artifacts and metrics with one deterministic terminal status.

### AIS009-R2 — Medium — Failed run cannot reject changed case identity for the same run ID

- Confidence: high
- Location: `runner/benchmark_runner.py:395-450`
- Evidence: the no-overwrite guard compares `case_id` and `task_type` only through `agent-answer.json`. Failed runs have no answer artifact and run metadata deliberately excludes identity, so a same-ID retry with another case silently returns the stale failure instead of raising `RunConflictError`.
- Violated: AIS-009 acceptance criterion that one run ID must not silently overwrite or serve a different input.
- Expected: persist or otherwise compare immutable case/task identity for every terminal run, including failures, and add a failed-run conflict regression test.

### AIS009-N1 — Low — `policy_enforced` metadata conflicts with actual enforcement for `False`

- Confidence: medium
- Location: `runner/benchmark_runner.py:175`, `:243`, `:344`
- Evidence: the option is stored in metadata, while `validate_policy` always runs. Passing `False` claims policy was not enforced while the run remains policy-enforced.
- Expected: treat the option as a supported behavior switch, or reject/normalize `False` so audit metadata remains truthful.

## Verification evidence

- Review task tests: `40 passed`.
- Full suite: `442 passed`.
- Ruff and diff check passed.

## Accepted remediation packet

Base remediation on `55ff91a6dd310fe9f2f6890d018e05151d7c1439`.

Resolve: `AIS009-R1`, `AIS009-R2`, `AIS009-N1`. Add targeted regressions for each reachable failure/identity/flag path. Avoid changes outside the original AIS-009 scope.

## Remediation review

Reviewed range: `55ff91a6dd310fe9f2f6890d018e05151d7c1439..8a593e84d145e44b9a920bf378acf386ae72127d`

Verdict: `CHANGES_REQUIRED`

- `AIS009-R1`, `AIS009-R2`, `AIS009-N1`: resolved. Independent reproduction and review confirmed truthful pre-write policy rejection, all-terminal-run identity guarding, and mandatory truthful policy enforcement.
- Accepted non-blocking notes: `run-input.json` remains Runner-internal guard state without a formal cross-component Schema; rejected policy inputs have no raw response, an existing design boundary also used for execution failures.
- `AIS009-R3` — Low — Formatting check fails. Controller verification found `ruff format --check` would reformat `runner/benchmark_runner.py`, `runner/policy_validation.py`, `tests/runner/test_benchmark_runner.py`, and `tests/runner/test_policy_validation.py`. Run the formatter on only these four task-scope files, commit the mechanical change, then rerun the required tests and format check.

## Format-remediation review

Reviewed range: `8a593e84d145e44b9a920bf378acf386ae72127d..ac69d7125b35d876b1ae3653b010ec26490aa62d`

Verdict: `PASS`

- `AIS009-R3`: resolved by a formatting-only commit touching exactly the four task-scope Runner and test files; no logic changed.
- Controller verification: Runner tests `82 passed`; full suite `455 passed`; Ruff lint, repository-wide format check, and diff check passed.

## Integration verification

Integrated commits: `fa541e3`, `af6189d`, `7550146`.

Combined integration verification: full suite `519 passed`; Ruff, format, pip check, and diff check passed.
