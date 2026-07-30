"""Shared pytest fixtures for schema validation tests.

A strict, stdlib-only RFC 3339 ``date-time`` format checker is registered so the
``format`` annotations on run-metadata are actually enforced without pulling in
the optional ``rfc3339-validator`` dependency. ``datetime.fromisoformat`` alone
is too lax (it accepts date-only and timezone-less values), so the string is
first matched against the RFC 3339 date-time shape and then range-validated.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ._validators import load_schema

_FORMAT_CHECKER = FormatChecker()

#: Strict RFC 3339 date-time shape: full-date "T" full-time with a mandatory
#: offset (Z or ±HH:MM). Rejects date-only and timezone-less values, which
#: ``datetime.fromisoformat`` would otherwise accept.
_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


@_FORMAT_CHECKER.checks("date-time")
def _is_strict_rfc3339_date_time(value: object) -> bool:
    """True for strict RFC 3339 date-time strings (full date-time with offset)."""
    if not isinstance(value, str):
        return True  # type constraints are enforced separately
    if not _RFC3339_DATE_TIME.match(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return False
    return True


@pytest.fixture
def make_validator():
    """Return a factory that builds a Draft 2020-12 validator with format checks."""

    def _make(name: str) -> Draft202012Validator:
        return Draft202012Validator(load_schema(name), format_checker=_FORMAT_CHECKER)

    return _make
