from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphbenchmark.config import load_yaml


PUBLIC_CASE_FIELDS = {"id", "name", "level", "task_type", "prompt"}


def graph_guidance(run: dict[str, Any]) -> dict[str, Any]:
    provider = run["graph_provider"]
    repository = run["graph_repository"]
    return {
        "provider": provider,
        "purpose": f"Evaluate normal code investigation enhanced by the {provider} graph provider.",
        "main_discovery_path": (
            f"Begin or early-stage the investigation with {provider} graph discovery tools. "
            "Use graph results to establish the main execution path before finalizing the answer."
        ),
        "repo_identity": (
            f"Every repo-scoped {provider} call MUST use repository identity {repository!r}. "
            "Confirm that the first graph result belongs to this repository."
        ),
        "discovery_tools": run["graph_discovery_tools"],
        "text_search_role": (
            "rg/grep/glob/find/read are allowed for local confirmation and fallback after graph discovery; "
            "do not build the answer entirely from text search."
        ),
        "reporting": (
            f"Record concrete {provider} MCP tool names, include the repository identity in input_summary, "
            "and set metrics.graph_query_count to the number of graph calls."
        ),
    }


def result_template(run: dict[str, Any], policy_enforced: bool = True) -> dict[str, Any]:
    return {
        "case_id": run["case_id"],
        "run_id": run["run_id"],
        "agent": run["agent"],
        "agent_model": "",
        "tool_policy": run["tool_policy"],
        "graph_provider": run["graph_provider"],
        "graph_repository": run["graph_repository"],
        "policy_enforced": policy_enforced,
        "target_repo": run["target_project"],
        "target_commit": "",
        "started_at": "",
        "ended_at": "",
        "status": "passed",
        "final_answer": {
            "summary": "",
            "entrypoints": [],
            "symbols": [],
            "files": [],
            "call_chains": [],
            "data_flows": [],
            "risks": [],
            "recommended_tests": [],
        },
        "evidence": [],
        "tool_calls": [{
            "tool": "Concrete MCP graph tool name or Bash",
            "purpose": "Why this tool was used.",
            "input_summary": "For graph calls, include the provider-specific repository identity.",
            "output_summary": "Short factual result summary.",
            "allowed_by_policy": True,
        }],
        "metrics": {
            "tool_call_count": 0,
            "files_read_count": 0,
            "search_query_count": 0,
            "graph_query_count": 0,
            "elapsed_ms": 0,
        },
        "violations": [],
    }


def build_prompt(
    run: dict[str, Any],
    schema_path: Path,
    validation_commands: list[str],
    policy_enforced: bool = True,
) -> str:
    case = load_yaml(Path(run["case_file"]))
    public_case = {key: value for key, value in case.items() if key in PUBLIC_CASE_FIELDS}
    payload: dict[str, Any] = {
        "benchmark_run": {key: run[key] for key in (
            "case_id", "run_id", "agent", "tool_policy", "target_project", "modification_case"
        )},
        "case": public_case,
        "tool_policy": {
            "grep": "Do not use any code-graph MCP or CLI retrieval capability.",
            "graph": "The selected graph provider must participate in the main discovery path.",
        }[run["tool_policy"]],
        "output_contract": {
            "schema": str(schema_path.resolve()),
            "format": "Return only one JSON object matching the schema; no Markdown or fenced code blocks.",
            "golden_answers_are_hidden": True,
            "template": result_template(run, policy_enforced),
        },
        "validation_commands": validation_commands,
    }
    if run["tool_policy"] == "graph":
        payload["graph_policy_guidance"] = graph_guidance(run)
    return (
        "你正在执行一个代码图谱能力 benchmark run。\n"
        "不要读取 Ground Truth、历史 run 输出或评分说明。\n"
        "必须遵守工具策略，并且只输出符合 schema 的 JSON。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
