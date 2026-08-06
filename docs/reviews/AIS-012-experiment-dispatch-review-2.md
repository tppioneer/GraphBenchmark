# AIS-012 experiment dispatch — Review 2

Verdict: PASS

Reviewed range: `4406a39fb5aecce3002b6b85f591f9e7341bcd59..a6cd41976e8f20e3447d2d9a9c23c828887a38cf`

Reviewer: Claude Code (`glm-5.2`), independent read-only review.

## Verification

- Grep's default factory now clears Graph tool-name patterns while preserving search/file-read patterns; Graph and Mixed retain their defaults. The default factory path was tested non-vacuously.
- Duplicate condition IDs and sanitized run-ID collisions are rejected before execution with deterministic diagnostics and safe single-component IDs.
- Runtime override repository, MCP, Skill and Plugin paths are validated before Adapter construction.
- Non-object GT YAML is explicitly rejected; Case/GT/Profile checks remain before subprocess launch.
- Smoke refusal, explicit execute guard, dry-run behavior, `execute_run` handoff, CLI compatibility and no-fabrication/credential boundaries remain intact.
- Focused dispatch tests: 55 passed. Full suite: 821 passed. Ruff passed. No real Claude/MCP/Judge call occurred.

## Non-blocking observations

- One override MCP-path branch has no dedicated test despite being covered by shared validation.
- Programmatic (not CLI-reachable) runtime objects can defer Skill field exclusivity to Adapter construction.
- Three-way collision diagnostics are cosmetic.

No blocking findings remain.
