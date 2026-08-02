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

## Remediation review

Reviewed range: `1fd1fdccce94c44843be6259d27d11bbf9a95f40..cffa8473494bdad1e2c19da8858ce53482dd506e`

Verdict: `PASS_WITH_NOTES`

- R1, R2, and R3 are verified fixed; Judge tests `204 passed`, full suite `624 passed`, and diff check passed.
- N2 is accepted for follow-up: when Judge C itself causes a model-consistency rejection, its audit record must still be retained, per design §13.4 and §13.5.
- N3 is accepted for follow-up: add an end-to-end provider-call regression test so an effective-model overwrite cannot silently return.

## Final remediation review

Reviewed range: `cffa8473494bdad1e2c19da8858ce53482dd506e..4d37058ef1d4901069623d64e356140cf48413f3`

Verdict: `PASS`

- N2 now retains Judge C, all three audits, and arbiter state for model-consistency failure paths without producing a formal score.
- N3 now tests the end-to-end provider call path for an unverifiable effective model when `--model` is unavailable.
- Claude Code (`glm-5.2`) verification: Judge tests `207 passed`; full suite `627 passed`; targeted N2/N3 tests `3 passed`; diff check passed.

## Integration verification

- Integrated commits: `2f39004`, `c980ff0`, `323c3f5`.
- Integration completed without conflicts after verified independent review.
- Final status: `INTEGRATED`.
