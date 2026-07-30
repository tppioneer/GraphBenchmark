# AIS-001: 建立可测试的 Python 工程骨架

State: INTEGRATED

## Objective

提供可安装、可测试、职责分层明确的最小工程，使后续任务能在稳定的包、配置和测试入口上独立开发。

## Source of truth

- Design: `docs/ai-scoring-design.md` §4、§18
- Base: `edc7fbdfbf819da85275fc54cc7107a9fbb250be`
- Dependencies: 首个基线 commit

## Execution envelope

- Executor: Claude Code CLI
- Working directory: `F:\develop\codes\GraphBenchmark-ai-score-v1-worktrees\ais-001-project-foundation`
- Branch: `codex/ais-001-project-foundation`
- Expected HEAD: `edc7fbdfbf819da85275fc54cc7107a9fbb250be`
- Return channel: commit + AGENT_RESULT

## Invariants

- `runner` 不负责语义评分。
- `judge` 不决定权重或自由总分。
- `scoring` 不调用被测 Agent。
- `report` 不调用或重跑 Judge。

## Allowed scope

- `pyproject.toml`
- `runner/`, `judge/`, `scoring/`, `report/`, `tests/` 的包初始化和共享测试配置
- 必要的开发依赖锁定文件

## Excluded scope

- 业务 Schema、评分算法、Provider、报告逻辑
- 示例 case 和正式实验配置
- 修改设计文档

## Acceptance criteria

- 项目可在干净环境安装。
- `pyproject.toml` 声明 `requires-python = ">=3.12,<3.13"`，并管理运行依赖、`pytest` 和 `ruff`。
- 四个职责包可被导入，且不存在相互循环导入。
- 测试和 Ruff 检查命令有唯一入口；v1 暂不强制 mypy。
- `[project.scripts]` 注册 `graphbenchmark`，并支持模块入口 `runner.benchmark_runner`。
- 配置和秘密信息不硬编码；本任务不引入真实凭据。
- 至少有一个 smoke test 证明测试发现和包导入正常。

## Verification

- `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`
- `.\.venv\Scripts\python.exe -m pytest tests -q`
- `.\.venv\Scripts\python.exe -m ruff check .`
- `.\.venv\Scripts\python.exe -m ruff format --check .`
- `graphbenchmark --help`
- `.\.venv\Scripts\python.exe -m runner.benchmark_runner --help`
- `git diff --check`

## Review evidence

- Reviewed range: `edc7fbdfbf819da85275fc54cc7107a9fbb250be..bee32a849661e64737cae6a51efb70573c453fa7`
- Verdict: `PASS_WITH_NOTES`
- Integrated into: `ai-score-v1` at `bee32a849661e64737cae6a51efb70573c453fa7`
- Independent clean environment: Python `3.12.10`
- Independent checks: editable install、`2 passed`、Ruff lint/format、两个 CLI `--help`、`pip check`、`git diff --check` 全部通过。
- Findings: none.
- Non-blocking note: `requirements-dev.txt` 是 Windows 开发环境的时间点锁定；跨平台环境应从 `pyproject.toml` 重新解析依赖。

## Delivery contract

- Commit SHA。
- 新增工程入口与依赖说明。
- 完整检查结果。
- 未锁定平台或 Python 版本的风险。
