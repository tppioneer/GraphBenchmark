"""Acceptance criterion 1: all eight schemas exist and lock $schema, $id and a
business version field; each is a well-formed Draft 2020-12 schema."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from ._validators import SCHEMA_DIR, SCHEMA_NAMES, load_schema


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_file_present(name: str) -> None:
    assert (SCHEMA_DIR / f"{name}.schema.json").exists()


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft2020_12(name: str) -> None:
    Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_locks_schema_id_and_business_version(name: str) -> None:
    schema = load_schema(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"https://graphbenchmark.dev/schemas/{name}.schema.json"
    version_field = schema["properties"]["schema_version"]
    assert "const" in version_field, f"{name} must lock schema_version as a const"
    assert version_field["const"] == f"{name}-v1"
