# AIS-004 Review 1

Reviewed range: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc..0ecb9212f56bf5df362924076ffb28fafd4fd238`

Verdict: `CHANGES_REQUIRED`

## Findings

### AIS004-R1 — High — Top-level validation accepts schema-invalid Ground Truth

- Confidence: high
- Location: `scoring/rubric_validator.py:516`
- Evidence: `validate_profile_and_rubric` loads the Profile and runs only the business validator. A GT missing `schema_version` and an item `criterion`, while adding an unexpected root field, returns zero issues although `ground-truth.schema.json` rejects it.
- Violated: AIS-004 objective requires deterministic validation before every Judge call; design §7.2 rule 8 and the AIS-002 GT contract remain mandatory.
- Expected: validate GT with the Draft 2020-12 schema in the production entry point, convert all schema errors to deterministic actionable issues with JSON Pointers, and add schema-invalid negative cases.

### AIS004-R2 — High — Production Profile loader imports a dev-only dependency

- Confidence: high
- Location: `scoring/profiles.py:24`, `pyproject.toml:10`
- Evidence: production code imports `yaml`, while `[project].dependencies` is empty and `PyYAML` exists only in the `dev` extra. A normal project installation cannot load Profiles.
- Violated: AIS-004 provides the production pre-Judge validation path.
- Expected: promote `PyYAML` to runtime dependencies. Coordinate `jsonschema` runtime ownership with AIS-003/AIS-004 remediation so the shared dependency file is edited once in the integration sequence.

## Verification evidence

- Task tests: `84 passed`.
- Full suite: `251 passed`.
- Ruff check, format check, pip check, and diff check: passed.
- Targeted schema-invalid GT counterexample: reproduced with zero reported issues.

## Remediation packet

Base remediation on `0ecb9212f56bf5df362924076ffb28fafd4fd238`.

Resolve: `AIS004-R1`, `AIS004-R2`. The controller authorizes the minimum required `pyproject.toml` dependency edit, but shared dependency changes must not be duplicated across parallel branches without integration coordination.

## Remediation round 1 review

Reviewed range: `0ecb9212f56bf5df362924076ffb28fafd4fd238..763d3c1df818dce5dc8783b79258932c01cd84d0`

Verdict: `CHANGES_REQUIRED`

- `AIS004-R1`: remains open. The original missing-version/criterion/extra-field counterexample is now reported correctly. However, the production entry point loads a task Profile before returning Schema issues; an invalid `task_type` produces four Schema issues through `validate_ground_truth_schema` but `validate_profile_and_rubric` raises `ProfileError` instead of returning them. This violates the remediation requirement to convert every Schema failure and the task requirement to report all actionable validation problems together.
- `AIS004-R2`: remains open. `jsonschema` and `PyYAML` are runtime dependencies, but the built wheel contains neither `schemas/ground-truth.schema.json` nor the YAML Profile files. Importing `scoring.rubric_validator` from the installed wheel raises `FileNotFoundError` before validation can run.
- Verification: task tests `98 passed`; full suite `265 passed`; ruff, format, pip check, and diff check passed.

Next remediation must validate Schema structure before task-dependent Profile loading, return deterministic Schema issues for invalid/missing task identity, and package/load Schema/Profile resources safely from an installed wheel.
