"""Profile (YAML) validation tests.

Acceptance criterion 4: common dimensions sum to 100, and a task Profile
cannot override the common Judge output protocol. These are business rules
that JSON Schema cannot express (the sum and the cross-profile equality), so
they are checked here with test-only validators. Each violation carries an
RFC 6901 JSON Pointer.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.schemas.examples import FINDING_KINDS

PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "profiles"
TASK_PROFILES = ["flow-tracing-v1", "bug-localization-v1", "impact-analysis-v1"]

FROZEN_DIMENSIONS = [
    ("core_correctness", 35),
    ("reasoning_correctness", 25),
    ("completeness", 20),
    ("scope_precision", 10),
    ("evidence_actionability", 10),
]

# Frozen underscore scoring-profile ids (design §6, R4) and the one-to-one
# mapping among filename, task_type, scoring_profile and profile_version.
FROZEN_SCORING_PROFILES = ["flow_tracing_v1", "bug_localization_v1", "impact_analysis_v1"]
EXPECTED_PROFILE_IDENTITY = {
    "flow-tracing-v1": {
        "task_type": "flow_tracing",
        "scoring_profile": "flow_tracing_v1",
        "profile_version": "flow-tracing-v1",
    },
    "bug-localization-v1": {
        "task_type": "bug_localization",
        "scoring_profile": "bug_localization_v1",
        "profile_version": "bug-localization-v1",
    },
    "impact-analysis-v1": {
        "task_type": "impact_analysis",
        "scoring_profile": "impact_analysis_v1",
        "profile_version": "impact-analysis-v1",
    },
}


class ProfileError(Exception):
    def __init__(self, message: str, pointer: str) -> None:
        self.pointer = pointer
        self.message = message
        super().__init__(f"{pointer}: {message}")


def load_profile(name: str) -> dict[str, Any]:
    with (PROFILE_DIR / f"{name}.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def validate_common_profile(profile: dict[str, Any]) -> None:
    dims = profile.get("dimensions")
    if not isinstance(dims, list) or not dims:
        raise ProfileError("common profile must define dimensions", "/dimensions")
    weights = [d.get("weight", 0) for d in dims]
    if sum(weights) != 100:
        raise ProfileError(f"dimension weights sum to {sum(weights)}, not 100", "/dimensions")
    if profile.get("judge_protocol") != "semantic_outcome_v1":
        raise ProfileError("common judge_protocol must be semantic_outcome_v1", "/judge_protocol")


def validate_task_profile(profile: dict[str, Any], common: dict[str, Any]) -> None:
    if profile.get("judge_protocol") != common["judge_protocol"]:
        raise ProfileError(
            "task profile must not override common judge_protocol", "/judge_protocol"
        )
    if "dimensions" in profile:
        raise ProfileError("task profile must not redefine dimension weights", "/dimensions")
    common_codes = {c["code"] for c in common["critical_caps"]}
    for i, code in enumerate(profile.get("critical_error_codes", [])):
        if code not in common_codes:
            raise ProfileError(
                f"undeclared critical_error_code {code!r}",
                f"/critical_error_codes/{i}",
            )


def validate_profile_identity(profile: dict[str, Any], filename: str) -> None:
    """Filename, task_type, scoring_profile and profile_version must map 1:1 (R4).

    Each task Profile explicitly declares its frozen underscore scoring_profile
    id, and the four identifiers cross-derive from one another so a mismatch in
    any one is caught with a locatable pointer.
    """
    expected = EXPECTED_PROFILE_IDENTITY[filename]
    scoring_profile = profile.get("scoring_profile")
    task_type = profile.get("task_type")
    profile_version = profile.get("profile_version")
    if scoring_profile not in FROZEN_SCORING_PROFILES:
        raise ProfileError(
            f"scoring_profile {scoring_profile!r} is not a frozen profile id",
            "/scoring_profile",
        )
    if scoring_profile != expected["scoring_profile"]:
        raise ProfileError(
            f"scoring_profile {scoring_profile!r} != expected {expected['scoring_profile']!r}",
            "/scoring_profile",
        )
    if task_type != expected["task_type"]:
        raise ProfileError(
            f"task_type {task_type!r} != expected {expected['task_type']!r}",
            "/task_type",
        )
    if profile_version != expected["profile_version"]:
        raise ProfileError(
            f"profile_version {profile_version!r} != expected {expected['profile_version']!r}",
            "/profile_version",
        )
    # Cross-derive to enforce a one-to-one mapping among all four identifiers.
    if scoring_profile != f"{task_type}_v1":
        raise ProfileError(
            f"scoring_profile {scoring_profile!r} != {task_type}_v1 derived from task_type",
            "/scoring_profile",
        )
    if profile_version != f"{task_type.replace('_', '-')}-v1":
        raise ProfileError(
            f"profile_version {profile_version!r} != derived {task_type.replace('_', '-')}-v1",
            "/profile_version",
        )
    if profile_version != filename:
        raise ProfileError(
            f"profile_version {profile_version!r} != filename {filename!r}",
            "/profile_version",
        )


COMMON = load_profile("common")


# ----------------------------- positive ------------------------------------ #


def test_common_dimensions_sum_to_100() -> None:
    validate_common_profile(COMMON)
    weights = [d["weight"] for d in COMMON["dimensions"]]
    assert sum(weights) == 100


def test_common_dimension_names_and_weights_frozen() -> None:
    actual = [(d["name"], d["weight"]) for d in COMMON["dimensions"]]
    assert actual == FROZEN_DIMENSIONS


def test_common_judge_protocol_frozen() -> None:
    assert COMMON["judge_protocol"] == "semantic_outcome_v1"


def test_common_credit_set_frozen() -> None:
    assert COMMON["credit_set"] == [0, 0.25, 0.5, 0.75, 1]


def test_common_critical_caps_frozen() -> None:
    caps = {c["code"]: c["cap"] for c in COMMON["critical_caps"]}
    assert caps == {"core_correctness_all_zero": 50, "reverse_critical_relation_zero": 60}
    assert COMMON["cap_selection"] == "min"


def test_common_consensus_thresholds_present() -> None:
    c = COMMON["consensus"]
    assert c["judges"] == 2
    assert c["noncritical_credit_diff_threshold"] == 0.25
    assert c["provisional_total_diff_threshold"] == 5
    assert c["critical_credit_range_threshold"] == 0.5
    assert c["critical_consensus_confidence_threshold"] == 0.70
    assert c["overall_confidence_threshold"] == 0.65


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_valid(name: str) -> None:
    validate_task_profile(load_profile(name), COMMON)


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_does_not_override_judge_protocol(name: str) -> None:
    assert load_profile(name)["judge_protocol"] == COMMON["judge_protocol"]


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_does_not_redefine_weights(name: str) -> None:
    assert "dimensions" not in load_profile(name)
    assert "dimension_semantics" in load_profile(name)


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_dimension_semantics_match_common(name: str) -> None:
    common_names = [d["name"] for d in COMMON["dimensions"]]
    assert list(load_profile(name)["dimension_semantics"]) == common_names


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_finding_kinds_match_schema_enums(name: str) -> None:
    profile = load_profile(name)
    assert profile["finding_kinds"] == FINDING_KINDS[profile["task_type"]]
    assert set(profile["required_finding_kinds"]) <= set(profile["finding_kinds"])


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_critical_codes_subset_of_common(name: str) -> None:
    common_codes = {c["code"] for c in COMMON["critical_caps"]}
    assert set(load_profile(name)["critical_error_codes"]) <= common_codes


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_extends_common(name: str) -> None:
    assert load_profile(name)["base"] == "common-v1"


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_task_profile_declares_scoring_profile(name: str) -> None:
    profile = load_profile(name)
    assert profile["scoring_profile"] in FROZEN_SCORING_PROFILES


@pytest.mark.parametrize("name", TASK_PROFILES)
def test_profile_identity_mapping(name: str) -> None:
    validate_profile_identity(load_profile(name), name)  # no raise


# ----------------------------- negative ------------------------------------ #


def test_common_dimensions_not_summing_to_100_rejected() -> None:
    bad = copy.deepcopy(COMMON)
    bad["dimensions"][0]["weight"] = 30  # 30+25+20+10+10 = 95
    with pytest.raises(ProfileError) as exc_info:
        validate_common_profile(bad)
    assert exc_info.value.pointer == "/dimensions"


def test_task_profile_overriding_judge_protocol_rejected() -> None:
    bad = copy.deepcopy(load_profile("flow-tracing-v1"))
    bad["judge_protocol"] = "pairwise_v1"
    with pytest.raises(ProfileError) as exc_info:
        validate_task_profile(bad, COMMON)
    assert exc_info.value.pointer == "/judge_protocol"


def test_task_profile_redefining_weights_rejected() -> None:
    bad = copy.deepcopy(load_profile("flow-tracing-v1"))
    bad["dimensions"] = [{"name": "core_correctness", "weight": 50}]
    with pytest.raises(ProfileError) as exc_info:
        validate_task_profile(bad, COMMON)
    assert exc_info.value.pointer == "/dimensions"


def test_task_profile_undeclared_critical_code_rejected() -> None:
    bad = copy.deepcopy(load_profile("bug-localization-v1"))
    bad["critical_error_codes"].append("invented_cap_code")
    with pytest.raises(ProfileError) as exc_info:
        validate_task_profile(bad, COMMON)
    assert exc_info.value.pointer == "/critical_error_codes/2"


def test_profile_identity_scoring_profile_mismatch_rejected() -> None:
    bad = copy.deepcopy(load_profile("flow-tracing-v1"))
    bad["scoring_profile"] = "bug_localization_v1"
    with pytest.raises(ProfileError) as exc_info:
        validate_profile_identity(bad, "flow-tracing-v1")
    assert exc_info.value.pointer == "/scoring_profile"


def test_profile_identity_task_type_mismatch_rejected() -> None:
    bad = copy.deepcopy(load_profile("bug-localization-v1"))
    bad["task_type"] = "flow_tracing"
    with pytest.raises(ProfileError) as exc_info:
        validate_profile_identity(bad, "bug-localization-v1")
    assert exc_info.value.pointer == "/task_type"


def test_profile_identity_version_mismatch_rejected() -> None:
    bad = copy.deepcopy(load_profile("impact-analysis-v1"))
    bad["profile_version"] = "flow-tracing-v1"
    with pytest.raises(ProfileError) as exc_info:
        validate_profile_identity(bad, "impact-analysis-v1")
    assert exc_info.value.pointer == "/profile_version"


def test_profile_identity_unknown_scoring_profile_rejected() -> None:
    bad = copy.deepcopy(load_profile("flow-tracing-v1"))
    bad["scoring_profile"] = "flow_tracing_v2"
    with pytest.raises(ProfileError) as exc_info:
        validate_profile_identity(bad, "flow-tracing-v1")
    assert exc_info.value.pointer == "/scoring_profile"
