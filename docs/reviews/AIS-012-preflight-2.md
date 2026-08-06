# AIS-012 Preflight 2

Result: PASS

Base: `b32068f` (`ai-score-v1`)
Date: 2026-08-06

## Preflight 1 blockers (all resolved)

1. **No approved formal cases/GT set** -- RESOLVED.
   `cases/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml` and
   `ground-truth/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml` are
   integrated (AIS-012-inputs-smoke, INTEGRATED).
2. **No frozen formal manifest/config** -- RESOLVED.
   `experiments/qwenpaw-corrupt-inbox-formal-v1.yaml` integrated as `3112fee`
   (AIS-012-formal-config, INTEGRATED). Declares case_id, task_type, scoring
   profile, paired Graph/Grep conditions, repeats=3, absolute runtime paths,
   and frozen repo revision.
3. **No concrete AgentAdapter** -- RESOLVED.
   `ClaudeCodeAgentAdapter` integrated (AIS-012-agent-adapter, INTEGRATED).
   Dispatcher wires it via `_default_adapter_factory` (AIS-012-experiment-
   dispatch, INTEGRATED). F2 skill isolation integrated as `b4ca65d`
   (AIS-012-skill-isolation, INTEGRATED).
4. **Judge authentication inactive** -- RESOLVED.
   `claude auth status` reports `loggedIn: true`, `authMethod: oauth_token`,
   `apiProvider: firstParty`.

## Acceptance-criteria gates

- Mandatory dependencies VERIFIED/INTEGRATED: AIS-008, AIS-009, AIS-011 all
  INTEGRATED. PASS.
- Credentials available: Claude CLI auth active. PASS.
- Model precisely pinned: formal config pins `agent_model: glm-5.2` and
  `judge_model: glm-5.2`. PASS.
- Claude CLI version: `2.1.223` available. PASS.
- Runtime paths verified:
  - QwenPaw repo: `F:\develop\codes\QwenPaw\QwenPaw` exists.
  - QwenPaw revision: `09fc515c88a5e817870e6b975e66b5be81893e03` matches frozen
    `repo_revision`.
  - Graph MCP config: exists.
  - Graph Skill: exists.
  - Runs root: exists.
  PASS.
- Full suite: 840 passed. PASS.
- `dispatch --dry-run`: 6 planned runs (3 Graph + 3 Grep). PASS.

## Notes

- The pipeline is multi-phase with no unified orchestrator: agent execution
  (CLI), Judge (Python API), consensus/scoring (Python API), reporting (Python
  API). The Judge/scoring/reporting phases require a glue script.
- Human calibration (AIS-010) is not a v1 mandatory dependency; it remains DRAFT.

No Judge call or artifact generation was attempted during preflight.
