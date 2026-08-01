# AIS-008 Review 1

Reviewed range: `5fd8cf1f93b738b2fcaca963e75d6d3b309541fd..1fd1fdccce94c44843be6259d27d11bbf9a95f40`

Verdict: `CHANGES_REQUIRED`

## Findings

### R1 — High — Effective model is falsely reported when `--model` is unavailable

- Confidence: high
- Location: `judge/provider.py:258-262`
- Evidence: the adapter sets the effective model to the requested model even when the installed CLI lacks the `--model` flag, so it cannot have verified the actual model.
- Violated: design §13.3 requires a formal run to be invalid when the actual model cannot be verified.
- Expected: expose an unverifiable model state and reject the formal run.

### R2 — Medium — JWT redaction preserves the full token

- Confidence: high
- Location: `judge/provider.py:54`
- Evidence: a standalone three-segment JWT is transformed to `<original-token>=<REDACTED>`, leaving the credential in persisted output.
- Violated: design §13.6 requires secret redaction before stdout, stderr, errors, command summaries, and failure audits are persisted.
- Expected: replace the entire JWT with `<REDACTED>` and add a regression test.

### R3 — Low — Runner does not reject requested/effective model mismatch

- Confidence: high
- Location: `judge/judge_runner.py:222-275`
- Evidence: Judge results are accepted without checking that the effective model is both verifiable and equal to the requested model.
- Violated: AIS-008 acceptance criterion for A/B/C model consistency and formal-run invalidation.
- Expected: return a stable failure state without producing a formal score.

## Verification evidence

- Judge tests: `200 passed`.
- Full suite: `620 passed`.
- Diff check passed.

## Accepted remediation packet

Base remediation on `1fd1fdccce94c44843be6259d27d11bbf9a95f40`.

Resolve: R1, R2, R3. Do not change unrelated behavior or task-controlled documentation. Add focused regression tests, then run task tests, full tests, and diff check.
