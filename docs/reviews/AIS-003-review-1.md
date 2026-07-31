# AIS-003 Review 1

Reviewed range: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc..e01a9e3b46df43336c4ffe0e8e327456c02efbbd`

Verdict: `CHANGES_REQUIRED`

## Findings

### AIS003-R1 — High — Runner identity is not enforced on parsed Agent Answer

- Confidence: high
- Location: `runner/execution.py:159`
- Evidence: `produce_agent_artifacts` validates the model-provided document but never compares its `case_id` or `task_type` with the authoritative function arguments. A document declaring `wrong-case` and `impact_analysis` is returned as `completed` while the Runner requested `expected-case` and `bug_localization`.
- Violated: AIS-003 objective and artifact identity boundary; downstream Judge input must describe one case/task and must not trust the model to select run identity.
- Expected: reject or deterministically downgrade identity-mismatched structured output, and produce an artifact whose authoritative identity comes from the Runner. Add negative tests for both fields.

### AIS003-R2 — High — Production runner imports a dev-only dependency

- Confidence: high
- Location: `runner/artifact_validation.py:22`, `pyproject.toml:10`
- Evidence: production code imports `jsonschema`, while `[project].dependencies` is empty and `jsonschema` exists only in the `dev` extra. A normal `pip install .` does not install the module required to import the Runner.
- Violated: AIS-003 delivers production artifact validation; the project must run outside a developer-only environment.
- Expected: promote `jsonschema` to runtime dependencies and retain testing tools in the dev extra; verify an ordinary project install can import the production module.

## Verification evidence

- Task tests: `22 passed`.
- Full suite: `189 passed`.
- Ruff check, format check, pip check, and diff check: passed.
- Targeted identity-mismatch counterexample: reproduced.

## Remediation packet

Base remediation on `e01a9e3b46df43336c4ffe0e8e327456c02efbbd`.

Resolve: `AIS003-R1`, `AIS003-R2`. The controller authorizes the minimum required `pyproject.toml` dependency edit for `AIS003-R2`; avoid unrelated packaging changes.
