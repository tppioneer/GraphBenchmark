from __future__ import annotations

import argparse
import json
from pathlib import Path

from graphbenchmark.config import expand_plan, find_run, load_yaml, validate_plan
from graphbenchmark.prompting import build_prompt
from graphbenchmark.runner import execute_matrix
from graphbenchmark.scoring import score_result


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _mcp_configs(values: list[str] | None) -> dict[str, str]:
    configs: dict[str, str] = {}
    for value in values or []:
        if "=" in value:
            provider, path = value.split("=", 1)
            if not provider.strip() or not path.strip():
                raise ValueError("--mcp-config must be PATH or PROVIDER=PATH")
            configs[provider.strip()] = path.strip()
        else:
            configs["*"] = value
    return configs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphbenchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--plan", type=Path, required=True)
    prompt.add_argument("--run-id", required=True)
    execute = sub.add_parser("execute-matrix")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--out-root", type=Path)
    execute.add_argument("--model")
    execute.add_argument("--mcp-config", action="append", help="PATH for all providers or PROVIDER=PATH; repeatable")
    execute.add_argument("--case-id", action="append")
    execute.add_argument("--tool-policy", action="append")
    execute.add_argument("--graph-provider", action="append")
    execute.add_argument("--timeout-seconds", type=int, default=1800)
    execute.add_argument("--dry-run", action="store_true")
    score = sub.add_parser("score")
    score.add_argument("--result", type=Path, required=True)
    score.add_argument("--ground-truth", type=Path, required=True)
    score.add_argument("--out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        errors = validate_plan(args.plan)
        _json({"valid": not errors, "errors": errors})
        return 0 if not errors else 1
    if args.command == "plan":
        _json(expand_plan(args.plan))
        return 0
    if args.command == "prompt":
        run = find_run(expand_plan(args.plan), args.run_id)
        schema = Path(__file__).resolve().parents[1] / "benchmark" / "schemas" / "run-result.schema.json"
        commands = load_yaml(args.plan)["target_project"]["validation_commands"]
        print(build_prompt(run, schema, commands))
        return 0
    if args.command == "execute-matrix":
        _json(execute_matrix(
            args.plan, out_root=args.out_root, dry_run=args.dry_run, model=args.model,
            mcp_configs=_mcp_configs(args.mcp_config), case_ids=set(args.case_id or []) or None,
            policies=set(args.tool_policy or []) or None, timeout_seconds=args.timeout_seconds,
            providers=set(args.graph_provider or []) or None,
        ))
        return 0
    if args.command == "score":
        result = score_result(args.result, args.ground_truth)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _json(result)
        return 0
    return 2
