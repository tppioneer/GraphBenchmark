# AIS-012 Claude Code AgentAdapter — Review 1

Verdict: CHANGES_REQUIRED

Reviewed range: `12e8077f58e5f90721e610cdccbcb0538e9f92b3..3fd2e04744e222b21cba6fbbec80ba58b8153840`

Reviewer: Claude Code (`glm-5.2`), independent read-only review.

## Blocking findings

- **P1 — Windows CLI discovery**: `_find_claude` takes the first line of `where claude`. On this host that is the extensionless npm shim, which is not a valid Win32 executable and raises WinError 193. Use Windows-safe resolution such as `shutil.which("claude")` and test the default path.
- **P1 — output encoding**: `subprocess.run(text=True)` relies on the Windows locale (`cp936` here), while Claude stream-json is UTF-8. Non-ASCII case answers can raise `UnicodeDecodeError`. Force UTF-8 with an explicit documented error policy and add a regression test.

## Additional findings

- **P2**: plain launch `OSError` is not converted to `AgentLaunchError`.
- **P2**: tests never exercise default `_find_claude` because every fixture passes `cli_path` explicitly.
- **P2**: invalid Grep MCP path lacks a dedicated regression test.
- **P3**: result error subtypes are not checked; `--` parsing against the real CLI remains a residual risk.

## Evidence

- Focused tests: 54 passed.
- Full suite: 760 passed.
- CLI version/help checks passed; no real Claude service was called.

The core command construction, Graph/Grep isolation, stream parser, Runner source stamping and scope boundaries were otherwise judged sound.
