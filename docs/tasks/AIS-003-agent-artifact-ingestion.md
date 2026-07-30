# AIS-003: 保存原始响应并生成 Agent Answer

State: IMPLEMENTING

## Objective

无论模型输出结构化 JSON 还是有效自然语言，都保存原始响应并产生明确状态的 `agent-answer.json`；空或不可读输出被确定性拒绝。

## Source of truth

- Design: `docs/ai-scoring-design.md` §8、§17
- Base: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Dependencies: AIS-002

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-003-agent-artifact-ingestion`
- Branch: `codex/ais-003-agent-artifact-ingestion`
- Expected HEAD: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Return channel: commit + AGENT_RESULT

## Invariants

- `raw-response.txt` 始终先于解析保存。
- 不回退到字符串 scorer。
- 自然语言降级包装只保存模型真实内容，不伪造 evidence、finding 或 summary。
- Schema compliance 与语义正确性是两个不同状态。

## Allowed scope

- `runner/execution.py`
- `runner/artifact_validation.py`
- 必要的 Runner 内部模型
- `tests/runner/test_agent_artifacts.py` 及 fixtures

## Excluded scope

- 工具策略判定和过程指标
- Judge 调用、评分或报告
- 修改 Schema 的语义

## Acceptance criteria

- 合法 JSON 被验证并规范写入 `agent-answer.json`。
- 非法 JSON 但非空的 Markdown 被包装为 `completed_with_schema_warning`。
- 空白、编码不可读和 I/O 失败有不同的可审计错误状态。
- finding 的 evidence 引用在存在时可验证；缺失可选数组不制造空 artifact 文件。
- 原始文本逐字节可追溯，并有 digest。
- 写入中断不会留下被误认为完整的最终 JSON。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/runner/test_agent_artifacts.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- 输入场景与最终状态对照表。
- 原子写入和 digest 测试证据。
- 未支持编码或超大响应的限制。
