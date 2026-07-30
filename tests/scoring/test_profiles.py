"""Tests for the production profile loader/validator (``scoring.profiles``).

Acceptance criterion 1: Profile name, major version, task type and common
protocol are cross-validated. These tests exercise the production module against
the shipped ``profiles/*.yaml`` files (promoted from the AIS-002 test-only
checks in ``tests/profiles/test_profiles.py``).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scoring import profiles as prof

PROFILE_DIR = prof.PROFILE_DIR
TASK_PROFILES = prof.TASK_PROFILE_FILES


# ----------------------------- constants ----------------------------------- #


def test_frozen_dimensions_sum_to_100() -> None:
    assert sum(w for _, w in prof.FROZEN_DIMENSIONS) == 100


def test_frozen_dimension_weights_match_design() -> None:
    assert prof.FROZEN_DIMENSIONS == [
        ("core_correctness", 35),
        ("reasoning_correctness", 25),
        ("completeness", 20),
        ("scope_precision", 10),
        ("evidence_actionability", 10),
    ]


def test_frozen_credit_set() -> None:
    assert prof.FROZEN_CREDIT_SET == [0, 0.25, 0.5, 0.75, 1]


def test_frozen_judge_protocol() -> None:
    assert prof.FROZEN_JUDGE_PROTOCOL == "semantic_outcome_v1"


def test_task_profile_map_is_one_to_one() -> None:
    # Each identifier set is distinct and the same size as the task-type set.
    task_types = set(prof.TASK_PROFILE_MAP)
    scoring = {info["scoring_profile"] for info in prof.TASK_PROFILE_MAP.values()}
    versions = {info["profile_version"] for info in prof.TASK_PROFILE_MAP.values()}
    files = {info["filename"] for info in prof.TASK_PROFILE_MAP.values()}
    assert len(task_types) == len(scoring) == len(versions) == len(files) == 3
    assert scoring == set(prof.SCORING_PROFILES)


# ----------------------------- loading ------------------------------------- #


def test_load_common_profile() -> None:
    common = prof.load_common_profile()
    assert common["profile_version"] == prof.COMMON_PROFILE_VERSION
    assert common["judge_protocol"] == prof.FROZEN_JUDGE_PROTOCOL


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_load_task_profile(task_type: str) -> None:
    profile = prof.load_task_profile(task_type)
    assert profile["task_type"] == task_type
    assert profile["base"] == prof.COMMON_PROFILE_VERSION


def test_load_task_profile_unknown_task_type_raises() -> None:
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.load_task_profile("not_a_task")
    assert exc_info.value.pointer == "/task_type"


def test_load_profile_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        prof.load_profile("does-not-exist", tmp_path)


# ----------------------------- positive validation ------------------------- #


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_validate_common_profile_passes(task_type: str) -> None:
    # The common profile shipped on disk must validate.
    prof.validate_common_profile(prof.load_common_profile())


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_validate_task_profile_passes(task_type: str) -> None:
    common = prof.load_common_profile()
    prof.validate_task_profile(prof.load_task_profile(task_type), common)


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_validate_profile_identity_passes(task_type: str) -> None:
    profile = prof.load_task_profile(task_type)
    filename = prof.TASK_PROFILE_MAP[task_type]["filename"]
    prof.validate_profile_identity(profile, filename)


@pytest.mark.parametrize("task_type", prof.TASK_TYPES)
def test_load_validated_task_profile_returns_pair(task_type: str) -> None:
    task, common = prof.load_validated_task_profile(task_type)
    assert task["task_type"] == task_type
    assert common["profile_version"] == prof.COMMON_PROFILE_VERSION
    assert prof.dimension_weights(common) == prof.FROZEN_DIMENSION_WEIGHTS


def test_known_dimension_names() -> None:
    assert prof.known_dimension_names() == prof.FROZEN_DIMENSION_NAMES


def test_dimension_weights_extracts_from_common() -> None:
    common = prof.load_common_profile()
    assert prof.dimension_weights(common) == prof.FROZEN_DIMENSION_WEIGHTS


# ----------------------------- negative: common ---------------------------- #


def _common() -> dict:
    return copy.deepcopy(prof.load_common_profile())


def test_common_bad_version_rejected() -> None:
    bad = _common()
    bad["profile_version"] = "common-v2"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_common_profile(bad)
    assert exc_info.value.pointer == "/profile_version"


def test_common_bad_protocol_rejected() -> None:
    bad = _common()
    bad["judge_protocol"] = "pairwise_v1"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_common_profile(bad)
    assert exc_info.value.pointer == "/judge_protocol"


def test_common_dimensions_not_summing_to_100_rejected() -> None:
    bad = _common()
    bad["dimensions"][0]["weight"] = 30  # 30+25+20+10+10 = 95
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_common_profile(bad)
    assert exc_info.value.pointer == "/dimensions/0/weight"


def test_common_unknown_dimension_rejected() -> None:
    bad = _common()
    bad["dimensions"][0]["name"] = "not_a_dimension"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_common_profile(bad)
    assert exc_info.value.pointer.startswith("/dimensions")


def test_common_bad_credit_set_rejected() -> None:
    bad = _common()
    bad["credit_set"] = [0, 0.5, 1]
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_common_profile(bad)
    assert exc_info.value.pointer == "/credit_set"


# ----------------------------- negative: task ------------------------------ #


def _task(name: str) -> dict:
    return copy.deepcopy(prof.load_profile(name))


def test_task_overriding_judge_protocol_rejected() -> None:
    bad = _task("flow-tracing-v1")
    bad["judge_protocol"] = "pairwise_v1"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_task_profile(bad, prof.load_common_profile())
    assert exc_info.value.pointer == "/judge_protocol"


def test_task_redefining_weights_rejected() -> None:
    bad = _task("flow-tracing-v1")
    bad["dimensions"] = [{"name": "core_correctness", "weight": 50}]
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_task_profile(bad, prof.load_common_profile())
    assert exc_info.value.pointer == "/dimensions"


def test_task_dimension_semantics_mismatch_rejected() -> None:
    bad = _task("bug-localization-v1")
    bad["dimension_semantics"] = {"core_correctness": "x", "completeness": "y"}
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_task_profile(bad, prof.load_common_profile())
    assert exc_info.value.pointer == "/dimension_semantics"


def test_task_undeclared_critical_code_rejected() -> None:
    bad = _task("bug-localization-v1")
    bad["critical_error_codes"].append("invented_cap_code")
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_task_profile(bad, prof.load_common_profile())
    assert exc_info.value.pointer == "/critical_error_codes/2"


# ----------------------------- negative: identity -------------------------- #


def test_identity_scoring_profile_mismatch_rejected() -> None:
    bad = _task("flow-tracing-v1")
    bad["scoring_profile"] = "bug_localization_v1"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_profile_identity(bad, "flow-tracing-v1")
    assert exc_info.value.pointer == "/scoring_profile"


def test_identity_task_type_mismatch_rejected() -> None:
    bad = _task("bug-localization-v1")
    bad["task_type"] = "flow_tracing"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_profile_identity(bad, "bug-localization-v1")
    assert exc_info.value.pointer == "/task_type"


def test_identity_version_mismatch_rejected() -> None:
    bad = _task("impact-analysis-v1")
    bad["profile_version"] = "flow-tracing-v1"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_profile_identity(bad, "impact-analysis-v1")
    assert exc_info.value.pointer == "/profile_version"


def test_identity_wrong_filename_rejected() -> None:
    # A valid flow-tracing profile passed under the bug-localization filename:
    # the filename anchors the identity, so the task_type mismatch is caught.
    bad = _task("flow-tracing-v1")
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_profile_identity(bad, "bug-localization-v1")
    assert exc_info.value.pointer == "/task_type"


def test_identity_unknown_task_type_rejected() -> None:
    bad = _task("flow-tracing-v1")
    bad["task_type"] = "not_a_task"
    with pytest.raises(prof.ProfileError) as exc_info:
        prof.validate_profile_identity(bad, "flow-tracing-v1")
    assert exc_info.value.pointer == "/task_type"


def test_load_validated_task_profile_propagates_profile_error(tmp_path: Path) -> None:
    # Ship a corrupted common profile in a temp dir; validation must reject it.
    common = _common()
    common["judge_protocol"] = "pairwise_v1"
    (tmp_path / "common.yaml").write_text(__import__("yaml").safe_dump(common), encoding="utf-8")
    for name in prof.TASK_PROFILE_FILES:
        src = PROFILE_DIR / f"{name}.yaml"
        (tmp_path / f"{name}.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(prof.ProfileError):
        prof.load_validated_task_profile("flow_tracing", tmp_path)
