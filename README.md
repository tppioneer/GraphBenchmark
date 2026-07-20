# GraphBenchmark

独立的代码图谱能力评测系统。当前以 telecom case V 为第一条可执行纵切面，比较相同 Agent 在 `grep` 基线与多个 `graph` provider 下的执行流追踪质量。

快速开始见 [quick_start.md](quick_start.md)。

当前范围：

- 项目：`telecom-ops-platform`
- Case：`telecom-case-v-workorder-notification-flow`
- Agent：Claude Code
- Policy：`grep`、`graph`
- Graph provider：`gitnexus`、`aka`
- 重复次数：每组 3 次，共 9 次
- 默认结果目录：`runs/v1/telecom-{model}`

Demo 项目：[`tppioneer/Gitnexus-case-project` 的 `telecom-ops-platform` 分支](https://github.com/tppioneer/Gitnexus-case-project/tree/telecom-ops-platform)。首次使用可执行：

```powershell
git clone --branch telecom-ops-platform --single-branch `
  https://github.com/tppioneer/Gitnexus-case-project.git `
  telecom-ops-platform
```

当前 Case V 参考提交为 `5c2fdcdf9e3b7829fc8ace183e2b0876ccbb188a`。正式评测时仍应在每个 `agent-result.json` 中记录实际使用的 `target_commit`，避免分支后续更新造成结果不可复现。

设计原则：

- Case 与 Ground Truth 分离，评测 Agent 永远看不到 Ground Truth。
- Ground Truth 的每个评分项目都有显式 ID。
- Graph policy 要求所选 provider 参与主要发现路径，但允许 `rg` 和文件读取进行局部确认。
- 自动评分保留 item-level 明细，方便后续 Agent 对话式人工裁决。

## 如何让其他人或 Agent 了解项目

第一次接触本仓库时，可以直接使用下面的提示词：

```text
请阅读本仓库的 README.md、quick_start.md、
benchmark/graph-providers.yaml 和
benchmark/plans/telecom-case-v.yaml，理解 GraphBenchmark 的设计。

请说明：
1. grep、graph、graph_provider 三者的关系；
2. Case、Ground Truth、Plan、Runner、Scorer 如何衔接；
3. GitNexus 与 Aka 如何通过 MCP 接入；
4. 当前 Case V 会展开为哪些 run；
5. 默认结果保存在哪里；
6. 当前已实现和暂未实现的功能。

本次只分析，不修改代码。
```

提问时建议始终说明工作目录、Case 或 Plan、provider、模型名称、dry-run 还是真实运行，以及是否允许修改或人工调整评分。

## 让 Agent 运行 Case V

在正式产生外部 Agent 调用前，先要求执行 dry-run：

```text
请在 F:\develop\codes\GraphBenchmark 中运行 telecom Case V。

要求：
1. 先阅读 quick_start.md；
2. 校验 benchmark/plans/telecom-case-v.yaml；
3. 展开并确认运行矩阵；
4. 检查 GitNexus 和 Aka 的 MCP 配置是否包含正确的 server；
5. 先执行 dry-run；
6. 展示两个 graph provider 的关键 prompt、允许工具和输出目录；
7. 未经我确认，不执行正式的 Agent 调用。
```

确认 dry-run 后，可以继续下发：

```text
dry-run 检查通过，使用模型 <明确模型名称> 正式执行。
完成后检查 runner-result.json 和 agent-result.json，
但暂时不要修改 Ground Truth 或人工调整评分。
```

具体 CLI 命令及 MCP 参数格式见 [quick_start.md](quick_start.md)。

## 让 Agent 分析结果

当前系统支持单次评分，尚未实现完整的多结果聚合报告。可以让 Agent 按相同 grep 基线分别比较两个 provider：

```text
请读取本次 Case V 的所有 agent-result.json 和评分结果。

按以下三组分别汇总：
1. grep
2. graph:gitnexus
3. graph:aka

请比较 automatic_total、各评分维度、每个显式 Ground Truth ID、
policy violations、工具调用数量和耗时。

分别计算：
- GitNexus uplift = GitNexus - Grep
- Aka uplift = Aka - Grep

不要把两个 graph provider 合并成一个平均分。
不要自动进行人工评分修正。
```

## 让 Agent 增加新 Case

```text
请参考 telecom Case V，为 GraphBenchmark 增加一个新的 <场景类型> case。

要求：
1. Case 与 Ground Truth 分离；
2. 每个可评分项目使用显式 ID；
3. 不把任何产品专属信息写入业务 Ground Truth；
4. 同时适用于 grep、GitNexus 和 Aka；
5. 更新 plan 的 total_required_runs；
6. 增加测试，验证 Ground Truth 不会进入 Agent prompt；
7. 先给出设计和预计修改文件，不要立即修改。
```

## 让 Agent 增加 Graph Provider

```text
请为 GraphBenchmark 接入新的 graph provider：<产品名>。

MCP 信息：
- server 名称：<名称>
- MCP 工具：<工具列表>
- 主要 discovery 工具：<工具列表>
- 目标仓库身份：<repo 名称>
- MCP 配置路径：<路径>

要求：
1. 只新增 provider profile，不复制 Case 或 Ground Truth；
2. run ID 必须包含 provider；
3. 工具白名单只开放该 provider 的工具；
4. policy 评分能够识别该 provider 的实际调用；
5. 增加 dry-run、prompt 和评分测试。
```

## 当前能力边界

- 已实现 Plan 校验、矩阵展开、prompt 生成、Claude Code 执行、结果提取和单次自动评分。
- 已实现 grep、GitNexus、Aka 三组运行身份及 provider 隔离。
- 尚未实现多次运行的聚合分析报告和可视化报告。
- Aka 正式运行前需要提供真实 MCP 配置，并用真实工具列表替换 profile 中的通配配置。
- `tests/fixtures/combined-mcp.json` 仅用于测试 MCP server 路由，不是正式运行配置。
