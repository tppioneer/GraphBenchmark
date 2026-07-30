"""manifest.schema.json positive and negative tests (R6).

The manifest is the ninth contract (design §17). A present artifact must carry
a repository-relative path and sha256 digest; non-present artifacts must not use
empty placeholder files; v1 adjudication is always not_applicable.
"""

from __future__ import annotations

from . import examples as ex
from ._validators import json_pointer


def test_full_valid(make_validator) -> None:
    assert make_validator("manifest").is_valid(ex.FULL_MANIFEST)


def test_minimal_valid(make_validator) -> None:
    assert make_validator("manifest").is_valid(ex.MINIMAL_MANIFEST)


def test_unknown_name_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_with_unknown_name()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/artifacts/0/name" in {json_pointer(e) for e in errors}


def test_unknown_status_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_with_unknown_status()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/artifacts/0/status" in {json_pointer(e) for e in errors}


def test_present_missing_path_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_present_missing_path()
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("path" in e.message and "required" in e.message for e in errors)


def test_present_missing_digest_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_present_missing_digest()
    errors = list(v.iter_errors(bad))
    assert errors
    assert any("sha256" in e.message and "required" in e.message for e in errors)


def test_present_invalid_path_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_present_invalid_path()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/artifacts/0/path" in {json_pointer(e) for e in errors}


def test_present_invalid_digest_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_present_invalid_digest()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/artifacts/0/sha256" in {json_pointer(e) for e in errors}


def test_adjudication_wrong_status_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_adjudication_wrong_status()
    errors = list(v.iter_errors(bad))
    assert errors
    assert "/artifacts/0/status" in {json_pointer(e) for e in errors}


def test_non_present_with_placeholder_rejected(make_validator) -> None:
    v = make_validator("manifest")
    bad = ex.manifest_non_present_with_placeholder()
    errors = list(v.iter_errors(bad))
    assert errors
    pointers = {json_pointer(e) for e in errors}
    assert "/artifacts/0" in pointers or any(p.startswith("/artifacts/0") for p in pointers)


def test_adjudication_not_applicable_valid(make_validator) -> None:
    v = make_validator("manifest")
    good = {
        "schema_version": "manifest-v1",
        "artifacts": [{"name": "adjudication", "status": "not_applicable"}],
    }
    assert v.is_valid(good)
