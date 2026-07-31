"""Production scoring profile loading and validation.

Promotes the AIS-002 test-only profile checks (``tests/profiles/test_profiles.py``)
into production code so the rubric validator (``scoring.rubric_validator``) and the
scoring core can rely on a validated Profile.

A Profile is the frozen, versioned definition of (see docs/ai-scoring-design.md
§5, §6, §12, §13.1):

* the common first-level result dimensions and weights (35/25/20/10/10);
* the frozen single-Judge credit set (0/0.25/0.5/0.75/1);
* task-specific dimension semantics, finding kinds and critical-error codes.

Task profiles MUST NOT redefine dimension weights or override the common Judge
output protocol (``semantic_outcome_v1``). Dimension weights are frozen at
35/25/20/10/10 and sum to 100 (DEC-001 #1). Different major versions of a
Profile may not be mixed (design §20).
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

# Repository root is two levels up from this module: scoring/profiles.py. Kept
# for tests that copy shipped profiles into a temp directory; production loading
# uses ``importlib.resources`` (AIS-004 R2) so it works from an installed wheel
# without the source checkout.
REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = REPO_ROOT / "profiles"

#: Frozen common profile version.
COMMON_PROFILE_VERSION = "common-v1"
#: Frozen Judge output protocol shared by every task profile (DEC-001 #1).
FROZEN_JUDGE_PROTOCOL = "semantic_outcome_v1"

#: Frozen first-level result dimensions and weights (DEC-001 #1, design §5).
#: Order is significant for cross-profile dimension-semantics alignment.
FROZEN_DIMENSIONS: list[tuple[str, int]] = [
    ("core_correctness", 35),
    ("reasoning_correctness", 25),
    ("completeness", 20),
    ("scope_precision", 10),
    ("evidence_actionability", 10),
]
FROZEN_DIMENSION_NAMES: list[str] = [name for name, _ in FROZEN_DIMENSIONS]
FROZEN_DIMENSION_WEIGHTS: dict[str, int] = dict(FROZEN_DIMENSIONS)

#: Frozen single-Judge credit set (DEC-001 #2). Consensus credit may be an exact
#: mean/median and is not restricted to this set.
FROZEN_CREDIT_SET: list[float] = [0, 0.25, 0.5, 0.75, 1]

#: Frozen critical-failure cap codes (DEC-001 #4, design §12).
FROZEN_CRITICAL_CAP_CODES: tuple[str, ...] = (
    "core_correctness_all_zero",
    "reverse_critical_relation_zero",
)

# One-to-one mapping among task_type, scoring_profile, profile_version and the
# profile filename stem (design §6, AIS-002 R4). Each task Profile explicitly
# declares its frozen underscore scoring_profile id; the four identifiers
# cross-derive from one another so a mismatch in any one is caught.
TASK_PROFILE_MAP: dict[str, dict[str, str]] = {
    "flow_tracing": {
        "task_type": "flow_tracing",
        "scoring_profile": "flow_tracing_v1",
        "profile_version": "flow-tracing-v1",
        "filename": "flow-tracing-v1",
    },
    "bug_localization": {
        "task_type": "bug_localization",
        "scoring_profile": "bug_localization_v1",
        "profile_version": "bug-localization-v1",
        "filename": "bug-localization-v1",
    },
    "impact_analysis": {
        "task_type": "impact_analysis",
        "scoring_profile": "impact_analysis_v1",
        "profile_version": "impact-analysis-v1",
        "filename": "impact-analysis-v1",
    },
}
TASK_TYPES: list[str] = list(TASK_PROFILE_MAP)
SCORING_PROFILES: list[str] = [info["scoring_profile"] for info in TASK_PROFILE_MAP.values()]
TASK_PROFILE_FILES: list[str] = [info["filename"] for info in TASK_PROFILE_MAP.values()]


class ProfileError(Exception):
    """A profile validation error carrying an RFC 6901 JSON Pointer."""

    def __init__(self, message: str, pointer: str) -> None:
        self.pointer = pointer
        self.message = message
        super().__init__(f"{pointer}: {message}")


def load_profile(name: str, profile_dir: Path | None = None) -> dict[str, Any]:
    """Load a profile YAML by its stem name (e.g. ``common`` or ``flow-tracing-v1``).

    By default the profile is loaded through ``importlib.resources`` from the
    installed ``profiles`` package so production works from a wheel without the
    source checkout (AIS-004 R2). The optional ``profile_dir`` overrides the
    resource location (used by tests with a temp directory).
    """
    if profile_dir is not None:
        with (profile_dir / f"{name}.yaml").open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    else:
        resource = files("profiles").joinpath(f"{name}.yaml")
        data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"profile {name!r} must be a YAML mapping", "")
    return data


def load_common_profile(profile_dir: Path | None = None) -> dict[str, Any]:
    """Load and return the raw common profile YAML."""
    return load_profile("common", profile_dir)


def load_task_profile(task_type: str, profile_dir: Path | None = None) -> dict[str, Any]:
    """Load and return the raw task profile YAML for ``task_type``."""
    if task_type not in TASK_PROFILE_MAP:
        raise ProfileError(
            f"unknown task_type {task_type!r}; expected one of {TASK_TYPES}",
            "/task_type",
        )
    return load_profile(TASK_PROFILE_MAP[task_type]["filename"], profile_dir)


def known_dimension_names() -> list[str]:
    """Return the frozen first-level dimension names (in frozen order)."""
    return list(FROZEN_DIMENSION_NAMES)


def dimension_weights(common: dict[str, Any]) -> dict[str, int]:
    """Extract ``dimension name -> weight`` from a common profile dict."""
    weights: dict[str, int] = {}
    for dim in common.get("dimensions", []):
        if isinstance(dim, dict) and "name" in dim and "weight" in dim:
            weights[dim["name"]] = dim["weight"]
    return weights


def validate_common_profile(profile: dict[str, Any]) -> None:
    """Validate the common profile against the frozen baseline (DEC-001 #1).

    Raises :class:`ProfileError` (with an RFC 6901 pointer) on the first
    violation. Checks the frozen profile version, Judge protocol, the frozen
    dimension set and weights (which implies the 100-point total), and the
    frozen credit set.
    """
    if not isinstance(profile, dict):
        raise ProfileError("common profile must be a mapping", "")
    if profile.get("profile_version") != COMMON_PROFILE_VERSION:
        raise ProfileError(
            f"profile_version must be {COMMON_PROFILE_VERSION!r}", "/profile_version"
        )
    if profile.get("judge_protocol") != FROZEN_JUDGE_PROTOCOL:
        raise ProfileError(
            f"common judge_protocol must be {FROZEN_JUDGE_PROTOCOL!r}", "/judge_protocol"
        )
    dims = profile.get("dimensions")
    if not isinstance(dims, list) or not dims:
        raise ProfileError("common profile must define a non-empty dimensions array", "/dimensions")
    actual_names = [d.get("name") if isinstance(d, dict) else None for d in dims]
    if set(actual_names) != set(FROZEN_DIMENSION_NAMES):
        raise ProfileError(
            f"dimension names must be {FROZEN_DIMENSION_NAMES!r}, got {actual_names!r}",
            "/dimensions",
        )
    for i, dim in enumerate(dims):
        if not isinstance(dim, dict):
            raise ProfileError(f"dimension entry {i} must be a mapping", f"/dimensions/{i}")
        name = dim.get("name")
        weight = dim.get("weight")
        if name not in FROZEN_DIMENSION_WEIGHTS or FROZEN_DIMENSION_WEIGHTS[name] != weight:
            raise ProfileError(
                f"dimension {name!r} weight must be "
                f"{FROZEN_DIMENSION_WEIGHTS.get(name)!r}, got {weight!r}",
                f"/dimensions/{i}/weight",
            )
    weights = [d.get("weight", 0) for d in dims]
    if sum(weights) != 100:
        raise ProfileError(f"dimension weights sum to {sum(weights)}, not 100", "/dimensions")
    if profile.get("credit_set") != FROZEN_CREDIT_SET:
        raise ProfileError(
            f"credit_set must be {FROZEN_CREDIT_SET!r}, got {profile.get('credit_set')!r}",
            "/credit_set",
        )


def validate_task_profile(profile: dict[str, Any], common: dict[str, Any]) -> None:
    """Validate a task profile against the common profile.

    A task profile MUST NOT override the common Judge output protocol or
    redefine dimension weights, its ``dimension_semantics`` keys must match the
    common dimensions, and its ``critical_error_codes`` must be a subset of the
    common cap codes (design §6.2, DEC-001 #4).
    """
    if not isinstance(profile, dict):
        raise ProfileError("task profile must be a mapping", "")
    if profile.get("judge_protocol") != common.get("judge_protocol"):
        raise ProfileError(
            "task profile must not override common judge_protocol", "/judge_protocol"
        )
    if "dimensions" in profile:
        raise ProfileError("task profile must not redefine dimension weights", "/dimensions")
    semantics = profile.get("dimension_semantics")
    if isinstance(semantics, dict):
        common_names = [d["name"] for d in common.get("dimensions", []) if isinstance(d, dict)]
        if list(semantics) != common_names:
            raise ProfileError(
                "dimension_semantics keys must match common dimensions in order",
                "/dimension_semantics",
            )
    common_codes = {c["code"] for c in common.get("critical_caps", []) if isinstance(c, dict)}
    for i, code in enumerate(profile.get("critical_error_codes", [])):
        if code not in common_codes:
            raise ProfileError(
                f"undeclared critical_error_code {code!r}",
                f"/critical_error_codes/{i}",
            )


def _identity_for_filename(filename: str) -> dict[str, str] | None:
    """Return the frozen identity block for a profile filename stem, or None."""
    for info in TASK_PROFILE_MAP.values():
        if info["filename"] == filename:
            return info
    return None


def validate_profile_identity(profile: dict[str, Any], filename: str | None = None) -> None:
    """Validate the 1:1 mapping among task_type, scoring_profile and profile_version.

    Each task Profile explicitly declares its frozen underscore ``scoring_profile``
    id, ``task_type`` and hyphenated ``profile_version``; the three cross-derive
    from one another so a mismatch in any one is caught with a locatable pointer
    (design §6, AIS-002 R4).

    When ``filename`` (the YAML stem) is given it is the identity anchor - the
    file name declares which profile it is, so every declared field is checked
    against the filename-derived expectation and a misnamed file is caught. When
    no filename is given the declared ``task_type`` anchors the cross-derivation.
    """
    if not isinstance(profile, dict):
        raise ProfileError("task profile must be a mapping", "")
    task_type = profile.get("task_type")
    scoring_profile = profile.get("scoring_profile")
    profile_version = profile.get("profile_version")

    if filename is not None:
        expected = _identity_for_filename(filename)
        if expected is None:
            raise ProfileError(f"unknown profile filename {filename!r}", "/profile_version")
    else:
        if task_type not in TASK_PROFILE_MAP:
            raise ProfileError(f"task_type {task_type!r} is not a frozen task type", "/task_type")
        expected = TASK_PROFILE_MAP[task_type]

    if task_type != expected["task_type"]:
        raise ProfileError(
            f"task_type {task_type!r} != expected {expected['task_type']!r}",
            "/task_type",
        )
    if scoring_profile != expected["scoring_profile"]:
        raise ProfileError(
            f"scoring_profile {scoring_profile!r} != expected {expected['scoring_profile']!r}",
            "/scoring_profile",
        )
    if profile_version != expected["profile_version"]:
        raise ProfileError(
            f"profile_version {profile_version!r} != expected {expected['profile_version']!r}",
            "/profile_version",
        )
    # Cross-derive to enforce a one-to-one mapping among all identifiers.
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


def load_validated_task_profile(
    task_type: str, profile_dir: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load, validate and return ``(task_profile, common_profile)`` for ``task_type``.

    Both the common and task profiles are validated, and the task profile's
    identity mapping is cross-checked against its on-disk filename. The returned
    pair can be passed directly to
    :func:`scoring.rubric_validator.validate_rubric`.
    """
    common = load_common_profile(profile_dir)
    validate_common_profile(common)
    task = load_task_profile(task_type, profile_dir)
    validate_task_profile(task, common)
    validate_profile_identity(task, TASK_PROFILE_MAP[task_type]["filename"])
    return task, common
