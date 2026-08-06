# AIS-012 e2e-release review

Verdict: `PASS_WITH_NOTES`

Reviewed range: `b32068f..46953c8` (AIS-012 commits only; AIS-013 commits in
the same range are out of scope for this review)
Reviewer: Codex (controller)
Date: 2026-08-06

## Scope

Independent review of the AIS-012 end-to-end formal experiment execution:
preflight-2, agent execution (6 runs), Judge + consensus + scoring + reporting
pipeline, and 6 narrow integration fixes for CLI 2.1.223 compatibility.

## Acceptance-criteria verification

1. Preflight gates all PASS: **PASS**. Preflight-2 (`docs/reviews/AIS-012-preflight-2.md`)
   confirms all 4 preflight-1 blockers resolved (formal cases/GT, manifest,
   AgentAdapter, Judge auth) plus runtime path verification and QwenPaw revision
   match.
2. CLI version pinned, judge_model effective: **PASS**. CLI 2.1.223, judge_model
   glm-5.2, effective_model verified in scored runs' effective-score.json.
3. Credentials confirmed, no secret read/written: **PASS**. `claude auth status`
   reports loggedIn=true; no credential value in any artifact or argv.
4. Freeze manifest with code SHA, protocol, digests: **PASS**.
   `freeze-manifest.yaml` in runs_root contains all required fields.
5. Run artifacts satisfy §17: **PASS**. All 6 run directories have manifest.json,
   run-metadata.json, policy-result.json. Scored runs have agent-answer.json,
   blind-input.json, judge-a/b.json, judge-score.json, effective-score.json.
6. judge_failed isolated and listed: **PASS**. 3 judge_failed runs are isolated
   in the report with reasons; not in formal score.
7. Paired stats consume complete pairs only: **PASS**. 1 paired pair
   (graph r01 vs grep r03), all missing/invalid reasons listed.
8. Offline rebuild digest match, 0 Judge calls: **PASS**.
   `sha256:e2500981...` verified identical; `JUDGE_CALL_COUNT = 0`.
9. Report includes absolute, paired diff, cost, stability, limitations: **PASS**.
   `report.md` contains all required sections.

## Design invariant verification

- Same case/repeat Graph/Grep uses same model, Agent, Judge protocol: **PASS**.
  All runs use glm-5.2 for both agent and judge.
- Formal mode dual Judge with arbiter: **PASS**. Scored runs show 2-3 judges
  (mean/median consensus); arbiter called when A/B disagreement triggers fire.
- Identity/tool policy blinded to Judge: **PASS**. blind-input.json present for
  all Judge-processed runs.
- Invalid runs not in correctness stats: **PASS**. graph r03 (policy invalid)
  is isolated.
- No rollback to old scorer: **PASS**. No scorer code changes.
- Protocol input changes produce new version/digest: **PASS**. Freeze manifest
  with all digests; report digest verified.

## Code-change review (6 narrow integration fixes)

- `3c043fb` `--verbose` for stream-json: correct, tested, minimal.
- `98ed513` `.CMD` -> `.exe` resolution + timeout 600s -> 1200s: correct,
  tested (`_resolve_cmd_wrapper` + `test_resolve_cmd_wrapper_*`). Timeout
  increase is reasonable for bug localization.
- `b963a9f` Provider `.CMD` -> `.exe` CLI discovery: mirrors adapter fix;
  independent implementation avoids `judge -> runner` reverse dependency.
- `ebf8572` Provider UTF-8 decoding + pipeline script: `_to_text` helper handles
  both bytes and str (mock-compatible). Pipeline script correctly chains all
  phases.
- `d296013` Skip `--json-schema`: reasonable workaround for CLI 2.1.223
  incompatibility. Provider's own validation compensates.
- `75c6fc7` Parse judge output from CLI result wrapper: correctly extracts JSON
  from `result` field, handles markdown code fences. Tested via provider tests.

## Checks

- Full suite: 842 passed.
- `ruff check`: passed.
- `git diff --check`: clean.
- Offline report rebuild: digest match, 0 Judge calls.

## Findings

- R1 (note, low, confidence high): Stale `judge-c.json` files from previous
  pipeline runs remain on disk when the current run does not call Judge C.
  Observed: graph r01 has `judge-c.json` with raw CLI result wrapper content,
  but the manifest correctly marks `judge_c: absent` and the report uses the
  manifest. The report is unaffected. The pipeline script should clean up stale
  artifacts between runs or the script should be idempotent.
  File: `scripts/formal_pipeline.py` (run_judge_phase).
- R2 (note, low, confidence high): Judge token usage is 0 in the report. The
  CLI result wrapper contains `usage.input_tokens` and `usage.output_tokens`,
  but the provider's `_judge_cost` in the pipeline script extracts tokens from
  `audit.judge_output` (the parsed judge output, which does not carry usage).
  The tokens are available in the raw CLI output but not propagated to the
  JudgeAuditEntry. Cost reporting for the Judge is incomplete.
  File: `scripts/formal_pipeline.py` (`_judge_cost`), `judge/provider.py`.
- R3 (note, medium, confidence high): Only 1 of 3 possible paired comparisons
  completed (2/6 runs scored). The 50% Judge failure rate (3/5 valid runs) due
  to invalid JSON output from glm-5.2 is a significant limitation. The report
  correctly documents this as isolated runs. The single paired comparison
  (graph r01 vs grep r03) uses runs from different repeats, reducing
  comparability.
- R4 (note, low, confidence high): `scripts/formal_pipeline.py` is not covered
  by the test suite. Its manifest rewriting, artifact writing, and judge cost
  extraction logic are untested. The script is a glue layer over tested APIs,
  but its own logic could regress.
  File: `scripts/formal_pipeline.py`.
- R5 (note, low, confidence high): The `--json-schema` skip (R1 of the code
  changes) removes CLI-side output validation. The provider's `_parse_output`
  handles markdown-fenced JSON, but 3/5 Judge calls still failed due to invalid
  JSON (empty result or syntax errors). A JSON repair step or increased retry
  count could improve Judge success rate in a future task.
  File: `judge/provider.py` (`_build_cli_args`).

## Residual risks (as documented in task card)

- Only 1/3 possible paired comparisons (2/6 scored)
- 50% Judge failure rate (glm-5.2 JSON output instability)
- 1 Graph run policy-invalid (agent did not use Graph tools)
- Single paired comparison uses different repeats (r01 vs r03)
- Judge token usage not captured

## Scope note

The diff range `b32068f..46953c8` includes AIS-013 (OpenCode adapter) commits
(`d11db0c`, `2f222a0`, `d57c732`). These are NOT part of the AIS-012
e2e-release task and were already in the branch before AIS-012 execution
started. They are excluded from this review.

No formal experiment, MCP process, or external service beyond the Judge CLI
was executed during this review. The offline report rebuild was verified with
0 Judge calls.
