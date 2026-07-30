"""Shared pytest fixtures for schema validation tests.

A stdlib-based ``date-time`` format checker is registered so the ``format``
annotations on run-metadata are actually enforced without pulling in the
optional ``rfc3339-validator`` dependency.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ._validators import load_schema

_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_date_time(value: object) -> bool:
    """True for valid ISO-8601/RFC-3339 date-time strings."""
    if not isinstance(value, str):
        return True  # type constraints are enforced separately
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


@pytest.fixture
def make_validator():
    """Return a factory that builds a Draft 2020-12 validator with format checks."""

    def _make(name: str) -> Draft202012Validator:
        return Draft202012Validator(load_schema(name), format_checker=_FORMAT_CHECKER)

    return _make
