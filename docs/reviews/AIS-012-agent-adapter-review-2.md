# AIS-012 Claude Code AgentAdapter — Review 2

Verdict: PASS

Reviewed range: `3fd2e04744e222b21cba6fbbec80ba58b8153840..65012a6475baa8f6eb578d572f6fd6a34432c689`

Reviewer: Claude Code (`glm-5.2`), independent read-only review.

## Verification

- `shutil.which("claude")` resolves the runnable `claude.CMD` on Windows; the extensionless npm shim is no longer selected and shell invocation was removed.
- Subprocess output is captured as bytes and decoded explicitly as UTF-8 with the documented replacement policy; Chinese output is covered by regression tests.
- Plain launch `OSError` is converted to `AgentLaunchError`; non-success stream result subtypes raise `AgentOutputError`.
- Graph/Grep MCP isolation, strict MCP loading, explicit skill/plugin handling, redaction, configurable event classification, Runner source stamping and `AgentRunOutcome` compatibility remain intact.
- Adapter tests: 60 passed. Full suite: 766 passed. CLI help/version checks passed. No real Claude or Judge call was made.

## Residual risks

- Missing/non-string result subtype remains permissive for backward compatibility.
- `errors="replace"` can turn corrupt bytes into replacement characters; unavailable usage remains `0` rather than fabricated.
- Module docstring references an older CLI patch version; cosmetic only.

No blocking findings remain.
