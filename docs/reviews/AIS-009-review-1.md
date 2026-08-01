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
