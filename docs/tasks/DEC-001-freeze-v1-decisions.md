# DEC-001: 冻结 semantic_outcome_v1 决策

State: VERIFIED

## Objective

将会改变 Schema、评分语义、Judge 调度、校准或正式报告可比性的决策写成版本化结论，使后续实现不依赖隐含假设。

## Source of truth

- Design: `docs/ai-scoring-design.md` §5–§6、§8.5、§10–§16、§20–§23
- Base: `ai-score-v1`（当前无 commit）
- Dependencies: none

## Execution envelope

- Executor: human + architecture owner
- Working directory: `F:\develop\codes\GraphBenchmark-ai-score-v1`
- Branch: `ai-score-v1`
- Expected HEAD: N/A（决策任务）
- Return channel: 更新设计文档或新增 ADR，并附决策摘要

## Required decisions

### 协议与评分

1. **RESOLVED** — 五维权重冻结为 `35/25/20/10/10`；校准失败后的调整必须提升 Profile major version。
2. **RESOLVED** — 单 Judge credit 冻结为 `0/0.25/0.5/0.75/1`；共识 credit 保存精确平均或中位数。
3. **RESOLVED** — GT 作者显式指定 item points，Profile 只约束维度总分；评分时不自动重分配。
4. **RESOLVED** — 成功执行后的空白/拒答/无关答案计 0；执行或 artifact 失败为 invalid；核心 critical 全 0 cap 50，明确反向关键关系 cap 60，多 cap 取最低。
5. **RESOLVED** — critical item A/B 不同、非 critical 差异大于 0.25 或临时总分差大于 5 时调用 C；三 Judge critical 极差大于 0.5、critical 共识置信度低于 0.70 或 overall confidence 低于 0.65 时人工复核。

### 已确认规则的解释

- cap 只能由 Profile 允许的 critical error code 触发；普通遗漏不触发 cap。
- confidence 只控制人工复核路由，不参与分数乘法。
- 未调用 Judge C 时，A/B 逐 item 精确平均；调用 C 时逐 item 取中位数。
- 上述数值作为 v1 基线；人工校准仅为可选诊断，不是正式实验门槛。
- Claude Code CLI adapter 必须记录 CLI 版本、请求模型和可核验的实际生效模型；无法验证时正式 run invalid。
- v1 的 `effective item credit` 始终等于 Judge consensus credit。

### Judge 与数据边界

6. **RESOLVED** — Judge 使用 Claude Code CLI 非交互模式；模型参数可传入，默认 `glm-5.2`；A/B/C 和同一实验前后使用相同的请求模型与实际生效模型。
7. **RESOLVED** — Judge 允许查看 GT 作者预选的静态源码 excerpt，采用单条 80 行/12,000 字符、单 case 40,000 字符上限，并记录 revision、位置和 digest；Judge 不直接访问仓库。
8. **RESOLVED/DEFERRED** — v1 人工 adjudication 只保留 `requires_human_review` 状态和原因，不实现人工 credit、审批或覆盖；未来启用时提升协议版本。
9. **RESOLVED** — v1 不支持跨 Judge 模型聚合；A/B/C 或同一实验前后模型不一致时，受影响的当前实验数据 invalid。

### 实验与发布

10. **RESOLVED** — v1 不实现 Pairwise Judge，不直接判断两个答案谁更好；正式比较只使用 absolute score 和配对分差。
11. **RESOLVED** — Judge 金额、Token 和耗时不作为正确性或发布门槛；A/B/C 每个最多重试一次，重试耗尽的答案标记 `judge_failed` 且不生成正式分数。
12. **RESOLVED/DEFERRED** — v1 不设置必须通过的人工校准门槛；人工相关性、item 一致率、critical 召回/误报和重复波动只作为可选诊断。

### 工程基线

13. **RESOLVED** — 使用仓库 `.venv` 的 Python 3.12（当前 3.12.10），`pyproject.toml` 管理运行/开发依赖和工具配置，测试使用 `pytest`/`ruff`，默认 Agent/Judge 为全局 Claude Code CLI 2.1.220，项目 CLI 为 `graphbenchmark`。
14. **RESOLVED** — 继承全局 Claude Code 登录和 Provider 配置；秘密不进入仓库、命令行或 artifact，日志写盘前脱敏，凭据不可用时标记 `judge_unavailable`。

## Invariants

- AI Judge 只输出 item verdict，不自由分配 points 或总分。
- 正确性与工具过程/成本分开。
- Agent 身份、模型与工具策略不进入 blind input。
- 原始响应始终保存；合法自然语言不能因 JSON 失败自动归零。
- 正式分数不得跨不兼容协议、Profile、模型、Prompt、GT 或共识版本混合。

## Allowed scope

- `docs/ai-scoring-design.md`
- `docs/adr/`
- 本任务卡状态

## Excluded scope

- 生产代码、Schema 或测试实现
- 实际调用 Judge
- 运行正式 Benchmark

## Acceptance criteria

- 14 项决策均有唯一结论、理由、版本影响和生效范围。
- cap、共识、人工复核和版本隔离规则不存在互相矛盾的边界情况。
- 具名 Judge 模型和 Provider 可由配置精确复现，不使用 `Auto`/`latest`。
- 未实现或延期的校准能力被明确标记，不能在报告中宣称已经完成人工稳定性验证。
- 设计状态更新为可实现，或明确列出仍阻塞的外部依赖。

## Verification

- 人工逐项核对本卡 14 项是否在设计或 ADR 中有可引用结论。
- `git diff --check`

## Delivery contract

- 设计/ADR 的 commit SHA 或 exact diff。
- 14 项决策映射表。
- 未决问题和外部依赖；当前协议决策无未决项。
- 对既有任务卡状态与依赖的影响。
