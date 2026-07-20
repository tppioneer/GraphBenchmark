import json
from pathlib import Path

from graphbenchmark.config import expand_plan, find_run, validate_plan
from graphbenchmark.prompting import build_prompt
from graphbenchmark.runner import apply_run_metadata, execute_matrix, extract_agent_result
from graphbenchmark.scoring import score_result


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "benchmark" / "plans" / "telecom-case-v.yaml"
GROUND_TRUTH = ROOT / "benchmark" / "cases" / "telecom" / "case-v" / "ground-truth.yaml"
SCHEMA = ROOT / "benchmark" / "schemas" / "run-result.schema.json"


def write_mcp_config(path: Path, *servers: str) -> Path:
    path.write_text(json.dumps({"mcpServers": {server: {} for server in servers}}), encoding="utf-8")
    return path


def test_plan_is_valid_and_expands_to_nine_runs():
    assert validate_plan(PLAN) == []
    expanded = expand_plan(PLAN)
    assert expanded["run_count"] == 9
    assert {run["tool_policy"] for run in expanded["runs"]} == {"grep", "graph"}
    graph_runs = [run for run in expanded["runs"] if run["tool_policy"] == "graph"]
    assert {run["graph_provider"] for run in graph_runs} == {"gitnexus", "aka"}
    assert all(run["graph_provider"] is None for run in expanded["runs"] if run["tool_policy"] == "grep")


def test_graph_prompt_is_provider_specific_and_hides_ground_truth():
    expanded = expand_plan(PLAN)
    gitnexus_run = find_run(expanded, "telecom-case-v-workorder-notification-flow__claude-code__graph__gitnexus__r1")
    aka_run = find_run(expanded, "telecom-case-v-workorder-notification-flow__claude-code__graph__aka__r1")
    gitnexus_prompt = build_prompt(gitnexus_run, SCHEMA, ["mvn test"])
    aka_prompt = build_prompt(aka_run, SCHEMA, ["mvn test"])
    assert '"graph_provider": "gitnexus"' in gitnexus_prompt
    assert "mcp__gitnexus__query" in gitnexus_prompt
    assert '"graph_provider": "aka"' in aka_prompt
    assert "Aka MCP graph search" in aka_prompt
    assert "repository identity 'telecom'" in gitnexus_prompt
    assert "ground-truth.yaml" not in gitnexus_prompt + aka_prompt
    assert "private_review_note" not in gitnexus_prompt + aka_prompt


def test_matrix_dry_run_uses_runs_v1_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mcp_config = write_mcp_config(tmp_path / ".mcp.json", "gitnexus", "aka")
    summary = execute_matrix(
        PLAN, out_root=None, dry_run=True, model="test-model", mcp_configs={"*": str(mcp_config)}
    )
    assert summary["selected_run_count"] == 9
    assert summary["selection_counts"] == {"graph:aka": 3, "graph:gitnexus": 3, "grep": 3}
    assert Path(summary["out_dir"]).as_posix().endswith("runs/v1/telecom-test-model")
    assert (Path(summary["out_dir"]) / "matrix-result.json").is_file()


def test_case_id_filter_accepts_repeated_cli_semantics(tmp_path: Path):
    summary = execute_matrix(
        PLAN, out_root=tmp_path / "runs", dry_run=True, model=None, mcp_configs={},
        case_ids={"telecom-case-v-workorder-notification-flow"}, policies={"graph"}, providers={"gitnexus"},
    )
    assert summary["selected_run_count"] == 3


def test_extracts_claude_code_wrapped_result():
    inner = {"case_id": "case-v", "final_answer": {"summary": "ok"}}
    stdout = json.dumps({"type": "result", "result": json.dumps(inner)})
    assert extract_agent_result(stdout) == inner


def test_runner_metadata_overrides_agent_reported_provider():
    run = find_run(
        expand_plan(PLAN), "telecom-case-v-workorder-notification-flow__claude-code__graph__aka__r1"
    )
    normalized = apply_run_metadata(
        {"case_id": "wrong", "graph_provider": "gitnexus", "final_answer": {"summary": "ok"}},
        run,
        True,
    )
    assert normalized["case_id"] == "telecom-case-v-workorder-notification-flow"
    assert normalized["graph_provider"] == "aka"
    assert normalized["graph_repository"] == "telecom"


def test_graph_dry_run_records_unenforced_policy_without_mcp(tmp_path: Path):
    summary = execute_matrix(
        PLAN, out_root=tmp_path / "runs", dry_run=True, model=None, mcp_configs={},
        policies={"graph"},
    )
    assert summary["selected_run_count"] == 6
    assert all(result["manifest"]["policy_enforced"] is False for result in summary["results"])
    prompt_path = Path(summary["results"][0]["manifest"]["prompt_file"])
    assert '"policy_enforced": false' in prompt_path.read_text(encoding="utf-8")


def test_scoring_uses_explicit_ids_and_structured_edges(tmp_path: Path):
    result_path = tmp_path / "agent-result.json"
    chain = [
        {"name": name, "symbol": name, "file": file, "reason": "graph evidence"}
        for name, file in [
            ("WorkOrderFlowService.assign", "workorder-service/src/main/java/com/example/telecom/workorder/service/WorkOrderFlowService.java"),
            ("WorkOrderStateMachine.transition", "workorder-service/src/main/java/com/example/telecom/workorder/workflow/WorkOrderStateMachine.java"),
            ("WorkOrderRepository.save", "workorder-service/src/main/java/com/example/telecom/workorder/repository/WorkOrderRepository.java"),
            ("WorkOrderEventPublisher.publish", "workorder-service/src/main/java/com/example/telecom/workorder/event/WorkOrderEventPublisher.java"),
        ]
    ]
    result = {
        "case_id": "telecom-case-v-workorder-notification-flow", "run_id": "sample", "agent": "claude-code",
        "tool_policy": "graph", "graph_provider": "gitnexus", "graph_repository": "telecom",
        "policy_enforced": True, "started_at": "", "ended_at": "", "status": "passed",
        "final_answer": {"summary": "Invalid transition fails before event publication.",
                         "entrypoints": chain[:1], "symbols": chain[1:], "call_chains": [chain],
                         "files": [], "data_flows": [], "risks": [], "recommended_tests": []},
        "evidence": chain, "tool_calls": [{"tool": "mcp__gitnexus__query", "purpose": "trace",
                                            "input_summary": "repo=telecom"}],
        "metrics": {"tool_call_count": 1, "files_read_count": 4, "graph_query_count": 1}, "violations": [],
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")
    score = score_result(result_path, GROUND_TRUTH)
    assert score["valid"] is True
    assert any(item["item_id"] == "edge.assign-to-transition" and item["automatic_credit"] == 1.0
               for item in score["item_scores"])
    assert all("legacy" not in item["item_id"] for item in score["item_scores"])

    result["graph_provider"] = "aka"
    result["tool_calls"] = [{"tool": "mcp__aka__search", "purpose": "trace",
                              "input_summary": "repository=telecom"}]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    aka_score = score_result(result_path, GROUND_TRUTH)
    assert aka_score["valid"] is True
    assert aka_score["graph_provider"] == "aka"


def test_provider_specific_mcp_configs_do_not_cross_enforce(tmp_path: Path):
    gitnexus_config = write_mcp_config(tmp_path / "gitnexus.json", "gitnexus")
    summary = execute_matrix(
        PLAN, out_root=tmp_path / "runs", dry_run=True, model=None,
        mcp_configs={"gitnexus": str(gitnexus_config)}, policies={"graph"},
    )
    by_provider = {result["manifest"]["graph_provider"]: result["manifest"]["policy_enforced"]
                   for result in summary["results"]}
    assert by_provider == {"gitnexus": True, "aka": False}
