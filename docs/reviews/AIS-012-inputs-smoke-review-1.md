# AIS-012 input conversion and smoke configuration — Review 1

Verdict: PASS

Reviewed range: `e2d2d7fb806fb936567853f76b1946df19ddf77c..3367ac9f99f4b8664563b035b4c6cb2a5140f8bb`

Reviewer: Claude Code (`glm-5.2`), read-only independent review.

## Evidence

- The Case is schema-valid and contains only the source question; reviewed legacy GT hints and the unverifiable repository name were excluded.
- The Ground Truth passes schema and production rubric validation. Dimensions total `35/25/20/10/10 = 100`; critical items carry `zero_credit`; all material source root causes, paths, exclusions and validation guidance are represented without invented source locations.
- The smoke configuration references the exact Case/GT paths, pins `glm-5.2`, declares paired Graph/Grep conditions, and explicitly remains non-executable (`agent_adapter: null`, no formal Judge result).
- Focused tests: 16 passed. Full suite: 706 passed. A reviewer mutation check exercised point totals, critical zero-credit, leakage and duplicate-id validation. The diff contains exactly four new scoped files.

## Non-blocking notes

- The append failure-chain rubric item is not critical. The reviewer found this a defensible scoring choice because the read chain supplies the relevant critical cap trigger; no source truth is omitted.
- No approved experiment-config schema exists, so its structure is tested directly. The configuration is explicit that it is smoke-only and not a formal manifest.

## Residual risks

This adds only validated input wiring. A real run still requires a concrete AgentAdapter, verified repository URL/revision for reproducible checkout, active Judge authentication, and a formal frozen manifest.
