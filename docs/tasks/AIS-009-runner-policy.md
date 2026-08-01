# AIS-009: 运行 Agent 并独立采集策略与成本指标

State: READY_FOR_REVIEW

## Objective

Runner 以固定实验条件执行被测 Agent，生成完整 run artifact，并独立判断 Graph/Grep 策略合规和采集过程指标。

## Source of truth

- Design: `docs/ai-scoring-design.md` §8.6–§8.8、§15、§17
- Base: `ai-score-v1`
- Dependencies: AIS-003, AIS-004

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-009-runner-policy`
- Branch: `codex/ais-009-runner-policy`
- Expected HEAD: `bc43a7b5c87e93fee474aacc14df0d61a5d7ed9b`
- Return channel: commit + AGENT_RESULT

## Execution result

- Executor: Claude Code (`glm-5.2`)
- Head: `55ff91a6dd310fe9f2f6890d018e05151d7c1439`
- Reported checks: task tests `40 passed`; full suite `442 passed`; Ruff and diff check passed

## Review evidence

- Review: `docs/reviews/AIS-009-review-1.md`
- Reviewed head: `55ff91a6dd310fe9f2f6890d018e05151d7c1439`
- Verdict: `CHANGES_REQUIRED`
- Accepted findings: `AIS009-R1`, `AIS009-R2`, `AIS009-N1`
- Remediation head: `8a593e84d145e44b9a920bf378acf386ae72127d`
- Remediation checks: task tests `53 passed`; full suite `455 passed`; Ruff and diff check passed

## Invariants

- Agent 不自报身份、工具、成本或违规。
- Graph 组无真实 Graph 调用、Grep 组调用 Graph 均使 run invalid。
- 过程指标不进入 correctness total。
- 不可评分状态不能回退到旧 scorer。

## Allowed scope

- `runner/benchmark_runner.py`
- `runner/policy_validation.py`
- Runner 生命周期协调代码
- `tests/runner/test_benchmark_runner.py`
- `tests/runner/test_policy_validation.py`

## Excluded scope

- 具体 Judge Provider
- 正确性聚合算法
- 报告展示

## Acceptance criteria

- 每个 run 有 manifest、raw response、answer、metadata 和 policy result；Judge artifact 按状态后续产生。
- Runner 采集开始/结束时间、tokens、工具/文件/Graph/Search 数量及可用错误信息。
- 工具事件有可核验来源，Agent 输出不能伪造合规。
- run 状态至少区分 valid、invalid、awaiting-judge、failed。
- 中断重启不会把半成品当成功；同一 run ID 不静默覆盖不同输入。
- correctness 与成本/策略字段在类型和存储层面隔离。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/runner/test_benchmark_runner.py tests/runner/test_policy_validation.py -q`
- 使用 fake Agent 覆盖 Graph 合规、Graph 缺失、Grep 越权、Schema warning、空响应和执行失败
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- Run 状态机和 artifact 生命周期。
- 策略真值表及 fake Agent 测试证据。
- 尚未支持的 Agent/工具适配器。
