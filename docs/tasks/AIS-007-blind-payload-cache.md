# AIS-007: 生成盲评输入、digest 与缓存键

State: READY_FOR_REVIEW

## Objective

从 case、Profile、GT 和 Agent Answer 构造不泄露实验身份的 Judge 输入，并以完整、规范化输入生成可审计 digest 和缓存键。

## Source of truth

- Design: `docs/ai-scoring-design.md` §9、§13.3、§20
- Base: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Dependencies: AIS-002

## Execution envelope

- Executor: Codex subagent or external terminal agent
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-007-blind-payload-cache`
- Branch: `codex/ais-007-blind-payload-cache`
- Expected HEAD: `6898c25c8dd8ee0c91df776699dc3b6ce76dfddc`
- Return channel: commit + AGENT_RESULT

## Invariants

- tool policy、Agent/模型身份、工具输出、成本、已有分数和候选身份不可见。
- Agent Answer 是不可信数据，不得变成系统指令。
- 相同语义输入的规范序列化和 digest 稳定。
- 任一影响 Judge 结果的输入或版本变化都必须缓存失效。

## Allowed scope

- `judge/blind_payload.py`
- `judge/cache.py`
- digest/canonical JSON 辅助模块
- `tests/judge/test_blind_payload.py`
- `tests/judge/test_cache.py`

## Excluded scope

- Provider 网络调用
- Prompt 文案
- Judge 共识和评分

## Acceptance criteria

- allowlist 构造 blind input，禁止仅靠 denylist 删除敏感字段。
- payload 对答案和 GT 使用明确数据边界并携带所需版本/digest。
- 递归泄漏测试覆盖嵌套 metadata、文件名、策略标签和未知扩展字段。
- 缓存键包含模型、Provider、生成参数、Prompt、GT、答案、Profile、盲化和协议版本。
- 缓存命中不重写原始 Judge artifact；损坏/不完整缓存被拒绝。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/judge/test_blind_payload.py tests/judge/test_cache.py -q`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- Blind input allowlist。
- Cache key 字段表和失效测试证据。
- 完整检查结果。
