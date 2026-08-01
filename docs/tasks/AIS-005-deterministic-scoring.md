# AIS-005: 确定性聚合 item、维度、总分和 critical cap

State: CHANGES_REQUIRED

## Objective

只使用已验证的 GT points 和 Judge credit，确定性生成 item 分、维度分、raw total、cap 和 capped total。

## Source of truth

- Design: `docs/ai-scoring-design.md` §10–§12、§20
- Base: `ai-score-v1`
- Dependencies: AIS-004

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-005-deterministic-scoring`
- Branch: `codex/ais-005-deterministic-scoring`
- Expected HEAD: `bc43a7b5c87e93fee474aacc14df0d61a5d7ed9b`
- Return channel: commit + AGENT_RESULT

## Execution result

- Executor: Claude Code (`glm-5.2`)
- Head: `f36711e2682cd36d6ca89a3a45a7fef8da36f0c5`
- Reported checks: task tests `62 passed`; full suite `464 passed`; Ruff and diff check passed

## Review evidence

- Review: `docs/reviews/AIS-005-review-1.md`
- Reviewed head: `f36711e2682cd36d6ca89a3a45a7fef8da36f0c5`
- Verdict: `CHANGES_REQUIRED`
- Accepted finding: `AIS005-F1`

## Invariants

- 公式固定为 `points × credit` 后求和。
- Judge 不能改变 points、维度权重、总分公式或 cap。
- 空答/拒答、核心全错和关键方向相反按冻结规则处理。
- 输出保留未封顶分和 cap 理由。

## Allowed scope

- `scoring/aggregator.py`
- 评分输出内部模型
- `tests/scoring/test_aggregator.py`

## Excluded scope

- 多 Judge 共识
- 人工 adjudication
- Provider、缓存或报告

## Acceptance criteria

- 每个 GT item 恰好匹配一个 verdict，未知/重复/缺失 item 被拒绝。
- Decimal/舍入策略明确，item、维度和总分加总无漂移。
- cap 只降低不提高分数；多条 cap 按最严格规则组合。
- 输出包含 item points、dimension totals、raw/capped total、cap code 和版本元数据。
- 边界测试覆盖全 0、全 1、分数台阶、方向相反、多个 cap 和无 cap。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/scoring/test_aggregator.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- 算法与舍入说明。
- critical cap 决策表和测试证据。
- 完整检查结果。
