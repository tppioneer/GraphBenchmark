"""Runner lifecycle coordination: execute the agent and collect run artifacts.

The Runner executes the agent under test under a fixed experimental condition
(``tool_policy``), persists the complete run artifact set (design §17), and
independently judges Graph/Grep policy compliance and collects process metrics
(§8.6, §8.7, §15). The agent never self-reports identity, tools, cost or
violations (invariant): the Runner collects all of these itself, and the
agent-answer schema forbids those fields.

Run state machine (acceptance criterion: "run 状态至少区分 valid、invalid、
awaiting-judge、failed"). A run ends in exactly one of:

* ``failed``         - agent execution raised; no judgeable answer artifact.
* ``invalid``        - completed but a policy/artifact admission rule was
                       violated (§15.1); not scorable, no fallback scorer.
* ``valid``          - completed and compliant, but the answer is empty or
                       refused (deterministic 0, §12).
* ``awaiting-judge`` - completed and compliant with a substantive answer
                       (``completed`` / ``completed_with_schema_warning``).

Interrupt/resume safety (acceptance criterion): the manifest is written LAST
and atomically, so an interrupted run leaves no complete manifest and is never
mistaken for success. Reusing a run id with a different input is rejected
(``RunConflictError``); an already-complete identical run is returned
idempotently. The full immutable input identity (including ``case_id``/
``task_type``) is recorded in ``run-input.json`` before execution and checked
for every existing run, so a failed run with no agent-answer is still protected
(AIS009-R2). Rejectable policy inputs (unknown ``tool_policy``, untrusted
tool-event source) are validated before any artifact write, so a rejectable
input fails into a truthful failed run rather than over real artifacts/metrics
(AIS009-R1). Tool-policy enforcement is mandatory, so ``policy_enforced=False``
is rejected and the audit field is always ``true`` (AIS009-N1).

Correctness and cost/policy fields are isolated at the storage layer
(acceptance criterion): ``agent-answer.json`` carries only answer content,
``run-metadata.json`` only identity/cost, and ``policy-result.json`` only
compliance - each schema enforces ``additionalProperties: false``. ``run-input.json``
is runner-internal guard state (not a manifest/scored artifact) and carries no
correctness/cost/policy content, preserving that isolation.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from judge.canonical import digest_bytes

from .execution import AgentAnswerStatus, produce_agent_artifacts
from .policy_validation import (
    PolicyResult,
    PolicyValidationError,
    ToolEvent,
    Violation,
    derive_metrics,
    validate_policy,
    validate_policy_inputs,
)

MANIFEST_FILENAME = "manifest.json"
RAW_RESPONSE_FILENAME = "raw-response.txt"
AGENT_ANSWER_FILENAME = "agent-answer.json"
RUN_METADATA_FILENAME = "run-metadata.json"
POLICY_RESULT_FILENAME = "policy-result.json"
RUN_INPUT_FILENAME = "run-input.json"

RUN_METADATA_SCHEMA_VERSION = "run-metadata-v1"
MANIFEST_SCHEMA_VERSION = "manifest-v1"
RUN_INPUT_SCHEMA_VERSION = "run-input-v1"

#: The Runner-collected violation code for an agent execution failure. The run
#: status is ``failed`` (no judgeable answer); this code records the cause in
#: ``policy-result.json`` so the failure is auditable without a separate file.
EXECUTION_FAILED_CODE = "execution_failed"

#: Manifest artifact names the Runner emits as ``absent``: Judge artifacts are
#: produced by a later phase (AIS-008+); v1 adjudication is reserved and always
#: ``not_applicable`` (§14, §17). Order follows design §17.
_JUDGE_ARTIFACT_NAMES = (
    "blind_input",
    "judge_a",
    "judge_b",
    "judge_c",
    "judge_score",
    "effective_score",
)

#: ``run-input.json`` is the Runner's immutable input-identity record for the
#: no-overwrite guard (AIS009-R2), NOT a manifest/scored artifact. It records the
#: full :class:`RunIdentity` (including ``case_id``/``task_type``, which the
#: run-metadata schema does not carry and which a failed run has no agent-answer
#: to fall back on) so reuse of a run id with a different input is detected for
#: every terminal run. It is written before execution and read first by the guard;
#: it is deliberately not listed in the manifest (design §17 enumerates the scored
#: artifact set) and carries no correctness/cost/policy content, preserving the
#: storage-layer isolation of those three buckets.


class RunStatus(str, Enum):
    """The terminal run state (acceptance criterion)."""

    FAILED = "failed"
    INVALID = "invalid"
    VALID = "valid"
    AWAITING_JUDGE = "awaiting-judge"


@dataclass(frozen=True)
class RunIdentity:
    """The fixed experimental condition and agent identity for one run.

    These fields are Runner-authoritative: the agent cannot declare them (the
    agent-answer schema forbids identity/tool/policy fields). ``tool_policy``/
    ``agent``/``agent_model`` are recorded in ``run-metadata.json``; the full
    identity (including ``case_id``/``task_type``, which the run-metadata schema
    does not carry) is recorded in ``run-input.json`` and used by the
    no-overwrite guard so every terminal run - even a failed one with no
    agent-answer - is identity-checked (AIS009-R2).
    """

    case_id: str
    task_type: str
    tool_policy: str
    agent: str
    agent_model: str


@dataclass(frozen=True)
class AgentRunOutcome:
    """The observed outcome of one agent execution (Runner's observation channel).

    ``raw_response`` is the model's raw bytes; ``tool_events`` are the tool calls
    the Runner observed (each stamped with the verifiable Runner source); tokens
    are observed by the Runner, never self-reported by the agent answer.
    """

    raw_response: bytes
    tool_events: tuple[ToolEvent, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


class AgentAdapter(Protocol):
    """The Runner's observation channel around the agent under test."""

    def execute(
        self, *, case_id: str, task_type: str, tool_policy: str
    ) -> AgentRunOutcome:
        """Run the agent and return the observed outcome.

        The adapter wraps/observes the agent's calls and returns the model's raw
        response plus the tool events the Runner observed. The agent never
        self-reports these; raising any exception marks the run ``failed``.
        """
        ...


class RunConflictError(Exception):
    """A run id is reused with a different input (no silent overwrite)."""


class RunnerError(Exception):
    """A runner lifecycle error (invalid run id, write failure, etc.)."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of one benchmark run."""

    run_id: str
    run_dir: Path
    status: RunStatus
    agent_answer_status: AgentAnswerStatus | None
    policy_valid: bool
    started_at: str
    ended_at: str
    metrics: dict[str, int]
    manifest_path: Path
    note: str = ""


# --------------------------------------------------------------------------- #
# Public lifecycle entry point
# --------------------------------------------------------------------------- #


def execute_run(
    *,
    runs_root: Path,
    run_id: str,
    identity: RunIdentity,
    agent: AgentAdapter,
    policy_enforced: bool = True,
) -> RunResult:
    """Execute one benchmark run end-to-end and persist its artifact set.

    The Runner records ``started_at``/``ended_at``, runs the agent, produces
    ``raw-response.txt`` + ``agent-answer.json`` (via :mod:`runner.execution`),
    derives cost metrics from observed tool events into ``run-metadata.json``,
    validates tool policy into ``policy-result.json``, and writes
    ``manifest.json`` last. Judge artifacts are left ``absent`` for a later
    phase. Returns the terminal :class:`RunResult`.

    ``policy_enforced`` records that tool-policy enforcement occurred; it is
    mandatory (design §15.1), so ``False`` is rejected and the audit field is
    always ``true`` for a persisted run (AIS009-N1).
    """
    _validate_run_id(run_id)
    _validate_policy_enforced(policy_enforced)
    runs_root = Path(runs_root)
    run_dir = runs_root / run_id

    # Guard: same run id with a different input is never silently overwritten;
    # an already-complete identical run is returned idempotently (resume-safe);
    # an incomplete identical run is re-executed to completion. The full
    # immutable identity (incl. case_id/task_type) is verified for every
    # existing run via run-input.json, so a failed run with no agent-answer is
    # still protected (AIS009-R2).
    existing = _check_existing_run(run_dir, run_id, identity)
    if existing is not None:
        return existing

    started_at_dt = _now_utc()
    started_at = _iso_z(started_at_dt)
    run_dir.mkdir(parents=True, exist_ok=True)
    # Persist the immutable input identity BEFORE execution so the no-overwrite
    # guard can verify it for every terminal run, including a failed run that
    # produces no agent-answer (AIS009-R2).
    _write_run_input(run_dir, identity)

    # Execute the agent (Runner's observation channel). Any failure -> failed.
    try:
        outcome = agent.execute(
            case_id=identity.case_id,
            task_type=identity.task_type,
            tool_policy=identity.tool_policy,
        )
        execution_error: str | None = None
    except Exception as exc:  # noqa: BLE001 - any agent failure is a failed run
        outcome = None
        execution_error = f"{type(exc).__name__}: {exc}"

    ended_at_dt = _now_utc()
    ended_at = _iso_z(ended_at_dt)
    elapsed_ms = max(0, int((ended_at_dt - started_at_dt).total_seconds() * 1000))

    if outcome is None:
        return _finalize_failed_run(
            run_dir=run_dir,
            run_id=run_id,
            identity=identity,
            started_at=started_at,
            ended_at=ended_at,
            elapsed_ms=elapsed_ms,
            error=execution_error or "",
            policy_enforced=policy_enforced,
        )

    # Validate rejectable policy inputs BEFORE any artifact write (AIS009-R1):
    # an unknown tool_policy or untrusted tool-event source is an adapter error.
    # Failing here - before raw-response/answer/metadata exist - keeps the
    # failed-run finalizer's terminal persistence truthful (the manifest's absent
    # raw_response/agent_answer and zero metrics reflect reality) and makes
    # execute_run and load_run_result agree on FAILED.
    try:
        validate_policy_inputs(
            tool_policy=identity.tool_policy,
            tool_events=outcome.tool_events,
        )
    except PolicyValidationError as exc:
        return _finalize_failed_run(
            run_dir=run_dir,
            run_id=run_id,
            identity=identity,
            started_at=started_at,
            ended_at=ended_at,
            elapsed_ms=elapsed_ms,
            error=f"policy_validation_error: {exc}",
            policy_enforced=policy_enforced,
        )

    # Produce agent artifacts (raw-response.txt + agent-answer.json).
    produced = produce_agent_artifacts(
        outcome.raw_response,
        case_id=identity.case_id,
        task_type=identity.task_type,
        run_dir=run_dir,
    )

    metrics = derive_metrics(
        tool_events=outcome.tool_events,
        elapsed_ms=elapsed_ms,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )
    metadata_doc = _build_run_metadata(identity, started_at, ended_at, metrics, policy_enforced)
    metadata_bytes = _canonical_json_bytes(metadata_doc)
    metadata_path = run_dir / RUN_METADATA_FILENAME
    _atomic_write_bytes(metadata_path, metadata_bytes)

    # Inputs were pre-validated before the artifact writes, so this full
    # evaluation cannot raise a PolicyValidationError over already-persisted
    # artifacts; it only produces the compliance verdict (AIS009-R1).
    policy = validate_policy(
        tool_policy=identity.tool_policy,
        tool_events=outcome.tool_events,
        agent_answer_status=produced.status.value,
    )
    policy_bytes = _canonical_json_bytes(policy.to_doc())
    policy_path = run_dir / POLICY_RESULT_FILENAME
    _atomic_write_bytes(policy_path, policy_bytes)

    status = _resolve_status(policy=policy, answer_status=produced.status)

    manifest_doc = _build_manifest(
        run_id=run_id,
        raw_response_present=True,
        raw_response_digest=produced.raw_response_sha256,
        agent_answer_present=True,
        agent_answer_digest=produced.agent_answer_sha256 or "",
        metadata_digest=digest_bytes(metadata_bytes),
        policy_digest=digest_bytes(policy_bytes),
    )
    manifest_bytes = _canonical_json_bytes(manifest_doc)
    manifest_path = run_dir / MANIFEST_FILENAME
    _atomic_write_bytes(manifest_path, manifest_bytes)

    return RunResult(
        run_id=run_id,
        run_dir=run_dir,
        status=status,
        agent_answer_status=produced.status,
        policy_valid=policy.valid,
        started_at=started_at,
        ended_at=ended_at,
        metrics=metrics,
        manifest_path=manifest_path,
        note=produced.note,
    )


# --------------------------------------------------------------------------- #
# Run status resolution and failed-run finalization
# --------------------------------------------------------------------------- #


def _resolve_status(*, policy: PolicyResult, answer_status: AgentAnswerStatus) -> RunStatus:
    """Map a successful execution's policy verdict + answer status to a run state.

    Execution success is assumed (the caller routes failures to
    ``_finalize_failed_run``). A policy/admission violation -> ``invalid``;
    an admissible empty/refused answer -> ``valid`` (deterministic 0, §12);
    a substantive answer -> ``awaiting-judge``.
    """
    if not policy.valid:
        return RunStatus.INVALID
    if answer_status in (AgentAnswerStatus.EMPTY, AgentAnswerStatus.REFUSED):
        return RunStatus.VALID
    return RunStatus.AWAITING_JUDGE


def _finalize_failed_run(
    *,
    run_dir: Path,
    run_id: str,
    identity: RunIdentity,
    started_at: str,
    ended_at: str,
    elapsed_ms: int,
    error: str,
    policy_enforced: bool,
) -> RunResult:
    """Persist a failed run: metadata + policy-result + manifest (no answer).

    The raw response and agent-answer artifacts are absent; the manifest is
    still written (last, atomically) so the failure is recorded and a restart is
    idempotent-safe rather than mistaking the run for half-done.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = derive_metrics(
        tool_events=(),
        elapsed_ms=elapsed_ms,
        input_tokens=0,
        output_tokens=0,
    )
    metadata_doc = _build_run_metadata(identity, started_at, ended_at, metrics, policy_enforced)
    metadata_bytes = _canonical_json_bytes(metadata_doc)
    metadata_path = run_dir / RUN_METADATA_FILENAME
    _atomic_write_bytes(metadata_path, metadata_bytes)

    policy = PolicyResult(
        valid=False,
        violations=[
            Violation(
                code=EXECUTION_FAILED_CODE,
                message=f"Agent execution failed before producing a response: {error}",
            )
        ],
        observations=[f"Agent execution failed: {error}"],
    )
    policy_bytes = _canonical_json_bytes(policy.to_doc())
    policy_path = run_dir / POLICY_RESULT_FILENAME
    _atomic_write_bytes(policy_path, policy_bytes)

    manifest_doc = _build_manifest(
        run_id=run_id,
        raw_response_present=False,
        raw_response_digest="",
        agent_answer_present=False,
        agent_answer_digest="",
        metadata_digest=digest_bytes(metadata_bytes),
        policy_digest=digest_bytes(policy_bytes),
    )
    manifest_bytes = _canonical_json_bytes(manifest_doc)
    manifest_path = run_dir / MANIFEST_FILENAME
    _atomic_write_bytes(manifest_path, manifest_bytes)

    return RunResult(
        run_id=run_id,
        run_dir=run_dir,
        status=RunStatus.FAILED,
        agent_answer_status=None,
        policy_valid=False,
        started_at=started_at,
        ended_at=ended_at,
        metrics=metrics,
        manifest_path=manifest_path,
        note=f"execution_failed:{error}",
    )


# --------------------------------------------------------------------------- #
# Interrupt/resume and no-overwrite guard
# --------------------------------------------------------------------------- #


def _check_existing_run(
    run_dir: Path, run_id: str, identity: RunIdentity
) -> RunResult | None:
    """Enforce no-silent-overwrite and idempotent resume.

    * No recorded identity                            -> ``None`` (fresh; execute).
    * Different input (any recorded identity field)   -> ``RunConflictError``.
    * Identical input, complete (manifest present)    -> :class:`RunResult`
      (idempotent; do not re-execute).
    * Identical input, incomplete (no manifest)       -> ``None`` (resume; execute).

    The authoritative input-identity record is ``run-input.json`` (written
    before execution for every run, so the full immutable identity - including
    ``case_id``/``task_type`` - is checked even for a failed run with no
    agent-answer, AIS009-R2). Runs persisted without that sidecar (or with an
    unreadable one) fall back to ``run-metadata.json`` (``tool_policy``/
    ``agent``/``agent_model``) plus, when present, the agent-answer's
    ``case_id``/``task_type``. A run with unreadable identity records from a
    crashed write is treated as fresh (re-execute).
    """
    # Primary path: the run-input.json sidecar carries the full identity.
    input_path = run_dir / RUN_INPUT_FILENAME
    if input_path.exists():
        try:
            recorded = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = None
        if recorded is not None:
            if not _recorded_identity_matches(recorded, identity):
                raise RunConflictError(
                    f"run id {run_id!r} already exists with a different input "
                    f"(case_id/task_type/tool_policy/agent/agent_model); "
                    f"refusing to overwrite"
                )
            # Identical input: a complete run is returned idempotently; an
            # incomplete run (no manifest, e.g. interrupted before the atomic
            # completion marker) is re-executed to completion.
            if (run_dir / MANIFEST_FILENAME).exists():
                return load_run_result(runs_root=run_dir.parent, run_id=run_id)
            return None

    # Legacy/fallback path: runs without a readable run-input.json are checked
    # via run-metadata (tool_policy/agent/agent_model) and, when the
    # agent-answer is present, its case_id/task_type.
    metadata_path = run_dir / RUN_METADATA_FILENAME
    if not metadata_path.exists():
        return None

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if (
        metadata.get("tool_policy") != identity.tool_policy
        or metadata.get("agent") != identity.agent
        or metadata.get("agent_model") != identity.agent_model
    ):
        raise RunConflictError(
            f"run id {run_id!r} already exists with a different input "
            f"(tool_policy/agent/agent_model); refusing to overwrite"
        )

    answer_path = run_dir / AGENT_ANSWER_FILENAME
    if answer_path.exists():
        try:
            answer = json.loads(answer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            answer = {}
        if answer.get("case_id") != identity.case_id or (
            answer.get("task_type") != identity.task_type
        ):
            raise RunConflictError(
                f"run id {run_id!r} already exists with a different input "
                f"(case_id/task_type); refusing to overwrite"
            )

    # Identical input: a complete run is returned idempotently; an incomplete
    # run (no manifest, e.g. interrupted before the atomic completion marker)
    # is re-executed to completion.
    if (run_dir / MANIFEST_FILENAME).exists():
        return load_run_result(runs_root=run_dir.parent, run_id=run_id)
    return None


def load_run_result(*, runs_root: Path, run_id: str) -> RunResult:
    """Reconstruct a :class:`RunResult` from a persisted run directory.

    The run status is re-derived from the persisted artifacts (it is not stored
    as a separate file): an absent agent-answer -> ``failed``; otherwise the
    policy verdict and answer status determine ``invalid`` / ``valid`` /
    ``awaiting-judge``. Used for resume/idempotency and external inspection.
    """
    _validate_run_id(run_id)
    run_dir = Path(runs_root) / run_id
    metadata_path = run_dir / RUN_METADATA_FILENAME
    if not metadata_path.exists():
        raise RunnerError(f"no run-metadata.json found for run id {run_id!r} at {run_dir}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    policy_path = run_dir / POLICY_RESULT_FILENAME
    policy_doc = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    policy_valid = bool(policy_doc.get("valid", False))

    answer_path = run_dir / AGENT_ANSWER_FILENAME
    if answer_path.exists():
        answer_doc = json.loads(answer_path.read_text(encoding="utf-8"))
        answer_status = AgentAnswerStatus(answer_doc["status"])
        if not policy_valid:
            status = RunStatus.INVALID
        elif answer_status in (AgentAnswerStatus.EMPTY, AgentAnswerStatus.REFUSED):
            status = RunStatus.VALID
        else:
            status = RunStatus.AWAITING_JUDGE
        agent_answer_status: AgentAnswerStatus | None = answer_status
    else:
        status = RunStatus.FAILED
        agent_answer_status = None

    return RunResult(
        run_id=run_id,
        run_dir=run_dir,
        status=status,
        agent_answer_status=agent_answer_status,
        policy_valid=policy_valid,
        started_at=metadata["started_at"],
        ended_at=metadata["ended_at"],
        metrics=dict(metadata["metrics"]),
        manifest_path=run_dir / MANIFEST_FILENAME,
        note="loaded_existing",
    )


# --------------------------------------------------------------------------- #
# Artifact document builders
# --------------------------------------------------------------------------- #


def _build_run_metadata(
    identity: RunIdentity,
    started_at: str,
    ended_at: str,
    metrics: dict[str, int],
    policy_enforced: bool,
) -> dict[str, Any]:
    """Build the run-metadata-v1 document (design §8.6).

    Carries only Runner-collected identity, timing and cost fields - no
    correctness, answer or policy content (storage-layer isolation).
    """
    return {
        "schema_version": RUN_METADATA_SCHEMA_VERSION,
        "agent": identity.agent,
        "agent_model": identity.agent_model,
        "tool_policy": identity.tool_policy,
        "policy_enforced": policy_enforced,
        "started_at": started_at,
        "ended_at": ended_at,
        "metrics": metrics,
    }


def _build_run_input(identity: RunIdentity) -> dict[str, Any]:
    """Build the run-input.json document: the immutable input-identity record.

    Unlike ``run-metadata.json`` (which the schema restricts to agent/model/
    policy + cost), this carries the FULL :class:`RunIdentity` including
    ``case_id``/``task_type``, so the no-overwrite guard can verify the complete
    input identity for every terminal run - including a failed run that has no
    agent-answer to fall back on (AIS009-R2). It is runner-internal guard state,
    not a manifest/scored artifact, and carries no correctness/cost/policy
    content.
    """
    return {
        "schema_version": RUN_INPUT_SCHEMA_VERSION,
        "case_id": identity.case_id,
        "task_type": identity.task_type,
        "tool_policy": identity.tool_policy,
        "agent": identity.agent,
        "agent_model": identity.agent_model,
    }


def _write_run_input(run_dir: Path, identity: RunIdentity) -> None:
    """Atomically persist the run-input.json identity record (AIS009-R2)."""
    doc = _canonical_json_bytes(_build_run_input(identity))
    _atomic_write_bytes(run_dir / RUN_INPUT_FILENAME, doc)


def _recorded_identity_matches(recorded: dict[str, Any], identity: RunIdentity) -> bool:
    """Whether a persisted run-input.json record matches the requested identity."""
    return (
        recorded.get("case_id") == identity.case_id
        and recorded.get("task_type") == identity.task_type
        and recorded.get("tool_policy") == identity.tool_policy
        and recorded.get("agent") == identity.agent
        and recorded.get("agent_model") == identity.agent_model
    )


def _build_manifest(
    *,
    run_id: str,
    raw_response_present: bool,
    raw_response_digest: str,
    agent_answer_present: bool,
    agent_answer_digest: str,
    metadata_digest: str,
    policy_digest: str,
) -> dict[str, Any]:
    """Build the manifest-v1 document (design §17).

    Present artifacts carry a run-relative path (``<run_id>/<filename>``) and a
    sha256 digest; absent artifacts carry status only (no placeholder files).
    Judge artifacts are ``absent`` (later phase); adjudication is
    ``not_applicable`` (§14).
    """
    artifacts: list[dict[str, Any]] = []

    def present(name: str, filename: str, digest: str) -> None:
        artifacts.append(
            {"name": name, "status": "present", "path": f"{run_id}/{filename}", "sha256": digest}
        )

    def absent(name: str) -> None:
        artifacts.append({"name": name, "status": "absent"})

    if raw_response_present:
        present("raw_response", RAW_RESPONSE_FILENAME, raw_response_digest)
    else:
        absent("raw_response")
    if agent_answer_present:
        present("agent_answer", AGENT_ANSWER_FILENAME, agent_answer_digest)
    else:
        absent("agent_answer")
    present("run_metadata", RUN_METADATA_FILENAME, metadata_digest)
    present("policy_result", POLICY_RESULT_FILENAME, policy_digest)
    for name in _JUDGE_ARTIFACT_NAMES:
        absent(name)
    artifacts.append({"name": "adjudication", "status": "not_applicable"})

    return {"schema_version": MANIFEST_SCHEMA_VERSION, "artifacts": artifacts}


# --------------------------------------------------------------------------- #
# Atomic I/O, digests and time helpers
# --------------------------------------------------------------------------- #


def _canonical_json_bytes(doc: dict[str, Any]) -> bytes:
    """Canonical, stable JSON bytes (sorted keys, indent 2, trailing newline)."""
    return (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` via a temp file + ``os.replace``.

    The final path is touched only by the atomic replace, so an interrupted
    write leaves at most a temp file (cleaned up here), never a final artifact
    that could be mistaken for complete (interrupt/resume safety).
    """
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp = Path(fh.name)
            fh.write(data)
            fh.flush()
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RunnerError(f"failed to atomically write {path}: {exc}") from exc


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """ISO-8601 UTC with a trailing ``Z`` (RFC 3339 date-time, run-metadata)."""
    return dt.isoformat().replace("+00:00", "Z")


def _validate_run_id(run_id: str) -> None:
    """A run id must be a single safe path component (no traversal/separators)."""
    if not isinstance(run_id, str) or not run_id:
        raise RunnerError("run_id must be a non-empty string")
    if "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise RunnerError(f"run_id must be a single path component, got {run_id!r}")


def _validate_policy_enforced(policy_enforced: bool) -> None:
    """Tool-policy enforcement is mandatory (design §15.1, invariants).

    The ``policy_enforced`` audit field records that the Runner enforced tool
    policy for this run; validation always occurs, so the field is always
    ``true`` for a persisted run. A caller requesting ``policy_enforced=False``
    is asking to bypass mandatory Graph/Grep compliance enforcement, which is
    unsupported; reject it so the audit field can never lie (AIS009-N1).
    """
    if not policy_enforced:
        raise RunnerError(
            "policy_enforced=False is not supported: tool-policy enforcement is "
            "mandatory (design §15.1); run-metadata policy_enforced always "
            "records that enforcement occurred"
        )


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level benchmark argument parser."""
    parser = argparse.ArgumentParser(
        prog="graphbenchmark",
        description=(
            "GraphBenchmark AI scoring runner (semantic_outcome_v1). "
            "Execution, judging and reporting are added by later tasks."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark CLI and return an exit code.

    v1 ships a minimal parser; the reusable lifecycle API is :func:`execute_run`
    (used directly by tests and future CLI wiring). Concrete CLI subcommands that
    load cases, select an agent adapter and dispatch :func:`execute_run` are
    added by later tasks.
    """
    parser = build_parser()
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
