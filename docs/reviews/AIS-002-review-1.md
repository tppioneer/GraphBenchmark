# AIS-002 Review 1

Reviewed range: `d1179998079f56f3dbda0fdcc4798e323bbf866d..8be8bb5da71aca6864ac556e89a5c664c809aaf6`

Verdict: `CHANGES_REQUIRED`

## Findings

### R1 — High — 降级包装被 Agent Answer Schema 拒绝

- Confidence: high
- Location: `schemas/agent-answer.schema.json:35`
- Evidence: 设计 §8.8 的 `completed_with_schema_warning` 示例允许 `answer.summary` 为空、`answer.explanation` 保存有效原始 Markdown；当前 `summary.minLength = 1` 会拒绝该示例。针对性验证返回 `design_fallback_valid=False`。
- Violated: 设计 §8.8；任务不变量“`answer.explanation` 是可独立评分的主要答案”；DEC-001“合法自然语言不能因 JSON 失败自动归零”。
- Expected: 按 `status` 条件化约束。至少允许 `completed_with_schema_warning` 使用空 summary 和非空 explanation，并为 `empty`、`refused` 定义不要求伪造答案内容的合法结构；增加对应正反例。

### R2 — High — 正式分数无法证明请求模型与实际生效模型一致

- Confidence: high
- Location: `schemas/score.schema.json:47`
- Evidence: Schema 只有一个任意非空 `judge_model`；`Auto` 和 `latest` 均验证通过，并且没有分别记录请求模型与实际生效模型。`judge-output.schema.json` 也不携带每次调用的模型、Provider 和 CLI 版本。
- Violated: DEC-001 #6、#9；设计 §13.3、§13.4、§20；正式实验不得跨模型聚合的可审计要求。
- Expected: 为 Judge 调用/正式分数定义可核验的 requested/effective model 字段或等价结构，显式拒绝 `Auto` 和不固定的 `latest`，并添加不一致与禁用值反例。

### R3 — Medium — 人工复核触发原因无法写入协议

- Confidence: high
- Location: `schemas/score.schema.json:158`
- Evidence: `requires_human_review=true` 且没有原因时 Schema 验证通过；由于根对象 `additionalProperties=false`，增加 `human_review_reasons` 反而被拒绝。
- Violated: DEC-001 #8；设计 §14 要求 `judge-score.json` 和 `effective-score.json` 保留 review 状态与触发原因。
- Expected: 增加受约束的 review reason 字段，并在 `requires_human_review=true` 时要求至少一个原因；false 时禁止或约束为空。

### R4 — Medium — Task Profile 标识与冻结的 scoring_profile 不一致

- Confidence: high
- Location: `profiles/flow-tracing-v1.yaml:7`、`profiles/bug-localization-v1.yaml:7`、`profiles/impact-analysis-v1.yaml:7`
- Evidence: 三个 Profile 都没有 `scoring_profile` 字段，只定义了带连字符的 `profile_version`；其他 Schema 和设计冻结的是 `flow_tracing_v1`、`bug_localization_v1`、`impact_analysis_v1`。现有测试没有核对二者映射。
- Violated: 设计 §6、§20；任务目标“版本明确的契约”。
- Expected: Profile 明确声明与 Schema 枚举完全一致的 `scoring_profile`，并测试文件、task_type、profile_version/scoring_profile 的一一对应关系。

### R5 — Medium — answer_evidence 只校验 Pointer 存在，不校验 quote 来源

- Confidence: high
- Location: `tests/schemas/_validators.py:127`
- Evidence: 将 pointer 指向 `/answer/explanation`、quote 设置为答案中不存在的 `TEXT NOT PRESENT`，`validate_answer_evidence_pointers` 仍然通过。
- Violated: 设计 §10.2“quote 确实来自被引用字段”；任务验收中的坏引用反例。
- Expected: 解析 pointer 后验证引用目标是可引用文本且包含 quote，增加 quote 不匹配的负例和 JSON Pointer 证据。

### R6 — Medium — 任务卡要求的 artifact 枚举未落地

- Confidence: high
- Location: `schemas/run-metadata.schema.json:7`
- Evidence: 八个 Schema 中不存在 artifact name/type/inventory 的受约束字段，无法机器表示 §17 的允许 artifact 集合或“可选 artifact 未生成时不创建空文件”的状态。全仓 Schema 搜索没有 artifact enum 命中。
- Violated: AIS-002 验收“状态和 artifact 枚举受约束”；设计 §17。
- Expected: 明确 artifact inventory/status 的协议归属并加入受约束枚举及测试；如果 v1 不需要该字段，应由控制器先修订任务卡和设计，而不是默认为已通过。

### R7 — Low — date-time 测试检查器接受非 RFC 3339 日期

- Confidence: high
- Location: `tests/schemas/conftest.py:20`
- Evidence: `datetime.fromisoformat` 接受 `2026-07-30`，因此 `started_at`/`ended_at` 只有日期时仍通过 `format: date-time` 验证。
- Violated: JSON Schema Draft 2020-12 `date-time` 格式语义；测试注释声称执行 RFC 3339 校验。
- Expected: 使用严格 RFC 3339 checker 或实现等价严格校验，并增加 date-only、无时区时间的反例。

## Verification evidence

- `python -m pytest tests/schemas tests/profiles -q`: `108 passed`
- `python -m pytest tests -q`: `110 passed`
- `python -m ruff check .`: pass
- `python -m ruff format --check .`: pass
- `python -m pip check`: pass
- `git diff --check`: pass
- Targeted protocol counterexamples: R1–R7 reproduced.

## Remediation packet

Base remediation on `8be8bb5da71aca6864ac556e89a5c664c809aaf6`.

Resolve: `R1`, `R2`, `R3`, `R4`, `R5`, `R6`, `R7`.

Controller decisions:

- R2: keep `judge_model` as the verified effective model; add required `judge_requested_model` for the CLI request value. Reject `Auto` and unpinned `latest` for both. Per-call A/B/C invocation metadata remains an AIS-008 adapter responsibility; a formal Score is emitted only after requested/effective model consistency is verified.
- R6: add `manifest.schema.json` as the ninth contract. It owns the allowed artifact-name and status enums. `present` entries require relative path and digest; optional artifacts use `absent` or `not_applicable` instead of empty files; failed required work uses `failed`; v1 `adjudication` is always `not_applicable`.

Stay within the original AIS-002 scope plus the controller-authorized dependency files. Do not modify design or task cards. The R2/R6 controller decisions above are authoritative for this remediation.

Return a new commit SHA, resolution notes for every finding ID, tests added or updated, complete verification results, remaining risks, and a complete `AGENT_RESULT`.

## Remediation round 1 receipt

- Executor status: `READY_FOR_REVIEW`
- Base: `8be8bb5da71aca6864ac556e89a5c664c809aaf6`
- Head: `13d303c024f4132c7e00f5e729003f0194491491`
- Scope check: passed; changed paths are limited to schemas, profiles, and schema/profile tests.
- Working tree: clean; base is an ancestor of head.
- Verification: schema/profile `165 passed`; full suite `167 passed`; ruff check, format check, pip check, and `git diff --check` all pass.
- Independent spot checks: R1 fallback validates; R2 rejects `Auto`/`latest` and the requested/effective mismatch checker rejects disagreement; R3 reason conditions validate; R4 profile mappings validate; R5 quote mismatch is rejected; R6 manifest enum/path/digest conditions validate; R7 date-only and timezone-less timestamps are rejected.
- Review disposition: remediation is ready for a second independent review; it is not yet `VERIFIED` or `INTEGRATED`.
- Non-blocking note: several new schema descriptions contain mojibake section markers (`搂`) from the executor output; behavior and tests are unaffected, but descriptions should be cleaned before integration if documentation quality is required.

## Independent review of remediation round 1

Reviewed range: `8be8bb5da71aca6864ac556e89a5c664c809aaf6..13d303c024f4132c7e00f5e729003f0194491491`

Verdict: `PASS_WITH_NOTES`

Evidence:

- Base is an ancestor of the remediation head; the worktree is clean.
- Changed paths remain within the authorized schemas, profiles, and tests scope.
- All nine schemas contain `$schema`, `$id`, and a required business `schema_version` field.
- Independent targeted checks passed for R1 fallback handling, R2 forbidden models and requested/effective mismatch, R3 review reasons, R4 profile identity mapping, R5 quote integrity, R6 manifest constraints, and R7 strict timestamp rejection.
- Schema metadata validation passed for all nine schemas.
- `pytest tests/schemas tests/profiles -q`: `165 passed`.
- `pytest tests -q`: `167 passed`.
- `ruff check .`, `ruff format --check .`, `pip check`, and `git diff --check`: all passed.

Finding disposition:

- R1–R7: resolved; no blocking findings remain.
- Note N1 (non-blocking): new schema descriptions contain mojibake section markers (`搂`). This does not affect validation behavior, but should be cleaned as documentation polish before integration.

The task was `VERIFIED` after this review and is now `INTEGRATED` through commits `10736ca` and `dec808b`. Post-integration regression verification remains green.
