# AIS-005 Review 1

Reviewed range: `bc43a7b5c87e93fee474aacc14df0d61a5d7ed9b..f36711e2682cd36d6ca89a3a45a7fef8da36f0c5`

Verdict: `CHANGES_REQUIRED`

## Findings

### AIS005-F1 — Low — Profile-declared empty critical-code set falls back to defaults

- Confidence: high
- Location: `scoring/aggregator.py:284-288`
- Evidence: `_allowed_critical_codes` checks `if declared:`. A profile containing `critical_error_codes: []` therefore falls back to the frozen cap-code set rather than allowing no codes.
- Violated: design §12 requires Judge critical-error codes to be declared by the Profile.
- Expected: use the frozen fallback only when no task profile is supplied or the key is absent; an explicit empty list must reject every critical-error code.

## Verification evidence

- Review task tests: `62 passed`.
- Full suite: `464 passed`.
- Ruff and diff check passed.

## Accepted remediation packet

Base remediation on `f36711e2682cd36d6ca89a3a45a7fef8da36f0c5`.

Resolve: `AIS005-F1`. Add a regression test for an explicitly empty `critical_error_codes` list. Avoid changes outside the original AIS-005 scope.

## Remediation review

Reviewed range: `f36711e2682cd36d6ca89a3a45a7fef8da36f0c5..1167de1a8a578ec47f33556cfa737aa9ba291684`

Verdict: `PASS`

- `AIS005-F1`: resolved. An explicit empty `critical_error_codes` declaration now returns an empty allowlist; only an absent profile/key uses the frozen fallback. Both paths have focused regression coverage.
- Controller verification: task tests `64 passed`; full suite `466 passed`; Ruff, format, and diff checks passed.

## Integration verification

Integrated commits: `b632c99`, `8ae816e`.

Combined integration verification: full suite `519 passed`; Ruff, format, pip check, and diff check passed.
