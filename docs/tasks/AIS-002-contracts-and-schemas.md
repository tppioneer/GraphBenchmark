# AIS-002: 定义并验证数据契约和 Profile

State: DRAFT

## Objective

把 case、GT、Agent Answer、运行元数据、策略结果、Judge 输入/输出和 Score 变成机器可验证、版本明确的契约。

## Source of truth

- Design: `docs/ai-scoring-design.md` §5–§10、§17、§20
- Base: `ai-score-v1`
- Dependencies: DEC-001, AIS-001

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: 派发时创建的绝对 worktree 路径
- Branch: `codex/ais-002-contracts-schemas`
- Expected HEAD: 派发时填写完整 SHA
- Return channel: commit + AGENT_RESULT

## Invariants

- Agent Answer 不包含 Runner 采集的身份、工具、策略或成本字段。
- `answer.explanation` 是可独立评分的主要答案；可选结构缺失不能自动使核心正确性归零。
- Agent 不接触 GT rubric item ID。
- 未生成的可选 artifact 不以空文件占位。
- 所有正式分数携带版本与输入 digest。

## Allowed scope

- `schemas/*.schema.json`
- `profiles/common.yaml`
- `profiles/flow-tracing-v1.yaml`
- `profiles/bug-localization-v1.yaml`
- `profiles/impact-analysis-v1.yaml`
- `tests/schemas/`, `tests/profiles/`

## Excluded scope

- Artifact 解析器
- Rubric 业务校验和评分算法
- Judge API 调用
- Runner 与报告实现

## Acceptance criteria

- 设计 §18 列出的八类 Schema 均存在，并锁定 `$schema`、`$id` 和业务版本字段。
- 三类任务、finding kind、credit、状态和 artifact 枚举受约束。
- 正例覆盖完整结构和最小合法结构；反例覆盖身份泄漏、非法 credit、未知 item、坏引用和 digest 缺失。
- Profile 共同维度合计 100，且 Profile 不能改写共同 Judge 输出协议。
- Schema 错误包含可定位的 JSON Pointer。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/schemas tests/profiles -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- Schema/Profile 清单及版本。
- 正反例测试结果。
- 与 DEC-001 的逐项一致性说明。
