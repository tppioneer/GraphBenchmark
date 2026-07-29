---
name: orchestrate-long-running-development
description: Orchestrate long-running software development from design documents through task decomposition, bounded agent implementation, evidence-based code review, remediation, verification, and integration. Use when Codex must turn an architecture or design document into executable task cards, dispatch work to internal subagents or external terminal agents such as Claude Code and OpenCode, review returned commits or diffs, generate follow-up instructions, coordinate independent worktrees, or maintain a multi-round development loop without losing decisions and completion criteria.
---

# Orchestrate Long-Running Development

Run development as a sequence of bounded, verifiable transactions. Keep requirements and decisions in repository artifacts; keep noisy exploration, build logs, and implementation details out of the orchestration context.

## Establish the control plane

1. Read applicable `AGENTS.md` files and repository instructions.
2. Inspect the design source, Git state, current branch, existing task cards, and relevant implementation before changing anything.
3. Identify the source of truth:
   - Design and architectural invariants belong in `docs/design/`, an existing design document, or ADRs.
   - Executable work belongs in `docs/tasks/<task-id>.md` when durable task cards are useful.
   - Important review decisions belong in `docs/reviews/` only when they must survive the chat.
   - Commits, diffs, tests, and CI are implementation evidence.
4. Preserve unrelated user changes. Never use the task workflow to clean or reset an existing dirty worktree.
5. Maintain one visible plan for the orchestration task. Update it when a task changes state or a material assumption changes.

Do not treat chat history as the only durable project state. Do not silently rewrite the design to match an implementation.

## Select the operating mode

- **Plan only:** refine the design, expose unresolved decisions, and produce task cards. Do not implement.
- **Execute:** create or select ready task cards, then implement or delegate them.
- **Review:** inspect a named commit, branch diff, pull request, or working-tree diff without editing first.
- **Remediate:** convert accepted review findings into a narrow follow-up task and verify the fixes.
- **Resume:** reconstruct state from design documents, task cards, Git history, reports, and tests before continuing.

If the user has not authorized implementation, stop after planning. If a missing decision would materially alter architecture, mark the task blocked and request that decision instead of guessing.

## Use the task state machine

Use these states consistently:

`DRAFT -> READY -> IMPLEMENTING -> READY_FOR_REVIEW -> CHANGES_REQUIRED -> READY_FOR_REVIEW -> VERIFIED -> INTEGRATED`

- Move to `READY` only when scope and acceptance criteria are unambiguous.
- Move to `READY_FOR_REVIEW` only when the implementer supplies a commit or exact diff plus verification evidence.
- Move to `VERIFIED` only after an independent review of the final diff and successful required checks.
- Move to `INTEGRATED` only after the change is actually merged or applied to the intended integration branch.
- Record `BLOCKED` separately with the exact missing decision, dependency, or failing external condition.

Never infer completion from an agent's confidence statement.

## Prepare an executable task card

Make each task small enough for one implementation pass and one or two remediation passes. Prefer one coherent behavioral outcome over a list of unrelated file edits.

Use this shape:

```markdown
# <TASK-ID>: <outcome>

State: DRAFT

## Objective
Describe the observable result.

## Source of truth
- Design: <path and section>
- Base: <branch or commit>
- Dependencies: <task IDs or none>

## Execution envelope
- Executor: <Claude Code, OpenCode, Codex subagent, or human>
- Working directory: <absolute worktree path>
- Branch: <dedicated branch>
- Expected HEAD: <full base commit SHA>
- Return channel: <terminal response, result file, or pull request>

## Invariants
- Behaviors and interfaces that must remain true.

## Allowed scope
- Paths or components that may change.

## Excluded scope
- Explicit non-goals and prohibited changes.

## Acceptance criteria
- Verifiable behavioral result.
- Required edge cases and compatibility.

## Verification
- Exact commands, tests, measurements, or manual checks.

## Delivery contract
- Commit SHA or exact diff.
- Changed-file summary.
- Verification commands and results.
- Acceptance-criteria checklist.
- Deviations, unresolved questions, and risks.
```

Reject or split a task when it mixes architectural decisions with implementation, spans tightly coupled unrelated modules, lacks objective verification, or permits broad opportunistic refactoring.

## Dispatch implementation

Give an implementer the task card path instead of restating the whole design. Require the implementer to:

1. Read the task card, referenced design sections, and applicable `AGENTS.md`.
2. Confirm the expected base revision.
3. Stop and report conflicts between the task and design.
4. Modify only the allowed scope.
5. Add or update tests for changed behavior.
6. Run every required verification command that the environment permits.
7. Commit a coherent change when Git actions are authorized.
8. Return the complete delivery contract, then stop for review.

### Dispatch to Claude Code or OpenCode

Treat Claude Code and OpenCode as external terminal executors. Do not assume they share Codex chat context, task state, memories, approvals, or tool configuration. Use repository files and Git identifiers as the protocol.

Before handoff:

1. Create a dedicated branch and preferably a dedicated worktree for the task.
2. Resolve the worktree path and full base commit SHA; never use a moving label such as `latest`.
3. Confirm the worktree is clean. If it is not clean, stop instead of mixing unrelated changes.
4. Mark the task `IMPLEMENTING` in the controller's state. Do not ask the executor to own orchestration state.
5. Generate a cold-start prompt using this adapter:

```text
You are the implementation executor for <TASK-ID>.

Repository: <absolute worktree path>
Task card: <repo-relative task path>
Expected HEAD: <full base commit SHA>
Branch: <branch>

Protocol:
1. Change to the repository path and read the task card, its referenced design
   sections, and every applicable AGENTS.md before editing.
2. Verify that git rev-parse HEAD equals Expected HEAD. If it differs, make no
   changes and return BLOCKED.
3. Treat the task card and design as controller-owned. Do not edit them unless
   the allowed scope explicitly permits it.
4. Stay inside the allowed scope and do not perform opportunistic refactors.
5. Implement, test, and commit the task changes.
6. Return exactly one AGENT_RESULT block using the schema below, then stop.

AGENT_RESULT
status: READY_FOR_REVIEW | BLOCKED
task_id: <TASK-ID>
executor: <claude-code | opencode>
base_commit: <full SHA>
head_commit: <full SHA or NONE>
changed_files:
  - <path>
acceptance:
  - criterion: <text>
    result: PASS | FAIL | NOT_RUN
checks:
  - command: <exact command>
    result: PASS | FAIL | NOT_RUN
    evidence: <concise output or reason>
scope_deviations:
  - <item or NONE>
open_questions:
  - <item or NONE>
risks:
  - <item or NONE>
END_AGENT_RESULT
```

Use the same portable prompt for interactive and non-interactive invocations. Do not hardcode Claude Code or OpenCode CLI flags in this skill because installed versions and approval modes can differ. If Codex is asked to launch a CLI itself, first inspect the installed command's local `--help`, then select flags that preserve the execution envelope and return channel.

When the installed Claude Code help lists `--permission-mode auto`, prefer auto mode for bounded implementation in an isolated worktree:

1. Run `claude auto-mode config` first and confirm that an effective classifier configuration is available.
2. Invoke Claude non-interactively with `-p`, `--permission-mode auto`, `--output-format json`, and an explicit tool set.
3. Keep destructive bypass flags disabled. Auto mode is a permission classifier, not a sandbox and not proof of completion.
4. Parse the JSON envelope and require `is_error: false`, `terminal_reason: completed`, no unexpected `permission_denials`, and a valid `AGENT_RESULT`.

Do not add `--max-budget-usd` unless the user explicitly requests a budget limit.

Use this command shape after checking the local help:

```text
claude -p <cold-start-prompt> \
  --permission-mode auto \
  --tools <task-specific-tools> \
  --no-session-persistence \
  --no-chrome \
  --output-format json
```

Prefer terminal output pasted back to Codex as the manual return channel. If automation requires a result file, place it in a controller-designated run-artifact path that is excluded from the implementation diff; do not let each executor invent a location.

On receipt, Codex must independently:

1. Parse the complete `AGENT_RESULT` block and reject incomplete or ambiguous reports.
2. Verify the reported base and head commits exist and that the head descends from the declared base.
3. Inspect `base_commit..head_commit`, changed paths, commit status, and required checks.
4. Compare the real diff with the allowed scope and acceptance criteria.
5. Treat missing commits, uncommitted task changes, unexpected paths, or unverified checks as `BLOCKED` or `CHANGES_REQUIRED`.

Never treat CLI exit code zero, the word "done," or the executor's self-assessment as acceptance evidence.

For remediation, send the original task path, current full head SHA, stable finding IDs, and the remediation packet. Reuse the terminal session only as a convenience; make every remediation prompt complete enough to cold-start safely.

### Dispatch to Codex subagents

Use subagents for concrete, bounded work when the user asks to execute or delegate the plan and delegation is available. Favor parallel agents for independent exploration, tests, triage, and summarization. Run write-heavy tasks in parallel only when their scopes do not overlap and each writer has an isolated worktree or branch. Otherwise serialize them.

Do not let multiple writers edit the same checkout. Do not create separate user-owned Codex tasks unless the user explicitly asks for separate tasks; use subagents for internal delegation.

## Review independently

Review before applying fixes. Inspect the actual diff from the declared base, not only the implementation report.

Check in this order:

1. Acceptance criteria.
2. Design invariants and ADRs.
3. Scope violations and unrelated changes.
4. Correctness, failure paths, concurrency, security, and compatibility as applicable.
5. Whether tests prove the promised behavior.
6. Unnecessary complexity or maintainability regressions.

Report each actionable issue with:

- Finding ID such as `R1`.
- Severity and confidence.
- File and tight location.
- Concrete evidence or reproduction.
- Violated design or acceptance item.
- Expected behavior.

End with exactly one verdict:

- `PASS`
- `PASS_WITH_NOTES`
- `CHANGES_REQUIRED`

Do not report speculative findings without evidence. Distinguish pre-existing problems from regressions introduced by the reviewed diff. A review verdict never substitutes for running required checks.

## Generate a remediation packet

For `CHANGES_REQUIRED`, send only accepted, unresolved findings back to the implementer:

```text
Base the remediation on <commit>.
Resolve: R1, R3, R4.
Do not address R2; it was rejected or accepted as an exception.
Avoid changes outside these findings and the original task scope.

Return:
1. new commit SHA or exact diff;
2. resolution notes for each finding ID;
3. tests added or updated;
4. complete verification results;
5. remaining risks or blocked items.
```

Keep finding IDs stable across rounds. Close a finding only after inspecting evidence in the new diff. Create a new ID for a newly introduced regression.

## Verify and integrate

Before declaring success:

1. Review the cumulative final diff against the original base.
2. Run the task's required checks and relevant broader regression checks.
3. Confirm every acceptance criterion with evidence.
4. Confirm no accepted finding remains open.
5. Record intentional design exceptions in the design document or an ADR.
6. Summarize delivered commits, verification results, residual risks, and integration status.

Require explicit user confirmation before destructive operations, production changes, or merging when the surrounding workflow requires it. Do not broaden authority merely because the work is long-running.

## Improve the loop

After closure, update durable guidance only when evidence justifies it:

- Add a concise `AGENTS.md` rule for repeated mistakes, recurring review feedback, or repository routing knowledge.
- Strengthen tests, linters, hooks, or CI for mechanically enforceable rules.
- Update this skill only when the orchestration process itself repeatedly fails.

Avoid copying task-specific architecture or temporary decisions into `AGENTS.md` or this skill.
