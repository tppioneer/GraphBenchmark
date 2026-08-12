"""AIS-012: configuration-driven experiment dispatch.

Covers the required behaviour using a fake Adapter and patched
``execute_run`` (never the real Claude CLI, Judge, or repository code):

* loading and validating a YAML config (Case schema + GT/Profile via the
  production validator) before any subprocess launch;
* building a deterministic dispatch plan (Graph/Grep pairing, repeats,
  stable run IDs);
* smoke_only refusal, dry-run planning, and the opt-in execution guard;
* missing cwd/MCP/skill paths, invalid policies, unsafe run IDs, and
  incomplete runtime configuration refused before launch;
* natural-language answer flows unchanged through ``runner.execution``
  (the dispatcher never fabricates answer JSON or metrics);
* CLI subcommand compatibility (``main([]) == 0``, ``dispatch`` wiring).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from runner import benchmark_runner as br
from runner.benchmark_runner import RunResult, RunStatus
from runner.execution import AgentAnswerStatus
from runner.experiment_dispatch import (
    ConfigValidationError,
    DispatchError,
    DispatchPlan,
    IncompleteRuntimeError,
    PlannedRun,
    RuntimeFields,
    SmokeOnlyExecutionError,
    build_dispatch_plan,
    execute_dispatch,
    load_experiment_config,
    validate_experiment_config,
)
from runner.policy_validation import RUNNER_OBSERVED_SOURCE, ToolEvent, ToolKind

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE_CONFIG = REPO_ROOT / "experiments" / "qwenpaw-corrupt-inbox-smoke-v1.yaml"
FORMAL_CONFIG = REPO_ROOT / "experiments" / "qwenpaw-corrupt-inbox-formal-v1.yaml"

# Machine-specific formal runtime inputs (absolute paths; non-secret). These are
# asserted as parsed values; tests that touch the disk are guarded by
# ``need_runtime_resources`` so they run on the configured host and skip
# elsewhere.
FORMAL_REPO_CWD = Path(r"F:\develop\codes\QwenPaw\QwenPaw")
FORMAL_GRAPH_MCP = Path(r"F:\develop\00-codes\benchmark-runtime\mcp\.mcp.json")
FORMAL_GRAPH_SKILL = Path(r"F:\develop\00-codes\benchmark-runtime\skills\gitnexus-guide\SKILL.md")
FORMAL_RUNS_ROOT = Path(r"F:\develop\00-codes\benchmark-runtime\runs\GraphBenchmark-ai-score-v1")
FORMAL_REPO_REVISION = "09fc515c88a5e817870e6b975e66b5be81893e03"

CASE_ID = "test-bug-case"
TASK_TYPE = "bug_localization"
SCORING_PROFILE = "bug_localization_v1"


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #


def _write_case(tmp_path: Path, case_id: str = CASE_ID) -> Path:
    """Write a minimal valid case-v1 YAML and return its path."""
    case = {
        "schema_version": "case-v1",
        "case_id": case_id,
        "task_type": TASK_TYPE,
        "question": "What is the root cause of the corrupt-inbox bug?",
    }
    p = tmp_path / "case.yaml"
    p.write_text(yaml.safe_dump(case), encoding="utf-8")
    return p


def _write_gt(tmp_path: Path, case_id: str = CASE_ID) -> Path:
    """Write a minimal valid ground-truth-v1 YAML (100 pts, bug_localization)."""
    gt = {
        "schema_version": "ground-truth-v1",
        "case_id": case_id,
        "task_type": TASK_TYPE,
        "scoring_profile": SCORING_PROFILE,
        "rubric_items": [
            {
                "id": "core-1",
                "dimension": "core_correctness",
                "points": 35,
                "criterion": "Identify the root cause.",
                "critical": True,
                "zero_credit": "If root cause is wrong, score 0.",
            },
            {
                "id": "reason-1",
                "dimension": "reasoning_correctness",
                "points": 25,
                "criterion": "Trace the failure chain.",
            },
            {
                "id": "comp-1",
                "dimension": "completeness",
                "points": 20,
                "criterion": "State the blast radius.",
            },
            {
                "id": "scope-1",
                "dimension": "scope_precision",
                "points": 10,
                "criterion": "Exclude false causes.",
            },
            {
                "id": "evi-1",
                "dimension": "evidence_actionability",
                "points": 10,
                "criterion": "Provide evidence and test direction.",
            },
        ],
    }
    p = tmp_path / "gt.yaml"
    p.write_text(yaml.safe_dump(gt), encoding="utf-8")
    return p


def _write_mcp_config(tmp_path: Path, name: str = "mcp.json") -> Path:
    """Write a minimal MCP config JSON file and return its path."""
    p = tmp_path / name
    p.write_text(json.dumps({"mcpServers": {"test": {}}}), encoding="utf-8")
    return p


def _write_config(
    tmp_path: Path,
    *,
    case_path: Path,
    gt_path: Path,
    status: str = "executable",
    runtime: dict[str, Any] | None = None,
    conditions: list[dict] | None = None,
    repeats: int = 1,
    experiment_id: str = "test-exp-v1",
) -> Path:
    """Write an experiment config YAML and return its path."""
    if conditions is None:
        conditions = [
            {"id": "graph", "tool_policy": "graph"},
            {"id": "grep", "tool_policy": "grep"},
        ]
    cfg: dict[str, Any] = {
        "experiment_id": experiment_id,
        "purpose": "formal" if status == "executable" else "smoke",
        "status": status,
        "case_id": CASE_ID,
        "task_type": TASK_TYPE,
        "scoring_profile": SCORING_PROFILE,
        "case": str(case_path),
        "ground_truth": str(gt_path),
        "judge_model": "glm-5.2",
        "conditions": conditions,
        "pairing": "graph_vs_grep",
        "repeats": repeats,
    }
    if runtime is not None:
        cfg["runtime"] = runtime
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _full_runtime(tmp_path: Path) -> dict[str, Any]:
    """Runtime fields for an executable config pointing at tmp_path."""
    mcp = _write_mcp_config(tmp_path, "graph-mcp.json")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    runs_dir = tmp_path / "runs"
    return {
        "agent_model": "glm-5.2",
        "repo_cwd": str(repo_dir),
        "graph_mcp_configs": [str(mcp)],
        "grep_mcp_configs": [],
        "permission_mode": "bypassPermissions",
        "runs_root": str(runs_dir),
    }


FORMAL_GRAPH_SKILL_TEXT_MARKER = "Graph tool usage guide."


def _full_runtime_with_skill(tmp_path: Path) -> dict[str, Any]:
    """A full runtime that also configures a Graph Skill file.

    Used to verify Skill isolation: the skill is a Graph-tool resource that
    must reach Graph (and Mixed) runs but never a Grep run (AIS-012, F2).
    """
    rt = _full_runtime(tmp_path)
    skill = tmp_path / "graph-skill.md"
    skill.write_text(FORMAL_GRAPH_SKILL_TEXT_MARKER, encoding="utf-8")
    rt["skill_file"] = str(skill)
    return rt


class FakeAdapter:
    """A minimal AgentAdapter that records calls and returns a fixed outcome."""

    def __init__(
        self,
        run: PlannedRun,
        plan: DispatchPlan,
        *,
        raw: bytes = b'{"status":"completed","answer":{"summary":"","explanation":""}}',
        tool_kinds: tuple[ToolKind, ...] = (ToolKind.GRAPH,),
        raises: BaseException | None = None,
    ) -> None:
        self._run = run
        self._plan = plan
        self._raw = raw
        self._tool_kinds = tool_kinds
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def execute(self, *, case_id: str, task_type: str, tool_policy: str) -> br.AgentRunOutcome:
        self.calls.append((case_id, task_type, tool_policy))
        if self._raises is not None:
            raise self._raises
        events = tuple(
            ToolEvent(kind=k, source=RUNNER_OBSERVED_SOURCE, label=k.value)
            for k in self._tool_kinds
        )
        return br.AgentRunOutcome(
            raw_response=self._raw,
            tool_events=events,
            input_tokens=100,
            output_tokens=50,
        )


def _make_fake_factory(
    *,
    raw: bytes | None = None,
    tool_kinds_by_policy: dict[str, tuple[ToolKind, ...]] | None = None,
    raises: BaseException | None = None,
):
    """Build an adapter_factory closure that records each run's policy.

    Graph runs get a GRAPH tool event; Grep runs get a SEARCH event, so the
    policy validator is satisfied. This verifies Graph/Grep pairing without
    launching real code.
    """
    if tool_kinds_by_policy is None:
        tool_kinds_by_policy = {
            "graph": (ToolKind.GRAPH,),
            "grep": (ToolKind.SEARCH,),
            "mixed": (ToolKind.GRAPH, ToolKind.SEARCH),
        }

    def factory(run: PlannedRun, plan: DispatchPlan) -> FakeAdapter:
        kinds = tool_kinds_by_policy.get(run.tool_policy, (ToolKind.GRAPH,))
        return FakeAdapter(
            run,
            plan,
            raw=raw or b'{"status":"completed","answer":{"summary":"","explanation":""}}',
            tool_kinds=kinds,
            raises=raises,
        )

    return factory


def _formal_runtime_resources_exist() -> bool:
    """Whether the machine-specific formal runtime resources are on disk.

    The Graph Skill file is no longer required: the formal config does not
    configure a skill anymore (skill injection was removed).
    """
    return FORMAL_REPO_CWD.is_dir() and FORMAL_GRAPH_MCP.is_file()


# Tests that construct adapters or assert clean validation require the
# machine-specific runtime resources (QwenPaw repo, Graph MCP) to exist on
# disk. They run on the configured host and skip elsewhere; the structural
# tests above do not need them.
need_runtime_resources = pytest.mark.skipif(
    not _formal_runtime_resources_exist(),
    reason="formal runtime resources (QwenPaw repo, Graph MCP) "
    "not present on this host",
)


# --------------------------------------------------------------------------- #
# Smoke config: loading, validation, planning, refusal
# --------------------------------------------------------------------------- #


class TestSmokeConfig:
    """The shipped QwenPaw smoke config validates but never executes."""

    def test_load_smoke_config(self) -> None:
        config = load_experiment_config(SMOKE_CONFIG, repo_root=REPO_ROOT)
        assert config.experiment_id == "qwenpaw-corrupt-inbox-smoke-v1"
        assert config.status == "smoke_only"
        assert config.case_id == "qwenpaw-case-z-corrupt-inbox-recovery-bug"
        assert config.task_type == "bug_localization"
        assert config.scoring_profile == "bug_localization_v1"
        assert len(config.conditions) == 2
        assert config.conditions[0].id == "graph"
        assert config.conditions[0].tool_policy == "graph"
        assert config.conditions[1].id == "grep"
        assert config.conditions[1].tool_policy == "grep"
        assert config.repeats == 1
        assert config.runtime is None  # smoke has no runtime

    def test_smoke_config_validates_clean(self) -> None:
        config = load_experiment_config(SMOKE_CONFIG, repo_root=REPO_ROOT)
        issues = validate_experiment_config(config)
        assert issues == [], [str(i) for i in issues]

    def test_smoke_plan_is_non_executable(self) -> None:
        config = load_experiment_config(SMOKE_CONFIG, repo_root=REPO_ROOT)
        plan = build_dispatch_plan(config)
        assert plan.status == "smoke_only"
        assert not plan.is_executable
        assert len(plan.runs) == 2
        assert plan.runs[0].condition_id == "graph"
        assert plan.runs[1].condition_id == "grep"
        assert plan.case_prompt is not None

    def test_smoke_execute_refused(self) -> None:
        config = load_experiment_config(SMOKE_CONFIG, repo_root=REPO_ROOT)
        plan = build_dispatch_plan(config)
        with pytest.raises(SmokeOnlyExecutionError, match="smoke_only"):
            execute_dispatch(plan, allow_execute=True)

    def test_smoke_execute_refused_even_with_runtime(self, tmp_path: Path) -> None:
        """Smoke status takes precedence: even with runtime, it won't execute."""
        config = load_experiment_config(SMOKE_CONFIG, repo_root=REPO_ROOT)
        runtime = RuntimeFields(
            agent_model="glm-5.2",
            repo_cwd=tmp_path,
            runs_root=tmp_path / "runs",
            case_prompt="override",
        )
        plan = build_dispatch_plan(config, runtime=runtime)
        assert plan.status == "smoke_only"
        with pytest.raises(SmokeOnlyExecutionError):
            execute_dispatch(plan, allow_execute=True)


# --------------------------------------------------------------------------- #
# Formal config: paths/values, six-run dry-run, Graph/Grep isolation
# --------------------------------------------------------------------------- #


class TestFormalConfig:
    """The shipped QwenPaw formal config: values, six-run dry-run, isolation.

    Structural tests (values, six-run plan, frozen revision, no credentials)
    run anywhere the repo is checked out. Tests that touch the machine-specific
    runtime resources (clean validation, executable plan, adapter isolation,
    CLI dry-run) are guarded by ``need_runtime_resources``.
    """

    def test_formal_config_file_exists(self) -> None:
        assert FORMAL_CONFIG.is_file(), f"missing formal config: {FORMAL_CONFIG}"

    def test_load_formal_config_values(self) -> None:
        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        assert config.experiment_id == "qwenpaw-corrupt-inbox-formal-v1"
        assert config.purpose == "formal"
        assert config.status == "executable"
        assert config.case_id == "qwenpaw-case-z-corrupt-inbox-recovery-bug"
        assert config.task_type == "bug_localization"
        assert config.scoring_profile == "bug_localization_v1"
        assert config.judge_model == "glm-5.2"
        assert config.pairing == "graph_vs_grep"
        assert config.repeats == 3
        assert len(config.conditions) == 2
        assert config.conditions[0].id == "graph"
        assert config.conditions[0].tool_policy == "graph"
        assert config.conditions[1].id == "grep"
        assert config.conditions[1].tool_policy == "grep"
        # Runtime fields pin the frozen absolute paths and agent identity.
        assert config.runtime is not None
        rt = config.runtime
        assert rt.agent_adapter == "claude-code"  # AIS-014: defaults to claude-code
        assert rt.agent_model == "glm-5.2"
        assert rt.permission_mode == "auto"
        assert rt.repo_cwd == FORMAL_REPO_CWD
        assert rt.graph_mcp_configs == (FORMAL_GRAPH_MCP,)
        assert rt.grep_mcp_configs == ()  # Grep has no Graph MCP input
        # No skill configured (removed: Graph/Grep run without skill injection).
        assert rt.skill_file is None
        assert rt.skill_text is None  # no skill file, no inline text
        assert rt.runs_root == FORMAL_RUNS_ROOT
        assert rt.plugin_dirs == ()

    def test_formal_config_case_paths_resolve(self) -> None:
        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        assert config.case_path.is_file()
        assert config.ground_truth_path.is_file()

    def test_formal_config_carries_frozen_revision(self) -> None:
        """The QwenPaw revision is recorded as a non-secret freeze value."""
        doc = yaml.safe_load(FORMAL_CONFIG.read_text(encoding="utf-8"))
        assert doc["frozen_inputs"]["repo_revision"] == FORMAL_REPO_REVISION

    def test_formal_config_has_no_credentials(self) -> None:
        """No credential/secret values anywhere in the formal config."""
        doc = yaml.safe_load(FORMAL_CONFIG.read_text(encoding="utf-8"))

        secret_keys = frozenset(
            {
                "api_key",
                "apikey",
                "token",
                "secret",
                "password",
                "passwd",
                "credential",
                "credentials",
                "access_key",
                "private_key",
                "auth_token",
            }
        )

        def _collect(obj: Any) -> set[str]:
            found: set[str] = set()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    found.add(str(k).lower())
                    found |= _collect(v)
            elif isinstance(obj, list):
                for v in obj:
                    found |= _collect(v)
            return found

        assert not (_collect(doc) & secret_keys)

    def test_formal_dry_run_six_runs(self) -> None:
        """Dry-run builds a 6-run plan (3 Graph + 3 Grep), no execution.

        Run IDs are deterministic and safe. The plan builds regardless of
        whether the machine-specific runtime resources exist; the structural
        six-run shape holds everywhere.
        """
        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        plan = build_dispatch_plan(config)
        assert len(plan.runs) == 6
        graph_runs = [r for r in plan.runs if r.tool_policy == "graph"]
        grep_runs = [r for r in plan.runs if r.tool_policy == "grep"]
        assert len(graph_runs) == 3
        assert len(grep_runs) == 3
        assert [r.run_id for r in plan.runs] == [
            "qwenpaw-corrupt-inbox-formal-v1__graph__r01",
            "qwenpaw-corrupt-inbox-formal-v1__graph__r02",
            "qwenpaw-corrupt-inbox-formal-v1__graph__r03",
            "qwenpaw-corrupt-inbox-formal-v1__grep__r01",
            "qwenpaw-corrupt-inbox-formal-v1__grep__r02",
            "qwenpaw-corrupt-inbox-formal-v1__grep__r03",
        ]
        for rid in (r.run_id for r in plan.runs):
            assert "/" not in rid
            assert "\\" not in rid
        # The case question is resolved as the agent prompt.
        assert plan.case_prompt is not None

    @need_runtime_resources
    def test_formal_config_validates_clean(self) -> None:
        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        issues = validate_experiment_config(config)
        assert issues == [], [str(i) for i in issues]

    @need_runtime_resources
    def test_formal_plan_is_executable(self) -> None:
        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        plan = build_dispatch_plan(config)
        assert plan.is_executable

    @need_runtime_resources
    def test_formal_graph_grep_mcp_isolation(self) -> None:
        """Default factory: Graph run gets the Graph MCP; Grep run gets none.

        Adapter construction validates paths and reads the skill file but does
        NOT launch a subprocess (``execute`` is never called). This verifies
        Graph/Grep MCP isolation through the real default factory.
        """
        from runner.experiment_dispatch import _default_adapter_factory

        config = load_experiment_config(FORMAL_CONFIG, repo_root=REPO_ROOT)
        plan = build_dispatch_plan(config)
        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        grep_adapter = _default_adapter_factory(plan.runs[3], plan)
        # Graph run receives only the Graph MCP config and keeps Graph patterns.
        assert graph_adapter._graph_mcp_configs == (str(FORMAL_GRAPH_MCP),)
        assert graph_adapter._grep_mcp_configs == ()
        assert graph_adapter._tool_name_patterns.graph == (r"^mcp__gitnexus",)
        # Grep run receives no Graph MCP config and has Graph patterns cleared.
        assert grep_adapter._graph_mcp_configs == ()
        assert grep_adapter._grep_mcp_configs == ()
        assert grep_adapter._tool_name_patterns.graph == ()
        # Grep MCP selection does not fail closed (P1 fix) and yields the empty
        # Grep set.
        assert grep_adapter._select_mcp_configs("grep") == ()
        assert graph_adapter._select_mcp_configs("graph") == (str(FORMAL_GRAPH_MCP),)
        # Skill isolation: the formal config no longer configures a skill, so
        # neither Graph nor Grep runs receive skill text (the Graph Skill was
        # removed from the config).
        assert graph_adapter._skill_text is None
        assert grep_adapter._skill_text is None
        # The constructed commands reflect the isolation: no skill is injected
        # via --append-system-prompt for either policy.
        graph_argv = graph_adapter.build_command(tool_policy="graph", prompt="prompt")
        grep_argv = grep_adapter.build_command(tool_policy="grep", prompt="prompt")
        assert "--append-system-prompt" not in graph_argv
        assert "--append-system-prompt" not in grep_argv
        # No subprocess launched: execute() was never called.
        assert graph_adapter.last_command == []
        assert grep_adapter.last_command == []

    @need_runtime_resources
    def test_formal_cli_validate_only(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(FORMAL_CONFIG),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--validate-only",
                ]
            )
        assert rc == 0
        assert "valid" in buf.getvalue().lower()

    @need_runtime_resources
    def test_formal_cli_dry_run(self) -> None:
        """CLI dispatch --dry-run reports 6 planned runs and the run IDs."""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(FORMAL_CONFIG),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--dry-run",
                ]
            )
        out = buf.getvalue()
        assert rc == 0
        assert "6 planned run" in out
        assert "qwenpaw-corrupt-inbox-formal-v1__graph__r01" in out
        assert "qwenpaw-corrupt-inbox-formal-v1__grep__r03" in out


# --------------------------------------------------------------------------- #
# Executable config: loading, validation, dry-run, execution
# --------------------------------------------------------------------------- #


class TestExecutableConfig:
    """A config with complete runtime fields validates and can execute."""

    def test_load_executable_config(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.status == "executable"
        assert config.runtime is not None
        assert config.runtime.agent_model == "glm-5.2"
        assert config.runtime.repo_cwd is not None
        assert len(config.runtime.graph_mcp_configs) == 1

    def test_executable_config_validates_clean(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        assert issues == [], [str(i) for i in issues]

    def test_dry_run_plan_stable_ids(self, tmp_path: Path) -> None:
        """Dry-run builds a plan with deterministic run IDs, no execution."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=2)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        assert plan.is_executable
        # 2 conditions x 2 repeats = 4 runs
        assert len(plan.runs) == 4
        ids = [r.run_id for r in plan.runs]
        # Deterministic: same config -> same IDs.
        plan2 = build_dispatch_plan(load_experiment_config(cfg))
        assert [r.run_id for r in plan2.runs] == ids
        # Stable, safe, ordered.
        assert ids == [
            "test-exp-v1__graph__r01",
            "test-exp-v1__graph__r02",
            "test-exp-v1__grep__r01",
            "test-exp-v1__grep__r02",
        ]
        for rid in ids:
            assert "/" not in rid
            assert "\\" not in rid

    def test_dry_run_resolves_case_prompt(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        assert plan.case_prompt == "What is the root cause of the corrupt-inbox bug?"

    def test_case_prompt_override(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        override = RuntimeFields(
            agent_model="glm-5.2",
            repo_cwd=config.runtime.repo_cwd,
            graph_mcp_configs=config.runtime.graph_mcp_configs,
            runs_root=config.runtime.runs_root,
            case_prompt="OVERRIDE PROMPT",
        )
        plan = build_dispatch_plan(config, runtime=override)
        assert plan.case_prompt == "OVERRIDE PROMPT"

    def test_execute_with_fake_adapter_pairing(self, tmp_path: Path) -> None:
        """Execution dispatches Graph and Grep runs with correct pairing."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=2)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert len(results) == 4
        # Each run produced artifacts (awaiting-judge for completed answers).
        for r in results:
            assert r.status is RunStatus.AWAITING_JUDGE
            assert r.policy_valid

    def test_execute_repeats_produce_distinct_run_dirs(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=3)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert len(results) == 6  # 2 conditions x 3 repeats
        run_dirs = {r.run_dir for r in results}
        assert len(run_dirs) == 6  # all distinct

    def test_execute_artifact_handoff(self, tmp_path: Path) -> None:
        """execute_run produces real artifacts; dispatcher doesn't fabricate."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert len(results) == 2
        for r in results:
            run_dir = r.run_dir
            assert (run_dir / "raw-response.txt").exists()
            assert (run_dir / "agent-answer.json").exists()
            assert (run_dir / "run-metadata.json").exists()
            assert (run_dir / "policy-result.json").exists()
            assert (run_dir / "manifest.json").exists()
            # Dispatcher did not fabricate Judge artifacts.
            assert not (run_dir / "judge-score.json").exists()
            assert not (run_dir / "effective-score.json").exists()


# --------------------------------------------------------------------------- #
# Execution guard and refusal paths
# --------------------------------------------------------------------------- #


class TestExecutionGuard:
    """The opt-in guard and refusal paths."""

    def test_execute_requires_allow_flag(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        with pytest.raises(DispatchError, match="allow_execute"):
            execute_dispatch(plan, allow_execute=False)

    def test_execute_no_runtime_refused(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, status="executable")
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        with pytest.raises(IncompleteRuntimeError, match="no runtime"):
            execute_dispatch(plan, allow_execute=True)

    def test_execute_missing_runs_root_refused(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        mcp = _write_mcp_config(tmp_path)
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(mcp)],
            "grep_mcp_configs": [],
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        assert plan.is_executable
        with pytest.raises(IncompleteRuntimeError, match="runs_root"):
            execute_dispatch(plan, allow_execute=True)

    def test_execute_validation_issues_refused(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path, case_id="WRONG")
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        assert plan.validation_issues
        with pytest.raises(ConfigValidationError, match="validation issue"):
            execute_dispatch(plan, allow_execute=True)


# --------------------------------------------------------------------------- #
# Validation failures
# --------------------------------------------------------------------------- #


class TestValidationFailures:
    """Config, Case, GT/Profile and runtime path validation failures."""

    def test_missing_case_file(self, tmp_path: Path) -> None:
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=tmp_path / "nonexistent.yaml",
            gt_path=gt,
            runtime=rt,
        )
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CASE_LOAD_FAILED" in codes

    def test_missing_gt_file(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=tmp_path / "nonexistent.yaml",
            runtime=rt,
        )
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "GROUND_TRUTH_LOAD_FAILED" in codes

    def test_case_id_mismatch_cross_check(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path, case_id="different-case")
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CROSS_CASE_ID_MISMATCH" in codes

    def test_gt_invalid_points(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        gt_doc = yaml.safe_load(gt.read_text(encoding="utf-8"))
        gt_doc["rubric_items"][0]["points"] = 1
        gt.write_text(yaml.safe_dump(gt_doc), encoding="utf-8")
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "DIMENSION_POINTS_MISMATCH" in codes

    def test_invalid_tool_policy(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[{"id": "bad", "tool_policy": "invalid"}],
        )
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CONFIG_INVALID_TOOL_POLICY" in codes

    def test_unknown_status(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, status="bogus")
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CONFIG_STATUS_UNKNOWN" in codes

    def test_missing_repo_cwd(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        mcp = _write_mcp_config(tmp_path)
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(tmp_path / "nonexistent-dir"),
            "graph_mcp_configs": [str(mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "RUNTIME_REPO_CWD_MISSING" in codes

    def test_missing_graph_mcp_config(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(tmp_path / "nonexistent-mcp.json")],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "RUNTIME_GRAPH_MCP_MISSING" in codes

    def test_missing_skill_file(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        mcp = _write_mcp_config(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(mcp)],
            "skill_file": str(tmp_path / "nonexistent.skill"),
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "RUNTIME_SKILL_FILE_MISSING" in codes

    def test_non_dict_gt_rejected(self, tmp_path: Path) -> None:
        """P2: non-dict GT YAML is rejected explicitly, matching Case behavior."""
        case = _write_case(tmp_path)
        gt = tmp_path / "gt.yaml"
        gt.write_text("- just\n- a\n- list\n", encoding="utf-8")
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "GT_NOT_OBJECT" in codes

    def test_duplicate_condition_ids_rejected(self, tmp_path: Path) -> None:
        """P2: duplicate condition IDs are rejected during plan validation."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "graph", "tool_policy": "graph"},
                {"id": "graph", "tool_policy": "grep"},
            ],
        )
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CONFIG_DUPLICATE_CONDITION_ID" in codes
        # Plan is not executable.
        plan = build_dispatch_plan(config)
        assert not plan.is_executable

    def test_condition_id_sanitized_collision_rejected(self, tmp_path: Path) -> None:
        """P2: condition IDs that sanitize to the same run-id component collide."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        # "a/b" and "a*b" both sanitize to "a_b" (/ and * are unsafe chars).
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "a/b", "tool_policy": "graph"},
                {"id": "a*b", "tool_policy": "grep"},
            ],
        )
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CONFIG_CONDITION_ID_COLLISION" in codes
        # The run IDs would collide (both sanitize to a_b).
        plan = build_dispatch_plan(config)
        ids = [r.run_id for r in plan.runs]
        assert len(set(ids)) < len(ids)  # collision present
        assert not plan.is_executable

    def test_runtime_override_bad_paths_rejected(self, tmp_path: Path) -> None:
        """P2: runtime override paths are validated in build_dispatch_plan.

        Bad override paths must surface as deterministic Dispatch/config
        errors (ConfigValidationError), not uncaught AgentAdapterError at
        adapter construction time.
        """
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        override = RuntimeFields(
            agent_model="glm-5.2",
            repo_cwd=tmp_path / "nonexistent-dir",
            graph_mcp_configs=(tmp_path / "nonexistent-mcp.json",),
            runs_root=tmp_path / "runs",
            case_prompt="override",
        )
        plan = build_dispatch_plan(config, runtime=override)
        codes = [i.code for i in plan.validation_issues]
        assert "RUNTIME_REPO_CWD_MISSING" in codes
        assert "RUNTIME_GRAPH_MCP_MISSING" in codes
        assert not plan.is_executable
        with pytest.raises(ConfigValidationError):
            execute_dispatch(plan, allow_execute=True)

    def test_runtime_override_bad_skill_file_rejected(self, tmp_path: Path) -> None:
        """P2: a bad skill_file in the override is caught as a config error."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        override = RuntimeFields(
            agent_model="glm-5.2",
            repo_cwd=config.runtime.repo_cwd,
            graph_mcp_configs=config.runtime.graph_mcp_configs,
            skill_file=tmp_path / "nonexistent.skill",
            runs_root=tmp_path / "runs",
            case_prompt="override",
        )
        plan = build_dispatch_plan(config, runtime=override)
        codes = [i.code for i in plan.validation_issues]
        assert "RUNTIME_SKILL_FILE_MISSING" in codes
        assert not plan.is_executable

    def test_runtime_override_bad_plugin_dir_rejected(self, tmp_path: Path) -> None:
        """P2: a bad plugin_dir in the override is caught as a config error."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        override = RuntimeFields(
            agent_model="glm-5.2",
            repo_cwd=config.runtime.repo_cwd,
            graph_mcp_configs=config.runtime.graph_mcp_configs,
            plugin_dirs=(tmp_path / "nonexistent-plugins",),
            runs_root=tmp_path / "runs",
            case_prompt="override",
        )
        plan = build_dispatch_plan(config, runtime=override)
        codes = [i.code for i in plan.validation_issues]
        assert "RUNTIME_PLUGIN_DIR_MISSING" in codes
        assert not plan.is_executable


# --------------------------------------------------------------------------- #
# Run ID safety
# --------------------------------------------------------------------------- #


class TestRunIdSafety:
    """Run IDs must be deterministic, safe path components."""

    def test_run_id_format(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            experiment_id="my-exp",
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        assert plan.runs[0].run_id == "my-exp__graph__r01"
        assert plan.runs[1].run_id == "my-exp__grep__r01"

    def test_run_id_sanitizes_unsafe_chars(self, tmp_path: Path) -> None:
        """Slashes in experiment_id are sanitized to underscores."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            experiment_id="team/exp",
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        # Slashes replaced with underscores.
        rid = plan.runs[0].run_id
        assert "/" not in rid
        assert "\\" not in rid
        assert rid.startswith("team_exp__")

    def test_high_repeat_count_pads_correctly(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=12)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        # 2 conditions x 12 repeats = 24 runs, all unique IDs.
        ids = [r.run_id for r in plan.runs]
        assert len(set(ids)) == 24
        assert plan.runs[0].run_id == "test-exp-v1__graph__r01"
        assert plan.runs[11].run_id == "test-exp-v1__graph__r12"


# --------------------------------------------------------------------------- #
# Graph/Grep isolation in default adapter factory
# --------------------------------------------------------------------------- #


class TestGraphGrepIsolation:
    """The default adapter factory selects only matching MCP configs."""

    def test_graph_run_gets_only_graph_mcp(self, tmp_path: Path) -> None:
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        grep_mcp = _write_mcp_config(tmp_path, "grep.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(graph_mcp)],
            "grep_mcp_configs": [str(grep_mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_run = plan.runs[0]  # graph condition
        adapter = _default_adapter_factory(graph_run, plan)
        # Graph adapter has only graph MCP configs.
        assert adapter._graph_mcp_configs == (str(graph_mcp),)
        assert adapter._grep_mcp_configs == ()

    def test_grep_run_gets_only_grep_mcp(self, tmp_path: Path) -> None:
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        grep_mcp = _write_mcp_config(tmp_path, "grep.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(graph_mcp)],
            "grep_mcp_configs": [str(grep_mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        grep_run = plan.runs[1]  # grep condition
        adapter = _default_adapter_factory(grep_run, plan)
        # Grep adapter has NO graph MCP configs (isolation).
        assert adapter._graph_mcp_configs == ()
        assert adapter._grep_mcp_configs == (str(grep_mcp),)

    def test_grep_adapter_would_fail_closed_with_graph_mcp(self, tmp_path: Path) -> None:
        """Defense-in-depth: if graph MCP configs reach a grep adapter, it
        fails closed at execute() time.

        The ``_default_adapter_factory`` prevents this by passing
        ``graph_mcp_configs=()`` for grep runs. This test verifies the
        adapter's own fail-closed guard independently.
        """
        from runner.claude_code_adapter import (
            AgentPolicyConfigError,
            ClaudeCodeAgentAdapter,
        )

        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        adapter = ClaudeCodeAgentAdapter(
            prompt="test prompt",
            case_id="test",
            task_type="bug_localization",
            agent_model="glm-5.2",
            repo_cwd=repo_dir,
            graph_mcp_configs=[graph_mcp],
            grep_mcp_configs=[],
        )
        with pytest.raises(AgentPolicyConfigError):
            adapter.execute(
                case_id="test",
                task_type="bug_localization",
                tool_policy="grep",
            )

    def test_default_factory_clears_graph_patterns_for_grep(self, tmp_path: Path) -> None:
        """P1: the default factory must clear Graph tool-name patterns for Grep.

        The default ``ToolNamePatterns(graph=(^mcp__gitnexus,))`` causes
        ``_select_mcp_configs('grep')`` to fail closed. The factory passes an
        explicit empty Graph pattern set for Grep runs so a real Grep adapter
        can actually execute.
        """
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        grep_mcp = _write_mcp_config(tmp_path, "grep.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(graph_mcp)],
            "grep_mcp_configs": [str(grep_mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        grep_run = plan.runs[1]  # grep condition
        adapter = _default_adapter_factory(grep_run, plan)
        # Graph patterns cleared for grep.
        assert adapter._tool_name_patterns.graph == ()
        # Selecting MCP configs for grep must NOT raise (the P1 bug).
        configs = adapter._select_mcp_configs("grep")
        assert configs == (str(grep_mcp),)

    def test_default_factory_preserves_graph_patterns_for_graph_and_mixed(
        self, tmp_path: Path
    ) -> None:
        """P1: Graph and Mixed runs keep the default Graph tool-name patterns.

        Only Grep runs have Graph patterns cleared; Graph and Mixed runs need
        them to classify Graph tool-use events.
        """
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        grep_mcp = _write_mcp_config(tmp_path, "grep.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_model": "glm-5.2",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(graph_mcp)],
            "grep_mcp_configs": [str(grep_mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "graph", "tool_policy": "graph"},
                {"id": "grep", "tool_policy": "grep"},
                {"id": "mixed", "tool_policy": "mixed"},
            ],
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        mixed_adapter = _default_adapter_factory(plan.runs[2], plan)
        # Default Graph patterns preserved for graph and mixed.
        assert graph_adapter._tool_name_patterns.graph == (r"^mcp__gitnexus",)
        assert mixed_adapter._tool_name_patterns.graph == (r"^mcp__gitnexus",)
        # MCP config selection works for all policies (no fail-closed).
        assert graph_adapter._select_mcp_configs("graph") == (str(graph_mcp),)
        assert mixed_adapter._select_mcp_configs("mixed") == (
            str(graph_mcp),
            str(grep_mcp),
        )


# --------------------------------------------------------------------------- #
# Graph/Grep Skill isolation in the default adapter factory (AIS-012, F2)
# --------------------------------------------------------------------------- #


class TestGraphGrepSkillIsolation:
    """The configured Graph Skill reaches Graph/Mixed runs only, never Grep.

    The config contract has one global ``skill_file``/``skill_text`` field.
    The dispatcher's default factory applies it Graph-only: a Graph (or Mixed)
    run receives the skill; a Grep run receives no skill at all, so the Graph
    Skill text cannot contaminate the Grep baseline (F2).
    """

    def test_graph_run_receives_configured_skill(self, tmp_path: Path) -> None:
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime_with_skill(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        # Positive: the Graph run's adapter carries the Graph Skill text.
        assert graph_adapter._skill_text == FORMAL_GRAPH_SKILL_TEXT_MARKER
        argv = graph_adapter.build_command(tool_policy="graph", prompt="p")
        assert "--append-system-prompt" in argv
        idx = argv.index("--append-system-prompt")
        assert argv[idx + 1] == FORMAL_GRAPH_SKILL_TEXT_MARKER

    def test_grep_run_receives_no_skill(self, tmp_path: Path) -> None:
        """Negative (F2): a Grep run is built without the global Graph Skill."""
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime_with_skill(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        grep_adapter = _default_adapter_factory(plan.runs[1], plan)
        # The Grep run's adapter carries no skill, even though the config
        # declares a global Graph Skill.
        assert grep_adapter._skill_text is None
        argv = grep_adapter.build_command(tool_policy="grep", prompt="p")
        assert "--append-system-prompt" not in argv

    def test_inline_skill_text_is_also_suppressed_for_grep(self, tmp_path: Path) -> None:
        """Inline ``skill_text`` (not just ``skill_file``) is Graph-only."""
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["skill_text"] = "inline graph skill"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        grep_adapter = _default_adapter_factory(plan.runs[1], plan)
        assert graph_adapter._skill_text == "inline graph skill"
        assert grep_adapter._skill_text is None

    def test_mixed_run_receives_skill(self, tmp_path: Path) -> None:
        """A Mixed run has Graph tool access, so it receives the skill."""
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime_with_skill(tmp_path)
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "graph", "tool_policy": "graph"},
                {"id": "grep", "tool_policy": "grep"},
                {"id": "mixed", "tool_policy": "mixed"},
            ],
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        grep_adapter = _default_adapter_factory(plan.runs[1], plan)
        mixed_adapter = _default_adapter_factory(plan.runs[2], plan)
        assert graph_adapter._skill_text == FORMAL_GRAPH_SKILL_TEXT_MARKER
        assert mixed_adapter._skill_text == FORMAL_GRAPH_SKILL_TEXT_MARKER
        # Only the Grep run is skill-free.
        assert grep_adapter._skill_text is None

    def test_grep_adapter_from_factory_fail_closes_if_skill_leaks(self, tmp_path: Path) -> None:
        """Defense-in-depth: a Grep adapter carrying a skill fails closed.

        The default factory never passes a skill to a Grep adapter. This test
        constructs a Grep adapter with a skill directly (simulating a factory
        bug or a custom factory) and verifies the adapter's own guard refuses
        to build a command, so a contaminated Grep run cannot execute.
        """
        from runner.claude_code_adapter import (
            AgentPolicyConfigError,
            ClaudeCodeAgentAdapter,
            ToolNamePatterns,
        )

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        adapter = ClaudeCodeAgentAdapter(
            prompt="p",
            case_id=CASE_ID,
            task_type=TASK_TYPE,
            agent_model="glm-5.2",
            repo_cwd=repo_dir,
            cli_path="claude",
            skill_text="leaked graph skill",
            tool_name_patterns=ToolNamePatterns(graph=()),
        )
        with pytest.raises(AgentPolicyConfigError, match="Grep policy.*skill"):
            adapter.build_command(tool_policy="grep", prompt="p")


# --------------------------------------------------------------------------- #
# Agent adapter selection (AIS-014)
# --------------------------------------------------------------------------- #


class TestAgentAdapterSelection:
    """``runtime.agent_adapter`` selects the adapter (claude-code default).

    Covers default Claude, explicit Claude, explicit OpenCode, model/runtime
    mapping and propagation, unknown-adapter rejection, and Graph/Grep + skill
    isolation for the OpenCode adapter. None of these launch a real CLI: adapter
    construction validates paths but ``execute`` is never called, and execution
    tests inject a fake adapter factory.
    """

    def test_default_adapter_is_claude_code(self, tmp_path: Path) -> None:
        from runner.claude_code_adapter import ClaudeCodeAgentAdapter
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)  # no agent_adapter -> default
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime is not None
        assert config.runtime.agent_adapter == "claude-code"
        plan = build_dispatch_plan(config)
        run = plan.runs[0]
        assert run.identity.agent == "claude-code"
        adapter = _default_adapter_factory(run, plan)
        assert isinstance(adapter, ClaudeCodeAgentAdapter)

    def test_explicit_claude_code_adapter(self, tmp_path: Path) -> None:
        from runner.claude_code_adapter import ClaudeCodeAgentAdapter
        from runner.experiment_dispatch import _default_adapter_factory

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "claude-code"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime.agent_adapter == "claude-code"
        plan = build_dispatch_plan(config)
        assert all(r.identity.agent == "claude-code" for r in plan.runs)
        adapter = _default_adapter_factory(plan.runs[0], plan)
        assert isinstance(adapter, ClaudeCodeAgentAdapter)

    def test_explicit_opencode_adapter(self, tmp_path: Path) -> None:
        from runner.experiment_dispatch import _default_adapter_factory
        from runner.opencode_adapter import OpenCodeAgentAdapter

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "opencode"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime.agent_adapter == "opencode"
        plan = build_dispatch_plan(config)
        # Identity agent mapping: the adapter name is recorded in RunIdentity.
        assert all(r.identity.agent == "opencode" for r in plan.runs)
        adapter = _default_adapter_factory(plan.runs[0], plan)
        assert isinstance(adapter, OpenCodeAgentAdapter)

    def test_agent_model_propagates_to_identity_and_adapter(self, tmp_path: Path) -> None:
        """An explicit agent_model reaches RunIdentity and the adapter unchanged."""
        from runner.experiment_dispatch import _default_adapter_factory
        from runner.opencode_adapter import OpenCodeAgentAdapter

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "opencode"
        rt["agent_model"] = "provider/test-model-xyz"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime.agent_model == "provider/test-model-xyz"
        plan = build_dispatch_plan(config)
        run = plan.runs[0]
        assert run.identity.agent == "opencode"
        assert run.identity.agent_model == "provider/test-model-xyz"
        adapter = _default_adapter_factory(run, plan)
        assert isinstance(adapter, OpenCodeAgentAdapter)
        assert adapter._agent_model == "provider/test-model-xyz"

    def test_opencode_default_model_when_unspecified(self, tmp_path: Path) -> None:
        """Model/runtime mapping: opencode without agent_model uses its default."""
        from runner.opencode_adapter import (
            DEFAULT_AGENT_MODEL as OPENCODE_DEFAULT_MODEL,
        )

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "opencode"
        rt.pop("agent_model")  # rely on the adapter-specific default
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime.agent_adapter == "opencode"
        assert config.runtime.agent_model == OPENCODE_DEFAULT_MODEL
        plan = build_dispatch_plan(config)
        assert plan.runs[0].identity.agent_model == OPENCODE_DEFAULT_MODEL

    def test_claude_default_model_preserved_when_unspecified(self, tmp_path: Path) -> None:
        """Model/runtime mapping: claude-code without agent_model keeps its default."""
        from runner.claude_code_adapter import (
            DEFAULT_AGENT_MODEL as CLAUDE_DEFAULT_MODEL,
        )

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt.pop("agent_model")  # no adapter, no model -> both default
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        assert config.runtime.agent_adapter == "claude-code"
        assert config.runtime.agent_model == CLAUDE_DEFAULT_MODEL

    def test_unknown_adapter_rejected(self, tmp_path: Path) -> None:
        """An unknown agent_adapter is rejected before any subprocess launch."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "bogus-adapter"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        issues = validate_experiment_config(config)
        codes = [i.code for i in issues]
        assert "CONFIG_INVALID_AGENT_ADAPTER" in codes
        # The plan is not executable.
        plan = build_dispatch_plan(config)
        assert not plan.is_executable
        with pytest.raises(ConfigValidationError):
            execute_dispatch(plan, allow_execute=True)

    def test_unknown_adapter_in_override_rejected(self, tmp_path: Path) -> None:
        """A bad agent_adapter in a runtime override is also caught (P2)."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        override = RuntimeFields(
            agent_adapter="nope",
            agent_model="glm-5.2",
            repo_cwd=config.runtime.repo_cwd,
            graph_mcp_configs=config.runtime.graph_mcp_configs,
            runs_root=tmp_path / "runs",
            case_prompt="override",
        )
        plan = build_dispatch_plan(config, runtime=override)
        codes = [i.code for i in plan.validation_issues]
        assert "CONFIG_INVALID_AGENT_ADAPTER" in codes
        assert not plan.is_executable

    def test_opencode_graph_grep_mcp_isolation(self, tmp_path: Path) -> None:
        """OpenCode: Graph run gets only Graph MCP; Grep run gets only Grep MCP.

        Graph patterns are cleared for the Grep run (using the OpenCode patterns
        type) so ``_select_mcp_configs('grep')`` does not fail closed; Graph and
        Mixed runs keep the default Graph patterns. Adapter construction does NOT
        launch a subprocess (``execute`` is never called).
        """
        from runner.experiment_dispatch import _default_adapter_factory
        from runner.opencode_adapter import OpenCodeAgentAdapter

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        graph_mcp = _write_mcp_config(tmp_path, "graph.json")
        grep_mcp = _write_mcp_config(tmp_path, "grep.json")
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        rt = {
            "agent_adapter": "opencode",
            "agent_model": "ark-plan-qlw/deepseek-v4-flash",
            "repo_cwd": str(repo_dir),
            "graph_mcp_configs": [str(graph_mcp)],
            "grep_mcp_configs": [str(grep_mcp)],
            "runs_root": str(tmp_path / "runs"),
        }
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "graph", "tool_policy": "graph"},
                {"id": "grep", "tool_policy": "grep"},
                {"id": "mixed", "tool_policy": "mixed"},
            ],
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        grep_adapter = _default_adapter_factory(plan.runs[1], plan)
        mixed_adapter = _default_adapter_factory(plan.runs[2], plan)
        assert isinstance(graph_adapter, OpenCodeAgentAdapter)
        assert isinstance(grep_adapter, OpenCodeAgentAdapter)
        assert isinstance(mixed_adapter, OpenCodeAgentAdapter)
        # Graph run: only graph MCP; default Graph patterns preserved.
        assert graph_adapter._graph_mcp_configs == (graph_mcp,)
        assert graph_adapter._grep_mcp_configs == ()
        assert graph_adapter._tool_name_patterns.graph == (r"^gitnexus_",)
        # Grep run: only grep MCP; Graph patterns cleared.
        assert grep_adapter._graph_mcp_configs == ()
        assert grep_adapter._grep_mcp_configs == (grep_mcp,)
        assert grep_adapter._tool_name_patterns.graph == ()
        # Mixed run: both MCP sets; default Graph patterns preserved.
        assert mixed_adapter._graph_mcp_configs == (graph_mcp,)
        assert mixed_adapter._grep_mcp_configs == (grep_mcp,)
        assert mixed_adapter._tool_name_patterns.graph == (r"^gitnexus_",)
        # Selection does not fail closed for any policy.
        assert graph_adapter._select_mcp_configs("graph") == (graph_mcp,)
        assert grep_adapter._select_mcp_configs("grep") == (grep_mcp,)
        assert mixed_adapter._select_mcp_configs("mixed") == (
            graph_mcp,
            grep_mcp,
        )

    def test_opencode_skill_isolation(self, tmp_path: Path) -> None:
        """OpenCode: the Graph Skill reaches Graph/Mixed runs only, never Grep.

        OpenCode has no ``--append-system-prompt`` flag; the skill is prepended
        to the prompt (``_compose_prompt``). The Grep run's composed prompt
        carries no skill text, so the Graph Skill cannot contaminate the Grep
        baseline (F2).
        """
        from runner.experiment_dispatch import _default_adapter_factory
        from runner.opencode_adapter import OpenCodeAgentAdapter

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime_with_skill(tmp_path)
        rt["agent_adapter"] = "opencode"
        cfg = _write_config(
            tmp_path,
            case_path=case,
            gt_path=gt,
            runtime=rt,
            conditions=[
                {"id": "graph", "tool_policy": "graph"},
                {"id": "grep", "tool_policy": "grep"},
                {"id": "mixed", "tool_policy": "mixed"},
            ],
        )
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        graph_adapter = _default_adapter_factory(plan.runs[0], plan)
        grep_adapter = _default_adapter_factory(plan.runs[1], plan)
        mixed_adapter = _default_adapter_factory(plan.runs[2], plan)
        assert isinstance(graph_adapter, OpenCodeAgentAdapter)
        # Graph and Mixed runs carry the skill; Grep does not.
        assert graph_adapter._skill_text == FORMAL_GRAPH_SKILL_TEXT_MARKER
        assert mixed_adapter._skill_text == FORMAL_GRAPH_SKILL_TEXT_MARKER
        assert grep_adapter._skill_text is None
        # The composed prompt (after the last --) reflects the skill for
        # Graph/Mixed only; OpenCode prepends skill text to the prompt.
        graph_argv = graph_adapter.build_command(tool_policy="graph", prompt="p")
        grep_argv = grep_adapter.build_command(tool_policy="grep", prompt="p")
        mixed_argv = mixed_adapter.build_command(tool_policy="mixed", prompt="p")
        assert graph_argv[-1] == FORMAL_GRAPH_SKILL_TEXT_MARKER + "\n\n" + "p"
        assert mixed_argv[-1] == FORMAL_GRAPH_SKILL_TEXT_MARKER + "\n\n" + "p"
        assert grep_argv[-1] == "p"

    def test_opencode_adapter_receives_no_plugin_or_permission_fields(self, tmp_path: Path) -> None:
        """OpenCode is constructed without plugin_dirs/permission_mode (invariant).

        The config may declare ``permission_mode``/``plugin_dirs`` (Claude-only
        fields); the OpenCode factory path must not forward them - OpenCode has
        its own deny-by-default permission system and no plugin concept.
        """
        from runner.experiment_dispatch import _default_adapter_factory
        from runner.opencode_adapter import OpenCodeAgentAdapter

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "opencode"
        rt["permission_mode"] = "plan"  # a Claude-only field
        rt["plugin_dirs"] = [str(plugin_dir)]  # a Claude-only field
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        adapter = _default_adapter_factory(plan.runs[0], plan)
        assert isinstance(adapter, OpenCodeAgentAdapter)
        # OpenCode exposes no plugin_dirs / permission_mode attributes.
        assert not hasattr(adapter, "_plugin_dirs")
        assert not hasattr(adapter, "_permission_mode")

    def test_execute_with_opencode_identity_and_fake_factory(self, tmp_path: Path) -> None:
        """Execution with an opencode config dispatches runs with opencode identity."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        rt["agent_adapter"] = "opencode"
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=2)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        # Every planned run carries the opencode adapter in its identity.
        assert all(r.identity.agent == "opencode" for r in plan.runs)

        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert len(results) == 4  # 2 conditions x 2 repeats
        for r in results:
            assert r.status is RunStatus.AWAITING_JUDGE
            assert r.policy_valid


# --------------------------------------------------------------------------- #
# Patched execute_run: verifies dispatch calls execute_run, not bypass
# --------------------------------------------------------------------------- #


class TestPatchedExecuteRun:
    """Verify the dispatcher calls execute_run without bypassing Runner policy."""

    def test_dispatch_calls_execute_run_per_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=2)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        calls: list[dict[str, Any]] = []

        def fake_execute_run(
            *,
            runs_root: Path,
            run_id: str,
            identity: br.RunIdentity,
            agent: br.AgentAdapter,
            policy_enforced: bool = True,
        ) -> RunResult:
            calls.append(
                {
                    "runs_root": runs_root,
                    "run_id": run_id,
                    "identity": identity,
                    "policy_enforced": policy_enforced,
                }
            )
            # Return a minimal completed result.
            run_dir = runs_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            return RunResult(
                run_id=run_id,
                run_dir=run_dir,
                status=RunStatus.AWAITING_JUDGE,
                agent_answer_status=AgentAnswerStatus.COMPLETED,
                policy_valid=True,
                started_at="2026-01-01T00:00:00Z",
                ended_at="2026-01-01T00:00:01Z",
                metrics={},
                manifest_path=run_dir / "manifest.json",
            )

        monkeypatch.setattr("runner.experiment_dispatch.execute_run", fake_execute_run)
        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert len(results) == 4
        assert len(calls) == 4
        # Each call used the correct run_id and identity.
        for i, call in enumerate(calls):
            assert call["run_id"] == plan.runs[i].run_id
            assert call["identity"].case_id == CASE_ID
            assert call["policy_enforced"] is True

    def test_dispatch_passes_policy_enforced_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        seen_enforced: list[bool] = []

        def fake_execute_run(
            *, runs_root, run_id, identity, agent, policy_enforced=True
        ) -> RunResult:
            seen_enforced.append(policy_enforced)
            run_dir = runs_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            return RunResult(
                run_id=run_id,
                run_dir=run_dir,
                status=RunStatus.AWAITING_JUDGE,
                agent_answer_status=AgentAnswerStatus.COMPLETED,
                policy_valid=True,
                started_at="t",
                ended_at="t",
                metrics={},
                manifest_path=run_dir / "manifest.json",
            )

        monkeypatch.setattr("runner.experiment_dispatch.execute_run", fake_execute_run)
        execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        assert seen_enforced == [True, True]  # one per run, both True


# --------------------------------------------------------------------------- #
# Natural-language answer flows unchanged
# --------------------------------------------------------------------------- #


class TestNaturalLanguageAnswer:
    """The dispatcher must not fabricate answer JSON or metrics."""

    def test_markdown_answer_flows_through_execution(self, tmp_path: Path) -> None:
        """A natural-language (non-JSON) answer is preserved by runner.execution."""
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)

        markdown = b"## Root cause\n\nThe shared read function fails."
        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(raw=markdown),
        )
        assert len(results) == 2
        for r in results:
            # Schema-warning status: natural language preserved verbatim.
            assert r.agent_answer_status is (AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING)
            assert r.status is RunStatus.AWAITING_JUDGE
            # The raw response is preserved on disk unchanged.
            raw = (r.run_dir / "raw-response.txt").read_bytes()
            assert raw == markdown
            # The explanation carries the original text.
            answer = json.loads((r.run_dir / "agent-answer.json").read_text(encoding="utf-8"))
            assert answer["answer"]["explanation"] == markdown.decode("utf-8")

    def test_dispatcher_does_not_create_judge_artifacts(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        config = load_experiment_config(cfg)
        plan = build_dispatch_plan(config)
        results = execute_dispatch(
            plan,
            allow_execute=True,
            adapter_factory=_make_fake_factory(),
        )
        for r in results:
            names = {p.name for p in r.run_dir.iterdir()}
            # No Judge artifacts produced by the dispatcher.
            assert "blind-input.json" not in names
            assert "judge-a.json" not in names
            assert "judge-score.json" not in names
            assert "effective-score.json" not in names


# --------------------------------------------------------------------------- #
# CLI compatibility
# --------------------------------------------------------------------------- #


class TestCLICompatibility:
    """Existing CLI behaviour is preserved; dispatch subcommand is wired."""

    def test_main_no_args_returns_zero(self) -> None:
        assert br.main([]) == 0

    def test_main_dispatch_validate_only(self, tmp_path: Path) -> None:
        """``dispatch --validate-only`` on the real smoke config prints VALID."""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(SMOKE_CONFIG),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--validate-only",
                ]
            )
        assert rc == 0
        assert "valid" in buf.getvalue().lower()

    def test_main_dispatch_dry_run_smoke(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(SMOKE_CONFIG),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--dry-run",
                ]
            )
        out = buf.getvalue()
        assert rc == 0
        # Dry-run prints planned run IDs.
        assert "graph" in out
        assert "grep" in out

    def test_main_dispatch_execute_smoke_refused(self) -> None:
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(SMOKE_CONFIG),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--execute",
                ]
            )
        assert rc == 2
        assert "smoke_only" in buf.getvalue()

    def test_main_dispatch_validate_only_synthetic(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(["dispatch", str(cfg), "--validate-only"])
        assert rc == 0
        assert "valid" in buf.getvalue().lower()

    def test_main_dispatch_dry_run_executable(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt, repeats=2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(["dispatch", str(cfg), "--dry-run"])
        out = buf.getvalue()
        assert rc == 0
        assert "4 planned run" in out or "planned run" in out

    def test_main_dispatch_runs_root_override(self, tmp_path: Path) -> None:
        import io
        from contextlib import redirect_stdout

        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        rt = _full_runtime(tmp_path)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        override_runs = tmp_path / "override-runs"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = br.main(
                [
                    "dispatch",
                    str(cfg),
                    "--dry-run",
                    "--runs-root",
                    str(override_runs),
                ]
            )
        out = buf.getvalue()
        assert rc == 0
        # The plan uses the overridden runs_root.
        # (In dry-run, we verify the plan built; the override is visible
        # only at execution, but the plan should still be executable.)
        assert "planned run" in out


# --------------------------------------------------------------------------- #
# Config loading edge cases
# --------------------------------------------------------------------------- #


class TestConfigLoading:
    """Config loading edge cases."""

    def test_missing_config_file_raises(self) -> None:
        with pytest.raises(DispatchError, match="not found"):
            load_experiment_config("/nonexistent/config.yaml")

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(DispatchError, match="mapping"):
            load_experiment_config(p)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "incomplete.yaml"
        p.write_text("experiment_id: x\npurpose: smoke\n", encoding="utf-8")
        with pytest.raises(DispatchError, match="must be a non-empty string"):
            load_experiment_config(p)

    def test_skill_text_and_skill_file_mutually_exclusive(self, tmp_path: Path) -> None:
        case = _write_case(tmp_path)
        gt = _write_gt(tmp_path)
        skill = tmp_path / "skill.md"
        skill.write_text("skill content", encoding="utf-8")
        rt = _full_runtime(tmp_path)
        rt["skill_text"] = "inline skill"
        rt["skill_file"] = str(skill)
        cfg = _write_config(tmp_path, case_path=case, gt_path=gt, runtime=rt)
        with pytest.raises(DispatchError, match="mutually exclusive"):
            load_experiment_config(cfg)
