"""AIS-003 R2: jsonschema is a runtime dependency, not a dev-only tool.

``runner.artifact_validation`` imports ``jsonschema`` in production code, so it
must be declared in ``[project].dependencies`` (installed by default) rather
than only in the ``dev`` optional-dependency extra. pytest and ruff are
genuinely test-only and remain in the dev extra. PyYAML is intentionally not
asserted here: AIS-004 loads Profile YAML in production, so PyYAML may be
promoted to a runtime dependency by that task, and AIS-003 tests must not
contradict AIS-004's production use of PyYAML.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# Importing the production module proves jsonschema is installed for runtime
# use, not merely as a test convenience.
import runner.artifact_validation  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]


def _dep_name(spec: str) -> str:
    return spec.split(">=")[0].split("==")[0].strip().lower()


def test_jsonschema_is_runtime_dependency() -> None:
    """jsonschema is declared in [project].dependencies (AIS-003 R2)."""
    names = {_dep_name(d) for d in _project().get("dependencies", [])}
    assert "jsonschema" in names, "jsonschema must be a runtime dependency"


def test_test_only_tools_remain_in_dev_extra() -> None:
    """pytest and ruff stay in the dev extra (AIS-003 R2)."""
    dev = _project().get("optional-dependencies", {}).get("dev", [])
    names = {_dep_name(d) for d in dev}
    assert {"pytest", "ruff"} <= names
