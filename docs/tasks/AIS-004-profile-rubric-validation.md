# AIS-004: 加载 Profile 并验证 Ground Truth Rubric

State: VERIFIED

## Objective

在任何 Judge 调用前，确定性验证 Profile 与 GT Rubric 的维度、points、critical 条件、引用和盲化安全性。

## Source of truth

- Design: `docs/ai-scoring-design.md` §5–§7
- Base: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Dependencies: AIS-002

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-004-profile-rubric-validation`
- Branch: `codex/ais-004-profile-rubric-validation`
- Expected HEAD: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Return channel: commit + AGENT_RESULT

## Invariants

- 每个 item 只属于一个共同维度。
- points 由 GT/Profile 决定，Judge 无权修改。
- GT 不泄露被测策略或答案来源。
- critical item 必须有明确零分条件。

## Allowed scope

- `scoring/profiles.py`
- `scoring/rubric_validator.py`
- `tests/scoring/test_profiles.py`
- `tests/scoring/test_rubric_validator.py`

## Excluded scope

- 分数计算和 cap 执行
- Judge 输出验证
- case 内容编写和人工校准

## Acceptance criteria

- Profile 名、major version、任务类型和共同协议交叉验证。
- item ID 唯一稳定，points 为正，各维度和总分严格等于 Profile 约束。
- unknown dimension、重复 ID、缺失 critical zero condition、坏 reference 和泄漏字段均失败。
- 验证错误一次报告全部可行动问题，并带 item/path 定位。
- 同一输入验证结果确定且不依赖字典顺序。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/scoring/test_profiles.py tests/scoring/test_rubric_validator.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Review evidence

- Review: `docs/reviews/AIS-004-review-1.md`
- Reviewed head: `0ecb9212f56bf5df362924076ffb28fafd4fd238`
- Verdict: `CHANGES_REQUIRED`
- Open findings: `AIS004-R1`, `AIS004-R2`
- Remediation round 1: `CHANGES_REQUIRED`
- Remediation head: `763d3c1df818dce5dc8783b79258932c01cd84d0`
- Finding status: `AIS004-R1`, `AIS004-R2` remain open
- Remediation round 2: `PASS`
- Reviewed remediation head: `4491a2a7dd5eb6f437da94f48ca00ae0e1a279ab`
- Finding status: `AIS004-R1`, `AIS004-R2` resolved

## Delivery contract

- Commit SHA。
- 校验规则与错误代码列表。
- 设计 §7.2 九条规则的测试映射。
- 完整检查结果。
