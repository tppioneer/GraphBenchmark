# AIS-006: 形成 Judge 共识和 v1 有效分

State: DRAFT

## Objective

根据冻结的分歧规则决定是否需要 Judge C，逐 item 形成共识，记录待人工复核状态，并以共识 credit 生成 v1 `effective-score.json`。

## Source of truth

- Design: `docs/ai-scoring-design.md` §13–§14
- Base: `ai-score-v1`
- Dependencies: AIS-005

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: 派发时创建的绝对 worktree 路径
- Branch: `codex/ais-006-consensus-adjudication`
- Expected HEAD: 派发时填写完整 SHA
- Return channel: commit + AGENT_RESULT

## Invariants

- 正式模式不依赖单次 Judge；开发模式必须显式标识。
- 仲裁决策逐 item 进行。
- v1 不实现或应用人工 credit。
- `requires_human_review` 只表示状态，不改变共识分。

## Allowed scope

- `judge/consensus.py`
- `tests/judge/test_consensus.py`
- effective-score 组装和测试

## Excluded scope

- 发起模型调用
- 盲评 payload 和缓存
- 报告可视化
- 人工 adjudication 的编辑、审批、失效和分数覆盖

## Acceptance criteria

- A/B item 差异、critical 分歧、总分差和低置信度按 DEC-001 精确触发 Judge C/人工复核。
- 三 Judge 按 item 中位数聚合；平均值产生非 credit 枚举值时的表示规则明确。
- 缺失、非法或协议不一致的 Judge 输出不被静默纳入共识。
- 持续分歧或低置信度设置 `requires_human_review` 和稳定原因码。
- `effective item credit` 始终等于当前 Judge consensus credit。
- 不创建 `adjudication.json` 空文件，也不接受人工覆盖输入。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/judge/test_consensus.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- 共识状态机和仲裁真值表。
- 人工复核占位状态和禁止覆盖的测试证据。
- 完整检查结果。
