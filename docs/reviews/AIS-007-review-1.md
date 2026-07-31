# AIS-007 Review 1

Reviewed range: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc..a16dd50f117ac4a26659f4e5745683ceb481b22f`

Verdict: `CHANGES_REQUIRED`

## Findings

### AIS007-R1 — High — Nested values inside allowlisted fields can leak sensitive structure

- Confidence: high
- Location: `judge/blind_payload.py:240`
- Evidence: `limitations` and `recommended_actions` are copied with `list(...)`, and other allowlisted values are shallow-copied. Supplying `limitations=[{"tool_policy":"graph","agent_model":"secret-model"}]` places that structure unchanged in the blind payload.
- Violated: AIS-007 allowlist invariant, recursive leak acceptance criterion, and design §9.2/§9.3 treatment of Agent Answer as untrusted input.
- Expected: validate or reconstruct every nested allowlisted value according to its contract, rejecting invalid types instead of copying them. Add adversarial tests for nested payloads inside limitations, actions, evidence IDs, findings, evidence, and rubric fields.

### AIS007-R2 — High — Cache accepts a result under a key unrelated to its key components

- Confidence: high
- Location: `judge/cache.py:179`
- Evidence: `JudgeCache.put` records optional `key_components` but never verifies `key == compute_cache_key(key_input)`. A result can be stored and returned under `sha256:` plus 64 zeros even though the supplied key input computes to another digest.
- Violated: cache key completeness/invalidation invariant and auditability requirement.
- Expected: when key components are supplied, require an exact computed-key match; reject malformed keys and mismatches. Ensure integrity verification covers the association needed to prevent stale or poisoned hits.

## Verification evidence

- Task tests: `62 passed`.
- Full suite: `229 passed`.
- Ruff check, format check, pip check, and diff check: passed.
- Targeted nested-leak and mismatched-cache-key counterexamples: reproduced.

## Remediation packet

Base remediation on `a16dd50f117ac4a26659f4e5745683ceb481b22f`.

Resolve: `AIS007-R1`, `AIS007-R2`. Do not broaden into Provider calls, prompt text, consensus, scoring, or persistent cache design.

## Remediation round 1 review

Reviewed range: `a16dd50f117ac4a26659f4e5745683ceb481b22f..a392086da365586ce121422ec5ff1fd86b36d28f`

Verdict: `PASS`

- `AIS007-R1`: resolved. Every nested allowlisted Agent Answer, evidence, rubric, and excerpt value is reconstructed with contract-aware type checks; the original nested-leak counterexample is rejected.
- `AIS007-R2`: resolved. `put` rejects a supplied key that does not match `compute_cache_key(key_input)`, and reads verify stored key components hash back to the lookup key.
- Verification: task tests `99 passed`; full suite `266 passed`; ruff, format, pip check, and diff check passed.

## Integration verification

Integrated commits: `f37c0be`, `106822b`.

Integrated verification: `402 passed`; Ruff, format, pip check, and diff check passed.
- Residual note: callers may still use the backward-compatible `put(..., key_input=None)` path, which cannot verify key components. This is not part of the accepted finding but production callers should always supply `key_input`.
