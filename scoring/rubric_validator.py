"""Deterministic Ground Truth rubric validation (design §7.2).

Validates a GT rubric against a scoring Profile BEFORE any Judge call. Every
violation is collected and reported at once with an actionable error code, an
RFC 6901 JSON Pointer (path) and, where applicable, the offending rubric item
id (acceptance criterion 4). The issue list is sorted deterministically and
does not depend on dict or set iteration order (acceptance criterion 5).

The nine §7.2 rules and their error codes:

1. case_id / task_type / scoring_profile exist and are valid
   - GT_CASE_ID_MISSING, GT_TASK_TYPE_INVALID, GT_SCORING_PROFILE_INVALID
2. each rubric item has a stable, unique id
   - ITEM_ID_MISSING, ITEM_ID_DUPLICATE
3. each item belongs to one known first-level dimension
   - DIMENSION_UNKNOWN
4. each item's ``points`` is a positive number
   - POINTS_NOT_POSITIVE
5. each dimension's points sum equals the Profile weight
   - DIMENSION_POINTS_MISMATCH
6. total of all item points equals 100
   - TOTAL_POINTS_MISMATCH
7. critical items define a clear zero-credit condition
   - CRITICAL_ZERO_CREDIT_MISSING
8. referenced files / symbols / line ranges satisfy the schema
   - REFERENCE_INVALID
9. GT contains no field leaking strategy or answer source
   - GT_LEAK_DETECTED

Structural validation (additionalProperties, required fields, number bounds) is
performed by the JSON Schemas (AIS-002); this module performs the business
validation that schemas cannot express (cross-field sums, cross-document
identity, leak-field defence-in-depth).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from scoring.profiles import (
    FROZEN_DIMENSION_NAMES,
    FROZEN_DIMENSION_WEIGHTS,
    SCORING_PROFILES,
    TASK_TYPES,
    dimension_weights,
    load_validated_task_profile,
)

# ---------------------------------------------------------------------------
# Error codes (see the §7.2 mapping table in the module docstring).
# ---------------------------------------------------------------------------

GT_CASE_ID_MISSING = "GT_CASE_ID_MISSING"
GT_TASK_TYPE_INVALID = "GT_TASK_TYPE_INVALID"
GT_SCORING_PROFILE_INVALID = "GT_SCORING_PROFILE_INVALID"
ITEM_ID_MISSING = "ITEM_ID_MISSING"
ITEM_ID_DUPLICATE = "ITEM_ID_DUPLICATE"
DIMENSION_UNKNOWN = "DIMENSION_UNKNOWN"
POINTS_NOT_POSITIVE = "POINTS_NOT_POSITIVE"
DIMENSION_POINTS_MISMATCH = "DIMENSION_POINTS_MISMATCH"
TOTAL_POINTS_MISMATCH = "TOTAL_POINTS_MISMATCH"
CRITICAL_ZERO_CREDIT_MISSING = "CRITICAL_ZERO_CREDIT_MISSING"
REFERENCE_INVALID = "REFERENCE_INVALID"
GT_LEAK_DETECTED = "GT_LEAK_DETECTED"
RUBRIC_ITEMS_MISSING = "RUBRIC_ITEMS_MISSING"

#: All rubric error codes, in §7.2 rule order.
ERROR_CODES: tuple[str, ...] = (
    GT_CASE_ID_MISSING,
    GT_TASK_TYPE_INVALID,
    GT_SCORING_PROFILE_INVALID,
    ITEM_ID_MISSING,
    ITEM_ID_DUPLICATE,
    DIMENSION_UNKNOWN,
    POINTS_NOT_POSITIVE,
    DIMENSION_POINTS_MISMATCH,
    TOTAL_POINTS_MISMATCH,
    CRITICAL_ZERO_CREDIT_MISSING,
    REFERENCE_INVALID,
    GT_LEAK_DETECTED,
    RUBRIC_ITEMS_MISSING,
)

#: Field names that must never appear in a GT document. Their presence leaks the
#: experiment group, the agent or model identity, the tool policy, or Runner
#: process/cost data that the Judge must not see (design §9.2). The GT JSON
#: Schema enforces ``additionalProperties: false``; this set is a deterministic
#: defence-in-depth check so a leak is still caught if schema validation is
#: bypassed or the schema is widened.
LEAK_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "agent_model",
        "tool_policy",
        "tool_calls",
        "tool_call_count",
        "files_read_count",
        "graph_query_count",
        "search_query_count",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "metrics",
        "violations",
        "judge_model",
        "judge_requested_model",
        "judge_provider",
        "judge_cli_version",
        "policy_enforced",
        "credential_source",
        "credentials_present",
    }
)


@dataclass(frozen=True)
class RubricIssue:
    """A single rubric validation problem with an actionable location.

    ``pointer`` is an RFC 6901 JSON Pointer into the GT document. ``item_id`` is
    the rubric item id when the problem is item-scoped, or ``None`` for
    document-level or aggregate problems.
    """

    code: str
    message: str
    pointer: str
    item_id: str | None = None

    def __str__(self) -> str:
        loc = f" (item {self.item_id!r})" if self.item_id else ""
        return f"{self.pointer}: [{self.code}] {self.message}{loc}"


class RubricValidationError(Exception):
    """Raised when a GT rubric fails validation. Holds all collected issues."""

    def __init__(self, issues: list[RubricIssue]) -> None:
        self.issues: list[RubricIssue] = list(issues)
        detail = "; ".join(str(i) for i in self.issues)
        super().__init__(f"rubric validation failed with {len(self.issues)} issue(s): {detail}")

    @property
    def codes(self) -> list[str]:
        return [i.code for i in self.issues]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    """Convert an int/float (not bool) to an exact Decimal; return None otherwise.

    ``Decimal(str(value))`` avoids binary float drift so that integer and
    one-decimal points sum exactly (design §10.1).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return Decimal(str(value))


def _resolve_weights(common_profile: dict[str, Any] | None) -> dict[str, int]:
    """Dimension weights from the common profile, falling back to the frozen set."""
    weights: dict[str, int] = (
        dimension_weights(common_profile) if isinstance(common_profile, dict) else {}
    )
    for name, weight in FROZEN_DIMENSION_WEIGHTS.items():
        weights.setdefault(name, weight)
    return weights


def _find_leak_fields(obj: Any, pointer: str, issues: list[RubricIssue]) -> None:
    """Recursively scan the GT for forbidden leak field names (rule 9)."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{pointer}/{key}"
            if key in LEAK_FIELD_NAMES:
                issues.append(
                    RubricIssue(
                        code=GT_LEAK_DETECTED,
                        message=(
                            f"forbidden leak field {key!r} exposes experiment or "
                            f"answer-source information"
                        ),
                        pointer=child,
                    )
                )
            _find_leak_fields(val, child, issues)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            _find_leak_fields(val, f"{pointer}/{i}", issues)


def _validate_reference(
    ref: Any, pointer: str, item_id: str | None, issues: list[RubricIssue]
) -> None:
    """Validate a single reference entry: symbol, file and optional line range."""
    if not isinstance(ref, dict):
        issues.append(
            RubricIssue(
                code=REFERENCE_INVALID,
                message="reference must be a mapping",
                pointer=pointer,
                item_id=item_id,
            )
        )
        return
    symbol = ref.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        issues.append(
            RubricIssue(
                code=REFERENCE_INVALID,
                message="reference symbol is missing or empty",
                pointer=f"{pointer}/symbol",
                item_id=item_id,
            )
        )
    file = ref.get("file")
    if not isinstance(file, str) or not file.strip():
        issues.append(
            RubricIssue(
                code=REFERENCE_INVALID,
                message="reference file is missing or empty",
                pointer=f"{pointer}/file",
                item_id=item_id,
            )
        )
    lines = ref.get("lines")
    if lines is not None:
        if not isinstance(lines, list) or len(lines) != 2:
            issues.append(
                RubricIssue(
                    code=REFERENCE_INVALID,
                    message="reference lines must be a [start, end] pair",
                    pointer=f"{pointer}/lines",
                    item_id=item_id,
                )
            )
        elif not all(isinstance(ln, int) and not isinstance(ln, bool) for ln in lines):
            issues.append(
                RubricIssue(
                    code=REFERENCE_INVALID,
                    message="reference lines must be integers",
                    pointer=f"{pointer}/lines",
                    item_id=item_id,
                )
            )
        elif lines[0] > lines[1]:
            issues.append(
                RubricIssue(
                    code=REFERENCE_INVALID,
                    message=f"reference lines start {lines[0]} is greater than end {lines[1]}",
                    pointer=f"{pointer}/lines",
                    item_id=item_id,
                )
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_rubric(
    ground_truth: dict[str, Any],
    task_profile: dict[str, Any],
    common_profile: dict[str, Any],
) -> list[RubricIssue]:
    """Validate a GT rubric against a task and common profile.

    Returns a list of every :class:`RubricIssue` found (empty means valid). The
    list is sorted deterministically by ``(pointer, code, item_id, message)`` so
    the result is stable and independent of dict or set iteration order
    (acceptance criterion 5). All actionable problems are reported in a single
    pass rather than failing on the first one (acceptance criterion 4).

    The caller is expected to have validated the profiles first via
    :func:`scoring.profiles.load_validated_task_profile`; this function cross-
    checks the GT's ``task_type`` / ``scoring_profile`` against the task profile
    (rule 1) and reads dimension weights from the common profile (rule 5).
    """
    issues: list[RubricIssue] = []

    if not isinstance(ground_truth, dict):
        issues.append(
            RubricIssue(
                code=RUBRIC_ITEMS_MISSING, message="ground truth must be a mapping", pointer=""
            )
        )
        return issues

    # ---- Rule 1: case_id / task_type / scoring_profile exist and are valid. --
    case_id = ground_truth.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        issues.append(
            RubricIssue(
                code=GT_CASE_ID_MISSING,
                message="case_id is missing or empty",
                pointer="/case_id",
            )
        )

    gt_task_type = ground_truth.get("task_type")
    if gt_task_type not in TASK_TYPES:
        issues.append(
            RubricIssue(
                code=GT_TASK_TYPE_INVALID,
                message=f"task_type {gt_task_type!r} is not a known task type",
                pointer="/task_type",
            )
        )
    elif task_profile.get("task_type") != gt_task_type:
        issues.append(
            RubricIssue(
                code=GT_TASK_TYPE_INVALID,
                message=(
                    f"GT task_type {gt_task_type!r} != profile task_type "
                    f"{task_profile.get('task_type')!r}"
                ),
                pointer="/task_type",
            )
        )

    gt_profile = ground_truth.get("scoring_profile")
    if gt_profile not in SCORING_PROFILES:
        issues.append(
            RubricIssue(
                code=GT_SCORING_PROFILE_INVALID,
                message=f"scoring_profile {gt_profile!r} is not a frozen profile id",
                pointer="/scoring_profile",
            )
        )
    elif task_profile.get("scoring_profile") != gt_profile:
        issues.append(
            RubricIssue(
                code=GT_SCORING_PROFILE_INVALID,
                message=(
                    f"GT scoring_profile {gt_profile!r} != profile scoring_profile "
                    f"{task_profile.get('scoring_profile')!r}"
                ),
                pointer="/scoring_profile",
            )
        )

    # ---- rubric_items must be a non-empty array. -----------------------------
    items = ground_truth.get("rubric_items")
    if not isinstance(items, list) or not items:
        issues.append(
            RubricIssue(
                code=RUBRIC_ITEMS_MISSING,
                message="rubric_items must be a non-empty array",
                pointer="/rubric_items",
            )
        )
        items = []

    # ---- Rule 2: stable, unique ids. ----------------------------------------
    seen_ids: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(
                RubricIssue(
                    code=ITEM_ID_MISSING,
                    message=f"rubric item at index {i} must be a mapping",
                    pointer=f"/rubric_items/{i}",
                )
            )
            continue
        iid = item.get("id")
        if not isinstance(iid, str) or not iid.strip():
            issues.append(
                RubricIssue(
                    code=ITEM_ID_MISSING,
                    message="item id is missing or empty",
                    pointer=f"/rubric_items/{i}/id",
                )
            )
        else:
            seen_ids.setdefault(iid, []).append(i)

    for iid, indices in seen_ids.items():
        if len(indices) > 1:
            # Flag every occurrence after the first so every duplicate location
            # is actionable and the count is stable regardless of order.
            for idx in indices[1:]:
                issues.append(
                    RubricIssue(
                        code=ITEM_ID_DUPLICATE,
                        message=f"item id {iid!r} is duplicated",
                        pointer=f"/rubric_items/{idx}/id",
                        item_id=iid,
                    )
                )

    # ---- Rules 3, 4, 7, 8: per-item checks. ---------------------------------
    dim_sums: dict[str, Decimal] = {name: Decimal(0) for name in FROZEN_DIMENSION_NAMES}
    total = Decimal(0)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        iid = item.get("id") if isinstance(item.get("id"), str) else None
        base_ptr = f"/rubric_items/{i}"

        # Rule 3: known first-level dimension.
        dim = item.get("dimension")
        if dim not in FROZEN_DIMENSION_NAMES:
            issues.append(
                RubricIssue(
                    code=DIMENSION_UNKNOWN,
                    message=f"dimension {dim!r} is not a known first-level dimension",
                    pointer=f"{base_ptr}/dimension",
                    item_id=iid,
                )
            )

        # Rule 4: positive points.
        points = item.get("points")
        pts_dec = _to_decimal(points)
        if pts_dec is None or pts_dec <= 0:
            issues.append(
                RubricIssue(
                    code=POINTS_NOT_POSITIVE,
                    message=f"points must be a positive number, got {points!r}",
                    pointer=f"{base_ptr}/points",
                    item_id=iid,
                )
            )

        # Accumulate exact sums for rules 5 and 6 (only valid positive numbers).
        if pts_dec is not None and pts_dec > 0:
            total += pts_dec
            if dim in dim_sums:
                dim_sums[dim] += pts_dec

        # Rule 7: critical items must define a clear zero-credit condition.
        if item.get("critical") is True:
            zero = item.get("zero_credit")
            if not isinstance(zero, str) or not zero.strip():
                issues.append(
                    RubricIssue(
                        code=CRITICAL_ZERO_CREDIT_MISSING,
                        message="critical item must define a non-empty zero_credit condition",
                        pointer=f"{base_ptr}/zero_credit",
                        item_id=iid,
                    )
                )

        # Rule 8: references satisfy the schema.
        refs = item.get("references")
        if refs is not None:
            if not isinstance(refs, list):
                issues.append(
                    RubricIssue(
                        code=REFERENCE_INVALID,
                        message="references must be an array",
                        pointer=f"{base_ptr}/references",
                        item_id=iid,
                    )
                )
            else:
                for j, ref in enumerate(refs):
                    _validate_reference(ref, f"{base_ptr}/references/{j}", iid, issues)

    # ---- Rule 9: no leak fields (recursive defence-in-depth scan). ----------
    _find_leak_fields(ground_truth, "", issues)

    # ---- Rule 5: per-dimension point sums equal Profile weights. ------------
    weights = _resolve_weights(common_profile)
    for name in FROZEN_DIMENSION_NAMES:
        expected = Decimal(str(weights.get(name, FROZEN_DIMENSION_WEIGHTS[name])))
        if dim_sums[name] != expected:
            issues.append(
                RubricIssue(
                    code=DIMENSION_POINTS_MISMATCH,
                    message=(
                        f"dimension {name!r} points sum to {dim_sums[name]}, expected {expected}"
                    ),
                    pointer="/rubric_items",
                )
            )

    # ---- Rule 6: total of all item points equals 100. -----------------------
    if total != Decimal(100):
        issues.append(
            RubricIssue(
                code=TOTAL_POINTS_MISMATCH,
                message=f"total points sum to {total}, expected 100",
                pointer="/rubric_items",
            )
        )

    # Deterministic ordering (acceptance criterion 5): sort by pointer, then
    # code, then item_id, then message. This makes the result independent of
    # dict/set iteration order while keeping item/path locations stable.
    issues.sort(key=lambda x: (x.pointer, x.code, x.item_id or "", x.message))
    return issues


def validate_rubric_or_raise(
    ground_truth: dict[str, Any],
    task_profile: dict[str, Any],
    common_profile: dict[str, Any],
) -> None:
    """Validate a GT rubric, raising :class:`RubricValidationError` on any issue."""
    issues = validate_rubric(ground_truth, task_profile, common_profile)
    if issues:
        raise RubricValidationError(issues)


def validate_profile_and_rubric(
    ground_truth: dict[str, Any],
    task_type: str | None = None,
    profile_dir: Path | None = None,
) -> list[RubricIssue]:
    """Load and validate the Profile, then validate the GT rubric against it.

    This is the top-level entry point meant to run before any Judge call
    (design §7.2). The Profile is loaded from ``profile_dir`` (default
    ``profiles/``) and fully validated via
    :func:`scoring.profiles.load_validated_task_profile`; if it is invalid a
    :class:`scoring.profiles.ProfileError` is raised. The GT rubric is then
    validated and its issues returned (empty means valid).
    """
    if task_type is None:
        task_type = ground_truth.get("task_type")
    task_profile, common_profile = load_validated_task_profile(task_type, profile_dir)
    return validate_rubric(ground_truth, task_profile, common_profile)


def issue_counter(issues: list[RubricIssue]) -> Counter[str]:
    """Return a multiset of error codes (handy for order-independent assertions)."""
    return Counter(i.code for i in issues)
