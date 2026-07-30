"""ground-truth.schema.json positive and negative tests."""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("ground-truth").is_valid(ex.FULL_GT)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("ground-truth").is_valid(ex.MINIMAL_GT)


def test_bad_dimension_rejected(make_validator) -> None:
    v = make_validator("ground-truth")
    bad = dict(ex.MINIMAL_GT)
    bad["rubric_items"][0]["dimension"] = "not_a_dimension"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/rubric_items/0/dimension" in {json_pointer(e) for e in errors}


def test_non_positive_points_rejected(make_validator) -> None:
    v = make_validator("ground-truth")
    bad = dict(ex.MINIMAL_GT)
    bad["rubric_items"][0]["points"] = 0
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/rubric_items/0/points" in {json_pointer(e) for e in errors}


def test_rubric_item_unknown_field_rejected(make_validator) -> None:
    v = make_validator("ground-truth")
    bad = dict(ex.MINIMAL_GT)
    bad["rubric_items"][0]["scoring_hint"] = "leak"
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/rubric_items/0" in {json_pointer(e) for e in errors}
