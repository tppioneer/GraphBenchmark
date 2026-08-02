# AIS-012: 端到端冻结并执行正式实验

State: IMPLEMENTING

## Execution

- Executor: Claude Code (`glm-5.2`, automatic permission mode)
- Fixed base: `23731d7424a776a55c2ccaf152820d3e3ee4fdcc`
- Isolated worktree: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-012-e2e-release`
- Dispatch mode: preflight first; formal Judge execution is permitted only after all frozen-input and credential-availability gates pass.

## Objective

以冻结的协议、Profile、GT、模型、Prompt、共识算法和代码运行完整配对实验，产出可审计、可复算的正式结果。

## Source of truth

- Design: `docs/ai-scoring-design.md` §16–§17、§20–§24
- Base: `ai-score-v1`
- Dependencies: AIS-008, AIS-009, AIS-011

## Execution envelope

- Executor: Codex orchestration + approved experiment runner
- Working directory: 派发时创建的绝对 worktree 路径
- Branch: `codex/ais-012-e2e-release`
- Expected HEAD: 派发时填写完整 SHA
- Return channel: immutable run artifacts, release report, commit/tag proposal

## Invariants

- 同 case/repeat 的 Graph/Grep 使用相同被测模型、Agent 和 Judge 协议条件。
- 正式模式双 Judge，按规则条件仲裁。
- 所有身份/工具策略对 Judge 盲化。
- 无效 run 不进入正确性统计；失败不得回退旧 scorer。
- 任何协议输入变化都产生新版本/digest，不覆盖旧结果。

## Allowed scope

- 正式实验 manifest/config
- `cases/` 中已批准正式 GT
- run artifacts 的受控输出位置
- 发布报告与冻结清单
- 仅为阻断正式运行的窄幅集成修复

## Excluded scope

- 运行中调整权重、Prompt、GT、cap 或 Judge 模型
- 混合不兼容分数
- 隐式重试后丢弃失败审计
- 未批准的生产或外部系统变更

## Acceptance criteria

- 预检确认所有强制依赖 `VERIFIED`、凭据可用、模型精确固定；人工校准不是 v1 强制依赖。
- 预检确认 Claude Code CLI 版本固定、`judge_model` 已传入且实际生效模型可核验；默认模型为 `glm-5.2`。
- 预检只确认全局 Claude Code 凭据可用，不读取或写入秘密值；不可用时停止并记录 `judge_unavailable`。
- 冻结清单包含代码 SHA、协议/Profile/Prompt/GT/共识版本和全部 digests。
- 每个 run artifact 满足 §17，策略、Judge、缓存、复核和报告状态可追踪。
- Judge 成本不阻塞发布；`judge_failed` 不进入正式分数并在完整性报告中单列。
- 配对统计只消费完整匹配对，并单列缺失/无效原因。
- 从冻结 artifacts 离线重建报告，不发生 Judge 网络调用且结果一致。
- 发布报告包含 absolute 质量、配对分差、成本、稳定性、限制和残余风险。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests -q`
- 先执行全链路 smoke experiment，再执行正式 manifest
- 运行 artifact/schema/policy 完整性检查
- 在隔离环境离线重建最终报告并比对 digest
- `git diff --check`

## Delivery contract

- 实验代码 commit SHA 与冻结清单。
- run artifact 根目录及完整性报告。
- 正式报告、离线重算证据和 Judge 调用审计。
- 无效/缺失 pair、人工复核和残余风险清单。
