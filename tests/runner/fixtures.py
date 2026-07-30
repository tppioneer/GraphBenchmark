"""Raw-response fixtures and an independent schema validator for AIS-003 tests.

The validator here is built directly from ``schemas/agent-answer.schema.json``
so that tests do not rely on ``runner.artifact_validation`` to vouch for the
artifact it just produced (an independent check of the same contract).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"

CASE_ID = "qwenpaw-case-z-corrupt-inbox-recovery-bug"
TASK_TYPE = "bug_localization"

_VALIDATOR: Draft202012Validator | None = None


def agent_answer_validator() -> Draft202012Validator:
    """A fresh-ish independent validator for the agent-answer schema."""
    global _VALIDATOR
    if _VALIDATOR is None:
        with (SCHEMA_DIR / "agent-answer.schema.json").open(encoding="utf-8") as fh:
            _VALIDATOR = Draft202012Validator(json.load(fh))
    return _VALIDATOR


def completed_answer_doc() -> dict[str, Any]:
    """A schema-valid, contract-valid completed agent answer (bug_localization)."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": CASE_ID,
        "task_type": TASK_TYPE,
        "status": "completed",
        "answer": {
            "summary": "损坏的 inbox JSON 会在共享读取函数中触发解析异常。",
            "explanation": (
                "列表接口和 append 路径都依赖同一个读取函数，因此损坏文件"
                "不仅影响读取，也会阻止后续事件写入。"
            ),
            "findings": [
                {
                    "id": "finding-1",
                    "kind": "root_cause",
                    "claim": "_load_events 未处理损坏 JSON，是直接根因。",
                    "evidence_ids": ["evidence-1"],
                },
            ],
            "limitations": [],
            "recommended_actions": ["增加损坏 JSON 的恢复和隔离策略。"],
        },
        "evidence": [
            {
                "id": "evidence-1",
                "file": "src/qwenpaw/app/inbox_store.py",
                "symbol": "_load_events",
                "line": 42,
                "reason": "该函数直接调用 json.loads，且没有处理解析异常。",
            }
        ],
    }


def completed_answer_bytes() -> bytes:
    """The completed answer serialized as raw UTF-8 JSON bytes."""
    return json.dumps(completed_answer_doc(), ensure_ascii=False).encode("utf-8")


def broken_evidence_ref_bytes() -> bytes:
    """Valid JSON, schema-valid, but a finding cites a missing evidence id."""
    doc = copy.deepcopy(completed_answer_doc())
    doc["answer"]["findings"][0]["evidence_ids"] = ["evidence-missing"]
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def schema_invalid_json_bytes() -> bytes:
    """Valid JSON object missing required agent-answer fields."""
    return b'{"schema_version":"agent-answer-v1","case_id":"x","task_type":"bug_localization"}'


# Non-empty Markdown that is not valid JSON.
MARKDOWN_BYTES = (
    b"## Root cause\n\n"
    b"The shared read function `_load_events` fails on corrupt JSON, "
    b"blocking both the list endpoint and subsequent appends."
)

# Whitespace-only output.
WHITESPACE_BYTES = b"   \n\t  \n"

# Bytes that are not valid UTF-8 (unreadable encoding).
INVALID_UTF8_BYTES = b"\xff\xfe\xfd not valid utf-8 \xff"

# Valid JSON that is not an object.
JSON_LIST_BYTES = b"[1, 2, 3]"
