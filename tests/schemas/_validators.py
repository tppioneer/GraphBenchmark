"""Test-only validation helpers for AIS-002.

This module is NOT production rubric/judge business validation (explicitly out
of scope for AIS-002 — see task card Excluded scope). It exists to:

* load the JSON Schemas shipped under ``schemas/``;
* expose locatable RFC 6901 JSON Pointers from ``jsonschema`` errors
  (acceptance criterion: "Schema 错误包含可定位的 JSON Pointer");
* exercise the cross-document negative-test categories required by the task
  card: unknown item, bad reference (evidence ids and answer_evidence pointers).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Repo root is three levels up from this file: tests/schemas/_validators.py.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"
PROFILE_DIR = REPO_ROOT / "profiles"

#: The nine artifact schemas required by docs/ai-scoring-design.md §17 (the eight
#: contracts plus the run artifact manifest).
SCHEMA_NAMES = (
    "case",
    "ground-truth",
    "agent-answer",
    "run-metadata",
    "policy-result",
    "judge-input",
    "judge-output",
    "score",
    "manifest",
)


def load_schema(name: str) -> dict[str, Any]:
    """Load a schema by its short name (e.g. ``agent-answer``)."""
    with (SCHEMA_DIR / f"{name}.schema.json").open(encoding="utf-8") as fh:
        return json.load(fh)


class ContractError(Exception):
    """A cross-document contract violation with an RFC 6901 JSON Pointer."""

    def __init__(self, message: str, pointer: str) -> None:
        self.pointer = pointer
        self.message = message
        super().__init__(f"{pointer}: {message}")


_MISSING: Any = object()


def json_pointer(error) -> str:
    """Build an RFC 6901 JSON Pointer for a jsonschema ``ValidationError``."""
    parts: list[str] = []
    for part in error.absolute_path:
        token = str(part).replace("~", "~0").replace("/", "~1")
        parts.append(token)
    return "/" + "/".join(parts)


def _resolve_pointer(doc: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON Pointer against ``doc``; return _MISSING if absent."""
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        return _MISSING
    cur: Any = doc
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError:
                return _MISSING
            if idx < 0 or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        elif isinstance(cur, dict):
            if token not in cur:
                return _MISSING
            cur = cur[token]
        else:
            return _MISSING
    return cur


def validate_evidence_references(agent_answer: dict[str, Any]) -> None:
    """Every finding.evidence_ids entry must reference an existing evidence id."""
    evidence_ids = {e["id"] for e in agent_answer.get("evidence", [])}
    for fi, finding in enumerate(agent_answer.get("answer", {}).get("findings", [])):
        for ei, eid in enumerate(finding.get("evidence_ids", [])):
            if eid not in evidence_ids:
                raise ContractError(
                    f"finding {finding.get('id')!r} references unknown evidence {eid!r}",
                    f"/answer/findings/{fi}/evidence_ids/{ei}",
                )


def validate_judge_output_items(judge_output: dict[str, Any], ground_truth: dict[str, Any]) -> None:
    """Every GT item judged exactly once; no unknown judge items (§10.2)."""
    gt_ids = {it["id"] for it in ground_truth["rubric_items"]}
    seen: set[str] = set()
    for i, item in enumerate(judge_output["items"]):
        iid = item["item_id"]
        if iid not in gt_ids:
            raise ContractError(
                f"judge references unknown rubric item {iid!r}",
                f"/items/{i}/item_id",
            )
        if iid in seen:
            raise ContractError(
                f"rubric item {iid!r} judged more than once",
                f"/items/{i}/item_id",
            )
        seen.add(iid)
    missing = gt_ids - seen
    if missing:
        raise ContractError(
            f"GT items not judged: {sorted(missing)}",
            "/items",
        )


def validate_answer_evidence_pointers(
    judge_output: dict[str, Any], answer_doc: dict[str, Any]
) -> None:
    """Every answer_evidence.json_pointer must resolve to referenceable text and
    the quote must occur within that referenced text (§10.2).

    The JSON Pointer must resolve to a string (referenceable text); a pointer
    that resolves to a non-text node, or a quote absent from the referenced
    text, is a contract violation. Production Judge validation remains outside
    AIS-002; this is the test-only cross-document checker.
    """
    for i, item in enumerate(judge_output["items"]):
        for j, ev in enumerate(item.get("answer_evidence", [])):
            resolved = _resolve_pointer(answer_doc, ev["json_pointer"])
            if resolved is _MISSING:
                raise ContractError(
                    f"answer_evidence pointer {ev['json_pointer']!r} does not resolve",
                    f"/items/{i}/answer_evidence/{j}/json_pointer",
                )
            if not isinstance(resolved, str):
                raise ContractError(
                    f"answer_evidence pointer {ev['json_pointer']!r} resolves to "
                    f"non-text ({type(resolved).__name__})",
                    f"/items/{i}/answer_evidence/{j}/json_pointer",
                )
            if ev["quote"] not in resolved:
                raise ContractError(
                    f"answer_evidence quote {ev['quote']!r} not found in referenced text",
                    f"/items/{i}/answer_evidence/{j}/quote",
                )


def validate_requested_effective_model(score: dict[str, Any]) -> None:
    """The requested and effective Judge models must agree for a formal score.

    JSON Schema cannot express sibling-field equality, so this cross-field
    invariant is enforced here as a test-only checker (mirroring the
    cross-document validators). A formal score is emitted only after
    requested/effective consistency is verified (DEC-001 #6, §13.3, §20);
    per-call A/B/C model metadata remains AIS-008 scope.
    """
    requested = score.get("judge_requested_model")
    effective = score.get("judge_model")
    if requested != effective:
        raise ContractError(
            f"requested model {requested!r} != effective model {effective!r}",
            "/judge_requested_model",
        )
