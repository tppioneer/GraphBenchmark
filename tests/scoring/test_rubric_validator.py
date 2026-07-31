"""Tests for ``scoring.rubric_validator`` (design §7.2 nine rules).

§7.2 test mapping (required by the AIS-004 delivery contract):

==  ================================  ================================================
§   Rule                              Test
==  ================================  ================================================
1   identity exists & valid           test_rule1_*  (case_id / task_type / scoring_profile)
2   stable, unique item id            test_rule2_*  (missing id / duplicate id)
3   known dimension                   test_rule3_dimension_unknown_rejected
4   positive points                   test_rule4_points_not_positive_rejected
5   dimension sum == Profile weight   test_rule5_dimension_points_mismatch_rejected
6   total points == 100               test_rule6_total_points_mismatch_rejected
7   critical zero-credit condition    test_rule7_critical_zero_credit_missing_rejected
8   references satisfy schema         test_rule8_reference_*_rejected
9   no leak field                     test_rule9_leak_*_rejected
-   GT JSON Schema (Draft 2020-12)    test_schema_*  (AIS-004 R1 structural checks)
-   report all problems at once       test_all_issues_reported_in_one_pass
-   schema + business reported together test_schema_and_business_issues_reported_together
-   deterministic / order-independent test_validation_deterministic*
==  ================================  ================================================
"""

from __future__ import annotations

import copy
import random
from collections import Counter
from typing import Any

import pytest

from scoring import profiles as prof
from scoring.rubric_validator import (
    CRITICAL_ZERO_CREDIT_MISSING,
    DIMENSION_POINTS_MISMATCH,
    DIMENSION_UNKNOWN,
    GT_CASE_ID_MISSING,
    GT_LEAK_DETECTED,
    GT_SCHEMA_INVALID,
    GT_SCORING_PROFILE_INVALID,
    GT_TASK_TYPE_INVALID,
    ITEM_ID_DUPLICATE,
    ITEM_ID_MISSING,
    POINTS_NOT_POSITIVE,
    REFERENCE_INVALID,
    RUBRIC_ITEMS_MISSING,
    TOTAL_POINTS_MISMATCH,
    RubricIssue,
    RubricValidationError,
    issue_counter,
    validate_ground_truth_schema,
    validate_profile_and_rubric,
    validate_rubric,
    validate_rubric_or_raise,
)
from tests.schemas.examples import FULL_GT

# A validated bug_localization profile pair, loaded once for the module.
BUG_TASK, BUG_COMMON = prof.load_validated_task_profile("bug_localization")


def _gt() -> dict[str, Any]:
    """A fresh deep copy of the valid bug_localization GT (FULL_GT)."""
    return copy.deepcopy(FULL_GT)


def _valid_gt_for(task_type: str) -> dict[str, Any]:
    """Build a minimal valid GT for any task type (one item per dimension)."""
    scoring_profile = prof.TASK_PROFILE_MAP[task_type]["scoring_profile"]
    items = [
        {
            "id": f"{dim}.main",
            "dimension": dim,
            "points": weight,
            "criterion": f"{dim} criterion.",
        }
        for dim, weight in prof.FROZEN_DIMENSIONS
    ]
    return {
        "schema_version": "ground-truth-v1",
        "case_id": f"case-{task_type}-minimal",
        "task_type": task_type,
        "scoring_profile": scoring_profile,
        "rubric_items": items,
    }


def _codes(issues: list[RubricIssue]) -> list[str]:
    return [i.code for i in issues]


# ----------------------------- positive ------------------------------------ #


def test_full_gt_validates_cleanly() -> None:
    issues = validate_rubric(_gt(), BUG_TASK, BUG_COMMON)
    assert issues == []


def test_validate_rubric_or_raise_accepts_valid() -> None:
    validate_rubric_or_raise(_gt(), BUG_TASK, BUG_COMMON)  # no raise


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_minimal_valid_gt_per_task_type(task_type: str) -> None:
    task, common = prof.load_validated_task_profile(task_type)
    issues = validate_rubric(_valid_gt_for(task_type), task, common)
    assert issues == [], [str(i) for i in issues]


def test_validate_profile_and_rubric_valid() -> None:
    issues = validate_profile_and_rubric(_gt())
    assert issues == []


# ----------------------------- Rule 1: identity ---------------------------- #


def test_rule1_case_id_missing_rejected() -> None:
    bad = _gt()
    del bad["case_id"]
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_CASE_ID_MISSING in _codes(issues)
    ptrs = [i.pointer for i in issues if i.code == GT_CASE_ID_MISSING]
    assert "/case_id" in ptrs


def test_rule1_task_type_unknown_rejected() -> None:
    bad = _gt()
    bad["task_type"] = "not_a_task"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_TASK_TYPE_INVALID in _codes(issues)
    assert "/task_type" in [i.pointer for i in issues if i.code == GT_TASK_TYPE_INVALID]


def test_rule1_task_type_mismatch_with_profile_rejected() -> None:
    bad = _gt()
    bad["task_type"] = "flow_tracing"
    bad["scoring_profile"] = "flow_tracing_v1"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_TASK_TYPE_INVALID in _codes(issues)


def test_rule1_scoring_profile_unknown_rejected() -> None:
    bad = _gt()
    bad["scoring_profile"] = "flow_tracing_v2"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_SCORING_PROFILE_INVALID in _codes(issues)
    assert "/scoring_profile" in [i.pointer for i in issues if i.code == GT_SCORING_PROFILE_INVALID]


def test_rule1_scoring_profile_mismatch_with_profile_rejected() -> None:
    bad = _gt()
    bad["scoring_profile"] = "flow_tracing_v1"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_SCORING_PROFILE_INVALID in _codes(issues)


# ----------------------------- Rule 2: item id ----------------------------- #


def test_rule2_item_id_missing_rejected() -> None:
    bad = _gt()
    del bad["rubric_items"][0]["id"]
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert ITEM_ID_MISSING in _codes(issues)
    assert any(i.pointer == "/rubric_items/0/id" for i in issues)


def test_rule2_item_id_empty_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["id"] = ""
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert ITEM_ID_MISSING in _codes(issues)


def test_rule2_duplicate_id_rejected() -> None:
    bad = _gt()
    dup_id = bad["rubric_items"][0]["id"]
    bad["rubric_items"][1]["id"] = dup_id
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert ITEM_ID_DUPLICATE in _codes(issues)
    dup = [i for i in issues if i.code == ITEM_ID_DUPLICATE]
    assert dup[0].item_id == dup_id
    assert dup[0].pointer == "/rubric_items/1/id"


# ----------------------------- Rule 3: dimension --------------------------- #


def test_rule3_dimension_unknown_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["dimension"] = "not_a_dimension"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert DIMENSION_UNKNOWN in _codes(issues)
    dim_issue = [i for i in issues if i.code == DIMENSION_UNKNOWN][0]
    assert dim_issue.pointer == "/rubric_items/0/dimension"
    assert dim_issue.item_id == "outcome.root-cause"


# ----------------------------- Rule 4: points ------------------------------ #


def test_rule4_points_not_positive_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = 0
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert POINTS_NOT_POSITIVE in _codes(issues)
    assert any(i.pointer == "/rubric_items/0/points" for i in issues)


def test_rule4_points_negative_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = -5
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert POINTS_NOT_POSITIVE in _codes(issues)


def test_rule4_points_non_numeric_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = "twenty"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert POINTS_NOT_POSITIVE in _codes(issues)


# ----------------------------- Rule 5: dimension sum ----------------------- #


def test_rule5_dimension_points_mismatch_rejected() -> None:
    bad = _gt()
    # core_correctness currently sums to 35 (20+15); lower one item to break it.
    bad["rubric_items"][0]["points"] = 10  # core becomes 10+15 = 25 != 35
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert DIMENSION_POINTS_MISMATCH in _codes(issues)
    msg = [i.message for i in issues if i.code == DIMENSION_POINTS_MISMATCH]
    assert any("core_correctness" in m for m in msg)


def test_rule5_dimension_sum_uses_exact_decimal() -> None:
    # Float points that drift under binary float addition must still match.
    # 34.9 + 0.1 == 34.999999999999996 in float but exactly 35.0 in Decimal.
    bad = _gt()
    bad["rubric_items"][0]["points"] = 34.9  # outcome.root-cause (core)
    bad["rubric_items"][1]["points"] = 0.1  # outcome.trigger (core) -> core = 35
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert DIMENSION_POINTS_MISMATCH not in _codes(issues)
    assert TOTAL_POINTS_MISMATCH not in _codes(issues)


# ----------------------------- Rule 6: total ------------------------------- #


def test_rule6_total_points_mismatch_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = 19  # total becomes 99
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert TOTAL_POINTS_MISMATCH in _codes(issues)
    assert any(i.pointer == "/rubric_items" for i in issues if i.code == TOTAL_POINTS_MISMATCH)


# ----------------------------- Rule 7: critical zero ----------------------- #


def test_rule7_critical_zero_credit_missing_rejected() -> None:
    bad = _gt()
    del bad["rubric_items"][0]["zero_credit"]  # outcome.root-cause is critical
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert CRITICAL_ZERO_CREDIT_MISSING in _codes(issues)
    z = [i for i in issues if i.code == CRITICAL_ZERO_CREDIT_MISSING][0]
    assert z.pointer == "/rubric_items/0/zero_credit"
    assert z.item_id == "outcome.root-cause"


def test_rule7_critical_empty_zero_credit_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["zero_credit"] = "   "
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert CRITICAL_ZERO_CREDIT_MISSING in _codes(issues)


def test_rule7_noncritical_without_zero_credit_ok() -> None:
    # A non-critical item is not required to have zero_credit.
    gt = _valid_gt_for("bug_localization")
    issues = validate_rubric(gt, BUG_TASK, BUG_COMMON)
    assert CRITICAL_ZERO_CREDIT_MISSING not in _codes(issues)


# ----------------------------- Rule 8: references -------------------------- #


def test_rule8_reference_missing_symbol_rejected() -> None:
    bad = _gt()
    del bad["rubric_items"][0]["references"][0]["symbol"]
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert REFERENCE_INVALID in _codes(issues)
    assert any(
        i.pointer == "/rubric_items/0/references/0/symbol"
        for i in issues
        if i.code == REFERENCE_INVALID
    )


def test_rule8_reference_missing_file_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["references"][0]["file"] = ""
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert REFERENCE_INVALID in _codes(issues)


def test_rule8_reference_bad_lines_pair_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["references"][0]["lines"] = [10]  # not a pair
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert REFERENCE_INVALID in _codes(issues)


def test_rule8_reference_lines_inverted_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["references"][0]["lines"] = [60, 40]  # start > end
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert REFERENCE_INVALID in _codes(issues)


def test_rule8_reference_not_a_mapping_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["references"][0] = "not-a-mapping"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert REFERENCE_INVALID in _codes(issues)


# ----------------------------- Rule 9: leak fields ------------------------- #


def test_rule9_leak_top_level_field_rejected() -> None:
    bad = _gt()
    bad["agent_model"] = "glm-5.2"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_LEAK_DETECTED in _codes(issues)
    assert any(i.pointer == "/agent_model" for i in issues if i.code == GT_LEAK_DETECTED)


def test_rule9_leak_tool_policy_rejected() -> None:
    bad = _gt()
    bad["tool_policy"] = "graph"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_LEAK_DETECTED in _codes(issues)


def test_rule9_leak_nested_in_item_rejected() -> None:
    bad = _gt()
    bad["rubric_items"][0]["metrics"] = {"tool_call_count": 14}
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert GT_LEAK_DETECTED in _codes(issues)
    assert any(i.pointer == "/rubric_items/0/metrics" for i in issues if i.code == GT_LEAK_DETECTED)


# ----------------------------- structural ---------------------------------- #


def test_rubric_items_missing_rejected() -> None:
    bad = _gt()
    del bad["rubric_items"]
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert RUBRIC_ITEMS_MISSING in _codes(issues)


def test_rubric_items_empty_rejected() -> None:
    bad = _gt()
    bad["rubric_items"] = []
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert RUBRIC_ITEMS_MISSING in _codes(issues)


def test_ground_truth_not_a_mapping_rejected() -> None:
    issues = validate_rubric([], BUG_TASK, BUG_COMMON)  # type: ignore[arg-type]
    assert len(issues) == 1
    assert issues[0].code == RUBRIC_ITEMS_MISSING


# ----------------------------- schema validation (AIS-004 R1) -------------- #
# validate_profile_and_rubric must enforce ground-truth.schema.json (Draft
# 2020-12) so a structurally invalid GT can no longer pass with zero issues.
# Every schema failure becomes a GT_SCHEMA_INVALID RubricIssue with an RFC 6901
# JSON Pointer. The focused tests below call validate_ground_truth_schema
# directly to isolate the structural layer from the §7.2 business rules.


def test_schema_valid_gt_has_no_issues() -> None:
    assert validate_ground_truth_schema(_gt()) == []


def test_schema_finding_scenario_caught_by_entry_point() -> None:
    """The exact AIS-004 R1 finding: missing schema_version + missing criterion
    + an unexpected field used to return zero issues. The entry point now
    reports every structural failure with a locatable pointer."""
    bad = _gt()
    del bad["schema_version"]
    for item in bad["rubric_items"]:
        item.pop("criterion", None)
    bad["unexpected_field"] = "boom"

    issues = validate_profile_and_rubric(bad)
    codes = set(_codes(issues))
    assert GT_SCHEMA_INVALID in codes
    # Every issue is a schema issue here (business rules pass on this input).
    assert codes == {GT_SCHEMA_INVALID}
    ptrs = {i.pointer for i in issues if i.code == GT_SCHEMA_INVALID}
    assert "/schema_version" in ptrs
    assert "/unexpected_field" in ptrs
    assert [f"/rubric_items/{i}/criterion" for i in range(len(bad["rubric_items"]))] <= [
        p for p in ptrs if p.endswith("/criterion")
    ]
    # Every issue carries a non-empty, actionable pointer.
    assert all(i.pointer for i in issues)


def test_schema_missing_schema_version_located() -> None:
    bad = _gt()
    del bad["schema_version"]
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].code == GT_SCHEMA_INVALID
    assert issues[0].pointer == "/schema_version"


def test_schema_missing_criterion_located() -> None:
    bad = _gt()
    del bad["rubric_items"][2]["criterion"]
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/rubric_items/2/criterion"


def test_schema_unexpected_top_level_field_located() -> None:
    bad = _gt()
    bad["unexpected"] = "x"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/unexpected"


def test_schema_unexpected_item_field_located() -> None:
    bad = _gt()
    bad["rubric_items"][1]["unexpected"] = "x"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/rubric_items/1/unexpected"


def test_schema_multiple_unexpected_fields_each_located() -> None:
    bad = _gt()
    bad["foo"] = 1
    bad["bar"] = 2
    issues = validate_ground_truth_schema(bad)
    ptrs = {i.pointer for i in issues}
    assert "/foo" in ptrs
    assert "/bar" in ptrs
    assert all(i.code == GT_SCHEMA_INVALID for i in issues)


def test_schema_bad_points_type_located() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = "twenty"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/rubric_items/0/points"


def test_schema_non_positive_points_located() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = 0  # exclusiveMinimum: 0
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/rubric_items/0/points"


def test_schema_bad_dimension_enum_located() -> None:
    bad = _gt()
    bad["rubric_items"][0]["dimension"] = "not_a_dimension"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/rubric_items/0/dimension"


def test_schema_bad_task_type_enum_located() -> None:
    bad = _gt()
    bad["task_type"] = "not_a_task"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/task_type"


def test_schema_bad_scoring_profile_enum_located() -> None:
    bad = _gt()
    bad["scoring_profile"] = "flow_tracing_v2"
    issues = validate_ground_truth_schema(bad)
    assert len(issues) == 1
    assert issues[0].pointer == "/scoring_profile"


def test_schema_validation_deterministic() -> None:
    bad = _gt()
    del bad["schema_version"]
    bad["foo"] = 1
    bad["bar"] = 2
    bad["rubric_items"][0]["points"] = "twenty"
    first = validate_ground_truth_schema(bad)
    second = validate_ground_truth_schema(bad)
    assert first == second  # exact list equality incl. order


def test_schema_and_business_issues_reported_together() -> None:
    """All-errors-at-once: schema and business failures are reported together
    through the production entry point (acceptance criterion 4)."""
    bad = _gt()
    del bad["schema_version"]  # schema (required)
    bad["unexpected"] = "x"  # schema (additionalProperties)
    bad["rubric_items"][0]["points"] = 0  # schema (exclusiveMinimum) + business (points)
    issues = validate_profile_and_rubric(bad)
    codes = set(_codes(issues))
    assert GT_SCHEMA_INVALID in codes
    assert POINTS_NOT_POSITIVE in codes
    # Every issue carries a non-empty, actionable pointer.
    assert all(i.pointer for i in issues)


# ----------------------------- report all at once -------------------------- #


def test_all_issues_reported_in_one_pass() -> None:
    """Acceptance criterion 4: every actionable problem is reported together."""
    bad = _gt()
    del bad["case_id"]  # rule 1
    bad["rubric_items"][1]["id"] = bad["rubric_items"][0]["id"]  # rule 2 duplicate
    bad["rubric_items"][2]["dimension"] = "unknown"  # rule 3
    bad["rubric_items"][3]["points"] = 0  # rule 4
    del bad["rubric_items"][0]["zero_credit"]  # rule 7 (outcome.root-cause is critical)
    bad["rubric_items"][0]["references"][0]["file"] = ""  # rule 8
    bad["agent_model"] = "glm-5.2"  # rule 9
    # Rules 5 & 6 also fire because points were zeroed and dimensions changed.

    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    codes = set(_codes(issues))
    expected = {
        GT_CASE_ID_MISSING,
        ITEM_ID_DUPLICATE,
        DIMENSION_UNKNOWN,
        POINTS_NOT_POSITIVE,
        CRITICAL_ZERO_CREDIT_MISSING,
        REFERENCE_INVALID,
        GT_LEAK_DETECTED,
    }
    assert expected <= codes, f"missing: {expected - codes}; got: {codes}"
    # Every issue carries a non-empty pointer (path location).
    assert all(i.pointer for i in issues)


def test_rubric_validation_error_holds_all_issues() -> None:
    bad = _gt()
    del bad["case_id"]
    bad["rubric_items"][0]["points"] = 0
    with pytest.raises(RubricValidationError) as exc_info:
        validate_rubric_or_raise(bad, BUG_TASK, BUG_COMMON)
    codes = exc_info.value.codes
    assert GT_CASE_ID_MISSING in codes
    assert POINTS_NOT_POSITIVE in codes


# ----------------------------- determinism --------------------------------- #


def test_validation_deterministic_same_input() -> None:
    """Acceptance criterion 5: same input yields identical, ordered output."""
    bad = _gt()
    bad["rubric_items"][0]["points"] = 0
    bad["rubric_items"][2]["dimension"] = "unknown"
    bad["agent_model"] = "glm-5.2"
    first = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    second = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    assert first == second  # exact list equality incl. order


def test_issues_returned_in_sorted_order() -> None:
    bad = _gt()
    bad["rubric_items"][0]["points"] = 0
    bad["rubric_items"][2]["dimension"] = "unknown"
    bad["rubric_items"][1]["id"] = bad["rubric_items"][0]["id"]
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    expected = sorted(issues, key=lambda i: (i.pointer, i.code, i.item_id or "", i.message))
    assert issues == expected


def test_validation_independent_of_item_order() -> None:
    """Acceptance criterion 5: result does not depend on item/dict order.

    Shuffling rubric_items must not change the multiset of (code, item_id)
    problems found — only the index in the pointer changes.
    """
    bad = _gt()
    bad["rubric_items"][0]["points"] = 0
    bad["rubric_items"][2]["dimension"] = "unknown"
    bad["rubric_items"][1]["id"] = bad["rubric_items"][0]["id"]

    shuffled = copy.deepcopy(bad)
    rng = random.Random(20260731)
    rng.shuffle(shuffled["rubric_items"])

    base = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    shuf = validate_rubric(shuffled, BUG_TASK, BUG_COMMON)

    def issue_key(issues: list[RubricIssue]) -> Counter[tuple[str, str | None]]:
        return Counter((i.code, i.item_id) for i in issues)

    assert issue_key(base) == issue_key(shuf), f"base={issue_key(base)} shuf={issue_key(shuf)}"


def test_issue_counter_helper() -> None:
    bad = _gt()
    del bad["case_id"]
    bad["tool_policy"] = "graph"
    issues = validate_rubric(bad, BUG_TASK, BUG_COMMON)
    counts = issue_counter(issues)
    assert counts[GT_CASE_ID_MISSING] == 1
    assert counts[GT_LEAK_DETECTED] == 1


# ----------------------------- cross-profile ------------------------------- #


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_profile_mismatch_detected(task_type: str) -> None:
    """A GT whose task_type differs from the loaded profile is rejected."""
    gt = _valid_gt_for(task_type)
    other_type = next(t for t in prof.TASK_TYPES if t != task_type)
    other_task, other_common = prof.load_validated_task_profile(other_type)
    issues = validate_rubric(gt, other_task, other_common)
    assert GT_TASK_TYPE_INVALID in _codes(issues)
    assert GT_SCORING_PROFILE_INVALID in _codes(issues)
