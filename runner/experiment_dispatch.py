"""Configuration-driven experiment dispatch (AIS-012).

Loads a YAML experiment configuration, validates its Case and Ground
Truth/Profile against the production validators, builds a deterministic
dispatch plan (one planned run per declared condition x repeat), and -
only when explicitly opted in - executes each planned run by constructing the
adapter selected by ``runtime.agent_adapter`` (``claude-code`` default ->
:class:`~runner.claude_code_adapter.ClaudeCodeAgentAdapter`; ``opencode`` ->
:class:`~runner.opencode_adapter.OpenCodeAgentAdapter`) and calling the
existing :func:`~runner.benchmark_runner.execute_run`.

Design invariants (see docs/ai-scoring-design.md S8.6-8.8, S15, S17-18, S20):

* Validation occurs BEFORE any subprocess launch. A config with an invalid
  Case, GT/Profile, or runtime configuration cannot reach execution.
* Adapter selection (AIS-014): ``runtime.agent_adapter`` selects the adapter,
  defaulting to ``claude-code`` for backward compatibility and accepting
  ``opencode`` (AIS-013). Unknown values are rejected as
  ``CONFIG_INVALID_AGENT_ADAPTER`` before any launch. The adapter name is
  recorded in :class:`RunIdentity.agent`; ``runtime.agent_model`` is passed
  unchanged to either adapter (and defaults to the selected adapter's model
  when unset).
* ``status: smoke_only`` configs (including the shipped QwenPaw smoke file)
  are explicitly non-executable: they validate inputs but never launch.
* Graph/Grep isolation: each run instantiates a fresh adapter with ONLY the
  MCP configs matching its ``tool_policy``. A Graph run receives only
  ``graph_mcp_configs``; a Grep run receives only ``grep_mcp_configs`` (the
  adapter fails closed if Graph configs leak into a Grep run). The configured
  skill is likewise injected only into runs whose policy grants Graph tool
  access (graph, mixed); a Grep run receives no skill, so the Graph Skill text
  cannot contaminate the Grep baseline (AIS-012, F2). The adapter fail-closes
  if a skill reaches a Grep run regardless. This isolation is identical for
  both adapters; OpenCode receives only the fields it supports (no plugins,
  no permission mode), while Claude receives its existing permission/plugin
  fields.
* The natural-language agent answer flows unchanged through
  :mod:`runner.execution`; the dispatcher never fabricates answer JSON,
  metrics, or Judge results. It collects :class:`RunResult` objects and
  nothing more.
* Run IDs are deterministic (same config -> same IDs) and safe (single path
  component, no traversal).
* No formal experiment JSON Schema is added; the config structure is
  validated by this module's own logic. No score, reporting, credentials,
  or Judge calls are produced.

This module is the reusable callable API; the CLI subcommand in
:mod:`runner.benchmark_runner` wires it to ``graphbenchmark dispatch``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator

from runner.benchmark_runner import (
    AgentAdapter,
    RunIdentity,
    RunResult,
    execute_run,
)
from runner.claude_code_adapter import (
    DEFAULT_AGENT_MODEL,
    DEFAULT_PERMISSION_MODE,
    ClaudeCodeAgentAdapter,
    ToolNamePatterns,
)
from runner.opencode_adapter import (
    DEFAULT_AGENT_MODEL as _OPENCODE_DEFAULT_AGENT_MODEL,
)
from runner.opencode_adapter import (
    OpenCodeAgentAdapter,
    OpenCodeToolNamePatterns,
)
from scoring.rubric_validator import validate_profile_and_rubric

__all__ = [
    "ConditionSpec",
    "ExperimentConfig",
    "RuntimeFields",
    "PlannedRun",
    "DispatchPlan",
    "ValidationIssue",
    "DispatchError",
    "SmokeOnlyExecutionError",
    "IncompleteRuntimeError",
    "ConfigValidationError",
    "load_experiment_config",
    "validate_experiment_config",
    "build_dispatch_plan",
    "execute_dispatch",
    "SMOKE_ONLY_STATUS",
    "EXECUTABLE_STATUS",
    "DEFAULT_AGENT_ADAPTER",
]

SMOKE_ONLY_STATUS = "smoke_only"
EXECUTABLE_STATUS = "executable"

_VALID_TOOL_POLICIES = ("graph", "grep", "mixed")

#: Default ``runtime.agent_adapter`` (AIS-014). ``claude-code`` preserves
#: backward compatibility for configs that do not declare an adapter; the
#: reference implementation is :class:`ClaudeCodeAgentAdapter`.
DEFAULT_AGENT_ADAPTER = "claude-code"

#: Accepted ``runtime.agent_adapter`` values. Unknown values are rejected by
#: :func:`validate_experiment_config` (``CONFIG_INVALID_AGENT_ADAPTER``) before
#: any subprocess launch, mirroring ``tool_policy`` enum validation.
_VALID_AGENT_ADAPTERS = ("claude-code", "opencode")

#: Adapter-specific default agent model (model/runtime mapping, AIS-014). When a
#: config selects an adapter without an explicit ``runtime.agent_model``, the
#: default is taken from the selected adapter's module so each adapter receives a
#: model valid for its provider. An explicit ``agent_model`` is passed unchanged
#: to either adapter (invariant); only the unset default is adapter-specific.
_DEFAULT_AGENT_MODELS = {
    "claude-code": DEFAULT_AGENT_MODEL,
    "opencode": _OPENCODE_DEFAULT_AGENT_MODEL,
}

#: Characters allowed in a sanitized run-id component.
_RUN_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _load_case_schema() -> dict[str, Any]:
    """Load the shipped ``case.schema.json`` via importlib.resources."""
    resource = files("schemas").joinpath("case.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


_CASE_VALIDATOR = Draft202012Validator(_load_case_schema())


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation problem with an actionable code and location.

    ``source`` identifies which validation layer produced the issue
    (``"config"``, ``"case_schema"``, ``"gt_profile"``, ``"runtime"``,
    ``"cross_check"``). ``pointer`` is an RFC 6901 JSON Pointer or a
    config-relative field path.
    """

    code: str
    message: str
    source: str
    pointer: str = ""

    def __str__(self) -> str:
        loc = f" at {self.pointer}" if self.pointer else ""
        return f"[{self.source}] {self.code}: {self.message}{loc}"


@dataclass(frozen=True)
class ConditionSpec:
    """One declared experimental condition (id + tool_policy)."""

    id: str
    tool_policy: str


@dataclass(frozen=True)
class RuntimeFields:
    """Explicit runtime configuration for an executable experiment.

    ``agent_adapter`` selects the agent adapter (``claude-code`` default,
    ``opencode`` for AIS-013); ``agent_model`` defaults to the selected
    adapter's model when not declared explicitly (model/runtime mapping). All
    paths are resolved (absolute) at load time. ``skill_text`` and
    ``skill_file`` are mutually exclusive. ``runs_root`` is required for
    execution but may be absent for dry-run planning.
    """

    agent_adapter: str = DEFAULT_AGENT_ADAPTER
    agent_model: str = DEFAULT_AGENT_MODEL
    repo_cwd: Path | None = None
    graph_mcp_configs: tuple[Path, ...] = ()
    grep_mcp_configs: tuple[Path, ...] = ()
    skill_text: str | None = None
    skill_file: Path | None = None
    plugin_dirs: tuple[Path, ...] = ()
    permission_mode: str = DEFAULT_PERMISSION_MODE
    runs_root: Path | None = None
    output_root: Path | None = None
    case_prompt: str | None = None


@dataclass(frozen=True)
class PlannedRun:
    """One planned execution in a dispatch plan."""

    run_id: str
    condition_id: str
    tool_policy: str
    repeat: int  # 1-indexed
    identity: RunIdentity


@dataclass(frozen=True)
class ExperimentConfig:
    """A parsed experiment configuration with resolved paths."""

    experiment_id: str
    purpose: str
    status: str
    case_id: str
    task_type: str
    scoring_profile: str
    case_path: Path
    ground_truth_path: Path
    judge_model: str
    conditions: tuple[ConditionSpec, ...]
    pairing: str
    repeats: int
    runtime: RuntimeFields | None
    config_path: Path
    repo_root: Path


@dataclass(frozen=True)
class DispatchPlan:
    """A deterministic dispatch plan (the dry-run output).

    Carries every planned run, the resolved case prompt, the effective
    runtime, and the collected validation issues. ``is_executable`` reports
    whether the plan can reach :func:`execute_dispatch` without refusal.
    """

    experiment_id: str
    status: str
    case_id: str
    task_type: str
    scoring_profile: str
    pairing: str
    repeats: int
    conditions: tuple[ConditionSpec, ...]
    runs: tuple[PlannedRun, ...]
    runtime: RuntimeFields | None
    case_prompt: str | None
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    config_path: Path = Path()
    repo_root: Path = Path()

    @property
    def is_executable(self) -> bool:
        """Whether the plan has no blocking issues for execution."""
        return (
            self.status == EXECUTABLE_STATUS
            and self.runtime is not None
            and not self.validation_issues
        )


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class DispatchError(Exception):
    """Base error for experiment dispatch failures (auditable)."""


class SmokeOnlyExecutionError(DispatchError):
    """Attempted to execute a smoke_only config (explicitly non-executable)."""


class IncompleteRuntimeError(DispatchError):
    """Runtime configuration is missing required fields for execution."""


class ConfigValidationError(DispatchError):
    """Experiment config validation failed (Case/GT/Profile/runtime)."""


# --------------------------------------------------------------------------- #
# Public API: load
# --------------------------------------------------------------------------- #


def load_experiment_config(
    config_path: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> ExperimentConfig:
    """Load and parse a YAML experiment configuration.

    Resolves repository-relative Case/GT paths against ``repo_root``
    (default: current working directory). Does NOT validate the Case or GT
    content yet; call :func:`validate_experiment_config` for that.

    Raises :class:`DispatchError` for a missing file, non-mapping YAML, or a
    structurally malformed config (missing required fields, bad types).
    """
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise DispatchError(f"config file not found: {config_path}")
    repo_root = Path(repo_root) if repo_root is not None else Path.cwd()

    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise DispatchError(f"config must be a YAML mapping, got {type(raw).__name__}")

    experiment_id = _require_str(raw, "experiment_id")
    purpose = _require_str(raw, "purpose")
    status = _require_str(raw, "status")
    case_id = _require_str(raw, "case_id")
    task_type = _require_str(raw, "task_type")
    scoring_profile = _require_str(raw, "scoring_profile")
    case_rel = _require_str(raw, "case")
    gt_rel = _require_str(raw, "ground_truth")
    judge_model = _require_str(raw, "judge_model")
    pairing = _require_str(raw, "pairing")
    repeats = _require_int(raw, "repeats", minimum=1)

    case_path = _resolve_path(case_rel, repo_root)
    ground_truth_path = _resolve_path(gt_rel, repo_root)
    conditions = _parse_conditions(raw.get("conditions"))
    runtime = _parse_runtime(raw.get("runtime"))

    return ExperimentConfig(
        experiment_id=experiment_id,
        purpose=purpose,
        status=status,
        case_id=case_id,
        task_type=task_type,
        scoring_profile=scoring_profile,
        case_path=case_path,
        ground_truth_path=ground_truth_path,
        judge_model=judge_model,
        conditions=conditions,
        pairing=pairing,
        repeats=repeats,
        runtime=runtime,
        config_path=config_path,
        repo_root=repo_root,
    )


# --------------------------------------------------------------------------- #
# Public API: validate
# --------------------------------------------------------------------------- #


def validate_experiment_config(
    config: ExperimentConfig,
) -> list[ValidationIssue]:
    """Validate the Case schema and Ground Truth/Profile before any launch.

    Loads the Case YAML and validates it against ``case.schema.json``; loads
    the GT YAML and validates it via the production
    :func:`scoring.rubric_validator.validate_profile_and_rubric`. Cross-checks
    case_id/task_type/scoring_profile consistency across config, Case, and GT.
    Validates runtime paths if runtime fields are present.

    Returns an empty list if everything is valid. All issues are collected and
    returned together (never fail on the first), sorted deterministically.
    """
    issues: list[ValidationIssue] = []

    # -- Config structural checks ------------------------------------------- #
    if config.status not in (SMOKE_ONLY_STATUS, EXECUTABLE_STATUS):
        issues.append(
            ValidationIssue(
                code="CONFIG_STATUS_UNKNOWN",
                message=(
                    f"status must be {SMOKE_ONLY_STATUS!r} or "
                    f"{EXECUTABLE_STATUS!r}, got {config.status!r}"
                ),
                source="config",
                pointer="/status",
            )
        )
    if not config.conditions:
        issues.append(
            ValidationIssue(
                code="CONFIG_NO_CONDITIONS",
                message="at least one condition must be declared",
                source="config",
                pointer="/conditions",
            )
        )
    for cond in config.conditions:
        if cond.tool_policy not in _VALID_TOOL_POLICIES:
            issues.append(
                ValidationIssue(
                    code="CONFIG_INVALID_TOOL_POLICY",
                    message=(
                        f"condition {cond.id!r} has unknown tool_policy "
                        f"{cond.tool_policy!r}; expected one of "
                        f"{_VALID_TOOL_POLICIES}"
                    ),
                    source="config",
                    pointer="/conditions",
                )
            )

    # -- Duplicate condition IDs and sanitized run-id collisions -------------- #
    # Run IDs are built from sanitized condition IDs; two conditions with the
    # same id, or ids that sanitize to the same component, would produce
    # colliding run IDs. No colliding runs may be executable.
    seen_ids: set[str] = set()
    seen_sanitized: dict[str, str] = {}
    for cond in config.conditions:
        if cond.id in seen_ids:
            issues.append(
                ValidationIssue(
                    code="CONFIG_DUPLICATE_CONDITION_ID",
                    message=f"duplicate condition id {cond.id!r}",
                    source="config",
                    pointer="/conditions",
                )
            )
        else:
            seen_ids.add(cond.id)
        sanitized = _sanitize_run_id_component(cond.id)
        prior = seen_sanitized.get(sanitized)
        if prior is not None and prior != cond.id:
            issues.append(
                ValidationIssue(
                    code="CONFIG_CONDITION_ID_COLLISION",
                    message=(
                        f"condition id {cond.id!r} sanitizes to {sanitized!r}, "
                        f"colliding with condition {prior!r}; run IDs would not "
                        f"be unique"
                    ),
                    source="config",
                    pointer="/conditions",
                )
            )
        else:
            seen_sanitized[sanitized] = cond.id

    # -- Case schema validation --------------------------------------------- #
    case_doc = _safe_load_yaml(config.case_path, issues, "case")
    if case_doc is not None:
        if isinstance(case_doc, dict):
            for err in _CASE_VALIDATOR.iter_errors(case_doc):
                issues.append(
                    ValidationIssue(
                        code="CASE_SCHEMA_INVALID",
                        message=err.message,
                        source="case_schema",
                        pointer=_json_pointer(err.absolute_path),
                    )
                )
            if case_doc.get("case_id") != config.case_id:
                issues.append(
                    ValidationIssue(
                        code="CROSS_CASE_ID_MISMATCH",
                        message=(
                            f"case case_id {case_doc.get('case_id')!r} != "
                            f"config case_id {config.case_id!r}"
                        ),
                        source="cross_check",
                        pointer="/case_id",
                    )
                )
            if case_doc.get("task_type") != config.task_type:
                issues.append(
                    ValidationIssue(
                        code="CROSS_TASK_TYPE_MISMATCH",
                        message=(
                            f"case task_type {case_doc.get('task_type')!r} != "
                            f"config task_type {config.task_type!r}"
                        ),
                        source="cross_check",
                        pointer="/task_type",
                    )
                )
        else:
            issues.append(
                ValidationIssue(
                    code="CASE_NOT_OBJECT",
                    message="case document must be a YAML mapping",
                    source="case_schema",
                )
            )

    # -- GT/Profile validation via production validator ----------------------- #
    gt_doc = _safe_load_yaml(config.ground_truth_path, issues, "ground_truth")
    if gt_doc is not None:
        if isinstance(gt_doc, dict):
            for ri in validate_profile_and_rubric(gt_doc):
                issues.append(
                    ValidationIssue(
                        code=ri.code,
                        message=ri.message,
                        source="gt_profile",
                        pointer=ri.pointer,
                    )
                )
            if gt_doc.get("case_id") != config.case_id:
                issues.append(
                    ValidationIssue(
                        code="CROSS_GT_CASE_ID_MISMATCH",
                        message=(
                            f"GT case_id {gt_doc.get('case_id')!r} != "
                            f"config case_id {config.case_id!r}"
                        ),
                        source="cross_check",
                        pointer="/case_id",
                    )
                )
            if gt_doc.get("task_type") != config.task_type:
                issues.append(
                    ValidationIssue(
                        code="CROSS_GT_TASK_TYPE_MISMATCH",
                        message=(
                            f"GT task_type {gt_doc.get('task_type')!r} != "
                            f"config task_type {config.task_type!r}"
                        ),
                        source="cross_check",
                        pointer="/task_type",
                    )
                )
            if gt_doc.get("scoring_profile") != config.scoring_profile:
                issues.append(
                    ValidationIssue(
                        code="CROSS_GT_PROFILE_MISMATCH",
                        message=(
                            f"GT scoring_profile {gt_doc.get('scoring_profile')!r} "
                            f"!= config scoring_profile {config.scoring_profile!r}"
                        ),
                        source="cross_check",
                        pointer="/scoring_profile",
                    )
                )
        else:
            issues.append(
                ValidationIssue(
                    code="GT_NOT_OBJECT",
                    message="ground truth document must be a YAML mapping",
                    source="gt_profile",
                )
            )

    # -- Runtime validation (adapter selection + paths) --------------------- #
    if config.runtime is not None:
        issues.extend(_validate_runtime_adapter(config.runtime))
        issues.extend(_validate_runtime_paths(config.runtime))

    issues.sort(key=lambda x: (x.source, x.code, x.pointer, x.message))
    return issues


# --------------------------------------------------------------------------- #
# Public API: plan (dry-run)
# --------------------------------------------------------------------------- #


def build_dispatch_plan(
    config: ExperimentConfig,
    *,
    runtime: RuntimeFields | None = None,
) -> DispatchPlan:
    """Build a deterministic dispatch plan (validate + plan, no execution).

    Validates the config (Case + GT/Profile + runtime paths), then builds one
    :class:`PlannedRun` per condition x repeat. Run IDs are deterministic and
    safe. This is the dry-run entry point: it returns the plan without
    launching anything.

    If ``runtime`` is given it overrides the config's runtime section.
    """
    issues = validate_experiment_config(config)

    effective_runtime = runtime if runtime is not None else config.runtime

    # When a runtime override is supplied, validate its adapter and paths
    # independently. validate_experiment_config checked config.runtime (if any);
    # the override replaces it for execution, so its adapter selection and paths
    # must also be validated here so misconfigured values surface as
    # deterministic Dispatch/config errors (ConfigValidationError at
    # execute_dispatch) rather than uncaught AgentAdapterError at adapter
    # construction time.
    if runtime is not None:
        issues.extend(_validate_runtime_adapter(runtime))
        issues.extend(_validate_runtime_paths(runtime))

    # Resolve the case prompt (the agent's input). If the case is loadable
    # and has a non-empty ``question``, use it. A runtime ``case_prompt``
    # override takes precedence.
    case_prompt: str | None = None
    try:
        case_doc = yaml.safe_load(config.case_path.read_text(encoding="utf-8"))
        if isinstance(case_doc, dict):
            q = case_doc.get("question")
            if isinstance(q, str) and q.strip():
                case_prompt = q
    except (OSError, yaml.YAMLError):
        case_prompt = None
    if effective_runtime is not None and effective_runtime.case_prompt:
        case_prompt = effective_runtime.case_prompt

    # Build planned runs; catch run-id safety issues as validation problems.
    runs: list[PlannedRun] = []
    try:
        runs = list(_build_planned_runs(config, effective_runtime))
    except DispatchError as exc:
        issues.append(
            ValidationIssue(
                code="RUN_ID_UNSAFE",
                message=str(exc),
                source="config",
            )
        )

    # Deterministic ordering for issues collected from multiple sources
    # (config validation, runtime override paths, run-id safety).
    issues.sort(key=lambda x: (x.source, x.code, x.pointer, x.message))

    return DispatchPlan(
        experiment_id=config.experiment_id,
        status=config.status,
        case_id=config.case_id,
        task_type=config.task_type,
        scoring_profile=config.scoring_profile,
        pairing=config.pairing,
        repeats=config.repeats,
        conditions=config.conditions,
        runs=tuple(runs),
        runtime=effective_runtime,
        case_prompt=case_prompt,
        validation_issues=issues,
        config_path=config.config_path,
        repo_root=config.repo_root,
    )


# --------------------------------------------------------------------------- #
# Public API: execute
# --------------------------------------------------------------------------- #


def execute_dispatch(
    plan: DispatchPlan,
    *,
    allow_execute: bool = False,
    adapter_factory: (Callable[[PlannedRun, DispatchPlan], AgentAdapter] | None) = None,
) -> list[RunResult]:
    """Execute a dispatch plan by launching each planned run.

    Requires ``allow_execute=True`` (opt-in guard). Refuses smoke_only
    configs, configs with validation issues, and configs with incomplete
    runtime fields. Each run instantiates a
    :class:`ClaudeCodeAgentAdapter` with only the MCP configs matching its
    ``tool_policy`` and calls :func:`execute_run`; the dispatcher does not
    bypass Runner policy or artifact handling.

    ``adapter_factory`` lets tests inject a fake adapter without launching the
    real Claude CLI. When ``None``, a :class:`ClaudeCodeAgentAdapter` is
    constructed for each run.
    """
    if not allow_execute:
        raise DispatchError(
            "execution requires allow_execute=True (opt-in guard); "
            "use build_dispatch_plan for dry-run planning"
        )
    if plan.status == SMOKE_ONLY_STATUS:
        raise SmokeOnlyExecutionError(
            f"experiment {plan.experiment_id!r} is smoke_only and cannot be executed"
        )
    if plan.validation_issues:
        raise ConfigValidationError(
            f"config has {len(plan.validation_issues)} validation issue(s); "
            "cannot execute (resolve them first)"
        )
    runtime = plan.runtime
    if runtime is None:
        raise IncompleteRuntimeError(
            "no runtime fields; cannot execute (provide a runtime section "
            "in the config or pass runtime to build_dispatch_plan)"
        )
    if runtime.runs_root is None:
        raise IncompleteRuntimeError("runtime.runs_root is required for execution")
    if not plan.case_prompt:
        raise IncompleteRuntimeError(
            "no case prompt resolved; the case question is missing or empty "
            "and no runtime case_prompt override was provided"
        )

    results: list[RunResult] = []
    for run in plan.runs:
        adapter = (
            adapter_factory(run, plan)
            if adapter_factory is not None
            else _default_adapter_factory(run, plan)
        )
        result = execute_run(
            runs_root=runtime.runs_root,
            run_id=run.run_id,
            identity=run.identity,
            agent=adapter,
        )
        results.append(result)
    return results


# --------------------------------------------------------------------------- #
# Internal: config parsing helpers
# --------------------------------------------------------------------------- #


def _require_str(raw: dict[str, Any], key: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise DispatchError(f"config field {key!r} must be a non-empty string, got {val!r}")
    return val


def _require_int(raw: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    val = raw.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise DispatchError(f"config field {key!r} must be an integer, got {val!r}")
    if minimum is not None and val < minimum:
        raise DispatchError(f"config field {key!r} must be >= {minimum}, got {val}")
    return val


def _resolve_path(rel_or_abs: str, repo_root: Path) -> Path:
    """Resolve a path: absolute as-is, relative against ``repo_root``."""
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return repo_root / p


def _parse_conditions(raw: Any) -> tuple[ConditionSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DispatchError(f"conditions must be a list, got {type(raw).__name__}")
    conditions: list[ConditionSpec] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise DispatchError(f"conditions[{i}] must be a mapping, got {type(entry).__name__}")
        cid = entry.get("id")
        policy = entry.get("tool_policy")
        if not isinstance(cid, str) or not cid.strip():
            raise DispatchError(f"conditions[{i}].id must be a non-empty string")
        if not isinstance(policy, str) or not policy.strip():
            raise DispatchError(f"conditions[{i}].tool_policy must be a non-empty string")
        conditions.append(ConditionSpec(id=cid, tool_policy=policy))
    return tuple(conditions)


def _parse_runtime(raw: Any) -> RuntimeFields | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DispatchError(f"runtime must be a mapping, got {type(raw).__name__}")

    agent_adapter = raw.get("agent_adapter", DEFAULT_AGENT_ADAPTER)
    if not isinstance(agent_adapter, str) or not agent_adapter.strip():
        raise DispatchError("runtime.agent_adapter must be a non-empty string")
    # Enum membership is validated by validate_experiment_config
    # (CONFIG_INVALID_AGENT_ADAPTER) so an unknown value surfaces as a
    # deterministic validation issue, mirroring tool_policy handling.

    # Model/runtime mapping: when no explicit agent_model is declared, default
    # to the selected adapter's model so each adapter receives a provider-valid
    # model. An unknown adapter falls back to the Claude default here; the bad
    # adapter value is reported separately by validate_experiment_config.
    agent_model_value = raw.get("agent_model")
    if agent_model_value is None:
        agent_model = _DEFAULT_AGENT_MODELS.get(agent_adapter, DEFAULT_AGENT_MODEL)
    else:
        agent_model = agent_model_value
    if not isinstance(agent_model, str) or not agent_model.strip():
        raise DispatchError("runtime.agent_model must be a non-empty string")

    repo_cwd = _opt_path(raw, "repo_cwd")
    graph_mcp_configs = _opt_path_list(raw, "graph_mcp_configs")
    grep_mcp_configs = _opt_path_list(raw, "grep_mcp_configs")
    skill_text = raw.get("skill_text")
    skill_file = _opt_path(raw, "skill_file")
    plugin_dirs = _opt_path_list(raw, "plugin_dirs")
    permission_mode = raw.get("permission_mode", DEFAULT_PERMISSION_MODE)
    if not isinstance(permission_mode, str) or not permission_mode.strip():
        raise DispatchError("runtime.permission_mode must be a non-empty string")
    runs_root = _opt_path(raw, "runs_root")
    output_root = _opt_path(raw, "output_root")
    case_prompt = raw.get("case_prompt")
    if case_prompt is not None and not isinstance(case_prompt, str):
        raise DispatchError("runtime.case_prompt must be a string if present")

    if skill_text is not None and skill_file is not None:
        raise DispatchError("runtime.skill_text and runtime.skill_file are mutually exclusive")

    return RuntimeFields(
        agent_adapter=agent_adapter,
        agent_model=agent_model,
        repo_cwd=repo_cwd,
        graph_mcp_configs=graph_mcp_configs,
        grep_mcp_configs=grep_mcp_configs,
        skill_text=skill_text,
        skill_file=skill_file,
        plugin_dirs=plugin_dirs,
        permission_mode=permission_mode,
        runs_root=runs_root,
        output_root=output_root,
        case_prompt=case_prompt,
    )


def _opt_path(raw: dict[str, Any], key: str) -> Path | None:
    val = raw.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise DispatchError(f"runtime.{key} must be a string path if present, got {val!r}")
    return Path(val)


def _opt_path_list(raw: dict[str, Any], key: str) -> tuple[Path, ...]:
    val = raw.get(key)
    if val is None:
        return ()
    if not isinstance(val, list):
        raise DispatchError(f"runtime.{key} must be a list if present, got {type(val).__name__}")
    paths: list[Path] = []
    for i, item in enumerate(val):
        if not isinstance(item, str):
            raise DispatchError(f"runtime.{key}[{i}] must be a string path, got {item!r}")
        paths.append(Path(item))
    return tuple(paths)


# --------------------------------------------------------------------------- #
# Internal: validation helpers
# --------------------------------------------------------------------------- #


def _safe_load_yaml(path: Path, issues: list[ValidationIssue], label: str) -> dict[str, Any] | None:
    """Load a YAML file, appending a validation issue on failure."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(
            ValidationIssue(
                code=f"{label.upper()}_LOAD_FAILED",
                message=f"failed to load {label} at {path}: {exc}",
                source=label,
            )
        )
        return None
    return doc


def _validate_runtime_adapter(
    runtime: RuntimeFields,
) -> list[ValidationIssue]:
    """Validate ``runtime.agent_adapter`` is a known adapter value (AIS-014).

    Mirrors ``tool_policy`` enum validation: an unknown value is collected as a
    ``CONFIG_INVALID_AGENT_ADAPTER`` issue (rather than raised at load time) so
    the plan is non-executable. :func:`execute_dispatch` refuses plans with
    validation issues, so an unknown adapter can never reach a subprocess launch.
    """
    if runtime.agent_adapter not in _VALID_AGENT_ADAPTERS:
        return [
            ValidationIssue(
                code="CONFIG_INVALID_AGENT_ADAPTER",
                message=(
                    f"runtime.agent_adapter must be one of "
                    f"{_VALID_AGENT_ADAPTERS}, got {runtime.agent_adapter!r}"
                ),
                source="runtime",
                pointer="/runtime/agent_adapter",
            )
        ]
    return []


def _validate_runtime_paths(
    runtime: RuntimeFields,
) -> list[ValidationIssue]:
    """Validate that runtime paths exist on disk before any subprocess launch."""
    issues: list[ValidationIssue] = []
    if runtime.repo_cwd is not None and not runtime.repo_cwd.is_dir():
        issues.append(
            ValidationIssue(
                code="RUNTIME_REPO_CWD_MISSING",
                message=f"repo_cwd is not a directory: {runtime.repo_cwd}",
                source="runtime",
                pointer="/runtime/repo_cwd",
            )
        )
    for cfg in runtime.graph_mcp_configs:
        if not cfg.is_file():
            issues.append(
                ValidationIssue(
                    code="RUNTIME_GRAPH_MCP_MISSING",
                    message=f"graph MCP config not found or not a file: {cfg}",
                    source="runtime",
                    pointer="/runtime/graph_mcp_configs",
                )
            )
    for cfg in runtime.grep_mcp_configs:
        if not cfg.is_file():
            issues.append(
                ValidationIssue(
                    code="RUNTIME_GREP_MCP_MISSING",
                    message=f"grep MCP config not found or not a file: {cfg}",
                    source="runtime",
                    pointer="/runtime/grep_mcp_configs",
                )
            )
    if runtime.skill_file is not None and not runtime.skill_file.is_file():
        issues.append(
            ValidationIssue(
                code="RUNTIME_SKILL_FILE_MISSING",
                message=f"skill_file not found or not a file: {runtime.skill_file}",
                source="runtime",
                pointer="/runtime/skill_file",
            )
        )
    for d in runtime.plugin_dirs:
        if not d.exists():
            issues.append(
                ValidationIssue(
                    code="RUNTIME_PLUGIN_DIR_MISSING",
                    message=f"plugin_dir not found: {d}",
                    source="runtime",
                    pointer="/runtime/plugin_dirs",
                )
            )
    return issues


# --------------------------------------------------------------------------- #
# Internal: run-id construction and planning
# --------------------------------------------------------------------------- #


def _sanitize_run_id_component(value: str) -> str:
    """Replace unsafe characters in a run-id component with underscores."""
    return _RUN_ID_SAFE_RE.sub("_", value)


def _validate_run_id_component(run_id: str) -> None:
    """Ensure a run-id is a single safe path component (no traversal)."""
    if not run_id:
        raise DispatchError("run_id is empty after sanitization")
    if "/" in run_id or "\\" in run_id:
        raise DispatchError(f"run_id contains path separators: {run_id!r}")
    if run_id in (".", ".."):
        raise DispatchError(f"run_id is a traversal component: {run_id!r}")


def _build_run_id(experiment_id: str, condition_id: str, repeat: int) -> str:
    """Build a deterministic, safe run id: ``<exp>__<cond>__r<NN>``."""
    exp = _sanitize_run_id_component(experiment_id)
    cond = _sanitize_run_id_component(condition_id)
    run_id = f"{exp}__{cond}__r{repeat:02d}"
    _validate_run_id_component(run_id)
    return run_id


def _build_planned_runs(
    config: ExperimentConfig, runtime: RuntimeFields | None
) -> tuple[PlannedRun, ...]:
    """Build one PlannedRun per condition x repeat with deterministic run IDs."""
    if runtime is not None:
        agent_adapter = runtime.agent_adapter
        agent_model = runtime.agent_model
    else:
        agent_adapter = DEFAULT_AGENT_ADAPTER
        agent_model = DEFAULT_AGENT_MODEL
    runs: list[PlannedRun] = []
    for cond in config.conditions:
        for repeat in range(1, config.repeats + 1):
            run_id = _build_run_id(config.experiment_id, cond.id, repeat)
            identity = RunIdentity(
                case_id=config.case_id,
                task_type=config.task_type,
                tool_policy=cond.tool_policy,
                agent=agent_adapter,
                agent_model=agent_model,
            )
            runs.append(
                PlannedRun(
                    run_id=run_id,
                    condition_id=cond.id,
                    tool_policy=cond.tool_policy,
                    repeat=repeat,
                    identity=identity,
                )
            )
    return tuple(runs)


# --------------------------------------------------------------------------- #
# Internal: default adapter factory
# --------------------------------------------------------------------------- #


def _default_adapter_factory(run: PlannedRun, plan: DispatchPlan) -> AgentAdapter:
    """Construct the selected AgentAdapter for a planned run (AIS-014).

    The adapter is chosen by ``runtime.agent_adapter`` (default ``claude-code``,
    selecting :class:`ClaudeCodeAgentAdapter`; ``opencode`` selects
    :class:`OpenCodeAgentAdapter`). For both adapters the policy-scoped MCP and
    skill selection is identical, so Graph/Grep and skill isolation remain
    unchanged across adapters:

    * Graph/Grep MCP isolation (S15.1): a Graph run receives only
      ``graph_mcp_configs``; a Grep run receives only ``grep_mcp_configs``; a
      Mixed run receives both. Each adapter fail-closes in its own
      ``_select_mcp_configs`` if Graph configs leak into a Grep run.
    * Skill isolation (AIS-012, F2): the configured skill is injected only into
      runs whose policy grants Graph tool access (graph, mixed); a Grep run
      receives no skill (``skill_text``/``skill_file`` are suppressed), so the
      Graph Skill text cannot contaminate the Grep baseline. Each adapter
      fail-closes if a skill ever reaches a Grep run regardless.
    * For Grep runs the default Graph tool-name patterns are cleared using the
      adapter's own patterns type so ``_select_mcp_configs('grep')`` does not
      fail closed; Graph and Mixed runs keep the default patterns.

    Adapter-specific construction (invariant): Claude receives its existing
    ``plugin_dirs`` and ``permission_mode`` fields; OpenCode receives only the
    fields it supports (no plugins, no permission mode - it enforces its own
    deny-by-default permission system). ``runtime.agent_model`` is passed
    unchanged to either adapter.
    """
    runtime = plan.runtime
    assert runtime is not None  # checked by execute_dispatch

    # -- Policy-scoped MCP selection (identical for both adapters) ----------- #
    if run.tool_policy == "graph":
        graph_mcp = runtime.graph_mcp_configs
        grep_mcp: tuple[Path, ...] = ()
    elif run.tool_policy == "grep":
        graph_mcp = ()
        grep_mcp = runtime.grep_mcp_configs
    elif run.tool_policy == "mixed":
        graph_mcp = runtime.graph_mcp_configs
        grep_mcp = runtime.grep_mcp_configs
    else:
        raise DispatchError(f"unknown tool_policy: {run.tool_policy!r}")

    # -- Skill isolation (AIS-012, F2; identical for both adapters) ---------- #
    # The configured skill is the Graph Skill, a Graph-tool resource. Inject it
    # only into runs whose policy grants Graph tool access (graph, mixed); a
    # Grep run receives no skill at all, so the Graph Skill text cannot
    # contaminate the Grep baseline. The config contract keeps a single global
    # ``skill_file``/``skill_text`` field; the dispatcher applies it Graph-only
    # rather than relying on a global resource reaching Grep. Each adapter
    # fail-closes if a skill ever reaches a Grep run (defense-in-depth).
    if run.tool_policy == "grep":
        skill_text: str | None = None
        skill_file: Path | None = None
    else:  # graph or mixed
        skill_text = runtime.skill_text
        skill_file = runtime.skill_file

    # -- Adapter selection (AIS-014) ----------------------------------------- #
    if runtime.agent_adapter == "opencode":
        # Clear Graph tool-name patterns for Grep runs using the OpenCode
        # patterns type so _select_mcp_configs('grep') does not fail closed.
        opencode_patterns = (
            OpenCodeToolNamePatterns(graph=()) if run.tool_policy == "grep" else None
        )
        # Opt-in per-step NDJSON audit capture (diagnostic, off by default).
        # When OPENCODE_AUDIT_STREAM=1 the raw event stream is persisted to the
        # run directory as opencode-stream.ndjson for per-step cost analysis.
        audit_stream_path = None
        if os.environ.get("OPENCODE_AUDIT_STREAM") == "1" and runtime.runs_root is not None:
            audit_stream_path = runtime.runs_root / run.run_id / "opencode-stream.ndjson"
        return OpenCodeAgentAdapter(
            prompt=plan.case_prompt,
            case_id=run.identity.case_id,
            task_type=run.identity.task_type,
            agent_model=runtime.agent_model,
            repo_cwd=runtime.repo_cwd,
            graph_mcp_configs=graph_mcp,
            grep_mcp_configs=grep_mcp,
            skill_text=skill_text,
            skill_file=skill_file,
            tool_name_patterns=opencode_patterns,
            audit_stream_path=audit_stream_path,
        )

    if runtime.agent_adapter != DEFAULT_AGENT_ADAPTER:
        # Defense-in-depth: validate_experiment_config should have rejected an
        # unknown adapter before execution (CONFIG_INVALID_AGENT_ADAPTER). Never
        # silently construct a Claude adapter for an unrecognized value.
        raise DispatchError(f"unknown agent_adapter: {runtime.agent_adapter!r}")

    # claude-code (default): clear Graph tool-name patterns for Grep runs so the
    # adapter does not fail closed in _select_mcp_configs for grep policy.
    claude_patterns = ToolNamePatterns(graph=()) if run.tool_policy == "grep" else None
    return ClaudeCodeAgentAdapter(
        prompt=plan.case_prompt,
        case_id=run.identity.case_id,
        task_type=run.identity.task_type,
        agent_model=runtime.agent_model,
        repo_cwd=runtime.repo_cwd,
        graph_mcp_configs=graph_mcp,
        grep_mcp_configs=grep_mcp,
        plugin_dirs=runtime.plugin_dirs,
        skill_text=skill_text,
        skill_file=skill_file,
        permission_mode=runtime.permission_mode,
        tool_name_patterns=claude_patterns,
    )


# --------------------------------------------------------------------------- #
# Internal: JSON pointer helper
# --------------------------------------------------------------------------- #


def _json_pointer(path: Any) -> str:
    """Build an RFC 6901 JSON Pointer from a jsonschema ``absolute_path``."""
    if not path:
        return ""
    parts: list[str] = []
    for segment in path:
        if isinstance(segment, int) and not isinstance(segment, bool):
            parts.append(str(segment))
        else:
            s = str(segment)
            parts.append(s.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(parts)
