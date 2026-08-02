"""Synthetic run-directory fixtures for AIS-011 report tests.

Every fixture builds a complete on-disk run directory with valid v1 artifacts
(manifest, run-metadata, policy-result, agent-answer, effective-score,
judge-a/b/c, judge-score). The artifacts conform to the shipped JSON Schemas;
score documents mirror the shape produced by ``scoring.aggregator.score_to_dict``
but are constructed directly here so the report tests stay independent of the
scoring core (excluded scope).

The fixtures are deterministic: the same builder always produces byte-identical
artifacts, so report snapshots are stable across runs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Shared constants for synthetic fixtures.
# ---------------------------------------------------------------------------

DIGEST_PROMPT = "sha256:" + "a" * 64
DIGEST_GT_A = "sha256:" + "b" * 64
DIGEST_GT_B = "sha256:" + ("b" * 63) + "c"
DIGEST_ANSWER_GRAPH_A = "sha256:" + "c" * 64
DIGEST_ANSWER_GREP_A = "sha256:" + ("c" * 63) + "d"
DIGEST_ANSWER_GRAPH_B = "sha256:" + ("c" * 63) + "e"
DIGEST_ANSWER_GREP_B = "sha256:" + ("c" * 63) + "f"
DIGEST_ARTIFACT = "sha256:" + "e" * 64

CASE_A = "synth-case-alpha"
CASE_B = "synth-case-beta"
AGENT = "claude-code"
AGENT_MODEL = "glm-5.2"
JUDGE_MODEL = "glm-5.2"
JUDGE_CLI_VERSION = "2.1.220"
SCORING_PROFILE = "bug_localization_v1"
TASK_TYPE = "bug_localization"

DIMENSIONS = [
    "core_correctness",
    "reasoning_correctness",
    "completeness",
    "scope_precision",
    "evidence_actionability",
]


# ---------------------------------------------------------------------------
# Canonical JSON writer (deterministic, sorted keys, 2-space indent).
# ---------------------------------------------------------------------------


def _write_json(path: Path, doc: Mapping[str, Any]) -> str:
    """Write a JSON document canonically and return its sha256 digest string."""
    from judge.canonical import digest_bytes

    data = (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(data)
    return digest_bytes(data)


# ---------------------------------------------------------------------------
# Artifact builders.
# ---------------------------------------------------------------------------


def _rubric_items() -> list[dict[str, Any]]:
    """A 10-item bug_localization rubric (35/25/20/10/10 = 100)."""
    return [
        {
            "id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 20,
            "criterion": "Identify the root cause.",
            "critical": True,
        },
        {
            "id": "outcome.trigger",
            "dimension": "core_correctness",
            "points": 15,
            "criterion": "Identify the trigger condition.",
        },
        {
            "id": "reasoning.chain",
            "dimension": "reasoning_correctness",
            "points": 12,
            "criterion": "Explain the failure chain.",
            "critical": True,
        },
        {
            "id": "reasoning.propagation",
            "dimension": "reasoning_correctness",
            "points": 13,
            "criterion": "Explain propagation.",
        },
        {
            "id": "completeness.blast",
            "dimension": "completeness",
            "points": 10,
            "criterion": "Cover blast radius.",
        },
        {
            "id": "completeness.recovery",
            "dimension": "completeness",
            "points": 10,
            "criterion": "Cover recovery.",
        },
        {
            "id": "precision.atomic",
            "dimension": "scope_precision",
            "points": 5,
            "criterion": "Exclude atomic-write mechanism.",
        },
        {
            "id": "precision.unrelated",
            "dimension": "scope_precision",
            "points": 5,
            "criterion": "Exclude unrelated modules.",
        },
        {
            "id": "evidence.validation",
            "dimension": "evidence_actionability",
            "points": 5,
            "criterion": "Provide actionable evidence.",
        },
        {
            "id": "evidence.repro",
            "dimension": "evidence_actionability",
            "points": 5,
            "criterion": "Provide repro advice.",
        },
    ]


def _item_ids() -> list[str]:
    return [it["id"] for it in _rubric_items()]


def _score_doc(
    *,
    case_id: str,
    answer_digest: str,
    gt_digest: str,
    credits: list[float],
    critical_cap: dict[str, Any] | None = None,
    consensus_mode: str = "mean",
    judges: int = 2,
    arbiter_used: bool = False,
    requires_human_review: bool = False,
    human_review_reasons: list[str] | None = None,
    run_mode: str = "formal",
    judge_requested_model: str = JUDGE_MODEL,
    judge_model: str = JUDGE_MODEL,
) -> dict[str, Any]:
    """Build a score-v1 document from per-item consensus credits.

    ``credits`` must align 1:1 with :func:`_rubric_items`. The raw/capped
    totals and dimension totals are computed exactly via Decimal to mirror
    ``scoring.aggregator``.
    """
    items_raw = _rubric_items()
    assert len(credits) == len(items_raw)
    items: list[dict[str, Any]] = []
    dim_totals: dict[str, Decimal] = {d: Decimal(0) for d in DIMENSIONS}
    raw_total = Decimal(0)
    for item, credit in zip(items_raw, credits, strict=True):
        points = Decimal(str(item["points"]))
        cr = Decimal(str(credit))
        score = points * cr
        items.append(
            {
                "item_id": item["id"],
                "dimension": item["dimension"],
                "points": _num(points),
                "consensus_credit": _num(cr),
                "item_score": _num(score),
            }
        )
        dim_totals[item["dimension"]] += score
        raw_total += score

    capped_total = raw_total
    if critical_cap is not None and critical_cap.get("applied"):
        cap_val = Decimal(str(critical_cap["cap_value"]))
        capped_total = min(raw_total, cap_val)

    doc: dict[str, Any] = {
        "schema_version": "score-v1",
        "benchmark_version": "ai-score-v1",
        "judge_protocol": "semantic_outcome_v1",
        "scoring_profile": SCORING_PROFILE,
        "judge_provider": "claude-code-cli",
        "judge_requested_model": judge_requested_model,
        "judge_model": judge_model,
        "judge_cli_version": JUDGE_CLI_VERSION,
        "judge_prompt_digest": DIGEST_PROMPT,
        "ground_truth_digest": gt_digest,
        "agent_answer_digest": answer_digest,
        "case_id": case_id,
        "task_type": TASK_TYPE,
        "items": items,
        "dimension_totals": {d: _num(dim_totals[d]) for d in DIMENSIONS},
        "raw_total": _num(raw_total),
        "critical_cap": critical_cap,
        "capped_total": _num(capped_total),
        "consensus": {
            "mode": consensus_mode,
            "judges": judges,
            "arbiter_used": arbiter_used,
            "human_review_triggered": requires_human_review,
        },
        "requires_human_review": requires_human_review,
        "run_mode": run_mode,
    }
    if requires_human_review:
        doc["human_review_reasons"] = human_review_reasons or ["overall_confidence"]
    return doc


def _num(value: Decimal) -> int | float:
    integral = int(value)
    return integral if value == integral else float(value)


def _judge_output_doc(
    *,
    credits: list[float],
    scoring_profile: str = SCORING_PROFILE,
    overall_confidence: float = 0.85,
    requires_human_review: bool = False,
) -> dict[str, Any]:
    """Build a judge-output-v1 document with per-item credits."""
    items = []
    for item, credit in zip(_rubric_items(), credits, strict=True):
        items.append(
            {
                "item_id": item["id"],
                "credit": credit,
                "verdict": "ok" if credit >= 0.5 else "weak",
                "reason": "synthetic",
                "confidence": 0.85,
            }
        )
    return {
        "schema_version": "judge-output-v1",
        "judge_protocol": "semantic_outcome_v1",
        "scoring_profile": scoring_profile,
        "items": items,
        "unsupported_claims": [],
        "critical_errors": [],
        "overall_confidence": overall_confidence,
        "requires_human_review": requires_human_review,
    }


def _run_metadata_doc(
    *,
    tool_policy: str,
    agent: str = AGENT,
    agent_model: str = AGENT_MODEL,
    metrics: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a run-metadata-v1 document."""
    if metrics is None:
        metrics = {
            "tool_call_count": 10,
            "files_read_count": 5,
            "graph_query_count": 7 if tool_policy == "graph" else 0,
            "search_query_count": 3 if tool_policy == "grep" else 2,
            "elapsed_ms": 180000,
            "input_tokens": 11000,
            "output_tokens": 2000,
        }
    return {
        "schema_version": "run-metadata-v1",
        "agent": agent,
        "agent_model": agent_model,
        "tool_policy": tool_policy,
        "policy_enforced": True,
        "started_at": "2025-01-15T10:30:00Z",
        "ended_at": "2025-01-15T10:33:00Z",
        "metrics": metrics,
    }


def _policy_result_doc(
    *, valid: bool = True, violations: list[dict] | None = None
) -> dict[str, Any]:
    """Build a policy-result-v1 document."""
    return {
        "schema_version": "policy-result-v1",
        "valid": valid,
        "violations": violations or [],
        "observations": ["synthetic observation"],
    }


def _agent_answer_doc(*, case_id: str, status: str = "completed") -> dict[str, Any]:
    """Build an agent-answer-v1 document."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": case_id,
        "task_type": TASK_TYPE,
        "status": status,
        "answer": {
            "summary": "Synthetic answer summary." if status == "completed" else "",
            "explanation": "Synthetic explanation." if status == "completed" else "",
        },
    }


def _judge_score_doc(
    *,
    judge_call_count: int = 2,
    total_latency_ms: int = 45000,
    total_retries: int = 0,
    input_tokens: int = 5000,
    output_tokens: int = 800,
    consensus_mode: str = "mean",
    judges: int = 2,
    arbiter_used: bool = False,
) -> dict[str, Any]:
    """Build a judge-score document with the optional judge_cost block."""
    return {
        "schema_version": "judge-score-v1",
        "consensus": {
            "mode": consensus_mode,
            "judges": judges,
            "arbiter_used": arbiter_used,
        },
        "judge_cost": {
            "judge_call_count": judge_call_count,
            "total_latency_ms": total_latency_ms,
            "total_retries": total_retries,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _manifest_doc(
    *,
    run_id: str,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a manifest-v1 document with explicit artifact statuses."""
    if artifacts is None:
        artifacts = [
            {
                "name": "raw_response",
                "status": "present",
                "path": f"{run_id}/raw-response.txt",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "agent_answer",
                "status": "present",
                "path": f"{run_id}/agent-answer.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "run_metadata",
                "status": "present",
                "path": f"{run_id}/run-metadata.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "policy_result",
                "status": "present",
                "path": f"{run_id}/policy-result.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "blind_input",
                "status": "present",
                "path": f"{run_id}/blind-input.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "judge_a",
                "status": "present",
                "path": f"{run_id}/judge-a.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {
                "name": "judge_b",
                "status": "present",
                "path": f"{run_id}/judge-b.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {"name": "judge_c", "status": "absent"},
            {
                "name": "judge_score",
                "status": "present",
                "path": f"{run_id}/judge-score.json",
                "sha256": DIGEST_ARTIFACT,
            },
            {"name": "adjudication", "status": "not_applicable"},
            {
                "name": "effective_score",
                "status": "present",
                "path": f"{run_id}/effective-score.json",
                "sha256": DIGEST_ARTIFACT,
            },
        ]
    return {"schema_version": "manifest-v1", "artifacts": artifacts}


# ---------------------------------------------------------------------------
# Run-directory builders.
# ---------------------------------------------------------------------------

# Default per-item consensus credits for the synthetic scored runs. These
# produce stable, human-readable totals for snapshot comparison.
#   case A graph: all 1.0 -> raw/capped 100
#   case A grep:  mixed  -> raw/capped ~70
#   case B graph: mixed  -> raw/capped ~80
#   case B grep:  lower  -> raw/capped ~50
_CREDITS_GRAPH_A = [1.0] * 10
_CREDITS_GREP_A = [1.0, 0.75, 0.75, 0.5, 0.5, 0.25, 1.0, 1.0, 0.75, 0.5]
_CREDITS_GRAPH_B = [1.0, 1.0, 0.75, 0.75, 0.75, 0.5, 1.0, 1.0, 0.75, 0.75]
_CREDITS_GREP_B = [0.5, 0.5, 0.5, 0.25, 0.25, 0.25, 0.75, 0.75, 0.5, 0.25]

# Judge A/B credits for the scored runs (used for disagreement stats). Judge A
# and B differ by 0.5 on items 2 and 7 (>0.25 threshold, §13.1 step 3).
_JUDGE_A_CREDITS = [1.0, 0.75, 0.75, 0.5, 0.5, 0.25, 1.0, 1.0, 0.75, 0.5]
_JUDGE_B_CREDITS = [1.0, 0.75, 0.25, 0.5, 0.5, 0.25, 1.0, 0.5, 0.75, 0.5]


def build_scored_run(
    run_dir: Path,
    *,
    run_id: str,
    case_id: str,
    tool_policy: str,
    credits: list[float],
    answer_digest: str,
    gt_digest: str = DIGEST_GT_A,
    judge_a_credits: list[float] | None = None,
    judge_b_credits: list[float] | None = None,
    judge_cost: bool = True,
    critical_cap: dict[str, Any] | None = None,
    consensus_mode: str = "mean",
    judges: int = 2,
    arbiter_used: bool = False,
    requires_human_review: bool = False,
    human_review_reasons: list[str] | None = None,
    run_mode: str = "formal",
    judge_requested_model: str = JUDGE_MODEL,
    judge_model: str = JUDGE_MODEL,
) -> None:
    """Build a complete scored run directory on disk."""
    run_dir.mkdir(parents=True, exist_ok=True)
    score_doc = _score_doc(
        case_id=case_id,
        answer_digest=answer_digest,
        gt_digest=gt_digest,
        credits=credits,
        critical_cap=critical_cap,
        consensus_mode=consensus_mode,
        judges=judges,
        arbiter_used=arbiter_used,
        requires_human_review=requires_human_review,
        human_review_reasons=human_review_reasons,
        run_mode=run_mode,
        judge_requested_model=judge_requested_model,
        judge_model=judge_model,
    )
    _write_json(run_dir / "effective-score.json", score_doc)

    ja_credits = judge_a_credits if judge_a_credits is not None else _JUDGE_A_CREDITS
    jb_credits = judge_b_credits if judge_b_credits is not None else _JUDGE_B_CREDITS
    _write_json(run_dir / "judge-a.json", _judge_output_doc(credits=ja_credits))
    _write_json(run_dir / "judge-b.json", _judge_output_doc(credits=jb_credits))

    if judge_cost:
        _write_json(
            run_dir / "judge-score.json",
            _judge_score_doc(
                consensus_mode=consensus_mode,
                judges=judges,
                arbiter_used=arbiter_used,
            ),
        )

    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(run_dir / "policy-result.json", _policy_result_doc())
    _write_json(run_dir / "agent-answer.json", _agent_answer_doc(case_id=case_id))
    _write_json(run_dir / "manifest.json", _manifest_doc(run_id=run_id))
    (run_dir / "raw-response.txt").write_text("synthetic raw response", encoding="utf-8")


def build_version_mismatch_run(
    run_dir: Path, *, run_id: str, case_id: str, tool_policy: str = "graph"
) -> None:
    """Build a run whose effective score has requested != effective model."""
    build_scored_run(
        run_dir,
        run_id=run_id,
        case_id=case_id,
        tool_policy=tool_policy,
        credits=_CREDITS_GRAPH_A,
        answer_digest=DIGEST_ANSWER_GRAPH_A,
        judge_requested_model="glm-5.2",
        judge_model="claude-sonnet-4",
    )


def build_judge_failed_run(
    run_dir: Path, *, run_id: str, case_id: str, tool_policy: str = "graph"
) -> None:
    """Build a run where the judge phase failed (manifest marks judge_score failed)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(run_dir / "policy-result.json", _policy_result_doc())
    _write_json(run_dir / "agent-answer.json", _agent_answer_doc(case_id=case_id))
    _write_json(
        run_dir / "manifest.json",
        _manifest_doc(
            run_id=run_id,
            artifacts=[
                {
                    "name": "raw_response",
                    "status": "present",
                    "path": f"{run_id}/raw-response.txt",
                    "sha256": DIGEST_ARTIFACT,
                },
                {
                    "name": "agent_answer",
                    "status": "present",
                    "path": f"{run_id}/agent-answer.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {
                    "name": "run_metadata",
                    "status": "present",
                    "path": f"{run_id}/run-metadata.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {
                    "name": "policy_result",
                    "status": "present",
                    "path": f"{run_id}/policy-result.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {
                    "name": "blind_input",
                    "status": "present",
                    "path": f"{run_id}/blind-input.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {"name": "judge_a", "status": "failed"},
                {"name": "judge_b", "status": "absent"},
                {"name": "judge_c", "status": "absent"},
                {"name": "judge_score", "status": "failed"},
                {"name": "adjudication", "status": "not_applicable"},
                {"name": "effective_score", "status": "failed"},
            ],
        ),
    )
    (run_dir / "raw-response.txt").write_text("synthetic raw response", encoding="utf-8")


def _awaiting_manifest(run_id: str) -> list[dict[str, Any]]:
    """Artifact list for a run not yet judged (all judge artifacts absent)."""
    return [
        {
            "name": "raw_response",
            "status": "present",
            "path": f"{run_id}/raw-response.txt",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "agent_answer",
            "status": "present",
            "path": f"{run_id}/agent-answer.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "run_metadata",
            "status": "present",
            "path": f"{run_id}/run-metadata.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "policy_result",
            "status": "present",
            "path": f"{run_id}/policy-result.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {"name": "blind_input", "status": "absent"},
        {"name": "judge_a", "status": "absent"},
        {"name": "judge_b", "status": "absent"},
        {"name": "judge_c", "status": "absent"},
        {"name": "judge_score", "status": "absent"},
        {"name": "adjudication", "status": "not_applicable"},
        {"name": "effective_score", "status": "absent"},
    ]


def build_awaiting_judge_run(
    run_dir: Path, *, run_id: str, case_id: str, tool_policy: str = "graph"
) -> None:
    """Build a valid run with a substantive answer but no effective score."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(run_dir / "policy-result.json", _policy_result_doc())
    _write_json(run_dir / "agent-answer.json", _agent_answer_doc(case_id=case_id))
    _write_json(
        run_dir / "manifest.json",
        _manifest_doc(
            run_id=run_id,
            artifacts=_awaiting_manifest(run_id),
        ),
    )
    (run_dir / "raw-response.txt").write_text("synthetic raw response", encoding="utf-8")


def build_valid_zero_run(
    run_dir: Path, *, run_id: str, case_id: str, tool_policy: str = "graph"
) -> None:
    """Build a valid run with an empty answer (deterministic 0, no score)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(run_dir / "policy-result.json", _policy_result_doc())
    _write_json(run_dir / "agent-answer.json", _agent_answer_doc(case_id=case_id, status="empty"))
    _write_json(
        run_dir / "manifest.json",
        _manifest_doc(
            run_id=run_id,
            artifacts=_awaiting_manifest(run_id),
        ),
    )
    (run_dir / "raw-response.txt").write_text("synthetic raw response", encoding="utf-8")


def build_invalid_run(
    run_dir: Path, *, run_id: str, case_id: str, tool_policy: str = "graph"
) -> None:
    """Build a run with a policy violation (invalid)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(
        run_dir / "policy-result.json",
        _policy_result_doc(
            valid=False,
            violations=[{"code": "graph_policy_no_graph_query", "message": "no graph query"}],
        ),
    )
    _write_json(run_dir / "agent-answer.json", _agent_answer_doc(case_id=case_id))
    _write_json(
        run_dir / "manifest.json",
        _manifest_doc(
            run_id=run_id,
            artifacts=_awaiting_manifest(run_id),
        ),
    )
    (run_dir / "raw-response.txt").write_text("synthetic raw response", encoding="utf-8")


def build_failed_run(run_dir: Path, *, run_id: str, tool_policy: str = "graph") -> None:
    """Build a run where agent execution failed (no agent-answer)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "run-metadata.json", _run_metadata_doc(tool_policy=tool_policy))
    _write_json(
        run_dir / "policy-result.json",
        _policy_result_doc(
            valid=False,
            violations=[{"code": "execution_failed", "message": "agent crashed"}],
        ),
    )
    _write_json(
        run_dir / "manifest.json",
        _manifest_doc(
            run_id=run_id,
            artifacts=[
                {"name": "raw_response", "status": "absent"},
                {"name": "agent_answer", "status": "absent"},
                {
                    "name": "run_metadata",
                    "status": "present",
                    "path": f"{run_id}/run-metadata.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {
                    "name": "policy_result",
                    "status": "present",
                    "path": f"{run_id}/policy-result.json",
                    "sha256": DIGEST_ARTIFACT,
                },
                {"name": "blind_input", "status": "absent"},
                {"name": "judge_a", "status": "absent"},
                {"name": "judge_b", "status": "absent"},
                {"name": "judge_c", "status": "absent"},
                {"name": "judge_score", "status": "absent"},
                {"name": "adjudication", "status": "not_applicable"},
                {"name": "effective_score", "status": "absent"},
            ],
        ),
    )


def build_missing_artifact_run(run_dir: Path, *, run_id: str) -> None:
    """Build a run directory missing required Runner artifacts."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent-answer.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# Complete synthetic experiment builder.
# ---------------------------------------------------------------------------


def build_synthetic_experiment(runs_root: Path) -> dict[str, str]:
    """Build a complete synthetic experiment under ``runs_root``.

    Creates 4 scored runs (2 cases x graph/grep), plus one of each isolated
    run type. Returns a dict mapping run_id -> isolation_reason (or "scored").
    The layout is deterministic so snapshot comparisons are stable.
    """
    runs_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, str] = {}

    # Scored: case A graph/grep.
    build_scored_run(
        runs_root / "caseA-graph-r1",
        run_id="caseA-graph-r1",
        case_id=CASE_A,
        tool_policy="graph",
        credits=_CREDITS_GRAPH_A,
        answer_digest=DIGEST_ANSWER_GRAPH_A,
    )
    summary["caseA-graph-r1"] = "scored"
    build_scored_run(
        runs_root / "caseA-grep-r1",
        run_id="caseA-grep-r1",
        case_id=CASE_A,
        tool_policy="grep",
        credits=_CREDITS_GREP_A,
        answer_digest=DIGEST_ANSWER_GREP_A,
    )
    summary["caseA-grep-r1"] = "scored"

    # Scored: case B graph/grep.
    build_scored_run(
        runs_root / "caseB-graph-r1",
        run_id="caseB-graph-r1",
        case_id=CASE_B,
        tool_policy="graph",
        credits=_CREDITS_GRAPH_B,
        answer_digest=DIGEST_ANSWER_GRAPH_B,
        gt_digest=DIGEST_GT_B,
    )
    summary["caseB-graph-r1"] = "scored"
    build_scored_run(
        runs_root / "caseB-grep-r1",
        run_id="caseB-grep-r1",
        case_id=CASE_B,
        tool_policy="grep",
        credits=_CREDITS_GREP_B,
        answer_digest=DIGEST_ANSWER_GREP_B,
        gt_digest=DIGEST_GT_B,
    )
    summary["caseB-grep-r1"] = "scored"

    # Isolated runs.
    build_version_mismatch_run(
        runs_root / "iso-version-mismatch",
        run_id="iso-version-mismatch",
        case_id=CASE_A,
    )
    summary["iso-version-mismatch"] = "version_mismatch"
    build_judge_failed_run(
        runs_root / "iso-judge-failed",
        run_id="iso-judge-failed",
        case_id=CASE_A,
    )
    summary["iso-judge-failed"] = "judge_failed"
    build_awaiting_judge_run(
        runs_root / "iso-awaiting-judge",
        run_id="iso-awaiting-judge",
        case_id=CASE_B,
    )
    summary["iso-awaiting-judge"] = "awaiting_judge"
    build_valid_zero_run(
        runs_root / "iso-valid-zero",
        run_id="iso-valid-zero",
        case_id=CASE_A,
    )
    summary["iso-valid-zero"] = "valid_zero"
    build_invalid_run(
        runs_root / "iso-invalid",
        run_id="iso-invalid",
        case_id=CASE_B,
    )
    summary["iso-invalid"] = "invalid"
    build_failed_run(
        runs_root / "iso-failed",
        run_id="iso-failed",
    )
    summary["iso-failed"] = "failed"
    build_missing_artifact_run(
        runs_root / "iso-missing-artifact",
        run_id="iso-missing-artifact",
    )
    summary["iso-missing-artifact"] = "missing_artifact"

    return summary
