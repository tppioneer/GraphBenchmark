from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graphbenchmark.adapters import build_command
from graphbenchmark.config import expand_plan, find_run, load_yaml
from graphbenchmark.prompting import build_prompt


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_out_root(plan_path: Path, model: str | None) -> Path:
    plan = load_yaml(plan_path)
    project = str(plan["target_project"].get("slug", plan["target_project"]["name"]))
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") if model else ""
    suffix = f"-{safe_model}" if safe_model else ""
    return Path("runs") / "v1" / f"{project}{suffix}"


def extract_agent_result(stdout: str) -> dict[str, Any] | None:
    """Extract the benchmark JSON from direct or Claude Code wrapped output."""
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(outer, dict) and "case_id" in outer and "final_answer" in outer:
        return outer
    if not isinstance(outer, dict):
        return None
    wrapped = outer.get("result")
    if isinstance(wrapped, dict) and "case_id" in wrapped:
        return wrapped
    if isinstance(wrapped, str):
        text = wrapped.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) and "case_id" in parsed else None
    return None


def apply_run_metadata(agent_result: dict[str, Any], run: dict[str, Any], policy_enforced: bool) -> dict[str, Any]:
    """Make runner-owned comparison identity authoritative."""
    normalized = dict(agent_result)
    normalized.update({
        "case_id": run["case_id"],
        "run_id": run["run_id"],
        "agent": run["agent"],
        "tool_policy": run["tool_policy"],
        "graph_provider": run["graph_provider"],
        "graph_repository": run["graph_repository"],
        "policy_enforced": policy_enforced,
        "target_repo": run["target_project"],
    })
    return normalized


def prepare_run(
    plan_path: Path,
    run: dict[str, Any],
    out_root: Path,
    *,
    model: str | None,
    mcp_configs: dict[str, str],
) -> dict[str, Any]:
    run_dir = out_root / run["agent"] / run["tool_policy"] / run["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).resolve().parents[1] / "benchmark" / "schemas" / "run-result.schema.json"
    commands = load_yaml(plan_path)["target_project"]["validation_commands"]
    prompt_path = run_dir / "prompt.txt"
    provider = run.get("graph_provider")
    mcp_config = mcp_configs.get(str(provider), mcp_configs.get("*")) if provider else None
    command = build_command(run, prompt_path, model=model, mcp_config=mcp_config)
    prompt_path.write_text(
        build_prompt(run, schema_path, commands, command.policy_enforced), encoding="utf-8"
    )
    manifest = {
        "run": run,
        "model": model,
        "graph_provider": provider,
        "mcp_config": str(Path(mcp_config).resolve()) if mcp_config else None,
        "prompt_file": str(prompt_path.resolve()),
        "command": command.command,
        "cwd": command.cwd,
        "policy_enforced": command.policy_enforced,
        "notes": command.notes,
        "stdin_file": command.stdin_file,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def execute_run(
    plan_path: Path,
    run: dict[str, Any],
    out_root: Path,
    *,
    dry_run: bool,
    model: str | None,
    mcp_configs: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    manifest = prepare_run(plan_path, run, out_root, model=model, mcp_configs=mcp_configs)
    run_dir = Path(manifest["prompt_file"]).parent
    started = now_iso()
    clock = time.monotonic()
    if dry_run:
        result = {"run_id": run["run_id"], "status": "dry-run", "manifest": manifest}
    elif run["tool_policy"] == "graph" and not manifest["policy_enforced"]:
        result = {
            "run_id": run["run_id"],
            "status": "error",
            "error": f"Graph provider {run['graph_provider']} requires a matching --mcp-config; execution was not started.",
            "manifest": manifest,
        }
    else:
        prompt = Path(manifest["stdin_file"]).read_text(encoding="utf-8")
        completed = subprocess.run(
            manifest["command"], cwd=manifest["cwd"], input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds, check=False,
        )
        (run_dir / "stdout.txt").write_text(completed.stdout or "", encoding="utf-8")
        (run_dir / "stderr.txt").write_text(completed.stderr or "", encoding="utf-8")
        agent_result = extract_agent_result(completed.stdout or "")
        if agent_result is not None:
            agent_result = apply_run_metadata(agent_result, run, manifest["policy_enforced"])
            (run_dir / "agent-result.json").write_text(
                json.dumps(agent_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        result = {
            "run_id": run["run_id"],
            "status": "passed" if completed.returncode == 0 and agent_result else
                      "invalid" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "stdout_file": str((run_dir / "stdout.txt").resolve()),
            "stderr_file": str((run_dir / "stderr.txt").resolve()),
            "agent_result_file": str((run_dir / "agent-result.json").resolve()) if agent_result else None,
            "error": None if agent_result or completed.returncode else
                     "Claude Code output did not contain a valid benchmark JSON object.",
            "manifest": manifest,
        }
    result.update({
        "started_at": started,
        "ended_at": now_iso(),
        "elapsed_ms": int((time.monotonic() - clock) * 1000),
    })
    (run_dir / "runner-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def execute_matrix(
    plan_path: Path,
    *,
    out_root: Path | None,
    dry_run: bool,
    model: str | None,
    mcp_configs: dict[str, str] | None,
    case_ids: set[str] | None = None,
    policies: set[str] | None = None,
    providers: set[str] | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    expanded = expand_plan(plan_path)
    selected = [run for run in expanded["runs"]
                if (not case_ids or run["case_id"] in case_ids)
                and (not policies or run["tool_policy"] in policies)
                and (not providers or run["graph_provider"] in providers)]
    if case_ids:
        missing = sorted(case_ids - {run["case_id"] for run in selected})
        if missing:
            raise ValueError(f"case_id not found in selected plan: {','.join(missing)}")
    if providers:
        available = {run["graph_provider"] for run in expanded["runs"] if run["graph_provider"]}
        missing_providers = sorted(providers - available)
        if missing_providers:
            raise ValueError(f"graph_provider not found in selected plan: {','.join(missing_providers)}")
    destination = out_root or default_out_root(plan_path, model)
    results = [execute_run(
        plan_path, run, destination, dry_run=dry_run, model=model,
        mcp_configs=mcp_configs or {}, timeout_seconds=timeout_seconds,
    ) for run in selected]
    summary = {
        "plan": str(plan_path.resolve()),
        "out_dir": str(destination.resolve()),
        "dry_run": dry_run,
        "selected_run_count": len(selected),
        "selection_counts": {
            label: sum(
                (run["tool_policy"] if run["tool_policy"] == "grep" else
                 f"graph:{run['graph_provider']}") == label
                for run in selected
            )
            for label in sorted({
                run["tool_policy"] if run["tool_policy"] == "grep" else f"graph:{run['graph_provider']}"
                for run in selected
            })
        },
        "status_counts": {status: sum(result["status"] == status for result in results)
                          for status in sorted({result["status"] for result in results})},
        "results": results,
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "matrix-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
