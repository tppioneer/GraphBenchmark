"""Tests for ``scoring.aggregator`` (design §11, §12, §20).

AIS-005 acceptance-criteria test mapping:

==  =========================================  ================================================
§   Criterion                                  Test
==  =========================================  ================================================
1   each GT item matches exactly one verdict   test_unknown_item_rejected /
                                               test_missing_item_rejected /
                                               test_duplicate_verdict_rejected /
                                               test_duplicate_gt_item_rejected
2   Decimal strategy, no drift                 test_item_score_is_points_times_credit /
                                               test_dimension_totals_sum_to_raw_total /
                                               test_exact_decimal_for_consensus_means
3   cap only lowers; multiple caps strictest   test_core_correctness_all_zero_cap_50 /
                                               test_reverse_critical_relation_cap_60 /
                                               test_multiple_caps_take_lowest /
                                               test_cap_only_lowers_never_raises
4   output has points/dims/raw/capped/cap/meta test_score_to_dict_valid_* /
                                               test_build_score_attaches_metadata
5   boundaries: all-0/all-1/steps/reverse/etc  test_all_credits_zero_scores_zero /
                                               test_all_credits_one_scores_full /
                                               test_fractional_steps /
                                               test_no_cap_when_core_critical_not_zero
==  =========================================  ================================================
"""

from __future__ import annotations

import copy
import dataclasses
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scoring import profiles as prof
from scoring.aggregator import (
    BENCHMARK_VERSION,
    CONSENSUS_MODES,
    CORE_CORRECTNESS_ALL_ZERO,
    HUMAN_REVIEW_REASONS,
    JUDGE_PROTOCOL,
    JUDGE_PROVIDER,
    REVERSE_CRITICAL_RELATION_ZERO,
    RUN_MODES,
    AggregationError,
    ConsensusInfo,
    ScoreResult,
    VersionMetadata,
    aggregate,
    build_score,
    score_to_dict,
)
from tests.schemas import examples as ex
from tests.schemas._validators import load_schema, validate_requested_effective_model

# A validated bug_localization profile pair, loaded once for the module.
BUG_TASK, BUG_COMMON = prof.load_validated_task_profile("bug_localization")

# Score schema validator (no format annotations need a format checker).
_SCORE_VALIDATOR = Draft202012Validator(load_schema("score"))

# Valid sha256-style digests for the version-metadata block.
_DIGEST_PROMPT = "sha256:" + "a" * 64
_DIGEST_GT = "sha256:" + "b" * 64
_DIGEST_ANSWER = "sha256:" + "c" * 64


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #


def _gt() -> dict[str, Any]:
    """A fresh deep copy of the valid bug_localization GT (ex.FULL_GT)."""
    return copy.deepcopy(ex.FULL_GT)


def _full_credits() -> dict[str, Any]:
    """Consensus credits derived from ex.FULL_JUDGE_OUTPUT (1:1 with FULL_GT)."""
    return {item["item_id"]: item["credit"] for item in ex.FULL_JUDGE_OUTPUT["items"]}


def _credits(**overrides: Any) -> dict[str, Any]:
    """Full credits with per-item overrides (``outcome.root-cause=0`` etc.)."""
    credits = _full_credits()
    credits.update(overrides)
    return credits


def _all_credits(value: Any) -> dict[str, Any]:
    """Every FULL_GT item set to the same credit."""
    return {item["id"]: value for item in _gt()["rubric_items"]}


def _version_metadata(**overrides: Any) -> VersionMetadata:
    """A valid §20 identity block with optional field overrides."""
    base = VersionMetadata(
        benchmark_version=BENCHMARK_VERSION,
        judge_protocol=JUDGE_PROTOCOL,
        scoring_profile="bug_localization_v1",
        judge_provider=JUDGE_PROVIDER,
        judge_requested_model="glm-5.2",
        judge_model="glm-5.2",
        judge_cli_version="2.1.220",
        judge_prompt_digest=_DIGEST_PROMPT,
        ground_truth_digest=_DIGEST_GT,
        agent_answer_digest=_DIGEST_ANSWER,
        case_id=ex.CASE_ID,
        task_type="bug_localization",
    )
    return dataclasses.replace(base, **overrides)


def _consensus(**overrides: Any) -> ConsensusInfo:
    """A formal two-Judge mean consensus with optional field overrides."""
    base = ConsensusInfo(mode="mean", judges=2, arbiter_used=False)
    return dataclasses.replace(base, **overrides)


def _reverse_error(item_id: str = "reasoning.failure-chain") -> dict[str, Any]:
    """A Judge critical error signalling a reverse critical relation."""
    return {
        "item_id": item_id,
        "code": REVERSE_CRITICAL_RELATION_ZERO,
        "reason": "causal direction reversed",
    }


def _gt_no_core_critical() -> dict[str, Any]:
    """A valid GT whose core_correctness dimension has NO critical item.

    Used to prove ``core_correctness_all_zero`` does not fire vacuously when no
    core_correctness critical item exists (§12).
    """
    return {
        "schema_version": "ground-truth-v1",
        "case_id": "no-core-crit-case",
        "task_type": "bug_localization",
        "scoring_profile": "bug_localization_v1",
        "rubric_items": [
            {"id": "core.main", "dimension": "core_correctness", "points": 35, "criterion": "core"},
            {
                "id": "reasoning.main",
                "dimension": "reasoning_correctness",
                "points": 25,
                "criterion": "reasoning",
                "critical": True,
                "zero_credit": "direction wrong",
            },
            {
                "id": "complete.main",
                "dimension": "completeness",
                "points": 20,
                "criterion": "complete",
            },
            {
                "id": "scope.main",
                "dimension": "scope_precision",
                "points": 10,
                "criterion": "scope",
            },
            {
                "id": "evidence.main",
                "dimension": "evidence_actionability",
                "points": 10,
                "criterion": "evidence",
            },
        ],
    }


# --------------------------------------------------------------------------- #
# §11 Core aggregation math
# --------------------------------------------------------------------------- #


def test_full_aggregation_matches_expected_values() -> None:
    """FULL_GT + FULL_JUDGE_OUTPUT credits reproduce the ex.FULL_SCORE math."""
    result = aggregate(_gt(), _full_credits(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.raw_total == Decimal("70.5")
    assert result.capped_total == Decimal("70.5")
    assert result.critical_cap.applied is False
    # Item scores match FULL_SCORE exactly.
    expected_items = {it["item_id"]: it for it in ex.FULL_SCORE["items"]}
    assert {it.item_id for it in result.items} == set(expected_items)
    for it in result.items:
        exp = expected_items[it.item_id]
        assert it.points == Decimal(str(exp["points"]))
        assert it.consensus_credit == Decimal(str(exp["consensus_credit"]))
        assert it.item_score == Decimal(str(exp["item_score"]))


def test_dimension_totals_match_full_score() -> None:
    result = aggregate(_gt(), _full_credits(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    for dim in prof.FROZEN_DIMENSION_NAMES:
        assert result.dimension_totals[dim] == Decimal(str(ex.FULL_SCORE["dimension_totals"][dim]))


def test_item_score_is_points_times_credit() -> None:
    result = aggregate(_gt(), _full_credits())
    for it in result.items:
        assert it.item_score == it.points * it.consensus_credit


def test_dimension_totals_sum_to_raw_total() -> None:
    result = aggregate(_gt(), _full_credits())
    assert sum(result.dimension_totals.values(), Decimal(0)) == result.raw_total


def test_raw_total_equals_sum_of_item_scores() -> None:
    result = aggregate(_gt(), _full_credits())
    assert sum((it.item_score for it in result.items), Decimal(0)) == result.raw_total


def test_items_preserve_gt_order() -> None:
    """Output items follow the GT rubric order for a deterministic payload."""
    gt = _gt()
    result = aggregate(gt, _full_credits())
    assert [it.item_id for it in result.items] == [it["id"] for it in gt["rubric_items"]]


# --------------------------------------------------------------------------- #
# §10.2 Item matching (acceptance criterion 1)
# --------------------------------------------------------------------------- #


def test_unknown_item_rejected() -> None:
    credits = _full_credits()
    credits["outcome.does-not-exist"] = 1
    with pytest.raises(AggregationError, match="unknown rubric item"):
        aggregate(_gt(), credits)


def test_missing_item_rejected() -> None:
    credits = _full_credits()
    del credits["outcome.root-cause"]
    with pytest.raises(AggregationError, match="without consensus credit"):
        aggregate(_gt(), credits)


def test_duplicate_verdict_rejected() -> None:
    """A sequence of verdicts with a repeated item id is rejected (§10.2)."""
    verdicts = [
        {"item_id": "outcome.root-cause", "credit": 1},
        {"item_id": "outcome.root-cause", "credit": 0.5},
        *(
            {"item_id": iid, "credit": c}
            for iid, c in _full_credits().items()
            if iid != "outcome.root-cause"
        ),
    ]
    with pytest.raises(AggregationError, match="duplicate consensus credit"):
        aggregate(_gt(), verdicts)


def test_duplicate_gt_item_rejected() -> None:
    gt = _gt()
    gt["rubric_items"].append(copy.deepcopy(gt["rubric_items"][0]))
    with pytest.raises(AggregationError, match="duplicate rubric item id"):
        aggregate(gt, _full_credits())


def test_unknown_and_missing_together_rejected() -> None:
    credits = _full_credits()
    del credits["outcome.root-cause"]
    credits["outcome.unknown"] = 1
    with pytest.raises(AggregationError):
        aggregate(_gt(), credits)


def test_sequence_of_verdict_dicts_accepted() -> None:
    """The judge-output item shape ({item_id, credit, ...}) is accepted directly."""
    verdicts = [
        {"item_id": it["item_id"], "credit": it["credit"], "verdict": it["verdict"]}
        for it in ex.FULL_JUDGE_OUTPUT["items"]
    ]
    result = aggregate(_gt(), verdicts)
    assert result.raw_total == Decimal("70.5")


# --------------------------------------------------------------------------- #
# Credit validation
# --------------------------------------------------------------------------- #


def test_credit_below_zero_rejected() -> None:
    with pytest.raises(AggregationError, match="out of range"):
        aggregate(_gt(), _credits(**{"outcome.root-cause": -0.25}))


def test_credit_above_one_rejected() -> None:
    with pytest.raises(AggregationError, match="out of range"):
        aggregate(_gt(), _credits(**{"outcome.root-cause": 1.25}))


def test_credit_zero_and_one_accepted() -> None:
    credits = _all_credits(1)
    credits["outcome.root-cause"] = 0
    result = aggregate(_gt(), credits)  # no raise
    assert result.raw_total == Decimal(80)


def test_non_numeric_credit_rejected() -> None:
    with pytest.raises(AggregationError, match="must be a number"):
        aggregate(_gt(), _credits(**{"outcome.root-cause": "full"}))


def test_bool_credit_rejected() -> None:
    """bool is a subclass of int but is not a valid credit."""
    with pytest.raises(AggregationError, match="must be a number"):
        aggregate(_gt(), _credits(**{"outcome.root-cause": True}))


def test_non_positive_points_rejected() -> None:
    gt = _gt()
    gt["rubric_items"][0]["points"] = 0
    with pytest.raises(AggregationError, match="points must be positive"):
        aggregate(gt, _full_credits())


# --------------------------------------------------------------------------- #
# §10.1 Decimal / rounding (acceptance criterion 2)
# --------------------------------------------------------------------------- #


def test_exact_decimal_for_consensus_means() -> None:
    """A two-Judge mean credit (0.125) is kept exact, never rounded to 0.13."""
    # 20 points * 0.125 = 2.5 exactly; a rounded float would drift.
    credits = _all_credits(0)
    credits["outcome.root-cause"] = Decimal("0.125")
    result = aggregate(_gt(), credits)
    root = next(it for it in result.items if it.item_id == "outcome.root-cause")
    assert root.consensus_credit == Decimal("0.125")
    assert root.item_score == Decimal("2.5")
    assert result.raw_total == Decimal("2.5")


def test_exact_decimal_for_0_375_mean() -> None:
    """Mean of 0.25 and 0.5 (0.375) stays exact across many items (no drift)."""
    credits = _all_credits(Decimal("0.375"))
    result = aggregate(_gt(), credits)
    # raw_total = 100 * 0.375 = 37.5 exactly
    assert result.raw_total == Decimal("37.5")
    assert sum(result.dimension_totals.values(), Decimal(0)) == result.raw_total


def test_no_float_drift_in_dimension_totals() -> None:
    """Quarter-step credits sum exactly per dimension (Decimal, not float)."""
    result = aggregate(_gt(), _full_credits())
    # core_correctness: 20*1 + 15*0.75 = 20 + 11.25 = 31.25 exactly
    assert result.dimension_totals["core_correctness"] == Decimal("31.25")
    # reasoning_correctness: 12*0.75 + 13*0.5 = 9 + 6.5 = 15.5 exactly
    assert result.dimension_totals["reasoning_correctness"] == Decimal("15.5")


# --------------------------------------------------------------------------- #
# §12 Critical cap (acceptance criterion 3)
# --------------------------------------------------------------------------- #


def test_no_cap_when_core_critical_not_zero() -> None:
    result = aggregate(_gt(), _full_credits(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.critical_cap.applied is False
    assert result.critical_cap.cap_value is None
    assert result.critical_cap.code is None
    assert result.critical_cap.triggered == ()
    assert result.capped_total == result.raw_total


def test_core_correctness_all_zero_cap_50() -> None:
    """Every core_correctness critical item at credit 0 caps the total at 50."""
    credits = _credits(**{"outcome.root-cause": 0})  # the only core_correctness critical item
    result = aggregate(_gt(), credits, task_profile=BUG_TASK, common_profile=BUG_COMMON)
    # raw = 70.5 - 20 = 50.5; cap 50 lowers it.
    assert result.raw_total == Decimal("50.5")
    cap = result.critical_cap
    assert cap.applied is True
    assert cap.cap_value == Decimal(50)
    assert cap.code == CORE_CORRECTNESS_ALL_ZERO
    assert result.capped_total == Decimal(50)


def test_core_all_zero_does_not_fire_without_core_critical() -> None:
    """No core_correctness critical item => the cap does not fire vacuously."""
    gt = _gt_no_core_critical()
    credits = {item["id"]: 0 for item in gt["rubric_items"]}
    result = aggregate(gt, credits, task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.critical_cap.applied is False
    assert result.raw_total == Decimal(0)
    assert result.capped_total == Decimal(0)


def test_core_all_zero_fires_but_does_not_lower() -> None:
    """All credits 0: the cap fires (audit) but the total is already 0 (§12)."""
    result = aggregate(_gt(), _all_credits(0), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.raw_total == Decimal(0)
    cap = result.critical_cap
    assert cap.applied is True
    assert cap.cap_value == Decimal(50)
    assert cap.code == CORE_CORRECTNESS_ALL_ZERO
    assert result.capped_total == Decimal(0)  # cap only lowers, never raises


def test_reverse_critical_relation_cap_60() -> None:
    """A reverse critical relation at credit 0 (Judge-signalled) caps at 60."""
    credits = _credits(**{"reasoning.failure-chain": 0})
    result = aggregate(
        _gt(),
        credits,
        critical_errors=[_reverse_error()],
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
    )
    # raw = 70.5 - 9 = 61.5; cap 60 lowers it.
    assert result.raw_total == Decimal("61.5")
    cap = result.critical_cap
    assert cap.applied is True
    assert cap.cap_value == Decimal(60)
    assert cap.code == REVERSE_CRITICAL_RELATION_ZERO
    assert result.capped_total == Decimal(60)


def test_reverse_cap_requires_judge_signal() -> None:
    """Credit 0 alone on a reverse-relation item does NOT fire the cap (§12)."""
    credits = _credits(**{"reasoning.failure-chain": 0})
    result = aggregate(_gt(), credits, task_profile=BUG_TASK, common_profile=BUG_COMMON)
    # core critical (outcome.root-cause) is NOT zero, and no Judge signal => no cap.
    assert result.critical_cap.applied is False
    assert result.capped_total == result.raw_total == Decimal("61.5")


def test_reverse_cap_requires_credit_zero() -> None:
    """A reverse signal on an item with non-zero credit does NOT fire the cap."""
    credits = _full_credits()  # reasoning.failure-chain credit 0.75
    result = aggregate(
        _gt(),
        credits,
        critical_errors=[_reverse_error()],
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
    )
    assert result.critical_cap.applied is False
    assert result.capped_total == result.raw_total


def test_multiple_caps_take_lowest() -> None:
    """Both caps fire: core_all_zero (50) and reverse (60) => strictest is 50."""
    credits = _credits(**{"outcome.root-cause": 0, "reasoning.failure-chain": 0})
    # All other items at full credit so the raw total stays above the cap.
    for iid in _full_credits():
        if iid not in ("outcome.root-cause", "reasoning.failure-chain"):
            credits[iid] = 1
    result = aggregate(
        _gt(),
        credits,
        critical_errors=[_reverse_error()],
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
    )
    # raw = 0 + 15 + 0 + 13 + 10 + 10 + 5 + 5 + 5 + 5 = 68
    assert result.raw_total == Decimal(68)
    cap = result.critical_cap
    assert cap.applied is True
    assert cap.cap_value == Decimal(50)  # lowest of {50, 60}
    assert cap.code == CORE_CORRECTNESS_ALL_ZERO
    assert result.capped_total == Decimal(50)
    # Both fired caps are retained for audit.
    codes = {t.code for t in cap.triggered}
    assert codes == {CORE_CORRECTNESS_ALL_ZERO, REVERSE_CRITICAL_RELATION_ZERO}


def test_cap_only_lowers_never_raises() -> None:
    """capped_total <= raw_total for every cap scenario (§12 invariant)."""
    scenarios = [
        _full_credits(),
        _credits(**{"outcome.root-cause": 0}),
        _credits(**{"reasoning.failure-chain": 0}),
        _all_credits(0),
        _all_credits(1),
        _credits(**{"outcome.root-cause": 0, "reasoning.failure-chain": 0}),
    ]
    for credits in scenarios:
        result = aggregate(_gt(), credits, critical_errors=[_reverse_error()])
        assert result.capped_total <= result.raw_total


def test_cap_values_resolved_from_common_profile() -> None:
    """Cap values come from the common profile (source of truth), not hardcoded."""
    common = copy.deepcopy(BUG_COMMON)
    for cap in common["critical_caps"]:
        if cap["code"] == CORE_CORRECTNESS_ALL_ZERO:
            cap["cap"] = 40  # override
    result = aggregate(
        _gt(),
        _credits(**{"outcome.root-cause": 0}),
        task_profile=BUG_TASK,
        common_profile=common,
    )
    assert result.critical_cap.cap_value == Decimal(40)
    assert result.capped_total == Decimal(40)  # raw 50.5 > 40


# --------------------------------------------------------------------------- #
# §10.2 Critical error validation
# --------------------------------------------------------------------------- #


def test_undeclared_critical_code_rejected() -> None:
    bad = {"item_id": "outcome.root-cause", "code": "bogus_code", "reason": "x"}
    with pytest.raises(AggregationError, match="not declared by the profile"):
        aggregate(
            _gt(),
            _full_credits(),
            critical_errors=[bad],
            task_profile=BUG_TASK,
            common_profile=BUG_COMMON,
        )


def test_critical_error_non_critical_item_rejected() -> None:
    """A critical error may only reference a GT critical item (§10.2)."""
    # outcome.trigger is NOT a critical item.
    bad = {"item_id": "outcome.trigger", "code": REVERSE_CRITICAL_RELATION_ZERO, "reason": "x"}
    with pytest.raises(AggregationError, match="non-critical or unknown"):
        aggregate(
            _gt(),
            _full_credits(),
            critical_errors=[bad],
            task_profile=BUG_TASK,
            common_profile=BUG_COMMON,
        )


def test_critical_error_unknown_item_rejected() -> None:
    bad = {"item_id": "no.such.item", "code": CORE_CORRECTNESS_ALL_ZERO, "reason": "x"}
    with pytest.raises(AggregationError, match="non-critical or unknown"):
        aggregate(
            _gt(),
            _full_credits(),
            critical_errors=[bad],
            task_profile=BUG_TASK,
            common_profile=BUG_COMMON,
        )


# --------------------------------------------------------------------------- #
# §12 Boundary cases (acceptance criterion 5)
# --------------------------------------------------------------------------- #


def test_all_credits_zero_scores_zero() -> None:
    """Empty/refused answer: all credits 0 => total 0 (cap fires but cannot raise)."""
    result = aggregate(_gt(), _all_credits(0), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.raw_total == Decimal(0)
    assert result.capped_total == Decimal(0)
    assert result.critical_cap.applied is True  # core_all_zero fired (audit)


def test_all_credits_one_scores_full() -> None:
    result = aggregate(_gt(), _all_credits(1), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert result.raw_total == Decimal(100)
    assert result.capped_total == Decimal(100)
    assert result.critical_cap.applied is False


def test_fractional_steps() -> None:
    """The full frozen credit ladder (0.25/0.5/0.75/1) computes exactly."""
    credits = _all_credits(0.25)
    result = aggregate(_gt(), credits)
    assert result.raw_total == Decimal(25)  # 100 * 0.25
    credits = _all_credits(0.5)
    assert aggregate(_gt(), credits).raw_total == Decimal(50)
    credits = _all_credits(0.75)
    assert aggregate(_gt(), credits).raw_total == Decimal(75)


def test_credit_step_zero_on_single_item() -> None:
    """A single item stepped from 1 to 0 reduces the total by exactly its points."""
    full = aggregate(_gt(), _all_credits(1))
    credits = _all_credits(1)
    credits["evidence.repro"] = 0
    stepped = aggregate(_gt(), credits)
    assert full.raw_total - stepped.raw_total == Decimal(5)  # evidence.repro is 5 points


# --------------------------------------------------------------------------- #
# §20 Version metadata + consensus + run mode
# --------------------------------------------------------------------------- #


def _build(**overrides: Any) -> ScoreResult:
    defaults: dict[str, Any] = dict(
        version_metadata=_version_metadata(),
        consensus=_consensus(),
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
    )
    defaults.update(overrides)
    return build_score(_gt(), _full_credits(), **defaults)


def test_build_score_attaches_metadata() -> None:
    score = _build(run_mode="formal")
    assert score.version_metadata.judge_model == "glm-5.2"
    assert score.run_mode == "formal"
    assert score.consensus.mode == "mean"
    assert score.raw_total == Decimal("70.5")
    assert score.schema_version == "score-v1"


def test_model_mismatch_rejected() -> None:
    with pytest.raises(AggregationError, match="judge_requested_model.*!="):
        _build(version_metadata=_version_metadata(judge_model="claude-sonnet-4"))


@pytest.mark.parametrize("sentinel", ["Auto", "auto", "latest", "LATEST"])
def test_unpinned_model_rejected(sentinel: str) -> None:
    with pytest.raises(AggregationError, match="not a pinned model"):
        _build(
            version_metadata=_version_metadata(judge_requested_model=sentinel, judge_model=sentinel)
        )


def test_bad_benchmark_version_rejected() -> None:
    with pytest.raises(AggregationError, match="benchmark_version"):
        _build(version_metadata=_version_metadata(benchmark_version="ai-score-v2"))


def test_bad_run_mode_rejected() -> None:
    with pytest.raises(AggregationError, match="run_mode"):
        _build(run_mode="experimental")


def test_bad_consensus_mode_rejected() -> None:
    with pytest.raises(AggregationError, match="consensus.mode"):
        _build(consensus=_consensus(mode="voting"))


def test_single_mode_requires_one_judge() -> None:
    with pytest.raises(AggregationError, match="single-judge"):
        _build(consensus=_consensus(mode="single", judges=2))


def test_run_modes_and_consensus_modes_frozen() -> None:
    """The frozen mode sets match the score.schema.json enums (§13.1, §13.2)."""
    assert RUN_MODES == ("development", "formal")
    assert CONSENSUS_MODES == ("single", "mean", "median")
    assert HUMAN_REVIEW_REASONS == (
        "critical_credit_range",
        "critical_consensus_confidence",
        "overall_confidence",
    )


# --------------------------------------------------------------------------- #
# §13.1 Human review
# --------------------------------------------------------------------------- #


def test_human_review_with_reasons_accepted() -> None:
    score = _build(
        requires_human_review=True,
        human_review_reasons=["critical_consensus_confidence", "overall_confidence"],
    )
    assert score.requires_human_review is True
    assert score.human_review_reasons == ("critical_consensus_confidence", "overall_confidence")


def test_human_review_without_reasons_rejected() -> None:
    with pytest.raises(AggregationError, match="no human_review_reasons"):
        _build(requires_human_review=True)


def test_human_review_false_with_reasons_rejected() -> None:
    with pytest.raises(AggregationError, match="human_review_reasons present"):
        _build(requires_human_review=False, human_review_reasons=["overall_confidence"])


def test_unknown_review_reason_rejected() -> None:
    with pytest.raises(AggregationError, match="unknown human_review_reasons"):
        _build(requires_human_review=True, human_review_reasons=["bogus_reason"])


# --------------------------------------------------------------------------- #
# §11/§20 Output schema validity (acceptance criterion 4)
# --------------------------------------------------------------------------- #


def test_score_to_dict_valid_no_cap() -> None:
    score = _build(run_mode="formal")
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["critical_cap"] is None
    assert d["raw_total"] == 70.5
    assert d["capped_total"] == 70.5
    assert "human_review_reasons" not in d


def test_score_to_dict_valid_with_cap() -> None:
    score = build_score(
        _gt(),
        _credits(**{"outcome.root-cause": 0}),
        version_metadata=_version_metadata(),
        consensus=_consensus(),
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
        run_mode="formal",
    )
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["critical_cap"] == {
        "applied": True,
        "cap_value": 50,
        "code": CORE_CORRECTNESS_ALL_ZERO,
        "reason": d["critical_cap"]["reason"],
    }
    assert d["raw_total"] == 50.5
    assert d["capped_total"] == 50


def test_score_to_dict_valid_with_human_review() -> None:
    score = _build(
        requires_human_review=True,
        human_review_reasons=["overall_confidence"],
        run_mode="formal",
    )
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["requires_human_review"] is True
    assert d["human_review_reasons"] == ["overall_confidence"]
    assert d["consensus"]["human_review_triggered"] is False


def test_score_to_dict_valid_all_zero_with_cap() -> None:
    """All-zero answer: cap fires, total 0, payload still schema-valid."""
    score = build_score(
        _gt(),
        _all_credits(0),
        version_metadata=_version_metadata(),
        consensus=_consensus(mode="single", judges=1),
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
        run_mode="development",
    )
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["raw_total"] == 0
    assert d["capped_total"] == 0
    assert d["critical_cap"]["applied"] is True
    assert d["critical_cap"]["code"] == CORE_CORRECTNESS_ALL_ZERO


def test_score_to_dict_dimension_totals_complete() -> None:
    score = _build()
    d = score_to_dict(score)
    assert set(d["dimension_totals"]) == set(prof.FROZEN_DIMENSION_NAMES)
    assert d["dimension_totals"]["core_correctness"] == 31.25


def test_requested_effective_model_check() -> None:
    """The test-only cross-field checker passes for an aggregator-built score."""
    score = _build(run_mode="formal")
    validate_requested_effective_model(score_to_dict(score))  # no raise


def test_score_to_dict_invalid_on_model_mismatch_blocked_earlier() -> None:
    """build_score blocks mismatched models before any dict is produced."""
    with pytest.raises(AggregationError):
        build_score(
            _gt(),
            _full_credits(),
            version_metadata=_version_metadata(judge_model="other-model"),
            consensus=_consensus(),
        )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_aggregation_is_deterministic_across_calls() -> None:
    """Same inputs => identical result (no hidden state or ordering drift)."""
    r1 = aggregate(_gt(), _full_credits(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    r2 = aggregate(_gt(), _full_credits(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert r1 == r2


def test_aggregate_without_profiles_uses_frozen_fallbacks() -> None:
    """The core works without profiles: frozen cap values/dimension set apply."""
    result = aggregate(_gt(), _full_credits())
    assert result.raw_total == Decimal("70.5")
    assert set(result.dimension_totals) == set(prof.FROZEN_DIMENSION_NAMES)
    # core_all_zero still fires from the frozen rule when the core critical is 0.
    capped = aggregate(_gt(), _credits(**{"outcome.root-cause": 0}))
    assert capped.critical_cap.applied is True
    assert capped.critical_cap.cap_value == Decimal(50)
