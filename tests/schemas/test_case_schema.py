"""case.schema.json positive and negative tests."""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("case").is_valid(ex.FULL_CASE)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("case").is_valid(ex.MINIMAL_CASE)


def test_unknown_field_rejected(make_validator) -> None:
    v = make_validator("case")
    bad = {**ex.MINIMAL_CASE, "extra_field": "forbidden"}
    errors = list(v.iter_errors(bad))
    assert errors
    pointers = {json_pointer(e) for e in errors}
    assert "/" in pointers
    assert any("extra_field" in e.message for e in errors)


def test_bad_task_type_rejected(make_validator) -> None:
    v = make_validator("case")
    bad = {**ex.MINIMAL_CASE, "task_type": "refactor"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/task_type" in {json_pointer(e) for e in errors}


def test_missing_question_rejected(make_validator) -> None:
    v = make_validator("case")
    bad = {k: v_ for k, v_ in ex.MINIMAL_CASE.items() if k != "question"}
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("question" in e.message and "required" in e.message for e in errors)
