"""Production artifact validation for runner-produced artifacts.

Scope (AIS-003): validate ``agent-answer.json`` against its JSON Schema
(``schemas/agent-answer.schema.json``) and enforce the cross-document
evidence-reference contract (design §8.5, §10.2): every
``finding.evidence_ids`` entry must reference an existing evidence id.

Schema compliance and semantic correctness are distinct states (AIS-003
invariant): a schema-valid answer may still be semantically wrong, and the
``completed_with_schema_warning`` fallback is schema-valid in its degraded
form. This module only asserts structural/contract compliance; it never
scores semantics and never falls back to a string scorer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# runner/artifact_validation.py -> runner/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"

_VALIDATOR: Draft202012Validator | None = None


def _validator() -> Draft202012Validator:
    """Return a cached Draft 2020-12 validator for the agent-answer schema."""
    global _VALIDATOR
    if _VALIDATOR is None:
        with (SCHEMA_DIR / "agent-answer.schema.json").open(encoding="utf-8") as fh:
            schema = json.load(fh)
        _VALIDATOR = Draft202012Validator(schema)
    return _VALIDATOR


@dataclass(frozen=True)
class SchemaIssue:
    """A single contract issue with an RFC 6901 JSON Pointer."""

    pointer: str
    message: str


class ArtifactContractError(Exception):
    """Raised when an agent-answer violates its schema or evidence contract."""

    def __init__(self, issues: list[SchemaIssue]) -> None:
        self.issues = list(issues)
        if self.issues:
            detail = "\n".join(f"  {i.pointer}: {i.message}" for i in self.issues)
            super().__init__("agent-answer contract violations:\n" + detail)
        else:
            super().__init__("agent-answer contract violations")


def _json_pointer(error) -> str:
    """Build an RFC 6901 JSON Pointer for a jsonschema ``ValidationError``."""
    parts: list[str] = []
    for part in error.absolute_path:
        token = str(part).replace("~", "~0").replace("/", "~1")
        parts.append(token)
    return "/" + "/".join(parts)


def agent_answer_schema_issues(doc: dict[str, Any]) -> list[SchemaIssue]:
    """Return JSON Schema validation issues for ``doc`` (empty if schema-valid)."""
    return [
        SchemaIssue(pointer=_json_pointer(err), message=err.message)
        for err in sorted(_validator().iter_errors(doc), key=_json_pointer)
    ]


def evidence_reference_issues(doc: dict[str, Any]) -> list[SchemaIssue]:
    """Return cross-document evidence-reference issues (design §8.5, §10.2).

    Every ``finding.evidence_ids`` entry must reference an existing evidence
    id. Verified only when findings/evidence are present; missing optional
    arrays produce no issues and never trigger placeholder artifacts.
    """
    issues: list[SchemaIssue] = []
    evidence_ids = {e["id"] for e in doc.get("evidence", [])}
    for fi, finding in enumerate(doc.get("answer", {}).get("findings", [])):
        for ei, eid in enumerate(finding.get("evidence_ids", [])):
            if eid not in evidence_ids:
                issues.append(
                    SchemaIssue(
                        pointer=f"/answer/findings/{fi}/evidence_ids/{ei}",
                        message=(
                            f"finding {finding.get('id')!r} references unknown evidence id {eid!r}"
                        ),
                    )
                )
    return issues


def agent_answer_contract_issues(doc: dict[str, Any]) -> list[SchemaIssue]:
    """All agent-answer contract issues: schema compliance + evidence references."""
    return agent_answer_schema_issues(doc) + evidence_reference_issues(doc)


def assert_agent_answer_contract(doc: dict[str, Any]) -> None:
    """Raise :class:`ArtifactContractError` if ``doc`` violates the contract."""
    issues = agent_answer_contract_issues(doc)
    if issues:
        raise ArtifactContractError(issues)
