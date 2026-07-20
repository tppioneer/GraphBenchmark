from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    case_id: str
    case_file: str
    golden_file: str
    agent: str
    tool_policy: str
    graph_provider: str | None
    graph_repository: str | None
    graph_mcp_server: str | None
    graph_allowed_tools: tuple[str, ...]
    graph_tool_prefixes: tuple[str, ...]
    graph_discovery_tools: tuple[str, ...]
    repeat: int
    target_project: str
    modification_case: bool


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def resolve_plan_path(plan_path: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (plan_path.parent / path).resolve()


def load_provider_profiles(plan_path: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = plan or load_yaml(plan_path)
    profile_path = resolve_plan_path(plan_path, str(plan.get("provider_profiles", "../graph-providers.yaml")))
    profiles = load_yaml(profile_path).get("providers", {})
    if not isinstance(profiles, dict):
        raise ValueError(f"{profile_path}: providers must be an object")
    return profiles


def _validate_explicit_ids(ground_truth: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ids: list[str] = []
    for section in (
        "expected_entrypoints",
        "expected_symbols",
        "expected_edges",
        "expected_facts",
        "noise_items",
    ):
        items = ground_truth.get(section, [])
        if not isinstance(items, list):
            errors.append(f"ground truth {section} must be a list")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"ground truth {section}[{index}] must be an object")
                continue
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                errors.append(f"ground truth {section}[{index}] is missing explicit id")
            else:
                ids.append(item_id)
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        errors.append(f"duplicate ground truth item ids: {duplicates}")
    return errors


def validate_plan(plan_path: Path) -> list[str]:
    plan = load_yaml(plan_path)
    errors: list[str] = []
    target = plan.get("target_project", {})
    target_path = Path(str(target.get("path", "")))
    if not target_path.is_dir():
        errors.append(f"target_project.path does not exist: {target_path}")
    if not target.get("validation_commands"):
        errors.append("target_project.validation_commands must not be empty")

    cases = plan.get("cases", [])
    if not isinstance(cases, list) or not cases:
        errors.append("cases must not be empty")
        cases = []
    case_ids: list[str] = []
    for case in cases:
        case_id = str(case.get("id", "")).strip()
        case_ids.append(case_id)
        case_path = resolve_plan_path(plan_path, str(case.get("file", "")))
        golden_path = resolve_plan_path(plan_path, str(case.get("ground_truth", "")))
        if not case_path.is_file():
            errors.append(f"{case_id}: case file does not exist: {case_path}")
        else:
            case_data = load_yaml(case_path)
            if case_data.get("id") != case_id:
                errors.append(f"{case_id}: case file id does not match")
        if not golden_path.is_file():
            errors.append(f"{case_id}: ground truth does not exist: {golden_path}")
        else:
            ground_truth = load_yaml(golden_path)
            if ground_truth.get("case_id") != case_id:
                errors.append(f"{case_id}: ground truth case_id does not match")
            errors.extend(f"{case_id}: {error}" for error in _validate_explicit_ids(ground_truth))
    duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicates:
        errors.append(f"duplicate case ids: {duplicates}")

    matrix = plan.get("run_matrix", {})
    agents = matrix.get("agents", [])
    policies = matrix.get("tool_policies", [])
    repeats = matrix.get("repeats_per_cell")
    if not agents:
        errors.append("run_matrix.agents must not be empty")
    if not policies:
        errors.append("run_matrix.tool_policies must not be empty")
    if not isinstance(repeats, int) or repeats < 1:
        errors.append("run_matrix.repeats_per_cell must be a positive integer")
    providers = matrix.get("graph_providers", [])
    try:
        provider_profiles = load_provider_profiles(plan_path, plan)
    except (OSError, ValueError) as exc:
        errors.append(f"provider profiles are invalid: {exc}")
        provider_profiles = {}
    if "graph" in policies and not providers:
        errors.append("run_matrix.graph_providers must not be empty when graph policy is selected")
    repositories = target.get("graph_repositories", {})
    for provider in providers:
        if provider not in provider_profiles:
            errors.append(f"graph provider has no profile: {provider}")
        else:
            profile = provider_profiles[provider]
            for key in ("mcp_server", "tool_prefixes", "allowed_tools", "discovery_tools"):
                if not profile.get(key):
                    errors.append(f"graph provider {provider} profile must define {key}")
        if not str(repositories.get(provider, "")).strip():
            errors.append(f"target_project.graph_repositories.{provider} must be explicit")
    cells_per_case_agent = sum(len(providers) if policy == "graph" else 1 for policy in policies)
    expected = len(cases) * len(agents) * cells_per_case_agent * (repeats if isinstance(repeats, int) else 0)
    declared = matrix.get("total_required_runs")
    if declared is not None and declared != expected:
        errors.append(f"total_required_runs {declared} does not match matrix computation {expected}")
    return errors


def expand_plan(plan_path: Path) -> dict[str, Any]:
    errors = validate_plan(plan_path)
    if errors:
        raise ValueError("Invalid benchmark plan:\n" + "\n".join(f"- {error}" for error in errors))
    plan = load_yaml(plan_path)
    target = plan["target_project"]
    matrix = plan["run_matrix"]
    provider_profiles = load_provider_profiles(plan_path, plan)
    runs: list[RunSpec] = []
    for case in plan["cases"]:
        for agent in matrix["agents"]:
            for policy in matrix["tool_policies"]:
                provider_names = matrix["graph_providers"] if policy == "graph" else [None]
                for provider_name in provider_names:
                    profile = provider_profiles.get(provider_name, {}) if provider_name else {}
                    graph_repository = target.get("graph_repositories", {}).get(provider_name) if provider_name else None
                    for repeat in range(1, matrix["repeats_per_cell"] + 1):
                        provider_segment = f"__{provider_name}" if provider_name else ""
                        run_id = f"{case['id']}__{agent}__{policy}{provider_segment}__r{repeat}"
                        runs.append(
                            RunSpec(
                                run_id=run_id,
                                case_id=case["id"],
                                case_file=str(resolve_plan_path(plan_path, case["file"])),
                                golden_file=str(resolve_plan_path(plan_path, case["ground_truth"])),
                                agent=agent,
                                tool_policy=policy,
                                graph_provider=provider_name,
                                graph_repository=graph_repository,
                                graph_mcp_server=str(profile.get("mcp_server", "")) or None,
                                graph_allowed_tools=tuple(profile.get("allowed_tools", [])),
                                graph_tool_prefixes=tuple(profile.get("tool_prefixes", [])),
                                graph_discovery_tools=tuple(profile.get("discovery_tools", [])),
                                repeat=repeat,
                                target_project=target["path"],
                                modification_case=bool(case.get("modification_case", False)),
                            )
                        )
    return {"name": plan.get("name"), "run_count": len(runs), "runs": [asdict(run) for run in runs]}


def find_run(expanded: dict[str, Any], run_id: str) -> dict[str, Any]:
    for run in expanded["runs"]:
        if run["run_id"] == run_id:
            return run
    raise ValueError(f"run_id not found: {run_id}")
