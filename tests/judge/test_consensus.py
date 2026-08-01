"""Tests for ``judge.consensus`` (design §13, §14).

AIS-006 acceptance-criteria test mapping:

==  ========================================  ================================================
§   Criterion                                Test
==  ========================================  ================================================
1   A/B triggers fire Judge C / human        test_arbiter_* / test_human_review_*
    review per DEC-001 (boundary cases)      (boundary cases at the exact thresholds)
2   three-Judge median; mean non-enum        test_mean_*_exact_decimal / test_median_*
3   missing/illegal/protocol-inconsistent     test_validation_*
    outputs not silently included
4   persistent disagreement / low            test_human_review_*_reasons /
    confidence sets review + stable codes    test_multiple_review_reasons_in_frozen_order
5   effective item credit == consensus       test_effective_credit_equals_consensus_*
6   no adjudication.json, no override        test_no_human_override_when_review_required /
                                            test_no_override_parameter
==  ========================================  ================================================
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from judge.consensus import (
    ARBITER_TRIGGER_CRITICAL_DISAGREEMENT,
    ARBITER_TRIGGER_NONCRITICAL_GAP,
    ARBITER_TRIGGER_PROVISIONAL_TOTAL_GAP,
    ConsensusError,
    build_effective_score,
    form_consensus,
    should_call_arbiter,
)
from scoring import profiles as prof
from scoring.aggregator import (
    BENCHMARK_VERSION,
    HUMAN_REVIEW_REASONS,
    JUDGE_PROTOCOL,
    JUDGE_PROVIDER,
    REVERSE_CRITICAL_RELATION_ZERO,
    VersionMetadata,
    score_to_dict,
)
from tests.schemas import examples as ex
from tests.schemas._validators import load_schema

# A validated bug_localization profile pair, loaded once for the module.
BUG_TASK, BUG_COMMON = prof.load_validated_task_profile("bug_localization")

# Score schema validator (effective-score.json reuses score-v1 in v1).
_SCORE_VALIDATOR = Draft202012Validator(load_schema("score"))

# Valid sha256-style digests for the version-metadata block.
_DIGEST_PROMPT = "sha256:" + "a" * 64
_DIGEST_GT = "sha256:" + "b" * 64
_DIGEST_ANSWER = "sha256:" + "c" * 64

# The two critical items in FULL_GT (bug_localization).
CRIT_ROOT = "outcome.root-cause"  # core_correctness, 20 points
CRIT_CHAIN = "reasoning.failure-chain"  # reasoning_correctness, 12 points
# A non-critical item with 5 points (handy for isolated gap tests).
NONCRIT_REPRO = "evidence.repro"
# Two non-critical 10-point items (handy for the provisional-total boundary).
NONCRIT_BLAST = "completeness.blast-radius"
NONCRIT_RECOVERY = "completeness.recovery"


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #


def _gt() -> dict[str, Any]:
    """A fresh deep copy of the valid bug_localization GT (ex.FULL_GT)."""
    return copy.deepcopy(ex.FULL_GT)


def _judge(
    *,
    credits: dict[str, Any] | None = None,
    confidences: dict[str, Any] | None = None,
    overall_confidence: Any = 0.84,
    critical_errors: list[Any] | None = None,
    scoring_profile: str | None = None,
    judge_protocol: str | None = None,
    schema_version: str | None = None,
) -> dict[str, Any]:
    """A fresh FULL_JUDGE_OUTPUT with per-item / header overrides."""
    out = copy.deepcopy(ex.FULL_JUDGE_OUTPUT)
    by_id = {it["item_id"]: it for it in out["items"]}
    if credits:
        for iid, c in credits.items():
            by_id[iid]["credit"] = c
    if confidences:
        for iid, c in confidences.items():
            by_id[iid]["confidence"] = c
    out["overall_confidence"] = overall_confidence
    if critical_errors is not None:
        out["critical_errors"] = copy.deepcopy(critical_errors)
    if scoring_profile is not None:
        out["scoring_profile"] = scoring_profile
    if judge_protocol is not None:
        out["judge_protocol"] = judge_protocol
    if schema_version is not None:
        out["schema_version"] = schema_version
    return out


def _reverse_error(item_id: str = CRIT_CHAIN) -> dict[str, Any]:
    """A Judge critical error signalling a reverse critical relation."""
    return {"item_id": item_id, "code": REVERSE_CRITICAL_RELATION_ZERO, "reason": "reversed"}


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


# --------------------------------------------------------------------------- #
# §13.1 steps 2-4: arbiter truth table (acceptance criterion 1)
# --------------------------------------------------------------------------- #


def test_arbiter_not_needed_when_ab_identical() -> None:
    decision = should_call_arbiter(
        _judge(), _judge(), _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON
    )
    assert decision.call_arbiter is False
    assert decision.triggers == ()


def test_arbiter_critical_disagreement_any_difference() -> None:
    """Step 2: any difference on a critical item triggers C (even 0.25)."""
    a = _judge(credits={CRIT_ROOT: 1})
    b = _judge(credits={CRIT_ROOT: 0.75})  # 0.25 apart on a critical item
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is True
    codes = {t.code for t in decision.triggers}
    assert ARBITER_TRIGGER_CRITICAL_DISAGREEMENT in codes


def test_arbiter_noncritical_gap_boundary_exactly_threshold_no_trigger() -> None:
    """Step 3 boundary: |A-B| == 0.25 on a non-critical item does NOT trigger."""
    a = _judge(credits={NONCRIT_REPRO: 0.25})
    b = _judge(credits={NONCRIT_REPRO: 0.5})  # diff exactly 0.25
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is False


def test_arbiter_noncritical_gap_above_threshold_triggers() -> None:
    """Step 3: |A-B| > 0.25 on a non-critical item triggers C."""
    a = _judge(credits={NONCRIT_REPRO: 0})
    b = _judge(credits={NONCRIT_REPRO: 0.5})  # diff 0.5 > 0.25
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is True
    assert {t.code for t in decision.triggers} == {ARBITER_TRIGGER_NONCRITICAL_GAP}


def test_arbiter_provisional_total_gap_boundary_exactly_five_no_trigger() -> None:
    """Step 4 boundary: |total_A - total_B| == 5 does NOT trigger (> is strict)."""
    # Two 10-point non-critical items each 0.25 apart (same direction) => 20 * 0.25 == 5.
    a = _judge(credits={NONCRIT_BLAST: 0.5, NONCRIT_RECOVERY: 0.5})
    b = _judge(credits={NONCRIT_BLAST: 0.25, NONCRIT_RECOVERY: 0.25})
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is False


def test_arbiter_provisional_total_gap_above_five_triggers() -> None:
    """Step 4: |total_A - total_B| > 5 triggers C, with no per-item gap."""
    # Three non-critical items (10+10+5 points) each 0.25 apart => 25 * 0.25 == 6.25.
    a = _judge(credits={NONCRIT_BLAST: 0.5, NONCRIT_RECOVERY: 0.5, NONCRIT_REPRO: 0.5})
    b = _judge(credits={NONCRIT_BLAST: 0.25, NONCRIT_RECOVERY: 0.25, NONCRIT_REPRO: 0.25})
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is True
    # No per-item gap (all diffs == 0.25, not > 0.25) and no critical disagreement.
    assert {t.code for t in decision.triggers} == {ARBITER_TRIGGER_PROVISIONAL_TOTAL_GAP}


def test_arbiter_multiple_triggers_reported() -> None:
    """All three triggers can fire at once and are each reported."""
    a = _judge(credits={CRIT_ROOT: 1, NONCRIT_REPRO: 0.5})
    b = _judge(credits={CRIT_ROOT: 0, NONCRIT_REPRO: 0})  # critical differ + noncrit gap 0.5
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert decision.call_arbiter is True
    codes = {t.code for t in decision.triggers}
    assert codes == {
        ARBITER_TRIGGER_CRITICAL_DISAGREEMENT,
        ARBITER_TRIGGER_NONCRITICAL_GAP,
        ARBITER_TRIGGER_PROVISIONAL_TOTAL_GAP,
    }


def test_arbiter_thresholds_resolved_from_common_profile() -> None:
    """The common profile is the source of truth for thresholds (not hardcoded)."""
    common = copy.deepcopy(BUG_COMMON)
    # Lower the non-critical gap threshold to 0.1 so a 0.25 diff now triggers.
    common["consensus"]["noncritical_credit_diff_threshold"] = 0.1
    a = _judge(credits={NONCRIT_REPRO: 0.25})
    b = _judge(credits={NONCRIT_REPRO: 0.5})  # diff 0.25 > 0.1 now
    decision = should_call_arbiter(a, b, _gt(), task_profile=BUG_TASK, common_profile=common)
    assert decision.call_arbiter is True
    assert {t.code for t in decision.triggers} == {ARBITER_TRIGGER_NONCRITICAL_GAP}


def test_arbiter_uses_frozen_fallbacks_without_profile() -> None:
    """Without profiles the frozen thresholds apply identically."""
    a = _judge(credits={NONCRIT_REPRO: 0})
    b = _judge(credits={NONCRIT_REPRO: 0.5})
    decision = should_call_arbiter(a, b, _gt())  # no profiles
    assert decision.call_arbiter is True
    assert {t.code for t in decision.triggers} == {ARBITER_TRIGGER_NONCRITICAL_GAP}


def test_arbiter_validates_inputs() -> None:
    """should_call_arbiter rejects an illegal Judge output instead of deciding."""
    with pytest.raises(ConsensusError, match="not in frozen credit set"):
        should_call_arbiter(_judge(credits={CRIT_ROOT: 0.6}), _judge(), _gt())


# --------------------------------------------------------------------------- #
# §13.1 steps 5-6: aggregation (acceptance criterion 2)
# --------------------------------------------------------------------------- #


def test_single_judge_dev_mode_passes_through() -> None:
    outcome = form_consensus([_judge()], _gt(), run_mode="development")
    assert outcome.mode == "single"
    assert outcome.judges == 1
    assert outcome.arbiter_used is False
    base = {it["item_id"]: Decimal(str(it["credit"])) for it in ex.FULL_JUDGE_OUTPUT["items"]}
    assert outcome.consensus_credits == base


def test_mean_two_judges_exact_decimal_0_375() -> None:
    """Two-Judge mean of 0.25 and 0.5 is exactly 0.375 (non-enum, §10.1)."""
    a = _judge(credits={NONCRIT_REPRO: 0.25})
    b = _judge(credits={NONCRIT_REPRO: 0.5})  # diff 0.25 (no arbiter trigger)
    outcome = form_consensus(
        [a, b], _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.mode == "mean"
    assert outcome.judges == 2
    assert outcome.arbiter_used is False
    assert outcome.consensus_credits[NONCRIT_REPRO] == Decimal("0.375")


def test_mean_two_judges_exact_decimal_0_125() -> None:
    """Two-Judge mean of 0 and 0.25 is exactly 0.125 (non-enum)."""
    a = _judge(credits={NONCRIT_REPRO: 0})
    b = _judge(credits={NONCRIT_REPRO: 0.25})
    outcome = form_consensus([a, b], _gt(), run_mode="formal")
    assert outcome.consensus_credits[NONCRIT_REPRO] == Decimal("0.125")


def test_mean_two_judges_identical_yields_enum_values() -> None:
    """Identical A/B keeps enum credits (mean of equal values)."""
    outcome = form_consensus([_judge(), _judge()], _gt(), run_mode="formal")
    assert outcome.consensus_credits[CRIT_ROOT] == Decimal("1")
    assert outcome.requires_human_review is False


def test_mean_two_judges_with_trigger_raises() -> None:
    """Formal mode with two disagreeing Judges must not silently mean them."""
    a = _judge(credits={CRIT_ROOT: 1})
    b = _judge(credits={CRIT_ROOT: 0})  # critical disagreement => arbiter required
    with pytest.raises(ConsensusError, match="arbiter .* required"):
        form_consensus([a, b], _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)


def test_median_three_judges_middle_value() -> None:
    """Three-Judge median of {0, 0.5, 1} is 0.5 (§13.1 step 6)."""
    a = _judge(credits={CRIT_ROOT: 0})
    b = _judge(credits={CRIT_ROOT: 0.5})
    c = _judge(credits={CRIT_ROOT: 1})
    outcome = form_consensus(
        [a, b, c], _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.mode == "median"
    assert outcome.judges == 3
    assert outcome.arbiter_used is True
    assert outcome.consensus_credits[CRIT_ROOT] == Decimal("0.5")


def test_median_three_identical_judges_no_review() -> None:
    """Three identical Judges: median == value, no human review (no disagreement)."""
    outcome = form_consensus([_judge(), _judge(), _judge()], _gt(), run_mode="formal")
    assert outcome.mode == "median"
    assert outcome.requires_human_review is False
    base = {it["item_id"]: Decimal(str(it["credit"])) for it in ex.FULL_JUDGE_OUTPUT["items"]}
    assert outcome.consensus_credits == base


def test_median_confidence_aggregated_same_way() -> None:
    """Consensus confidence is the median of the three per-item confidences."""
    a = _judge(confidences={CRIT_ROOT: 0.5}, overall_confidence=0.9)
    b = _judge(confidences={CRIT_ROOT: 0.7}, overall_confidence=0.9)
    c = _judge(confidences={CRIT_ROOT: 0.9}, overall_confidence=0.9)
    outcome = form_consensus([a, b, c], _gt(), run_mode="formal")
    assert outcome.consensus_confidences[CRIT_ROOT] == Decimal("0.7")
    assert outcome.consensus_overall_confidence == Decimal("0.9")


# --------------------------------------------------------------------------- #
# §13.1 steps 7-8: human review (acceptance criteria 1, 4)
# --------------------------------------------------------------------------- #


def _three_judges(
    *,
    root_credits: tuple[Any, Any, Any] = (0.75, 0.75, 0.75),
    root_confidences: tuple[Any, Any, Any] = (0.9, 0.9, 0.9),
    chain_credits: tuple[Any, Any, Any] = (0.75, 0.75, 0.75),
    chain_confidences: tuple[Any, Any, Any] = (0.9, 0.9, 0.9),
    overalls: tuple[Any, Any, Any] = (0.84, 0.84, 0.84),
) -> list[dict[str, Any]]:
    """Three Judges identical except the critical-item fields given."""
    judges = []
    for i in range(3):
        judges.append(
            _judge(
                credits={CRIT_ROOT: root_credits[i], CRIT_CHAIN: chain_credits[i]},
                confidences={
                    CRIT_ROOT: root_confidences[i],
                    CRIT_CHAIN: chain_confidences[i],
                },
                overall_confidence=overalls[i],
            )
        )
    return judges


def test_human_review_critical_credit_range_triggers() -> None:
    """Step 7: critical item credit range > 0.5 triggers review."""
    judges = _three_judges(root_credits=(0, 0.5, 1))  # range 1 > 0.5
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is True
    assert outcome.human_review_reasons == ("critical_credit_range",)


def test_human_review_critical_credit_range_boundary_no_trigger() -> None:
    """Step 7 boundary: range == 0.5 does NOT trigger (> is strict)."""
    judges = _three_judges(root_credits=(0, 0.25, 0.5))  # range 0.5, not > 0.5
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is False


def test_human_review_critical_consensus_confidence_triggers() -> None:
    """Step 8a: critical item consensus confidence < 0.70 triggers review."""
    judges = _three_judges(root_confidences=(0.5, 0.6, 0.8))  # median 0.6 < 0.70
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is True
    assert outcome.human_review_reasons == ("critical_consensus_confidence",)


def test_human_review_critical_confidence_boundary_no_trigger() -> None:
    """Step 8a boundary: consensus confidence == 0.70 does NOT trigger."""
    judges = _three_judges(root_confidences=(0.6, 0.7, 0.8))  # median 0.7 == 0.70
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is False


def test_human_review_overall_confidence_triggers() -> None:
    """Step 8b: consensus overall confidence < 0.65 triggers review."""
    judges = _three_judges(overalls=(0.5, 0.6, 0.7))  # median 0.6 < 0.65
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is True
    assert outcome.human_review_reasons == ("overall_confidence",)


def test_human_review_overall_confidence_boundary_no_trigger() -> None:
    """Step 8b boundary: consensus overall confidence == 0.65 does NOT trigger."""
    judges = _three_judges(overalls=(0.6, 0.65, 0.7))  # median 0.65 == 0.65
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is False


def test_multiple_review_reasons_in_frozen_order() -> None:
    """All three review triggers fire; reasons are in the frozen enum order."""
    judges = _three_judges(
        root_credits=(0, 0.5, 1),  # range 1 > 0.5
        root_confidences=(0.5, 0.6, 0.8),  # median 0.6 < 0.70
        overalls=(0.5, 0.6, 0.7),  # median 0.6 < 0.65
    )
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.requires_human_review is True
    assert outcome.human_review_reasons == HUMAN_REVIEW_REASONS


def test_review_reasons_are_frozen_subset() -> None:
    """Reasons are always a subset of the frozen HUMAN_REVIEW_REASONS."""
    judges = _three_judges(root_credits=(0, 0.5, 1))
    outcome = form_consensus(judges, _gt(), run_mode="formal")
    assert set(outcome.human_review_reasons).issubset(set(HUMAN_REVIEW_REASONS))


def test_no_human_review_in_two_judge_path() -> None:
    """Steps 7-8 are three-Judge phenomena; two-Judge mean never sets review."""
    outcome = form_consensus([_judge(), _judge()], _gt(), run_mode="formal")
    assert outcome.requires_human_review is False
    assert outcome.human_review_reasons == ()


def test_no_human_review_in_single_judge_path() -> None:
    """Development (single-Judge) mode never sets human review."""
    outcome = form_consensus([_judge()], _gt(), run_mode="development")
    assert outcome.requires_human_review is False


# --------------------------------------------------------------------------- #
# §10.2 validation (acceptance criterion 3)
# --------------------------------------------------------------------------- #


def test_validation_missing_output_rejected() -> None:
    with pytest.raises(ConsensusError, match="output is missing"):
        form_consensus([None], _gt(), run_mode="development")


def test_validation_non_mapping_output_rejected() -> None:
    with pytest.raises(ConsensusError, match="must be a mapping"):
        form_consensus(["not-a-dict"], _gt(), run_mode="development")


def test_validation_illegal_credit_rejected() -> None:
    with pytest.raises(ConsensusError, match="not in frozen credit set"):
        form_consensus([_judge(credits={CRIT_ROOT: 0.6})], _gt(), run_mode="development")


def test_validation_bool_credit_rejected() -> None:
    with pytest.raises(ConsensusError, match="must be a number"):
        form_consensus([_judge(credits={CRIT_ROOT: True})], _gt(), run_mode="development")


def test_validation_schema_version_mismatch_rejected() -> None:
    with pytest.raises(ConsensusError, match="schema_version"):
        form_consensus([_judge(schema_version="judge-output-v2")], _gt(), run_mode="development")


def test_validation_protocol_mismatch_rejected() -> None:
    with pytest.raises(ConsensusError, match="judge_protocol"):
        form_consensus([_judge(judge_protocol="something_else")], _gt(), run_mode="development")


def test_validation_profile_mismatch_rejected() -> None:
    with pytest.raises(ConsensusError, match="scoring_profile"):
        form_consensus([_judge(scoring_profile="flow_tracing_v1")], _gt(), run_mode="development")


def test_validation_unknown_item_rejected() -> None:
    bad = _judge()
    bad["items"][0]["item_id"] = "outcome.does-not-exist"
    with pytest.raises(ConsensusError, match="not in the ground truth"):
        form_consensus([bad], _gt(), run_mode="development")


def test_validation_missing_item_rejected() -> None:
    bad = _judge()
    del bad["items"][0]  # drop outcome.root-cause
    with pytest.raises(ConsensusError, match="not judged"):
        form_consensus([bad], _gt(), run_mode="development")


def test_validation_duplicate_item_rejected() -> None:
    bad = _judge()
    bad["items"].append(copy.deepcopy(bad["items"][0]))
    with pytest.raises(ConsensusError, match="duplicate item_id"):
        form_consensus([bad], _gt(), run_mode="development")


def test_validation_undeclared_critical_code_rejected() -> None:
    bad = _judge(critical_errors=[{"item_id": CRIT_ROOT, "code": "bogus_code", "reason": "x"}])
    with pytest.raises(ConsensusError, match="not declared by the profile"):
        form_consensus([bad], _gt(), task_profile=BUG_TASK, run_mode="development")


def test_validation_critical_error_on_noncritical_item_rejected() -> None:
    bad = _judge(critical_errors=[_reverse_error(item_id=NONCRIT_REPRO)])
    with pytest.raises(ConsensusError, match="non-critical or unknown"):
        form_consensus([bad], _gt(), task_profile=BUG_TASK, run_mode="development")


def test_validation_critical_errors_missing_rejected() -> None:
    bad = _judge()
    del bad["critical_errors"]
    with pytest.raises(ConsensusError, match="critical_errors is missing"):
        form_consensus([bad], _gt(), run_mode="development")


def test_validation_overall_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ConsensusError, match="overall_confidence.*out of range"):
        form_consensus([_judge(overall_confidence=1.5)], _gt(), run_mode="development")


def test_validation_item_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ConsensusError, match="confidence.*out of range"):
        form_consensus([_judge(confidences={CRIT_ROOT: 1.5})], _gt(), run_mode="development")


def test_validation_rejects_one_bad_judge_among_many() -> None:
    """A single illegal output among valid ones still aborts the consensus."""
    with pytest.raises(ConsensusError, match="judge B"):
        form_consensus([_judge(), _judge(credits={CRIT_ROOT: 0.6})], _gt(), run_mode="formal")


def test_judge_outputs_must_be_a_sequence() -> None:
    with pytest.raises(ConsensusError, match="must be a sequence"):
        form_consensus(_judge(), _gt(), run_mode="development")  # a single mapping


def test_run_mode_validation() -> None:
    with pytest.raises(ConsensusError, match="run_mode"):
        form_consensus([_judge()], _gt(), run_mode="experimental")


def test_development_mode_requires_one_judge() -> None:
    with pytest.raises(ConsensusError, match="development mode requires exactly 1"):
        form_consensus([_judge(), _judge()], _gt(), run_mode="development")


def test_formal_mode_requires_two_or_three_judges() -> None:
    with pytest.raises(ConsensusError, match="formal mode requires 2 or 3"):
        form_consensus([_judge()], _gt(), run_mode="formal")
    with pytest.raises(ConsensusError, match="formal mode requires 2 or 3"):
        form_consensus([_judge()] * 4, _gt(), run_mode="formal")


# --------------------------------------------------------------------------- #
# Critical-error consolidation (§12 signal majority)
# --------------------------------------------------------------------------- #


def test_consolidate_majority_two_of_three() -> None:
    judges = _three_judges()
    judges[0]["critical_errors"] = [_reverse_error()]
    judges[1]["critical_errors"] = [_reverse_error()]
    judges[2]["critical_errors"] = []
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert len(outcome.critical_errors) == 1
    assert outcome.critical_errors[0]["code"] == REVERSE_CRITICAL_RELATION_ZERO


def test_consolidate_one_of_three_not_consensus() -> None:
    judges = _three_judges()
    judges[0]["critical_errors"] = [_reverse_error()]
    judges[1]["critical_errors"] = []
    judges[2]["critical_errors"] = []
    outcome = form_consensus(
        judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON, run_mode="formal"
    )
    assert outcome.critical_errors == ()


def test_consolidate_two_of_two_consensus() -> None:
    a = _judge(critical_errors=[_reverse_error()])
    b = _judge(critical_errors=[_reverse_error()])
    outcome = form_consensus([a, b], _gt(), run_mode="formal")
    assert len(outcome.critical_errors) == 1


def test_consolidate_one_of_two_not_consensus() -> None:
    a = _judge(critical_errors=[_reverse_error()])
    b = _judge(critical_errors=[])
    outcome = form_consensus([a, b], _gt(), run_mode="formal")
    assert outcome.critical_errors == ()


def test_consolidate_single_judge_passes_through() -> None:
    a = _judge(critical_errors=[_reverse_error()])
    outcome = form_consensus([a], _gt(), task_profile=BUG_TASK, run_mode="development")
    assert len(outcome.critical_errors) == 1


# --------------------------------------------------------------------------- #
# §14 effective-score assembly (acceptance criteria 5, 6)
# --------------------------------------------------------------------------- #


def _build(judges: list[dict[str, Any]], *, run_mode: str = "formal", **kw: Any):
    defaults: dict[str, Any] = dict(
        version_metadata=_version_metadata(),
        task_profile=BUG_TASK,
        common_profile=BUG_COMMON,
        run_mode=run_mode,
    )
    defaults.update(kw)
    return build_effective_score(judges, _gt(), **defaults)


def test_effective_credit_equals_consensus_two_judges() -> None:
    """criterion 5: effective item credit == Judge consensus credit (mean)."""
    a = _judge(credits={NONCRIT_REPRO: 0.25})
    b = _judge(credits={NONCRIT_REPRO: 0.5})
    score = _build([a, b])
    repro = next(it for it in score.items if it.item_id == NONCRIT_REPRO)
    assert repro.consensus_credit == Decimal("0.375")  # mean, not overridden


def test_effective_credit_equals_consensus_three_judges() -> None:
    """criterion 5: effective item credit == Judge consensus credit (median)."""
    judges = _three_judges(root_credits=(0, 0.5, 1))
    score = _build(judges)
    root = next(it for it in score.items if it.item_id == CRIT_ROOT)
    assert root.consensus_credit == Decimal("0.5")  # median of {0, 0.5, 1}


def test_effective_credit_equals_consensus_single_judge() -> None:
    score = _build([_judge()], run_mode="development")
    root = next(it for it in score.items if it.item_id == CRIT_ROOT)
    assert root.consensus_credit == Decimal("1")


def test_no_human_override_when_review_required() -> None:
    """criterion 6: human review sets a status flag only; credit is unchanged."""
    judges = _three_judges(root_credits=(0, 0.5, 1))  # range 1 > 0.5 => review
    score = _build(judges)
    assert score.requires_human_review is True
    assert score.human_review_reasons == ("critical_credit_range",)
    # Effective credit is still the median consensus credit (no override applied).
    root = next(it for it in score.items if it.item_id == CRIT_ROOT)
    assert root.consensus_credit == Decimal("0.5")
    assert root.item_score == root.points * root.consensus_credit


def test_no_override_parameter_exposed() -> None:
    """build_effective_score accepts no human-credit / adjudication override."""
    sig = inspect.signature(build_effective_score)
    forbidden = {"override", "human_credit", "adjudication", "manual_credit"}
    assert not (forbidden & set(sig.parameters))


def test_effective_score_two_judges_schema_valid() -> None:
    score = _build([_judge(), _judge()], run_mode="formal")
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["consensus"]["mode"] == "mean"
    assert d["consensus"]["judges"] == 2
    assert d["consensus"]["arbiter_used"] is False
    assert d["requires_human_review"] is False
    assert "human_review_reasons" not in d


def test_effective_score_three_judges_schema_valid() -> None:
    score = _build([_judge(), _judge(), _judge()], run_mode="formal")
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["consensus"]["mode"] == "median"
    assert d["consensus"]["judges"] == 3
    assert d["consensus"]["arbiter_used"] is True


def test_effective_score_with_human_review_schema_valid() -> None:
    judges = _three_judges(root_credits=(0, 0.5, 1))
    score = _build(judges, run_mode="formal")
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["requires_human_review"] is True
    assert d["human_review_reasons"] == ["critical_credit_range"]
    assert d["consensus"]["human_review_triggered"] is True


def test_effective_score_single_judge_dev_schema_valid() -> None:
    score = _build([_judge()], run_mode="development")
    d = score_to_dict(score)
    assert _SCORE_VALIDATOR.is_valid(d), list(_SCORE_VALIDATOR.iter_errors(d))
    assert d["consensus"]["mode"] == "single"
    assert d["run_mode"] == "development"


def test_effective_score_reverse_cap_via_majority_signal() -> None:
    """End-to-end: a majority reverse-critical signal at credit 0 caps at 60."""
    judges = _three_judges(chain_credits=(0, 0, 0))  # reasoning.failure-chain at 0
    judges[0]["critical_errors"] = [_reverse_error(CRIT_CHAIN)]
    judges[1]["critical_errors"] = [_reverse_error(CRIT_CHAIN)]
    judges[2]["critical_errors"] = []
    score = _build(judges, run_mode="formal")
    assert score.critical_cap.applied is True
    assert score.critical_cap.code == REVERSE_CRITICAL_RELATION_ZERO
    assert score.critical_cap.cap_value == Decimal(60)


def test_effective_score_two_judges_with_trigger_raises() -> None:
    a = _judge(credits={CRIT_ROOT: 1})
    b = _judge(credits={CRIT_ROOT: 0})
    with pytest.raises(ConsensusError, match="arbiter .* required"):
        _build([a, b], run_mode="formal")


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_consensus_is_deterministic_across_calls() -> None:
    judges = _three_judges(root_credits=(0, 0.5, 1))
    o1 = form_consensus(judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    o2 = form_consensus(judges, _gt(), task_profile=BUG_TASK, common_profile=BUG_COMMON)
    assert o1 == o2


def test_effective_score_deterministic_across_calls() -> None:
    judges = _three_judges(root_credits=(0, 0.5, 1))
    s1 = _build(copy.deepcopy(judges), run_mode="formal")
    s2 = _build(copy.deepcopy(judges), run_mode="formal")
    assert s1 == s2
