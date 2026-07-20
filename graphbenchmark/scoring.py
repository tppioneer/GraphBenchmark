from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from graphbenchmark.config import load_yaml


WEIGHTS = {"location": 15.0, "symbols": 20.0, "call_edges": 25.0,
           "behavior_facts": 20.0, "evidence": 10.0, "noise": 10.0}
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "schemas" / "run-result.schema.json"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return ""


def _normalize(symbol: str) -> str:
    return re.sub(r"\s+", "", symbol).replace("#", ".").lower()


def _symbol(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or item.get("name") or "")


def _same_symbol(expected: str, actual: str) -> bool:
    left, right = _normalize(expected), _normalize(actual)
    return left == right or left.rsplit(".", 1)[-1] == right.rsplit(".", 1)[-1]


def _answer_edges(answer: dict[str, Any]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for field in ("call_chains", "data_flows"):
        for chain in answer.get(field, []):
            if isinstance(chain, list):
                nodes = [_symbol(item) for item in chain if isinstance(item, dict) and _symbol(item)]
                edges.extend(zip(nodes, nodes[1:]))
    return edges


def _fact_hit(description: str, answer_text: str) -> bool:
    tokens = list(dict.fromkeys(re.findall(r"[a-z_][a-z0-9_.-]{3,}", description.lower())))
    stop = {"that", "with", "from", "through", "before", "otherwise", "primary", "direct"}
    tokens = [token for token in tokens if token not in stop]
    required = min(len(tokens), max(2, (len(tokens) + 2) // 3))
    return bool(tokens) and sum(token in answer_text for token in tokens) >= required


def _provider_profiles() -> dict[str, Any]:
    return load_yaml(Path(__file__).resolve().parents[1] / "benchmark" / "graph-providers.yaml")["providers"]


def _graph_tools(result: dict[str, Any], prefixes: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    lowered = tuple(prefix.lower() for prefix in prefixes)
    return [call for call in result.get("tool_calls", []) if isinstance(call, dict)
            and str(call.get("tool", "")).lower().startswith(lowered)]


def score_result(result_path: Path, ground_truth_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda error: list(error.path))
    if schema_errors:
        messages = [f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in schema_errors]
        raise ValueError("Invalid run result:\n" + "\n".join(f"- {message}" for message in messages))
    ground_truth = load_yaml(ground_truth_path)
    answer = result.get("final_answer", {})
    answer_text = _text(answer).lower()
    structured_items = [item for field in ("entrypoints", "symbols")
                        for item in answer.get(field, []) if isinstance(item, dict)]
    evidence_items = [item for item in result.get("evidence", []) if isinstance(item, dict)]
    expected_symbols = ground_truth.get("expected_entrypoints", []) + ground_truth.get("expected_symbols", [])
    actual_edges = _answer_edges(answer)
    item_scores: list[dict[str, Any]] = []

    def add(item: dict[str, Any], dimension: str, credit: float, source: str) -> None:
        item_scores.append({"item_id": item["id"], "dimension": dimension,
                            "automatic_credit": credit, "match_source": source,
                            "expected": {key: value for key, value in item.items() if key != "id"}})

    for item in expected_symbols:
        symbol_hit = any(_same_symbol(item["symbol"], _symbol(actual)) for actual in structured_items)
        file_hit = any(str(item["file"]).lower() in _text(actual).lower() for actual in structured_items + evidence_items)
        evidence_hit = any(_same_symbol(item["symbol"], _symbol(actual)) and bool(actual.get("reason"))
                           for actual in structured_items + evidence_items)
        add(item, "symbols", 1.0 if symbol_hit else 0.0, "structured_symbol" if symbol_hit else "not_found")
        add({"id": item["id"] + ".location", "file": item["file"]}, "location",
            1.0 if file_hit else 0.0, "structured_file" if file_hit else "not_found")
        add({"id": item["id"] + ".evidence", "symbol": item["symbol"]}, "evidence",
            1.0 if evidence_hit else 0.0, "reasoned_evidence" if evidence_hit else "not_found")

    for item in ground_truth.get("expected_edges", []):
        exact = any(_normalize(item["from"]) == _normalize(source) and _normalize(item["to"]) == _normalize(target)
                    for source, target in actual_edges)
        short = any(_same_symbol(item["from"], source) and _same_symbol(item["to"], target)
                    for source, target in actual_edges)
        if exact:
            credit, source = 1.0, "structured_exact"
        elif short:
            credit, source = 0.8, "structured_short_symbol"
        else:
            start = answer_text.find(item["from"].lower())
            nearby = start >= 0 and item["to"].lower() in answer_text[start:start + 500]
            credit, source = (0.4, "text_proximity") if nearby else (0.0, "not_found")
        add(item, "call_edges", credit, source)

    for item in ground_truth.get("expected_facts", []):
        hit = _fact_hit(item["description"], answer_text)
        add(item, "behavior_facts", 1.0 if hit else 0.0, "lexical_fact" if hit else "not_found")

    negative_cues = ("not used", "not invoked", "excluded", "noise", "不调用", "未调用", "排除")
    for item in ground_truth.get("noise_items", []):
        term = str(item.get("symbol", item.get("text", ""))).lower()
        position = answer_text.find(term)
        if position < 0:
            credit, source = 1.0, "not_mentioned"
        else:
            window = answer_text[max(0, position - 120):position + len(term) + 120]
            excluded = any(cue in window for cue in negative_cues)
            credit, source = (1.0, "correctly_excluded") if excluded else (0.0, "erroneously_included")
        add(item, "noise", credit, source)

    dimensions: dict[str, float] = {}
    for dimension, weight in WEIGHTS.items():
        items = [item for item in item_scores if item["dimension"] == dimension]
        dimensions[dimension] = round(weight * sum(item["automatic_credit"] for item in items) / len(items), 4) if items else weight
    for item in item_scores:
        count = sum(candidate["dimension"] == item["dimension"] for candidate in item_scores)
        item["max_points"] = round(WEIGHTS[item["dimension"]] / count, 6) if count else 0.0
        item["automatic_points"] = round(item["max_points"] * item["automatic_credit"], 4)

    violations = list(result.get("violations", []))
    provider = result.get("graph_provider")
    profile = _provider_profiles().get(provider, {}) if provider else {}
    graph_calls = _graph_tools(result, profile.get("tool_prefixes", []))
    graph_count = int(result.get("metrics", {}).get("graph_query_count", 0))
    if result.get("tool_policy") == "graph":
        if not provider or not profile:
            violations.append({"type": "tool_policy", "description": "Graph policy has no recognized graph_provider."})
        if not graph_calls or graph_count < 1:
            violations.append({"type": "tool_policy", "description": "Graph policy used no selected-provider retrieval call."})
        repository = str(result.get("graph_repository", "")).lower()
        wrong_repo_calls = [call for call in graph_calls if not repository or repository not in str(call.get("input_summary", "")).lower()]
        if wrong_repo_calls:
            violations.append({"type": "tool_policy", "description": "A graph call did not record the selected repository identity."})
        if not result.get("policy_enforced"):
            violations.append({"type": "tool_policy", "description": "Graph policy was not enforceable by the runner."})
    all_prefixes = [prefix for value in _provider_profiles().values() for prefix in value.get("tool_prefixes", [])]
    any_graph_calls = _graph_tools(result, all_prefixes)
    if result.get("tool_policy") == "grep" and (any_graph_calls or graph_count or provider):
        violations.append({"type": "tool_policy", "description": "Grep policy used graph retrieval or declared a graph provider."})
    return {
        "case_id": result.get("case_id"),
        "run_id": result.get("run_id"),
        "agent": result.get("agent"),
        "tool_policy": result.get("tool_policy"),
        "graph_provider": provider,
        "automatic_total": round(sum(dimensions.values()), 4),
        "dimensions": dimensions,
        "item_scores": item_scores,
        "valid": not violations,
        "violations": violations,
    }
