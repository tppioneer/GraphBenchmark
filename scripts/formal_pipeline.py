"""AIS-012 formal experiment pipeline: Judge -> consensus -> scoring -> report.

This script chains the phases that have no CLI subcommand:
  1. For each awaiting-judge run, run the formal Judge (A+B, optionally C),
     form consensus, build the effective score, and write all artifacts.
  2. Load all runs from the runs_root and generate the report bundle.

Usage::

    python -m scripts.formal_pipeline <formal-config.yaml>

The script reads the experiment config to resolve paths (case, GT, profile,
runs_root, judge_model). It never modifies the config or the agent artifacts;
it only writes judge/score artifacts and rewrites the manifest.

No formal experiment, MCP process, or external service beyond the Judge CLI
is executed by the reporting phase.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from judge.cache import JudgeCache  # noqa: E402
from judge.consensus import build_effective_score  # noqa: E402
from judge.judge_runner import JudgeRunConfig, JudgeRunner  # noqa: E402
from judge.provider import ClaudeCodeCliProvider, JudgeProviderConfig  # noqa: E402
from report.aggregate import aggregate  # noqa: E402
from report.analysis_input import load_runs  # noqa: E402
from report.canonical_audit import report_bundle_digest  # noqa: E402
from report.visualization.text import render_report_bundle  # noqa: E402
from scoring.aggregator import score_to_dict  # noqa: E402
from scoring.profiles import load_validated_task_profile  # noqa: E402


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _canonical_json(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, doc: Any) -> None:
    path.write_text(_canonical_json(doc), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_manifest(run_dir: Path, run_id: str, updates: dict[str, str]) -> None:
    """Rewrite manifest.json with updated artifact statuses.

    ``updates`` maps artifact name -> status ("present", "failed", "absent").
    For "present", a path and sha256 are computed from the on-disk file.
    """
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename_map = {
        "blind_input": "blind-input.json",
        "judge_a": "judge-a.json",
        "judge_b": "judge-b.json",
        "judge_c": "judge-c.json",
        "judge_score": "judge-score.json",
        "effective_score": "effective-score.json",
    }
    for entry in manifest["artifacts"]:
        name = entry["name"]
        if name not in updates:
            continue
        status = updates[name]
        if status == "present":
            fname = filename_map[name]
            fpath = run_dir / fname
            entry["status"] = "present"
            entry["path"] = f"{run_id}/{fname}"
            entry["sha256"] = _sha256_file(fpath) if fpath.exists() else ""
        else:
            entry["status"] = status
            entry.pop("path", None)
            entry.pop("sha256", None)
    manifest_path.write_text(_canonical_json(manifest), encoding="utf-8")


def _judge_cost(result: Any) -> dict[str, Any]:
    """Extract judge cost metrics from a JudgeRunResult."""
    judges = tuple(j for j in (result.judge_a, result.judge_b, result.judge_c) if j is not None)
    call_count = len(judges)
    total_latency = sum(j.elapsed_ms for j in judges)
    total_retries = sum(j.retry_count for j in judges)
    input_tokens = 0
    output_tokens = 0
    for audit in result.audits:
        if audit.judge_output and isinstance(audit.judge_output, dict):
            usage = audit.judge_output.get("usage", {})
            if isinstance(usage, dict):
                input_tokens += int(usage.get("input_tokens", 0) or 0)
                output_tokens += int(usage.get("output_tokens", 0) or 0)
    return {
        "judge_call_count": call_count,
        "total_latency_ms": total_latency,
        "total_retries": total_retries,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _consensus_info(result: Any) -> dict[str, Any]:
    """Derive consensus mode/judges/arbiter from a JudgeRunResult."""
    judges = sum(1 for j in (result.judge_a, result.judge_b, result.judge_c) if j is not None)
    arbiter = result.arbiter_called
    if judges >= 3:
        mode = "median"
    elif judges >= 2:
        mode = "mean"
    else:
        mode = "single"
    return {"mode": mode, "judges": judges, "arbiter_used": arbiter}


def run_judge_phase(
    config_path: Path,
) -> None:
    """Run the Judge + consensus + scoring phase for all awaiting-judge runs."""
    config = _load_yaml(config_path)
    repo_root = config_path.resolve().parent.parent

    case = _load_yaml(repo_root / config["case"])
    ground_truth = _load_yaml(repo_root / config["ground_truth"])
    task_profile, common_profile = load_validated_task_profile(config["task_type"])

    runs_root = Path(config["runtime"]["runs_root"])
    judge_model = config.get("judge_model", "glm-5.2")

    provider = ClaudeCodeCliProvider(JudgeProviderConfig(judge_model=judge_model))
    cache = JudgeCache()
    runner = JudgeRunner(
        provider,
        config=JudgeRunConfig(judge_model=judge_model, run_mode="formal"),
        cache=cache,
    )

    run_dirs = sorted(d for d in runs_root.iterdir() if d.is_dir())
    for run_dir in run_dirs:
        run_id = run_dir.name
        answer_path = run_dir / "agent-answer.json"
        if not answer_path.exists():
            print(f"  {run_id}: no agent-answer.json, skipping")
            continue

        agent_answer = json.loads(answer_path.read_text(encoding="utf-8"))

        # Check policy validity
        policy_path = run_dir / "policy-result.json"
        if policy_path.exists():
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            if not policy.get("valid", False):
                print(f"  {run_id}: policy invalid, skipping Judge")
                continue

        print(f"  {run_id}: running Judge (formal mode)...")
        try:
            result = runner.run(
                case,
                task_profile,
                ground_truth,
                agent_answer,
                task_profile=task_profile,
                common_profile=common_profile,
            )
        except Exception as exc:
            print(f"  {run_id}: Judge FAILED: {exc}")
            _rewrite_manifest(run_dir, run_id, {
                "blind_input": "absent",
                "judge_a": "failed",
                "judge_b": "failed",
                "judge_c": "absent",
                "judge_score": "failed",
                "effective_score": "failed",
            })
            continue

        if not result.success or result.status != "completed":
            print(f"  {run_id}: Judge status={result.status}, reason={result.failure_reason}")
            updates: dict[str, str] = {}
            judge_pairs = [
                ("judge_a", result.judge_a),
                ("judge_b", result.judge_b),
                ("judge_c", result.judge_c),
            ]
            for label, j in judge_pairs:
                if j is not None and j.judge_output is not None:
                    _write_json(run_dir / f"judge-{label[-1].lower()}.json", j.judge_output)
                    updates[label] = "present"
                elif j is not None:
                    updates[label] = "failed"
                else:
                    updates[label] = "absent"
            if result.blind_input is not None:
                _write_json(run_dir / "blind-input.json", result.blind_input)
                updates["blind_input"] = "present"
            updates["judge_score"] = "failed"
            updates["effective_score"] = "failed"
            _rewrite_manifest(run_dir, run_id, updates)
            continue

        # Write judge outputs
        updates = {}
        judge_pairs = [
            ("judge_a", result.judge_a),
            ("judge_b", result.judge_b),
            ("judge_c", result.judge_c),
        ]
        for label, j in judge_pairs:
            if j is not None and j.judge_output is not None:
                _write_json(run_dir / f"judge-{label[-1].lower()}.json", j.judge_output)
                updates[label] = "present"
            else:
                updates[label] = "absent"

        # Write blind input
        if result.blind_input is not None:
            _write_json(run_dir / "blind-input.json", result.blind_input)
            updates["blind_input"] = "present"
        else:
            updates["blind_input"] = "absent"

        # Build effective score
        from scoring.aggregator import VersionMetadata

        judge_outputs = [
            j.judge_output
            for j in (result.judge_a, result.judge_b, result.judge_c)
            if j is not None and j.judge_output is not None
        ]

        version_metadata = VersionMetadata(
            benchmark_version="ai-score-v1",
            judge_protocol="semantic_outcome_v1",
            scoring_profile=ground_truth["scoring_profile"],
            judge_provider="claude-code-cli",
            judge_requested_model=result.judge_model,
            judge_model=result.judge_model,
            judge_cli_version=result.cli_version,
            judge_prompt_digest=result.prompt_digest,
            ground_truth_digest=result.ground_truth_digest,
            agent_answer_digest=result.agent_answer_digest,
            case_id=case["case_id"],
            task_type=case["task_type"],
        )

        try:
            score = build_effective_score(
                judge_outputs,
                ground_truth,
                version_metadata=version_metadata,
                task_profile=task_profile,
                common_profile=common_profile,
                run_mode=result.run_mode,
            )
        except Exception as exc:
            print(f"  {run_id}: consensus/scoring FAILED: {exc}")
            updates["judge_score"] = "failed"
            updates["effective_score"] = "failed"
            _rewrite_manifest(run_dir, run_id, updates)
            continue

        score_dict = score_to_dict(score)
        _write_json(run_dir / "effective-score.json", score_dict)
        updates["effective_score"] = "present"

        # Write judge-score.json
        judge_score_doc = {
            "schema_version": "judge-score-v1",
            "consensus": _consensus_info(result),
            "judge_cost": _judge_cost(result),
        }
        _write_json(run_dir / "judge-score.json", judge_score_doc)
        updates["judge_score"] = "present"

        _rewrite_manifest(run_dir, run_id, updates)
        print(
            f"  {run_id}: Judge completed, effective score written "
            f"(capped_total={score.capped_total})"
        )


def run_report_phase(config_path: Path) -> None:
    """Load all runs and generate the report bundle."""
    config = _load_yaml(config_path)
    runs_root = Path(config["runtime"]["runs_root"])

    records = load_runs(runs_root)
    bundle = aggregate(records)

    report_json = bundle.to_dict()
    report_path = runs_root / "report.json"
    _write_json(report_path, report_json)

    digest = report_bundle_digest(bundle)
    digest_path = runs_root / "report.digest.txt"
    digest_path.write_text(digest + "\n", encoding="utf-8")

    text_report = render_report_bundle(bundle)
    text_path = runs_root / "report.md"
    text_path.write_text(text_report, encoding="utf-8")

    scored = sum(1 for r in records if r.is_scored)
    isolated = len(records) - scored
    print(f"\nReport: {len(records)} runs ({scored} scored, {isolated} isolated)")
    print(f"  report.json: {report_path}")
    print(f"  report.md:   {text_path}")
    print(f"  digest:      {digest}")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python -m scripts.formal_pipeline <formal-config.yaml>")
        return 1

    config_path = Path(argv[0])
    if not config_path.is_file():
        print(f"error: config not found: {config_path}")
        return 1

    print("=== Judge phase ===")
    run_judge_phase(config_path)

    print("\n=== Report phase ===")
    run_report_phase(config_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
