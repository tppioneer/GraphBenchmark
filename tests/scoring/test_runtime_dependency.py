"""AIS-004 R2: runtime dependency declarations.

``scoring.rubric_validator`` imports ``jsonschema`` and ``scoring.profiles``
imports ``yaml`` (PyYAML) in production code, so both must be declared in
``[project].dependencies`` (installed by default) rather than only in the
``dev`` optional-dependency extra. pytest and ruff are genuinely test/dev-only
and remain in the dev extra. This is the declarative companion to the isolated
wheel test in ``test_packaging.py``, which proves the deps are actually needed
at runtime by exercising them from an installed wheel.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PYPROJECT = REPO_ROOT / "pyproject.toml"


def _project() -> dict:
    """Load the ``[project]`` table from pyproject.toml via stdlib tomllib."""
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh).get("project", {})


_DEP_NAME_RE = re.compile(r"([A-Za-z0-9_.-]+)")


def _dep_name(dep_spec: str) -> str:
    """Normalize a PEP 508 dependency spec to its lowercase distribution name."""
    match = _DEP_NAME_RE.match(dep_spec)
    return match.group(1).lower().replace("_", "-") if match else dep_spec.lower()


def test_jsonschema_and_pyyaml_are_runtime_dependencies() -> None:
    """jsonschema and PyYAML are both imported by production ``scoring`` code
    (AIS-004 R2), so they must be runtime dependencies."""
    deps = {_dep_name(d) for d in _project().get("dependencies", [])}
    assert "jsonschema" in deps, deps
    assert "pyyaml" in deps, deps


def test_test_only_tools_remain_in_dev_extra() -> None:
    """pytest and ruff stay in the dev extra (AIS-004 R2)."""
    dev = _project().get("optional-dependencies", {}).get("dev", [])
    names = {_dep_name(d) for d in dev}
    assert {"pytest", "ruff"} <= names, names
