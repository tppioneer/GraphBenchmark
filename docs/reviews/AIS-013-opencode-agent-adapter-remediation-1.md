# AIS-013 OpenCode AgentAdapter — Remediation 1

Result: READY_FOR_REVIEW

Base: `425caa27316bf5bcce47607a2ce6bff03ecd440a`

Head: `cff1a88890229bcfb39aab7ba21c61f8fac94ac3`

Executor: Claude Code (`glm-5.2`)

## Resolutions

- R1: native remote MCP `headers` are preserved and validated, with values confined to runtime config injection.
- R2: ID-less tool calls receive unique per-occurrence fallback identities while real-ID deduplication remains intact.
- R3: tool parts are counted only for assistant messages.
- R4: audit redaction uses the adapter-owned final prompt separator.
- R5: `OPENCODE_DISABLE_PROJECT_CONFIG=true` is child-process-only; inherited Provider environment is preserved and deny-by-default permissions remain enabled.

## Verification

- Focused adapter tests: 96 passed.
- Full suite: 917 passed.
- Ruff format/check and `git diff --check`: passed.
- Changed paths: only `runner/opencode_adapter.py` and `tests/runner/test_opencode_adapter.py`.
- No live OpenCode, MCP, Provider, or Judge call occurred.

Deferred live-contract risks remain documented in Review 1: real event shapes, tool ID guarantees, token accumulation semantics, and actual permission enforcement under `--auto`.
