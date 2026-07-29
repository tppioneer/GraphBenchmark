# AIS-010: 可选建立三个 Profile 的人工校准诊断

State: DRAFT

## Objective

可选为 Flow、Bug、Impact 构造代表性 GT 和多质量答案，由人工逐 item 标注并量化 Judge 稳定性、偏差和 critical 规则表现；本任务不阻塞 v1 正式实验。

## Source of truth

- Design: `docs/ai-scoring-design.md` §5–§7、§12–§13、§21
- Base: `ai-score-v1`
- Dependencies: AIS-006, AIS-008

## Execution envelope

- Executor: human labelers + Codex orchestration
- Working directory: 派发时创建的绝对 worktree 路径
- Branch: `codex/ais-010-calibration-profiles`
- Expected HEAD: 派发时填写完整 SHA
- Return channel: versioned fixtures, calibration report, commit

## Invariants

- 三类任务都必须覆盖，不能用单一 case 类型外推。
- 人工标注逐 rubric item，不只给自由总分。
- 校准集不得包含正式评测答案泄漏。
- 诊断结果不产生 v1 发布 PASS/FAIL，也不阻塞正式实验。

## Allowed scope

- `cases/calibration/`
- 三个 `profiles/*.yaml` 的校准性调整
- `tests/calibration/`
- `report/calibration/` 或等价离线校准工具
- 校准报告文档

## Excluded scope

- 正式 Benchmark case/result
- 为特定模型措辞定制 scorer
- 在未记录版本变化时修改协议语义

## Acceptance criteria

- 每个任务至少覆盖：正确改写、关系反向、症状非根因、不完整范围、伪根因/同名噪声、关键词堆砌、无依据冗长、空/拒答、prompt injection。
- 每个样本有冻结 GT digest、人工 item credit、理由和证据。
- 计算 DEC-001 规定的相关性、一致率、critical 召回/误报、重复波动、A/B 顺序偏差和风格/长度偏差。
- 对未达标指标给出 Profile、Prompt、阈值或 GT 的可审计整改，并重新版本化。
- 分 Profile 展示诊断结果和限制，不以总体均值掩盖单一 Profile 风险。
- 报告明确声明该诊断不构成 v1 发布门槛。

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/calibration -q`
- 运行冻结的校准命令并重算报告
- 在相同输入上重复运行，确认 digest 和统计可复现
- `git diff --check`

## Delivery contract

- Commit SHA。
- 校准集版本和 digest。
- 人工标注方法、审核人范围和分歧处理。
- 指标结果、观察结论与残余偏差。
