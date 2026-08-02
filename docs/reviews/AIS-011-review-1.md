# AIS-011 Review 1

Reviewed range: `96818da358c4a8f477f17cb2a79d30e9f4d5206e..3334d85d37c9a02e79c7830699298ba0f80197f4`

Verdict: `CHANGES_REQUIRED`

## Findings

### R1 — High — Paired aggregate crosses compatibility groups

- Confidence: high
- Location: `report/aggregate.py:260-273,499-570`
- Evidence: the pairing key includes only case and agent identity, allowing same-case runs with different Judge models to enter `paired_absolute_diffs` despite separate compatibility-matrix groups.
- Violated: design §16.1 and §20; incompatible versions must not enter the same formal aggregate.
- Expected: form pairs only within a complete compatibility group and add a cross-model regression test.

### R2 — Medium — Malformed score-v1 aborts the full report load

- Confidence: high
- Location: `report/analysis_input.py:649-702`
- Evidence: a schema-versioned score missing `dimension_totals` raises `KeyError` rather than isolating the run.
- Violated: design §15.1 and the invalid-artifact isolation acceptance criterion.
- Expected: catch malformed score extraction and return an invalid isolated record with a stable reason.

### R3 — Low — A/B disagreement documentation reverses the critical-item direction

- Confidence: high
- Location: `report/analysis_input.py:466-492`
- Evidence: critical items trigger Judge C for any nonzero difference, so applying `>0.25` uniformly under-counts critical sub-threshold differences; the docstring says the opposite.
- Expected: correct the documentation and add targeted coverage if behavior is retained.

## Protocol note

The v1 artifact set has no `repeat` field. This is not a code blocker if ambiguous candidates are explicitly handled, but the current deterministic-first pairing behavior is only a best-effort limitation and requires a later formal policy decision.

## Accepted remediation packet

Base remediation on `3334d85d37c9a02e79c7830699298ba0f80197f4`.

Resolve: R1, R2, R3 only. Do not change the repeat strategy without an explicit controller decision. Add focused tests; run report tests, full tests, and diff check.

## Remediation review

Reviewed range: `3334d85d37c9a02e79c7830699298ba0f80197f4..65c40b3bf798ce9ea9db1fb2814ab59ddcde97c1`

Verdict: `PASS_WITH_NOTES`

- R1 and R2 are verified fixed. R3's function documentation and behavior are correct.
- Follow-up finding: `JudgeDisagreement.ab_disagreement_items` retains an older field comment claiming GT-aware critical-item behavior; it must document the current uniform `>0.25` approximation and resulting under-count limitation.
