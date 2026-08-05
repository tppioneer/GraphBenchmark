# AIS-012 experiment dispatch — Review 1

Verdict: CHANGES_REQUIRED

Reviewed range: `8a227d795d1a9468838d47129c43afb0804a41c9..4406a39fb5aecce3002b6b85f591f9e7341bcd59`

Reviewer: Claude Code (`glm-5.2`), independent read-only review.

## Blocking findings

- **P1 — Grep default factory**: `_default_adapter_factory` does not pass an empty Graph pattern set for Grep. The Adapter's default `mcp__gitnexus` pattern therefore triggers its fail-closed policy before every real Grep launch. Fake factories in the 47 tests hid this path.
- **P2 — duplicate/colliding condition IDs**: duplicate IDs, or IDs that sanitize to the same component, produce identical run IDs without a planning error.
- **P2 — runtime override paths**: `build_dispatch_plan(..., runtime=override)` does not validate overridden repository/MCP/skill/plugin paths; execution can raise an uncaught Adapter error after planning reports executable.
- **P2 — non-object GT**: a YAML list in the GT file has no dispatcher-level error path, unlike a non-object Case.

## Verified correct areas

Case/GT validation order, deterministic run IDs, smoke-only refusal, explicit execution guard, dry-run side-effect behavior, Runner artifact handoff and CLI compatibility passed review. Focused tests (47) and full suite (813) passed, but default-factory Grep execution was not covered.
