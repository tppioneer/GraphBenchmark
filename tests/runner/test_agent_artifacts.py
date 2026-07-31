"""AIS-003: raw response persistence and agent-answer production.

Covers every acceptance criterion in docs/tasks/AIS-003-agent-artifact-ingestion.md:

* valid JSON is validated and canonically written;
* invalid JSON but non-empty Markdown is wrapped as ``completed_with_schema_warning``;
* whitespace, unreadable encoding and I/O failure have distinct auditable states;
* finding evidence references are verified when present, and missing optional
  arrays never create empty placeholder files;
* the raw text is byte-traceable with a digest;
* an interrupted write cannot leave a final JSON mistaken for complete.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runner import execution as exe
from runner.artifact_validation import (
    ArtifactContractError,
    assert_agent_answer_contract,
)

from . import fixtures as fx

RAW = "raw-response.txt"
ANSWER = "agent-answer.json"


# --------------------------------------------------------------------------- #
# Valid JSON -> completed
# --------------------------------------------------------------------------- #


def test_valid_json_completed_canonical_and_validated(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED
    assert result.note == "parsed_valid"

    answer_path = tmp_path / ANSWER
    assert answer_path.exists()
    # Canonical write: stable, sorted, trailing newline.
    text = answer_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert (
        text
        == json.dumps(fx.completed_answer_doc(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    # Parsed content is preserved exactly (key order is irrelevant to equality).
    assert json.loads(text) == fx.completed_answer_doc()
    # The written artifact is independently schema-valid.
    assert not list(fx.agent_answer_validator().iter_errors(json.loads(text)))


def test_completed_result_status_from_document(tmp_path: Path) -> None:
    doc = fx.completed_answer_doc()
    doc["status"] = "refused"  # schema-valid status the model may declare
    doc["answer"]["summary"] = ""
    doc["answer"]["explanation"] = ""
    result = exe.produce_agent_artifacts(
        json.dumps(doc, ensure_ascii=False).encode("utf-8"),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.REFUSED


# --------------------------------------------------------------------------- #
# Invalid JSON but non-empty Markdown -> completed_with_schema_warning
# --------------------------------------------------------------------------- #


def test_invalid_json_markdown_wrapped_as_schema_warning(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.MARKDOWN_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "json_parse_error"

    doc = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert doc["status"] == "completed_with_schema_warning"
    # Only the model's real content is stored; nothing is fabricated.
    assert doc["answer"]["explanation"] == fx.MARKDOWN_BYTES.decode("utf-8")
    assert doc["answer"]["summary"] == ""
    assert doc["answer"]["findings"] == []
    assert doc["answer"]["limitations"] == []
    assert doc["answer"]["recommended_actions"] == []
    assert doc["evidence"] == []
    # The wrapper is itself schema-valid.
    assert not list(fx.agent_answer_validator().iter_errors(doc))


def test_schema_invalid_json_downgraded(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.schema_invalid_json_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "schema_invalid"
    doc = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert doc["answer"]["explanation"] == fx.schema_invalid_json_bytes().decode("utf-8")
    assert not list(fx.agent_answer_validator().iter_errors(doc))


def test_json_not_object_downgraded(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.JSON_LIST_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "json_not_object"


# --------------------------------------------------------------------------- #
# Distinct auditable error states: empty / unreadable / I/O failure
# --------------------------------------------------------------------------- #


def test_empty_whitespace_status_empty(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.WHITESPACE_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.EMPTY
    assert result.note == "empty_or_whitespace"
    doc = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert doc["status"] == "empty"
    assert doc["answer"]["summary"] == ""
    assert doc["answer"]["explanation"] == ""
    assert not list(fx.agent_answer_validator().iter_errors(doc))


def test_unreadable_encoding_status_invalid(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.INVALID_UTF8_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.INVALID
    assert result.note == "unreadable_encoding:utf-8"
    # raw-response.txt still holds the exact unreadable bytes.
    assert (tmp_path / RAW).read_bytes() == fx.INVALID_UTF8_BYTES
    doc = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert doc["status"] == "invalid"
    assert not list(fx.agent_answer_validator().iter_errors(doc))


def test_io_failure_uncreatable_run_dir(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    run_dir = blocker / "sub"
    with pytest.raises(exe.ArtifactWriteError):
        exe.produce_agent_artifacts(
            fx.completed_answer_bytes(),
            case_id=fx.CASE_ID,
            task_type=fx.TASK_TYPE,
            run_dir=run_dir,
        )
    assert not run_dir.exists()


def test_atomic_write_interruption_leaves_no_complete_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted agent-answer write must not leave a complete-looking JSON."""
    real_replace = exe.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # 1st replace = raw-response.txt (succeeds); 2nd = agent-answer.json.
        if calls["n"] == 2:
            raise OSError("simulated interruption during agent-answer replace")
        return real_replace(src, dst)

    monkeypatch.setattr(exe.os, "replace", flaky_replace)

    with pytest.raises(exe.ArtifactWriteError):
        exe.produce_agent_artifacts(
            fx.completed_answer_bytes(),
            case_id=fx.CASE_ID,
            task_type=fx.TASK_TYPE,
            run_dir=tmp_path,
        )

    # raw-response.txt was already saved.
    assert (tmp_path / RAW).exists()
    # No final agent-answer.json can be mistaken for complete.
    assert not (tmp_path / ANSWER).exists()
    # No leftover temp files.
    assert not list(tmp_path.glob(".*.tmp"))


def test_distinct_error_states_for_empty_unreadable_io(tmp_path: Path) -> None:
    """empty / unreadable / I/O failure produce three distinguishable states."""
    empty = exe.produce_agent_artifacts(
        fx.WHITESPACE_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path / "empty",
    )
    unreadable = exe.produce_agent_artifacts(
        fx.INVALID_UTF8_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path / "unreadable",
    )
    assert empty.status is exe.AgentAnswerStatus.EMPTY
    assert unreadable.status is exe.AgentAnswerStatus.INVALID
    assert empty.status != unreadable.status
    # Both wrote an agent-answer.json; I/O failure (below) does not.
    assert (tmp_path / "empty" / ANSWER).exists()
    assert (tmp_path / "unreadable" / ANSWER).exists()

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(exe.ArtifactWriteError):
        exe.produce_agent_artifacts(
            fx.completed_answer_bytes(),
            case_id=fx.CASE_ID,
            task_type=fx.TASK_TYPE,
            run_dir=blocker / "sub",
        )
    assert not (blocker / "sub" / ANSWER).exists()


# --------------------------------------------------------------------------- #
# Evidence references and no placeholder files
# --------------------------------------------------------------------------- #


def test_evidence_references_verified_when_present(tmp_path: Path) -> None:
    # Broken reference -> downgraded to schema-warning (contract not trusted).
    result = exe.produce_agent_artifacts(
        fx.broken_evidence_ref_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "contract_invalid_evidence_refs"
    doc = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert not list(fx.agent_answer_validator().iter_errors(doc))


def test_valid_evidence_references_pass_completed(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED


def test_no_extra_placeholder_files(tmp_path: Path) -> None:
    exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {RAW, ANSWER}
    # No empty placeholder files for optional artifacts (findings/evidence/etc.).


def test_no_extra_placeholder_files_for_wrapper(tmp_path: Path) -> None:
    exe.produce_agent_artifacts(
        fx.MARKDOWN_BYTES,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {RAW, ANSWER}


# --------------------------------------------------------------------------- #
# Byte-traceability and digests
# --------------------------------------------------------------------------- #


def test_raw_response_byte_traceable_with_digest(tmp_path: Path) -> None:
    payload = fx.completed_answer_bytes()
    result = exe.produce_agent_artifacts(
        payload,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    # Byte-for-byte traceable.
    assert (tmp_path / RAW).read_bytes() == payload
    # Digest matches manifest form and recomputed value.
    assert result.raw_response_sha256 == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert result.raw_response_sha256.startswith("sha256:")
    assert len(result.raw_response_sha256) == len("sha256:") + 64


def test_agent_answer_digest_matches_file_bytes(tmp_path: Path) -> None:
    result = exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    file_bytes = (tmp_path / ANSWER).read_bytes()
    assert result.agent_answer_sha256 == "sha256:" + hashlib.sha256(file_bytes).hexdigest()


def test_raw_response_saved_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """raw-response.txt must exist before JSON parsing begins (invariant)."""
    real_loads = exe.json.loads
    state: dict[str, bool] = {}

    def spy_loads(s: str) -> object:
        state["raw_exists_at_parse"] = (tmp_path / RAW).exists()
        return real_loads(s)

    monkeypatch.setattr(exe.json, "loads", spy_loads)
    exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert state["raw_exists_at_parse"] is True


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #


def test_invalid_task_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        exe.produce_agent_artifacts(
            fx.completed_answer_bytes(),
            case_id=fx.CASE_ID,
            task_type="unknown",
            run_dir=tmp_path,
        )


def test_empty_case_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        exe.produce_agent_artifacts(
            fx.completed_answer_bytes(),
            case_id="",
            task_type=fx.TASK_TYPE,
            run_dir=tmp_path,
        )


# --------------------------------------------------------------------------- #
# Runner-authoritative identity (AIS003-R1)
# --------------------------------------------------------------------------- #


def test_case_id_mismatch_downgraded(tmp_path: Path) -> None:
    """A schema-valid doc whose case_id differs from the authoritative arg is
    downgraded; the artifact carries Runner-authoritative identity, never the
    model's claimed case_id."""
    doc = fx.completed_answer_doc()
    doc["case_id"] = "some-other-case-id"
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    result = exe.produce_agent_artifacts(
        raw,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "identity_mismatch:case_id"
    written = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    # Authoritative identity, not the model's claim.
    assert written["case_id"] == fx.CASE_ID
    assert written["task_type"] == fx.TASK_TYPE
    # Raw model text preserved verbatim in the degradation wrapper.
    assert written["answer"]["explanation"] == raw.decode("utf-8")
    # The wrapper is itself schema-valid.
    assert not list(fx.agent_answer_validator().iter_errors(written))


def test_task_type_mismatch_downgraded(tmp_path: Path) -> None:
    """A schema-valid doc whose task_type differs from the authoritative arg is
    downgraded; the artifact carries Runner-authoritative identity.

    The doc carries no findings, so it is schema-valid under the model's
    claimed task_type (impact_analysis) and the mismatch is caught purely by
    the identity check rather than the task_type-conditional finding enum.
    """
    doc = fx.completed_answer_doc()
    doc["task_type"] = "impact_analysis"
    doc["answer"]["findings"] = []
    doc["evidence"] = []
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    result = exe.produce_agent_artifacts(
        raw,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "identity_mismatch:task_type"
    written = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert written["case_id"] == fx.CASE_ID
    assert written["task_type"] == fx.TASK_TYPE
    assert not list(fx.agent_answer_validator().iter_errors(written))


def test_both_identity_fields_mismatch_downgraded(tmp_path: Path) -> None:
    """Both fields mismatching are reported together in the audit note."""
    doc = fx.completed_answer_doc()
    doc["case_id"] = "other-case"
    doc["task_type"] = "flow_tracing"
    doc["answer"]["findings"] = []
    doc["evidence"] = []
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    result = exe.produce_agent_artifacts(
        raw,
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED_WITH_SCHEMA_WARNING
    assert result.note == "identity_mismatch:case_id,task_type"


def test_matching_identity_completed_carries_authoritative_identity(
    tmp_path: Path,
) -> None:
    """A schema-valid doc with matching case_id/task_type is accepted as
    completed and the artifact carries Runner-authoritative identity."""
    result = exe.produce_agent_artifacts(
        fx.completed_answer_bytes(),
        case_id=fx.CASE_ID,
        task_type=fx.TASK_TYPE,
        run_dir=tmp_path,
    )
    assert result.status is exe.AgentAnswerStatus.COMPLETED
    assert result.note == "parsed_valid"
    written = json.loads((tmp_path / ANSWER).read_text(encoding="utf-8"))
    assert written["case_id"] == fx.CASE_ID
    assert written["task_type"] == fx.TASK_TYPE


# --------------------------------------------------------------------------- #
# Direct artifact_validation contract
# --------------------------------------------------------------------------- #


def test_assert_contract_accepts_valid_doc() -> None:
    assert_agent_answer_contract(fx.completed_answer_doc())  # no raise


def test_assert_contract_rejects_identity_leak() -> None:
    bad = fx.completed_answer_doc()
    bad["agent_model"] = "glm-5.2"  # Runner-collected field smuggled in
    with pytest.raises(ArtifactContractError) as exc:
        assert_agent_answer_contract(bad)
    assert any(issue.pointer == "/" for issue in exc.value.issues)


def test_assert_contract_rejects_broken_evidence_ref() -> None:
    bad = fx.completed_answer_doc()
    bad["answer"]["findings"][0]["evidence_ids"] = ["evidence-missing"]
    with pytest.raises(ArtifactContractError) as exc:
        assert_agent_answer_contract(bad)
    pointers = {issue.pointer for issue in exc.value.issues}
    assert "/answer/findings/0/evidence_ids/0" in pointers
