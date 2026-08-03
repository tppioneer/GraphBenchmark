# AIS-012 input conversion and smoke configuration

State: IMPLEMENTING

## Objective

Convert the user-approved QwenPaw corrupt-inbox case and its reviewed legacy ground truth into the v1 Case and Ground Truth contracts, then add a non-formal smoke experiment configuration.

## Source material

- Case: `F:\develop\codes\GitNexus_1.6.8-patch\GitNexus\docs\benchmark\v2\qwenpaw\cases\qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml`
- Ground truth: `F:\develop\codes\GitNexus_1.6.8-patch\GitNexus\docs\benchmark\v2\qwenpaw\ground-truth\qwenpaw-case-z-corrupt-inbox-recovery-bug-ground-truth.yaml`
- Contracts: `schemas/case.schema.json`, `schemas/ground-truth.schema.json`, `profiles/bug-localization-v1.yaml`

## Fixed destinations

- `cases/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml`
- `ground-truth/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml`
- `experiments/qwenpaw-corrupt-inbox-smoke-v1.yaml`

## Invariants

- The Case contains no GT rubric, scoring weights, or other answer leakage.
- The GT is an auditable `ground-truth-v1` rubric for `bug_localization_v1`; its item identifiers are stable and its points satisfy the Profile/validator requirements.
- Facts and scope exclusions derive only from the approved source material. Unknown repository URL/revision must be omitted rather than invented.
- The experiment configuration is explicitly smoke-only, produces no formal release claim, and contains no credential value or generated Judge result.
- Do not add a real AgentAdapter, modify Judge credentials, or invoke a Judge.

## Acceptance criteria

- All three files exist at their fixed destinations and are valid YAML.
- Case and GT satisfy their JSON schemas and the production rubric validator.
- The smoke configuration references the exact case and GT paths, pins `glm-5.2`, and declares Graph/Grep paired conditions without pretending a concrete adapter exists.
- Relevant tests plus the full suite and `git diff --check` pass.

## Delivery contract

- One scoped commit with the three configuration files and any focused tests needed for their validation.
- Strict `AGENT_RESULT` including source-to-rubric mapping, validation evidence, and unresolved execution gates.
