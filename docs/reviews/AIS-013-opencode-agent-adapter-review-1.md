# AIS-013 OpenCode AgentAdapter — Review 1

Verdict: PASS_WITH_NOTES → CHANGES_REQUIRED

Reviewed range: `09ef16d4367857960732d4cda1af3a3e7fb4f352..425caa27316bf5bcce47607a2ce6bff03ecd440a`

Reviewer: Claude Code (`glm-5.2`), independent read-only review.

## Accepted findings

### AIS013-R1 — Medium — Remote MCP headers are dropped

`runner/opencode_adapter.py` normalizes a native remote MCP server to its type and URL but silently discards `headers`. Preserve and validate the headers mapping without exposing values in audit/error output.

### AIS013-R2 — Low/Medium — ID-less same-name tools are undercounted

Tool deduplication falls back to the tool name when an event lacks an ID, so several ID-less `read` calls collapse into one. Use a per-occurrence fallback identity while retaining real-ID deduplication.

### AIS013-R3 — Low/Medium — Non-assistant tool parts are counted

`message.finish` role filtering applies to text but not tool parts. Count tool calls only from assistant messages.

### AIS013-R4 — Low — Prompt audit redaction can target the wrong separator

When `extra_args` contains `--`, `_redact_command` uses the first separator rather than the adapter-owned final prompt separator. Keep the prompt hidden and retain an accurate prompt placeholder.

### AIS013-R5 — Medium — Project configuration isolation should be explicit

The installed OpenCode binary exposes `OPENCODE_DISABLE_PROJECT_CONFIG`, but the adapter does not set it. Set the subprocess-only guard explicitly while retaining the inline deny-by-default permission policy; do not clear or inspect Provider authentication.

## Deferred live-contract risks

- Whether OpenCode `1.18.13` emits the assumed NDJSON shapes and always supplies tool IDs.
- Whether `step.finish` tokens are per-step or cumulative.
- Whether the injected permission structure and `--auto` enforce the expected deny rules in a real session.

These require a bounded live OpenCode model probe and are not authorized by the current fake-CLI implementation task.

## Verification evidence

- Focused tests: 83 passed.
- Full suite: 904 passed.
- Ruff and `git diff --check`: passed.
- Reviewer mutation probes reproduced AIS013-R1 through AIS013-R4; local CLI help confirmed OpenCode `1.18.13` flags.
