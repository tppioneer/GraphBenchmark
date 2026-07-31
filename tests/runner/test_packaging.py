"""AIS-003 R2: the production validator must work from an installed wheel.

The agent-answer JSON Schema is a runtime resource: it must ship inside the
wheel (declared as ``schemas`` package data) and be locatable through an
installed-package-safe mechanism (``importlib.resources``), so that calling
the production validator from an install that does NOT include the source
checkout succeeds rather than raising ``FileNotFoundError``.

This test builds a real wheel, installs it (without dependencies) into an
isolated directory, and runs the production validator in a subprocess whose
interpreter cannot see the source checkout: ``-S`` skips ``site`` (so the
venv's editable-install finder never runs) and ``PYTHONPATH`` exposes only the
isolated install plus the ``jsonschema`` already present in the test
environment. The subprocess asserts that ``runner.artifact_validation`` and
the ``schemas`` resource are imported from the isolated install, not the
source tree, and then runs Agent Answer validation end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A schema-valid, contract-valid completed agent answer (bug_localization).
_VALID_AGENT_ANSWER = {
    "schema_version": "agent-answer-v1",
    "case_id": "pkg-case-bug-localization",
    "task_type": "bug_localization",
    "status": "completed",
    "answer": {
        "summary": "损坏的 inbox JSON 在共享读取函数中触发解析异常。",
        "explanation": "列表接口与 append 路径依赖同一读取函数，损坏文件阻断后续写入。",
        "findings": [
            {
                "id": "finding-1",
                "kind": "root_cause",
                "claim": "_load_events 未处理损坏 JSON，是直接根因。",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "limitations": [],
        "recommended_actions": ["增加损坏 JSON 的恢复与隔离策略。"],
    },
    "evidence": [
        {
            "id": "evidence-1",
            "file": "src/qwenpaw/app/inbox_store.py",
            "symbol": "_load_events",
            "line": 42,
            "reason": "该函数直接调用 json.loads，且未处理解析异常。",
        }
    ],
}

# Valid JSON object that violates the agent-answer schema (empty case_id).
_INVALID_AGENT_ANSWER = {
    "schema_version": "agent-answer-v1",
    "case_id": "",
    "task_type": "bug_localization",
    "status": "completed",
    "answer": {"summary": "s", "explanation": "e"},
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
    runtime_deps = os.path.abspath(os.environ["GRAPHBENCHMARK_RUNTIME_DEPS"])

    # ``-S`` skipped site.py, so the editable-install finder is absent and the
    # source checkout is not on sys.path. Belt-and-braces: drop any entry that
    # points at the source checkout and ensure the isolated install is first.
    sys.path[:] = [isolated] + [
        p
        for p in sys.path
        if p
        and os.path.abspath(p) != repo
        and (
            not os.path.abspath(p).startswith(repo + os.sep)
            or os.path.abspath(p) == runtime_deps
        )
    ]

    import runner.artifact_validation as av
    import schemas
    from importlib.resources import files

    av_file = os.path.abspath(av.__file__)
    schemas_dir = os.path.abspath(os.path.dirname(schemas.__file__))
    assert av_file.startswith(isolated + os.sep), (
        f"runner.artifact_validation came from outside the isolated install: {av_file}"
    )
    assert schemas_dir.startswith(isolated + os.sep), (
        f"schemas came from outside the isolated install: {schemas_dir}"
    )

    schema_resource = files("schemas").joinpath("agent-answer.schema.json")
    assert schema_resource.is_file(), (
        f"agent-answer.schema.json missing from installed schemas package at {schemas_dir}"
    )

    with open(valid_path, encoding="utf-8") as fh:
        valid = json.load(fh)
    with open(invalid_path, encoding="utf-8") as fh:
        invalid = json.load(fh)

    # Valid doc -> no contract issues; assert form does not raise.
    assert av.agent_answer_contract_issues(valid) == [], "valid doc rejected"
    av.assert_agent_answer_contract(valid)

    # Invalid doc -> schema issues reported.
    issues = av.agent_answer_schema_issues(invalid)
    assert issues, "invalid doc not flagged by installed validator"

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


def _jsonschema_site_packages() -> Path:
    """Directory containing the installed ``jsonschema`` (and its deps)."""
    import jsonschema

    return Path(jsonschema.__file__).resolve().parent.parent


def test_installed_wheel_runs_agent_answer_validation(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "wheel")

    # The wheel must carry the schema resource and the schemas package marker.
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "runner/artifact_validation.py" in names, names
    assert "schemas/__init__.py" in names, names
    assert "schemas/agent-answer.schema.json" in names, names

    isolated = tmp_path / "install"
    _install_wheel(wheel, isolated)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    script_path = run_dir / "validate_installed.py"
    script_path.write_text(_RUNNER_SCRIPT, encoding="utf-8")
    valid_path = run_dir / "valid.json"
    invalid_path = run_dir / "invalid.json"
    valid_path.write_text(
        json.dumps(_VALID_AGENT_ANSWER, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    invalid_path.write_text(
        json.dumps(_INVALID_AGENT_ANSWER, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ``-S`` skips site.py, so the venv's editable-install finder (which would
    # redirect ``import runner`` to the source checkout) never runs. PYTHONPATH
    # exposes only the isolated install plus jsonschema's site-packages.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(isolated), str(_jsonschema_site_packages())])
    env["GRAPHBENCHMARK_RUNTIME_DEPS"] = str(_jsonschema_site_packages())
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
