# Quick start

## 1. 安装

```powershell
git clone --branch telecom-ops-platform --single-branch `
  https://github.com/tppioneer/Gitnexus-case-project.git `
  F:\develop\codes\Gitnexus-case-project\telecom-ops-platform

cd F:\develop\codes\GraphBenchmark
python -m pip install -e ".[dev]"
```

如果 Demo 项目克隆到了其他位置，请同步修改 `benchmark\plans\telecom-case-v.yaml` 中的 `target_project.path`。Case V 当前参考提交为 `5c2fdcdf9e3b7829fc8ace183e2b0876ccbb188a`。

## 2. 检查计划

```powershell
graphbenchmark validate --plan benchmark\plans\telecom-case-v.yaml
graphbenchmark plan --plan benchmark\plans\telecom-case-v.yaml
```

计划应展开为 9 次运行：grep 基线 3 次、GitNexus graph 3 次、Aka graph 3 次。

## 3. 查看各 Provider 的 Graph prompt

```powershell
graphbenchmark prompt `
  --plan benchmark\plans\telecom-case-v.yaml `
  --run-id telecom-case-v-workorder-notification-flow__claude-code__graph__gitnexus__r1
```

将 run ID 中的 `gitnexus` 改成 `aka` 可查看 Aka prompt。Graph prompt 会明确要求：

- 尽早使用所选 provider 的 graph discovery 工具建立主调用链；
- repo-scoped 调用使用 plan 为该 provider 指定的 `telecom` 仓库身份；
- `rg`、glob 和文件读取只能作为图检索后的局部确认或回退手段。

## 4. Dry-run

```powershell
graphbenchmark execute-matrix `
  --plan benchmark\plans\telecom-case-v.yaml `
  --model "claude-sonnet-4-5" `
  --mcp-config gitnexus=path\to\gitnexus.mcp.json `
  --mcp-config aka=path\to\aka.mcp.json `
  --dry-run
```

每个配置文件必须包含 provider profile 中 `mcp_server` 指定的 server。若一个 MCP 配置同时包含两个 server，也可以只传一次裸路径：

```powershell
graphbenchmark execute-matrix `
  --plan benchmark\plans\telecom-case-v.yaml `
  --mcp-config path\to\combined.mcp.json `
  --dry-run
```

未指定 `--out-root` 时，输出到：

```text
runs/v1/telecom-claude-sonnet-4-5/
```

## 5. 正式执行

去掉 `--dry-run` 即可。建议始终固定模型名称，并分别确认 telecom 仓库已在所选 graph provider 中建立最新索引。

## 6. 评分

runner 会从 Claude Code 的外层 JSON 中自动提取符合输出契约的内容，并在对应 run 目录保存为 `agent-result.json`。然后执行：

```powershell
graphbenchmark score `
  --result path\to\agent-result.json `
  --ground-truth benchmark\cases\telecom\case-v\ground-truth.yaml
```

评分输出包含 `automatic_total`、六个维度分数以及每个显式 ID 的命中明细。
