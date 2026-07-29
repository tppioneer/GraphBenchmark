# AIS-011: 生成 absolute、paired、稳定性与成本报告

State: DRAFT

## Objective

从冻结 artifact 生成可重算但不重跑 Judge 的 case、配对和汇总报告，分开呈现结果质量、合规性、稳定性与成本。

## Source of truth

- Design: `docs/ai-scoring-design.md` §15–§16、§19–§20
- Base: `ai-score-v1`
- Dependencies: AIS-006, AIS-009

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: 派发时创建的绝对 worktree 路径
- Branch: `codex/ais-011-reporting`
- Expected HEAD: 派发时填写完整 SHA
- Return channel: commit + rendered sample report + AGENT_RESULT

## Invariants

- 报告不调用 Judge，不隐式重新评分。
- correctness 不与成本合成单一不透明总分。
- 主比较使用同 case/repeat/model/agent/protocol/Profile/Judge 的配对分差。
- 不兼容版本不得进入同一正式聚合。

## Allowed scope

- `report/aggregate.py`
- `report/analysis_input.py`
- `report/visualization/`
- `tests/report/`
- 合成报告 fixtures

## Excluded scope

- Judge、Runner 或评分核心变更
- 正式实验数据
- 跨协议“校正”分数

## Acceptance criteria

- case 展示总分、五维、cap、item verdict、共识、仲裁和人工复核状态。
- 汇总展示配对 absolute 分差、有效/无效 run、Judge 分歧、复核覆盖率和独立成本指标。
- 报告只消费 absolute score 和配对分差，不生成或展示 Pairwise preference。
- 缺 artifact、awaiting-judge、invalid 和版本混合均显式失败或隔离。
- Judge 请求模型或实际生效模型不一致的数据标记 invalid，不进入任何正式聚合。
- Judge 金额、Token 和耗时只作为独立指标；完整分数不因成本高而失效。
- `judge_failed` 答案单列失败原因，不生成或推算正式分数。
- 相同 artifact 重算产生稳定结果且 Judge 调用计数为零。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/report -q`
- 用合成 fixtures 生成报告并与批准的结构快照比较
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- 样例报告及其输入 digest。
- 聚合兼容性矩阵。
- 完整检查结果和显示限制。
