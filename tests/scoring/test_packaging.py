"""AIS-004 R2: the production rubric validator must work from an installed wheel.

The Ground Truth JSON Schema and the Profile YAML files are runtime resources:
they must ship inside the wheel (declared as ``schemas`` and ``profiles``
package data) and be locatable through an installed-package-safe mechanism
(``importlib.resources``), so that calling the production validator from an
install that does NOT include the source checkout succeeds rather than raising
``FileNotFoundError``.

This test builds a real wheel, installs it (without dependencies) into an
isolated directory, and runs the production validator in a subprocess whose
interpreter cannot see the source checkout: ``-S`` skips ``site`` (so the
venv's editable-install finder never runs) and ``PYTHONPATH`` exposes only the
isolated install plus the runtime deps (``jsonschema`` and ``PyYAML``) already
present in the test environment. The subprocess asserts that
``scoring.rubric_validator``, ``scoring.profiles`` and the ``schemas`` /
``profiles`` resources are imported from the isolated install, loads a Profile
through the installed-package-safe loader, and validates both a valid and an
invalid GT end-to-end - without touching the source checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

from scoring import profiles as prof

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A schema-valid, contract-valid bug_localization GT (one item per dimension,
# points summing to 100) built only from the frozen Profile constants so the
# subprocess does not need anything from the source checkout.
_VALID_GT = {
    "schema_version": "ground-truth-v1",
    "case_id": "pkg-case-bug-localization",
    "task_type": "bug_localization",
    "scoring_profile": "bug_localization_v1",
    "rubric_items": [
        {
            "id": f"{dim}.main",
            "dimension": dim,
            "points": weight,
            "criterion": f"{dim} criterion.",
        }
        for dim, weight in prof.FROZEN_DIMENSIONS
    ],
}

# Valid JSON object that violates the ground-truth schema: a missing required
# ``schema_version`` plus an unexpected top-level field. Its rubric_items still
# sum to 100 so the only reported issues are structural Schema failures.
_INVALID_GT = {
    "case_id": "pkg-case-bug-localization",
    "task_type": "bug_localization",
    "scoring_profile": "bug_localization_v1",
    "rubric_items": [
        {
            "id": f"{dim}.main",
            "dimension": dim,
            "points": weight,
            "criterion": f"{dim} criterion.",
        }
        for dim, weight in prof.FROZEN_DIMENSIONS
    ],
    "unexpected_field": "boom",
}

# Run in the subprocess via a file (not ``python -c``) so cwd, not argv
# escaping, shapes sys.path and Unicode payloads ride in JSON files.
_RUNNER_SCRIPT = textwrap.dedent(
    """\
    import json
    import os
    import sys

    isolated = os.path.abspath(sys.argv[1])
    repo = os.path.abspath(sys.argv[2])
    valid_path = sys.argv[3]
    invalid_path = sys.argv[4]

    # ``-S`` skipped site.py, so the editable-install finder is absent and the
    # source checkout is not on sys.path. Belt-and-braces: drop any entry that
    # points at the source checkout and ensure the isolated install is first.
    sys.path[:] = [isolated] + [
        p
        for p in sys.path
        if p
        and os.path.abspath(p) != repo
        and not os.path.abspath(p).startswith(repo + os.sep)
    ]

    import scoring.profiles as profiles
    import scoring.rubric_validator as rv
    from importlib.resources import files

    prof_file = os.path.abspath(profiles.__file__)
    rv_file = os.path.abspath(rv.__file__)
    assert prof_file.startswith(isolated + os.sep), (
        f"scoring.profiles came from outside the isolated install: {prof_file}"
    )
    assert rv_file.startswith(isolated + os.sep), (
        f"scoring.rubric_validator came from outside the isolated install: {rv_file}"
    )

    # Runtime resources must ship inside the wheel, not be read from the source
    # checkout (AIS-004 R2).
    gt_schema = files("schemas").joinpath("ground-truth.schema.json")
    common_yaml = files("profiles").joinpath("common.yaml")
    task_yaml = files("profiles").joinpath("bug-localization-v1.yaml")
    assert gt_schema.is_file(), (
        f"ground-truth.schema.json missing from installed schemas at {gt_schema}"
    )
    assert common_yaml.is_file(), f"common.yaml missing from installed profiles at {common_yaml}"
    assert task_yaml.is_file(), (
        f"bug-localization-v1.yaml missing from installed profiles at {task_yaml}"
    )

    # Load a Profile through the installed-package-safe loader (AIS-004 R2).
    task, common = profiles.load_validated_task_profile("bug_localization")
    assert task["task_type"] == "bug_localization"
    assert common["profile_version"] == profiles.COMMON_PROFILE_VERSION

    with open(valid_path, encoding="utf-8") as fh:
        valid = json.load(fh)
    with open(invalid_path, encoding="utf-8") as fh:
        invalid = json.load(fh)

    # Valid GT -> no issues through the production entry point.
    assert rv.validate_profile_and_rubric(valid) == [], "valid GT rejected by installed validator"

    # Invalid GT -> deterministic issues reported (schema + business).
    issues = rv.validate_profile_and_rubric(invalid)
    assert issues, "invalid GT not flagged by installed validator"
    codes = [i.code for i in issues]
    assert rv.GT_SCHEMA_INVALID in codes, f"expected GT_SCHEMA_INVALID in {codes}"
    assert all(i.pointer for i in issues), "issue missing actionable pointer"

    print("PACKAGING_OK")
    """
)


def _build_wheel(wheel_dir: Path) -> Path:
    """Build the project wheel (build isolation supplies setuptools) and return it."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "-w",
            str(wheel_dir),
            str(REPO_ROOT),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _install_wheel(wheel: Path, target: Path) -> None:
    """Install the wheel into ``target`` with no deps and no index (offline)."""
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(target),
            str(wheel),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _runtime_site_packages() -> Path:
    """Directory holding the installed runtime deps (jsonschema and PyYAML).

    Both are runtime dependencies (AIS-004 R2): ``scoring.rubric_validator``
    imports ``jsonschema`` and ``scoring.profiles`` imports ``yaml``. The
    isolated wheel install (``--no-deps``) does not bring them, so the
    subprocess PYTHONPATH must expose this directory.
    """
    import jsonschema
    import yaml

    sp = Path(jsonschema.__file__).resolve().parent.parent
    # PyYAML and jsonschema must live in the same site-packages; if not, the
    # isolated subprocess would silently lack one of them.
    assert Path(yaml.__file__).resolve().parent.parent == sp, (
        "jsonschema and PyYAML are in different site-packages; "
        "isolated wheel test cannot expose both via one PYTHONPATH entry"
    )
    return sp


def test_installed_wheel_runs_rubric_validation(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "wheel")

    # The wheel must carry the runtime resources and the package markers.
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "scoring/rubric_validator.py" in names, names
    assert "scoring/profiles.py" in names, names
    assert "schemas/__init__.py" in names, names
    assert "schemas/ground-truth.schema.json" in names, names
    assert "profiles/__init__.py" in names, names
    assert "profiles/common.yaml" in names, names
    assert "profiles/bug-localization-v1.yaml" in names, names

    isolated = tmp_path / "install"
    _install_wheel(wheel, isolated)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    script_path = run_dir / "validate_installed.py"
    script_path.write_text(_RUNNER_SCRIPT, encoding="utf-8")
    valid_path = run_dir / "valid.json"
    invalid_path = run_dir / "invalid.json"
    valid_path.write_text(json.dumps(_VALID_GT, ensure_ascii=False, indent=2), encoding="utf-8")
    invalid_path.write_text(json.dumps(_INVALID_GT, ensure_ascii=False, indent=2), encoding="utf-8")

    # ``-S`` skips site.py, so the venv's editable-install finder (which would
    # redirect ``import scoring`` to the source checkout) never runs. PYTHONPATH
    # exposes only the isolated install plus the runtime-deps site-packages.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(isolated), str(_runtime_site_packages())])
    env.pop("PYTHONHOME", None)

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(script_path),
            str(isolated),
            str(REPO_ROOT),
            str(valid_path),
            str(invalid_path),
        ],
        cwd=str(run_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"installed-wheel validation subprocess failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "PACKAGING_OK" in result.stdout, result.stdout
