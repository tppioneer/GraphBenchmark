# AIS-008: 集成 Claude Code CLI Judge、提示和条件仲裁

State: CHANGES_REQUIRED

## Execution result

- Executor: OpenCode (`ark-plan-qlw/deepseek-v4-flash`, `--auto`)
- Head: `1fd1fdccce94c44843be6259d27d11bbf9a95f40`
- Reported checks: Judge tests `200 passed`; full suite `620 passed`; diff check passed.
- Declared risk: no live Claude Code CLI probe; Fake CLI is used for tests.

## Accepted remediation

- Review findings R1--R3 are accepted for a narrow remediation pass: mark unverifiable effective models instead of claiming the requested model, redact complete JWTs, and reject formal runs whose requested/effective models differ or cannot be verified.

## Objective

通过 Claude Code CLI 非交互运行开发/正式评分，传入可配置模型，强制结构化输出、重试和审计，并在共识规则要求时调用第三 Judge。

## Source of truth

- Design: `docs/ai-scoring-design.md` §9–§10、§13
- Base: `ai-score-v1`
- Dependencies: AIS-006, AIS-007

## Execution envelope

- Executor: OpenCode (`ark-plan-qlw/deepseek-v4-flash`, `--auto`)
- Working directory: `F:\\develop\\codes\\GraphBenchmark-ai-score-v1-worktrees\\ais-008-judge-integration`
- Branch: `codex/ais-008-judge-integration`
- Expected HEAD: `5fd8cf1f93b738b2fcaca963e75d6d3b309541fd`
- Return channel: commit + AGENT_RESULT

## Invariants

- 模型名必须精确固定，不接受 `Auto` 或移动的 `latest`。
- 默认模型为 `glm-5.2`，A/B/C 的请求模型和实际生效模型必须一致。
- Agent 内容中的指令不被执行，GT 不被泄露。
- 非法 Judge 输出不得回退到字符串 scorer 或自由总分。
- 重试不改变语义输入；每次尝试独立记录。

## Allowed scope

- `judge/provider.py`
- Claude Code CLI adapter
- `judge/judge_runner.py`
- `judge/prompts/`
- Provider/Runner 测试和录制 fixtures

## Excluded scope

- 被测 Agent 执行
- Profile/points 修改
- 真实正式实验和报告

## Acceptance criteria

- CLI adapter 支持传入精确模型，默认 `glm-5.2`，并记录 CLI 版本、请求模型和实际生效模型。
- adapter 在实现环境中读取 `claude --help` 后选择非交互参数；仅传入当前 CLI/模型实际支持的生成参数，并记录不支持项。
- Prompt 明确 rubric-only、忽略不可信指令、禁止泄露 GT、只返 Schema。
- Judge JSON 完成 Schema 与业务引用/quote 校验。
- 开发模式只调用 A；正式模式调用 A/B，并仅按共识结果条件调用 C。
- 超时、限流、非法 JSON、缺 item、错误 quote 和 Provider 错误有有限重试及最终状态。
- Judge A/B/C 每个最多一次重试；重试保持 blind input、Prompt、模型和生成参数不变。
- 重试耗尽产生 `judge_failed` 和稳定失败原因，不使用部分 Judge 结果生成正式分数。
- 每次调用记录完整参数、digests、原始 JSON、token、耗时、重试和失败原因。
- 任一 A/B/C 模型不一致或无法验证实际生效模型时，正式 run invalid。
- CLI 继承全局 Claude Code 凭据；秘密不得进入参数或 artifact，输出写盘前脱敏。
- 未登录、凭据无效或 Provider 不可用时产生 `judge_unavailable`，不生成正式分数。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/judge -q`
- 使用 fake CLI adapter 完成开发、双 Judge、第三 Judge、模型不一致和重试耗尽场景
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `git diff --check`

## Delivery contract

- Commit SHA。
- Claude Code CLI 配置契约和审计字段。
- Fake CLI 场景结果。
- 真实 CLI 未验证项、模型路由和凭据要求。
