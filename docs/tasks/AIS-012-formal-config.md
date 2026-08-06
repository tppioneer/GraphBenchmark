# AIS-012 formal configuration and dry-run

State: IMPLEMENTING

## Objective

Create the machine-specific formal YAML for the approved QwenPaw case using absolute runtime paths, then validate and dry-run the dispatcher without launching Agent, MCP or Judge processes.

## Frozen runtime inputs

- QwenPaw repository: `F:\develop\codes\QwenPaw\QwenPaw`
- QwenPaw revision: `09fc515c88a5e817870e6b975e66b5be81893e03`
- Graph MCP config: `F:\develop\00-codes\benchmark-runtime\mcp\.mcp.json`
- Graph Skill: `F:\develop\00-codes\benchmark-runtime\skills\gitnexus-guide\SKILL.md`
- Runs root: `F:\develop\00-codes\benchmark-runtime\runs\GraphBenchmark-ai-score-v1`
- Agent model: `glm-5.2`
- Permission mode: `auto`
- Repeats: `3`

## Required boundaries

- Formal YAML uses absolute paths and status `executable`; it contains no credential values.
- Grep has no Graph MCP or Graph Skill input; Graph uses only the specified MCP and Skill.
- Dry-run/validate-only must produce a deterministic plan of 6 runs (3 Graph + 3 Grep) without subprocess launch or artifact output.
- Do not execute the formal experiment or call Judge in this task.

## Acceptance criteria

- Formal YAML passes Case/GT/Profile and runtime validation.
- Dispatcher dry-run yields 6 safe deterministic run IDs and correct Graph/Grep isolation.
- Focused and full tests pass; no unrelated files change.
