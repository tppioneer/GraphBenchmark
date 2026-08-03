"""AIS-012 smoke input tests: parse and validate the converted case, ground
truth and smoke experiment config.

* the case parses and validates against case.schema.json (case-v1);
* the ground truth parses and validates against ground-truth.schema.json
  (ground-truth-v1) AND passes the production rubric validator
  (scoring.rubric_validator.validate_profile_and_rubric), which enforces the
  Draft 2020-12 schema plus the nine section-7.2 business rules;
* the smoke experiment config has the expected smoke-only structure and
  contains no credentials, fabricated SHA/revision, generated answer, score or
  artifact result (there is no approved experiment schema, so its structure is
  asserted here directly rather than against an invented production schema).

No Judge call is made and no AgentAdapter is exercised by these tests.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from scoring.rubric_validator import LEAK_FIELD_NAMES, validate_profile_and_rubric
from tests.schemas._validators import load_schema

REPO_ROOT = Path(__file__).resolve().parent.parent

CASE_PATH = REPO_ROOT / "cases" / "qwenpaw" / "qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml"
GT_PATH = (
    REPO_ROOT / "ground-truth" / "qwenpaw"
    / "qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml"
)
SMOKE_PATH = REPO_ROOT / "experiments" / "qwenpaw-corrupt-inbox-smoke-v1.yaml"

CASE_ID = "qwenpaw-case-z-corrupt-inbox-recovery-bug"


@pytest.fixture(scope="module")
def case_doc() -> dict[str, Any]:
    return yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gt_doc() -> dict[str, Any]:
    return yaml.safe_load(GT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def smoke_doc() -> dict[str, Any]:
    return yaml.safe_load(SMOKE_PATH.read_text(encoding="utf-8"))


# ----------------------------- case (case-v1) ------------------------------ #


def test_case_file_exists() -> None:
    assert CASE_PATH.is_file(), f"missing case file: {CASE_PATH}"


def test_case_validates_against_case_schema(case_doc: dict[str, Any]) -> None:
    validator = Draft202012Validator(load_schema("case"))
    errors = list(validator.iter_errors(case_doc))
    assert not errors, [f"{e.message} @ {list(e.absolute_path)}" for e in errors]


def test_case_carries_only_source_question(case_doc: dict[str, Any]) -> None:
    """The case carries only the source-derived question; no repo is invented
    and no GT/rubric/agent identity field is leaked."""
    assert case_doc["schema_version"] == "case-v1"
    assert case_doc["case_id"] == CASE_ID
    assert case_doc["task_type"] == "bug_localization"
    assert isinstance(case_doc["question"], str) and case_doc["question"].strip()
    assert "repo" not in case_doc
    for forbidden in (
        "rubric_items", "scoring_profile", "agent", "agent_model",
        "tool_policy", "answer", "score", "root_cause",
    ):
        assert forbidden not in case_doc, f"case leaks field {forbidden!r}"


# ------------------------ ground truth (ground-truth-v1) ------------------- #


def test_gt_file_exists() -> None:
    assert GT_PATH.is_file(), f"missing ground-truth file: {GT_PATH}"


def test_gt_validates_against_gt_schema(gt_doc: dict[str, Any]) -> None:
    validator = Draft202012Validator(load_schema("ground-truth"))
    errors = list(validator.iter_errors(gt_doc))
    assert not errors, [f"{e.message} @ {list(e.absolute_path)}" for e in errors]


def test_gt_passes_production_rubric_validator(gt_doc: dict[str, Any]) -> None:
    """The GT passes the production entry point: shipped Draft 2020-12 schema
    plus every section-7.2 business rule (point sums, critical zero-credit)."""
    issues = validate_profile_and_rubric(gt_doc)
    assert issues == [], [str(i) for i in issues]


def test_gt_dimension_and_total_points(gt_doc: dict[str, Any]) -> None:
    """Profile-compliant per-dimension totals (35/25/20/10/10) and 100 total."""
    sums: dict[str, Decimal] = defaultdict(Decimal)
    total = Decimal(0)
    for item in gt_doc["rubric_items"]:
        pts = Decimal(str(item["points"]))
        sums[item["dimension"]] += pts
        total += pts
    assert sums["core_correctness"] == 35
    assert sums["reasoning_correctness"] == 25
    assert sums["completeness"] == 20
    assert sums["scope_precision"] == 10
    assert sums["evidence_actionability"] == 10
    assert total == 100


def test_gt_critical_items_define_zero_credit(gt_doc: dict[str, Any]) -> None:
    critical = [it for it in gt_doc["rubric_items"] if it.get("critical") is True]
    assert critical, "expected at least one critical item"
    for item in critical:
        iid = item["id"]
        assert item.get("zero_credit", "").strip(), (
            f"critical item {iid!r} must define a zero_credit condition"
        )


def test_gt_references_carry_no_invented_lines(gt_doc: dict[str, Any]) -> None:
    """The source provides no line numbers, so references must not invent any."""
    for item in gt_doc["rubric_items"]:
        iid = item["id"]
        for ref in item.get("references", []):
            assert "lines" not in ref, (
                f"item {iid!r} invents a line range not in the source"
            )


def test_gt_has_no_leak_fields(gt_doc: dict[str, Any]) -> None:
    def _keys(obj: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                found.add(k)
                found |= _keys(v)
        elif isinstance(obj, list):
            for v in obj:
                found |= _keys(v)
        return found

    assert not (_keys(gt_doc) & LEAK_FIELD_NAMES)


# ---------------------- smoke experiment config ---------------------------- #


def test_smoke_config_file_exists() -> None:
    assert SMOKE_PATH.is_file(), f"missing smoke config: {SMOKE_PATH}"


def test_smoke_config_structure(smoke_doc: dict[str, Any]) -> None:
    assert smoke_doc["experiment_id"] == "qwenpaw-corrupt-inbox-smoke-v1"
    assert smoke_doc["purpose"] == "smoke"
    assert smoke_doc["status"] == "smoke_only"
    assert smoke_doc["case_id"] == CASE_ID
    assert smoke_doc["task_type"] == "bug_localization"
    assert smoke_doc["scoring_profile"] == "bug_localization_v1"
    expected_case = "cases/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml"
    expected_gt = (
        "ground-truth/qwenpaw/qwenpaw-case-z-corrupt-inbox-recovery-bug.yaml"
    )
    assert smoke_doc["case"] == expected_case
    assert smoke_doc["ground_truth"] == expected_gt
    assert smoke_doc["judge_model"] == "glm-5.2"


def test_smoke_config_paired_conditions(smoke_doc: dict[str, Any]) -> None:
    conditions = smoke_doc["conditions"]
    assert isinstance(conditions, list) and len(conditions) == 2
    by_id = {c["id"]: c["tool_policy"] for c in conditions}
    assert by_id == {"graph": "graph", "grep": "grep"}
    assert smoke_doc["pairing"] == "graph_vs_grep"
    assert smoke_doc["repeats"] == 1


def test_smoke_config_declares_no_adapter_or_judge_result(
    smoke_doc: dict[str, Any],
) -> None:
    """Smoke-only: no concrete AgentAdapter and no formal Judge result supplied."""
    assert smoke_doc["agent_adapter"] is None
    assert smoke_doc["formal_judge_result"] is None


def test_smoke_config_has_no_credentials_or_fabricated_results(
    smoke_doc: dict[str, Any],
) -> None:
    """No credentials, secret values, fabricated SHA/revision, generated answer,
    score, or artifact result may appear anywhere in the config."""

    forbidden_keys = frozenset(
        {
            "api_key", "apikey", "token", "secret", "password", "passwd",
            "credential", "credentials", "access_key", "private_key", "auth_token",
            "sha", "sha256", "revision", "commit", "digest",
            "answer", "generated_answer", "score", "raw_total", "capped_total",
            "artifact", "artifacts", "artifact_result", "judge_score",
            "effective_score",
        }
    )

    def _collect_keys(obj: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                found.add(str(k).lower())
                found |= _collect_keys(v)
        elif isinstance(obj, list):
            for v in obj:
                found |= _collect_keys(v)
        return found

    present = _collect_keys(smoke_doc)
    assert "repo" not in present, "smoke config must not pin an unverified repo"
    leaked = present & forbidden_keys
    assert not leaked, f"smoke config carries forbidden field(s): {sorted(leaked)}"


# ------------------------- cross-document wiring --------------------------- #


def test_smoke_inputs_are_consistently_wired(
    case_doc: dict[str, Any],
    gt_doc: dict[str, Any],
    smoke_doc: dict[str, Any],
) -> None:
    """case_id / task_type / scoring_profile agree across the three inputs, and
    the smoke config references paths that exist on disk."""
    assert case_doc["case_id"] == gt_doc["case_id"] == smoke_doc["case_id"] == CASE_ID
    assert case_doc["task_type"] == gt_doc["task_type"] == smoke_doc["task_type"]
    assert gt_doc["scoring_profile"] == smoke_doc["scoring_profile"]
    assert (REPO_ROOT / smoke_doc["case"]).is_file()
    assert (REPO_ROOT / smoke_doc["ground_truth"]).is_file()
