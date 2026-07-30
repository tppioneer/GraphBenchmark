"""Execution layer: persist the raw model response and produce agent-answer.json.

Invariants (AIS-003, design §8.8, §17):

* ``raw-response.txt`` is always saved BEFORE parsing.
* No fallback to the legacy string scorer.
* The natural-language degradation wrapper carries only the model's real
  content; it never fabricates evidence, findings or summary.
* Schema compliance and semantic correctness are distinct states.
* Writes are atomic: an interrupted write cannot leave a final JSON that
  could be mistaken for a complete artifact.

Distinct auditable error states (acceptance criterion):

* empty / whitespace-only input  -> status ``empty`` (artifact written);
* unreadable encoding            -> status ``invalid`` (artifact written);
* I/O failure                    -> :class:`ArtifactWriteError` (no partial
  final artifact; ``raw-response.txt`` may already be present).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .artifact_validation import (
    agent_answer_schema_issues,
    evidence_reference_issues,
)

RAW_RESPONSE_FILENAME = "raw-response.txt"
AGENT_ANSWER_FILENAME = "agent-answer.json"
SCHEMA_VERSION = "agent-answer-v1"
TASK_TYPES = ("flow_tracing", "bug_localization", "impact_analysis")


class AgentAnswerStatus(str, Enum):
    """The agent-answer status enum (mirrors agent-answer.schema.json)."""

    COMPLETED = "completed"
    COMPLETED_WITH_SCHEMA_WARNING = "completed_with_schema_warning"
    EMPTY = "empty"
    REFUSED = "refused"
    INVALID = "invalid"


class ArtifactWriteError(Exception):
    """I/O failure while persisting a run artifact (auditable error state)."""


@dataclass(frozen=True)
class ProduceResult:
    """Outcome of producing run artifacts for one model response."""

    run_dir: Path
    status: AgentAnswerStatus
    raw_response_path: Path
    raw_response_sha256: str
    agent_answer_path: Path | None
    agent_answer_sha256: str | None
    note: str


def produce_agent_artifacts(
    raw_bytes: bytes,
    *,
    case_id: str,
    task_type: str,
    run_dir: Path,
    encoding: str = "utf-8",
) -> ProduceResult:
    """Persist ``raw-response.txt`` then produce ``agent-answer.json``.

    The raw response is always saved first and is byte-traceable with a
    sha256 digest. The agent answer is then derived: valid conforming JSON
    becomes a ``completed`` artifact; non-empty but unparseable or
    schema/contract-invalid content is wrapped as
    ``completed_with_schema_warning`` (raw text preserved verbatim in
    ``explanation``); empty/whitespace becomes ``empty``; unreadable bytes
    become ``invalid``. I/O failures raise :class:`ArtifactWriteError` and
    leave no partial final artifact.
    """
    if not case_id:
        raise ValueError("case_id must be a non-empty string")
    if task_type not in TASK_TYPES:
        raise ValueError(f"task_type must be one of {TASK_TYPES}, got {task_type!r}")

    run_dir = Path(run_dir)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactWriteError(f"failed to create run_dir {run_dir}: {exc}") from exc

    # 1. Save raw-response.txt FIRST, before any parsing (invariant).
    raw_response_path = run_dir / RAW_RESPONSE_FILENAME
    _atomic_write_bytes(raw_response_path, raw_bytes)
    raw_sha = _sha256_digest(raw_bytes)

    # 2. Decode. Unreadable encoding is a distinct auditable state (invalid).
    try:
        text = raw_bytes.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        doc = _build_status_doc(case_id, task_type, AgentAnswerStatus.INVALID)
        return _write_and_return(
            run_dir,
            AgentAnswerStatus.INVALID,
            doc,
            raw_response_path,
            raw_sha,
            note=f"unreadable_encoding:{encoding}",
        )

    # 3. Empty/whitespace is a distinct auditable state (empty).
    if not text.strip():
        doc = _build_status_doc(case_id, task_type, AgentAnswerStatus.EMPTY)
        return _write_and_return(
            run_dir,
            AgentAnswerStatus.EMPTY,
            doc,
            raw_response_path,
            raw_sha,
            note="empty_or_whitespace",
        )

    # 4. Try structured JSON.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _wrap_schema_warning(
            run_dir,
            case_id,
            task_type,
            text,
            raw_response_path,
            raw_sha,
            note="json_parse_error",
        )

    if not isinstance(parsed, dict):
        return _wrap_schema_warning(
            run_dir,
            case_id,
            task_type,
            text,
            raw_response_path,
            raw_sha,
            note="json_not_object",
        )

    # 5. Validate schema + evidence contract. Any failure downgrades to the
    #    schema-warning wrapper; the model's raw text is preserved verbatim
    #    in explanation so the AI Judge can still evaluate it (§8.8).
    schema_issues = agent_answer_schema_issues(parsed)
    evidence_issues = evidence_reference_issues(parsed)
    if schema_issues or evidence_issues:
        if evidence_issues and not schema_issues:
            note = "contract_invalid_evidence_refs"
        elif schema_issues and not evidence_issues:
            note = "schema_invalid"
        else:
            note = "schema_and_contract_invalid"
        return _wrap_schema_warning(
            run_dir, case_id, task_type, text, raw_response_path, raw_sha, note=note
        )

    # 6. Valid structured answer -> canonical write. The model's declared
    #    status is schema-validated, so it is a known enum value.
    canonical = _canonical_json(parsed)
    agent_path = run_dir / AGENT_ANSWER_FILENAME
    _atomic_write_text(agent_path, canonical)
    agent_sha = _sha256_digest(canonical.encode("utf-8"))
    return ProduceResult(
        run_dir=run_dir,
        status=AgentAnswerStatus(parsed["status"]),
        raw_response_path=raw_response_path,
        raw_response_sha256=raw_sha,
        agent_answer_path=agent_path,
        agent_answer_sha256=agent_sha,
        note="parsed_valid",
    )


# --------------------------------------------------------------------------- #
# Document builders
# --------------------------------------------------------------------------- #


def _build_status_doc(case_id: str, task_type: str, status: AgentAnswerStatus) -> dict[str, Any]:
    """Minimal schema-valid doc for empty/invalid runs (no fabricated content)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "task_type": task_type,
        "status": status.value,
        "answer": {"summary": "", "explanation": ""},
    }


def _build_schema_warning_doc(case_id: str, task_type: str, raw_text: str) -> dict[str, Any]:
    """The §8.8 degradation wrapper: raw model text in explanation, no fabrication."""
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "task_type": task_type,
        "status": AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING.value,
        "answer": {
            "summary": "",
            "explanation": raw_text,
            "findings": [],
            "limitations": [],
            "recommended_actions": [],
        },
        "evidence": [],
    }


def _wrap_schema_warning(
    run_dir: Path,
    case_id: str,
    task_type: str,
    text: str,
    raw_response_path: Path,
    raw_sha: str,
    *,
    note: str,
) -> ProduceResult:
    doc = _build_schema_warning_doc(case_id, task_type, text)
    return _write_and_return(
        run_dir,
        AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING,
        doc,
        raw_response_path,
        raw_sha,
        note=note,
    )


def _write_and_return(
    run_dir: Path,
    status: AgentAnswerStatus,
    doc: dict[str, Any],
    raw_response_path: Path,
    raw_sha: str,
    *,
    note: str,
) -> ProduceResult:
    canonical = _canonical_json(doc)
    agent_path = run_dir / AGENT_ANSWER_FILENAME
    _atomic_write_text(agent_path, canonical)
    agent_sha = _sha256_digest(canonical.encode("utf-8"))
    return ProduceResult(
        run_dir=run_dir,
        status=status,
        raw_response_path=raw_response_path,
        raw_response_sha256=raw_sha,
        agent_answer_path=agent_path,
        agent_answer_sha256=agent_sha,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Atomic I/O and digests
# --------------------------------------------------------------------------- #


def _canonical_json(doc: dict[str, Any]) -> str:
    """Canonical, stable JSON text (sorted keys, indent 2, trailing newline)."""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256_digest(data: bytes) -> str:
    """Return the ``sha256:<64-hex>`` digest form used by the manifest contract."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write(path: Path, payload: bytes) -> None:
    """Atomically write ``payload`` to ``path`` via a temp file + ``os.replace``.

    The final path is touched only by the atomic replace, so an interrupted
    write can leave at most a temp file (cleaned up here), never a final JSON
    that could be mistaken for a complete artifact.
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
            fh.write(payload)
            fh.flush()
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise ArtifactWriteError(f"failed to atomically write {path}: {exc}") from exc
