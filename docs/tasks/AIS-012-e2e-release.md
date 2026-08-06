# AIS-012: 端到端冻结并执行正式实验

State: READY_FOR_REVIEW

## Execution

- Executor: Claude Code (`glm-5.2`, automatic permission mode)
- Fixed base: `b32068f` (`ai-score-v1`)
- Isolated worktree: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-012-e2e-release`
- Dispatch mode: preflight first; formal Judge execution is permitted only after all frozen-input and credential-availability gates pass.
- Preflight 1 (2026-08-03): BLOCKED. No approved formal cases/GT, no frozen manifest, no AgentAdapter, Judge auth inactive.
- Preflight 2 (2026-08-06): PASS. All four blockers resolved (formal cases/GT integrated, formal config integrated, AgentAdapter+dispatcher integrated, Claude auth active). See `docs/reviews/AIS-012-preflight-2.md`.

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

## Execution result

- Head commit: `2f222a0` (ai-score-v1)
- Runs root: `F:\develop\00-codes\benchmark-runtime\runs\GraphBenchmark-ai-score-v1`
- Freeze manifest: `freeze-manifest.yaml` in runs_root

### Agent phase (6 runs)

| Run | Status | Elapsed | Graph q | Search | File read |
|---|---|---|---|---|---|
| graph r01 | awaiting-judge | 171s | 1 | 2 | 7 |
| graph r02 | awaiting-judge | 309s | 2 | 3 | 5 |
| graph r03 | invalid | 180s | 0 | 5 | 7 |
| grep r01 | awaiting-judge | 181s | 0 | 2 | 8 |
| grep r02 | awaiting-judge | 182s | 0 | 5 | 7 |
| grep r03 | awaiting-judge | 189s | 0 | 6 | 5 |

### Judge phase (formal mode, 2 Judges + arbiter)

| Run | Judge result | Scored | Capped total |
|---|---|---|---|
| graph r01 | 2 judges, mean consensus | yes | 85.00 |
| graph r02 | judge_failed (invalid JSON) | no | - |
| graph r03 | skipped (policy invalid) | no | - |
| grep r01 | judge_failed (invalid JSON) | no | - |
| grep r02 | judge_failed (invalid JSON) | no | - |
| grep r03 | 3 judges, median consensus, arbiter | yes | 85.00 |

### Report

- Paired pairs: 1 (graph r01 vs grep r03, diff=0.00)
- Scored: 2, judge_failed: 3, invalid: 1
- Offline rebuild: digest `sha256:e2500981...` verified identical, 0 Judge calls
- Report: `report.md`, `report.json` in runs_root

### Acceptance criteria

- Preflight gates all PASS: yes
- CLI version pinned (2.1.223), judge_model effective (glm-5.2): yes
- Credentials confirmed (no secret read/written): yes
- Freeze manifest with code SHA, protocol, digests: yes
- Run artifacts satisfy §17: yes
- judge_failed isolated and listed, not in formal score: yes
- Paired stats consume complete pairs only, missing reasons listed: yes
- Offline rebuild digest match, 0 Judge calls: yes
- Report includes absolute, paired diff, cost, stability, limitations: yes

### Residual risks

- Only 1 of 3 possible paired comparisons (2/6 runs scored)
- 3 Judge failures due to invalid JSON output from glm-5.2 (50% Judge failure rate)
- 1 Graph run policy-invalid (agent did not use Graph tools)
- Single paired comparison uses runs from different repeats (r01 vs r03)
- Judge token usage not captured (provider could not extract from CLI result wrapper)

### Narrow integration fixes (allowed scope)

- `3c043fb`: adapter `--verbose` for CLI 2.1.223 stream-json
- `98ed513`: adapter `.CMD` -> `.exe` resolution + timeout 600s -> 1200s
- `b963a9f`: judge provider `.CMD` -> `.exe` CLI discovery
- `ebf8572`: judge provider UTF-8 decoding + formal pipeline script
- `d296013`: skip `--json-schema` (incompatible with CLI 2.1.223)
- `75c6fc7`: parse judge output from CLI result wrapper
- `scripts/formal_pipeline.py`: Judge -> consensus -> scoring -> report glue
