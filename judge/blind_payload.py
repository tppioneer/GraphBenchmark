"""Construct the blinded Judge input package (docs/ai-scoring-design.md §9).

The blind payload is assembled from four sources - the case, the scoring
Profile, the Ground Truth rubric and the Agent Answer - by **allowlist**
construction: only fields explicitly known to be safe are copied across.
Anything else the source artifacts happen to carry (Runner identity, tool
policy, tool output, cost metrics, prior scores, candidate identity, or
arbitrary unknown extension fields) is dropped because it is not on the
allowlist. This is stronger than a denylist, which can only remove fields it
already knows about and would let any new leaking field pass through unchanged
(invariant: "禁止仅靠 denylist 删除敏感字段").

The package also records the audit digests required by §9.2 / §20 (agent
answer, ground truth, judge prompt) plus the profile and blinding-protocol
versions, so that any input or version change invalidates the Judge cache (see
:mod:`judge.cache`).

Prompt-injection boundary (§9.3): the Agent Answer is untrusted data. It is
placed in the structured ``answer`` / ``evidence`` fields behind an explicit
data boundary and is never concatenated into an executable instruction string.
Building the actual Judge prompt text is excluded scope (Prompt 文案).
"""

from __future__ import annotations

from typing import Any, Mapping

from .canonical import digest_json, digest_text, is_valid_digest

#: Frozen Judge protocol for the blind-input contract (design §6, §20).
JUDGE_PROTOCOL = "semantic_outcome_v1"

#: Schema version of the judge-input contract produced here.
JUDGE_INPUT_SCHEMA_VERSION = "judge-input-v1"

#: Blinding-protocol version for the current payload layout. Bumped when the
#: set of fields copied into the blind input or the construction rules change;
#: a bump invalidates every cached Judge result (invariant).
BLINDING_PROTOCOL_VERSION = "blind-v1"

# --- Allowlists ----------------------------------------------------------- #
# Each tuple is the closed set of fields copied from the corresponding source
# object. A field not listed here never enters the blind payload, regardless of
# what the source carries. This is the allowlist required by invariant
# "禁止仅靠 denylist 删除敏感字段" and acceptance criterion 1. ``references`` is
# deliberately omitted from rubric items: the judge-input contract drops it, and
# the Judge evaluates the criterion text rather than peeking at GT-author
# reference locations (source context arrives separately via ``excerpts``).
#
# Every allowlisted value is type-validated and *reconstructed* (not
# shallow-copied) by the ``_build_*`` helpers below, so a smuggled value of the
# wrong type - e.g. ``limitations=[{"tool_policy": "graph"}]`` where the
# contract demands a list of strings - is rejected rather than emitted
# unchanged (AIS007-R1).

_ANSWER_FIELDS: tuple[str, ...] = (
    "summary",
    "explanation",
    "findings",
    "limitations",
    "recommended_actions",
)
_FINDING_FIELDS: tuple[str, ...] = ("id", "kind", "claim", "evidence_ids")
_EVIDENCE_FIELDS: tuple[str, ...] = (
    "id",
    "file",
    "symbol",
    "line",
    "reason",
    "excerpt",
)
_RUBRIC_ITEM_FIELDS: tuple[str, ...] = (
    "id",
    "dimension",
    "points",
    "criterion",
    "full_credit",
    "partial_credit",
    "zero_credit",
    "critical",
)
_EXCERPT_FIELDS: tuple[str, ...] = (
    "file",
    "symbol",
    "revision",
    "start_line",
    "end_line",
    "digest",
    "content",
)

#: Ordered dimensions for a deterministic profile brief.
_DIMENSIONS: tuple[str, ...] = (
    "core_correctness",
    "reasoning_correctness",
    "completeness",
    "scope_precision",
    "evidence_actionability",
)


class BlindPayloadError(ValueError):
    """Raised when the blind payload cannot be constructed coherently."""


# --- Type-validated allowlist reconstruction ------------------------------ #
# These helpers reconstruct each allowlisted value from scratch rather than
# shallow-copying it, so a value of the wrong type is rejected instead of being
# emitted unchanged (AIS007-R1). Each builder copies only fields on the
# corresponding allowlist tuple and validates the type of every copied value
# against its contract (schemas/agent-answer.schema.json,
# schemas/ground-truth.schema.json, schemas/judge-input.schema.json).


def _expect_str(value: Any, *, field: str, context: str) -> str:
    """Return ``value`` if it is a string, else reject (no shallow copy)."""
    if not isinstance(value, str):
        raise BlindPayloadError(
            f"{context}: {field!r} must be a string, got {type(value).__name__}"
        )
    return value


def _expect_str_list(value: Any, *, field: str, context: str) -> list[str]:
    """Reconstruct a list of strings, rejecting any non-string element."""
    if not isinstance(value, list):
        raise BlindPayloadError(
            f"{context}: {field!r} must be a list of strings, got {type(value).__name__}"
        )
    return [
        _expect_str(item, field=f"{field}[{i}]", context=context) for i, item in enumerate(value)
    ]


def _expect_int(value: Any, *, field: str, context: str) -> int:
    """Return ``value`` if it is an integer (not bool), else reject."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise BlindPayloadError(
            f"{context}: {field!r} must be an integer, got {type(value).__name__}"
        )
    return value


def _expect_number(value: Any, *, field: str, context: str) -> int | float:
    """Return ``value`` if it is a number (int or float, not bool), else reject."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BlindPayloadError(
            f"{context}: {field!r} must be a number, got {type(value).__name__}"
        )
    return value


def _expect_bool(value: Any, *, field: str, context: str) -> bool:
    """Return ``value`` if it is a boolean, else reject."""
    if not isinstance(value, bool):
        raise BlindPayloadError(
            f"{context}: {field!r} must be a boolean, got {type(value).__name__}"
        )
    return value


def _build_finding(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Reconstruct a single finding from ``_FINDING_FIELDS`` with type validation."""
    ctx = f"answer.findings[{index}]"
    finding: dict[str, Any] = {
        "id": _expect_str(item.get("id"), field="id", context=ctx),
        "kind": _expect_str(item.get("kind"), field="kind", context=ctx),
        "claim": _expect_str(item.get("claim"), field="claim", context=ctx),
    }
    if "evidence_ids" in item:
        finding["evidence_ids"] = _expect_str_list(
            item["evidence_ids"], field="evidence_ids", context=ctx
        )
    return finding


def _build_findings(findings: object) -> list[dict[str, Any]]:
    """Reconstruct each finding, rejecting non-objects and invalid nested types."""
    if findings is None:
        return []
    if not isinstance(findings, list):
        raise BlindPayloadError(f"answer.findings must be a list, got {type(findings).__name__}")
    built: list[dict[str, Any]] = []
    for i, item in enumerate(findings):
        if not isinstance(item, Mapping):
            raise BlindPayloadError(f"answer.findings[{i}] must be an object")
        built.append(_build_finding(item, i))
    return built


def _build_evidence_entry(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Reconstruct a single evidence entry from ``_EVIDENCE_FIELDS`` with validation."""
    ctx = f"evidence[{index}]"
    entry: dict[str, Any] = {
        "id": _expect_str(item.get("id"), field="id", context=ctx),
        "file": _expect_str(item.get("file"), field="file", context=ctx),
        "reason": _expect_str(item.get("reason"), field="reason", context=ctx),
    }
    if "symbol" in item:
        entry["symbol"] = _expect_str(item["symbol"], field="symbol", context=ctx)
    if "line" in item:
        entry["line"] = _expect_int(item["line"], field="line", context=ctx)
    if "excerpt" in item:
        entry["excerpt"] = _expect_str(item["excerpt"], field="excerpt", context=ctx)
    return entry


def _build_evidence(evidence: object) -> list[dict[str, Any]]:
    """Reconstruct each evidence entry, rejecting non-objects and invalid types."""
    if evidence is None:
        return []
    if not isinstance(evidence, list):
        raise BlindPayloadError(f"evidence must be a list, got {type(evidence).__name__}")
    built: list[dict[str, Any]] = []
    for i, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise BlindPayloadError(f"evidence[{i}] must be an object")
        built.append(_build_evidence_entry(item, i))
    return built


def _build_rubric_item(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Reconstruct a single rubric item from ``_RUBRIC_ITEM_FIELDS`` with validation."""
    ctx = f"rubric_items[{index}]"
    rubric: dict[str, Any] = {
        "id": _expect_str(item.get("id"), field="id", context=ctx),
        "dimension": _expect_str(item.get("dimension"), field="dimension", context=ctx),
        "points": _expect_number(item.get("points"), field="points", context=ctx),
        "criterion": _expect_str(item.get("criterion"), field="criterion", context=ctx),
    }
    if "full_credit" in item:
        rubric["full_credit"] = _expect_str(item["full_credit"], field="full_credit", context=ctx)
    if "partial_credit" in item:
        rubric["partial_credit"] = _expect_str(
            item["partial_credit"], field="partial_credit", context=ctx
        )
    if "zero_credit" in item:
        rubric["zero_credit"] = _expect_str(item["zero_credit"], field="zero_credit", context=ctx)
    if "critical" in item:
        rubric["critical"] = _expect_bool(item["critical"], field="critical", context=ctx)
    return rubric


def _build_profile_brief(profile: Mapping[str, Any]) -> str | None:
    """Build a deterministic Judge-facing brief from profile dimension semantics."""
    semantics = profile.get("dimension_semantics")
    if not isinstance(semantics, dict) or not semantics:
        return None
    lines = [f"{profile.get('scoring_profile', '')} dimension guidance:"]
    for dim in _DIMENSIONS:
        text = semantics.get(dim)
        if isinstance(text, str) and text:
            lines.append(f"- {dim}: {text}")
    if len(lines) == 1:
        return None
    return "\n".join(lines)


def _build_excerpts(excerpts: object) -> list[dict[str, Any]] | None:
    """Reconstruct excerpts with type validation and a matching content digest.

    The excerpt ``digest`` (§9.1) lets the Judge and auditors verify that the
    excerpt content has not been altered. If absent it is computed from the
    content; if present it must be a valid ``sha256:`` digest that matches the
    content, otherwise the payload is rejected. Each allowlisted field is
    type-validated and reconstructed rather than shallow-copied (AIS007-R1).
    """
    if not isinstance(excerpts, list) or not excerpts:
        return None
    built: list[dict[str, Any]] = []
    for i, exc in enumerate(excerpts):
        if not isinstance(exc, Mapping):
            raise BlindPayloadError(f"excerpt {i} is not an object")
        ctx = f"excerpt[{i}]"
        projected: dict[str, Any] = {
            "file": _expect_str(exc.get("file"), field="file", context=ctx),
            "revision": _expect_str(exc.get("revision"), field="revision", context=ctx),
            "start_line": _expect_int(exc.get("start_line"), field="start_line", context=ctx),
            "end_line": _expect_int(exc.get("end_line"), field="end_line", context=ctx),
            "content": _expect_str(exc.get("content"), field="content", context=ctx),
        }
        if "symbol" in exc:
            projected["symbol"] = _expect_str(exc["symbol"], field="symbol", context=ctx)
        content = projected["content"]
        digest = exc.get("digest")
        computed = digest_text(content)
        if digest is None:
            projected["digest"] = computed
        elif not is_valid_digest(digest):
            raise BlindPayloadError(f"excerpt {i} has malformed digest {digest!r}")
        elif digest != computed:
            raise BlindPayloadError(f"excerpt {i} digest {digest!r} does not match its content")
        else:
            projected["digest"] = digest
        built.append(projected)
    return built


def build_blind_input(
    *,
    case: Mapping[str, Any],
    profile: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    agent_answer: Mapping[str, Any],
    judge_prompt_digest: str,
    excerpts: list[Mapping[str, Any]] | None = None,
    blinding_protocol_version: str = BLINDING_PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Build the blinded Judge input from case, Profile, GT and Agent Answer.

    Construction is allowlist-based (see module docstring). The returned dict
    conforms to ``schemas/judge-input.schema.json``; schema validation itself
    is test-only (``jsonschema`` is a dev dependency, mirroring AIS-002).

    Raises :class:`BlindPayloadError` if the four sources are mutually
    inconsistent or a required field is missing.
    """
    # --- Cross-source consistency: the four sources must describe one case. -- #
    case_id = case.get("case_id")
    task_type = case.get("task_type")
    if not case_id or not task_type:
        raise BlindPayloadError("case must provide case_id and task_type")
    question = case.get("question")
    if not question:
        raise BlindPayloadError("case must provide a question")

    if ground_truth.get("case_id") != case_id:
        raise BlindPayloadError("ground_truth case_id does not match case")
    if agent_answer.get("case_id") != case_id:
        raise BlindPayloadError("agent_answer case_id does not match case")
    if ground_truth.get("task_type") != task_type:
        raise BlindPayloadError("ground_truth task_type does not match case")
    if agent_answer.get("task_type") != task_type:
        raise BlindPayloadError("agent_answer task_type does not match case")
    if profile.get("task_type") != task_type:
        raise BlindPayloadError("profile task_type does not match case")

    scoring_profile = ground_truth.get("scoring_profile")
    if not scoring_profile:
        raise BlindPayloadError("ground_truth must provide scoring_profile")
    if profile.get("scoring_profile") != scoring_profile:
        raise BlindPayloadError("profile scoring_profile does not match ground_truth")

    profile_version = profile.get("profile_version")
    if not profile_version:
        raise BlindPayloadError("profile must provide profile_version")

    if not is_valid_digest(judge_prompt_digest):
        raise BlindPayloadError(
            f"judge_prompt_digest is not a valid sha256 digest: {judge_prompt_digest!r}"
        )

    rubric_items = ground_truth.get("rubric_items")
    if not isinstance(rubric_items, list) or not rubric_items:
        raise BlindPayloadError("ground_truth must provide non-empty rubric_items")

    answer_source = agent_answer.get("answer")
    if not isinstance(answer_source, Mapping):
        raise BlindPayloadError("agent_answer must provide an answer object")
    summary = answer_source.get("summary")
    explanation = answer_source.get("explanation")
    if not isinstance(summary, str) or not isinstance(explanation, str):
        raise BlindPayloadError("agent_answer.answer must provide summary and explanation strings")

    # --- Allowlist construction (the core blinding step) -------------------- #
    # Only the closed field sets above are copied, and each copied value is
    # type-validated and reconstructed (not shallow-copied) so a smuggled value
    # of the wrong type is rejected (AIS007-R1). ``references``, ``task_details``
    # and any unknown extension field are dropped here by construction, so nested
    # metadata, policy tags, filenames or candidate-identity fields cannot leak.
    blinded_rubric: list[dict[str, Any]] = []
    for i, item in enumerate(rubric_items):
        if not isinstance(item, Mapping):
            raise BlindPayloadError(f"rubric_items[{i}] must be an object")
        blinded_rubric.append(_build_rubric_item(item, i))

    blinded_answer: dict[str, Any] = {"summary": summary, "explanation": explanation}
    findings = _build_findings(answer_source.get("findings"))
    if findings:
        blinded_answer["findings"] = findings
    if "limitations" in answer_source:
        blinded_answer["limitations"] = _expect_str_list(
            answer_source["limitations"], field="limitations", context="answer"
        )
    if "recommended_actions" in answer_source:
        blinded_answer["recommended_actions"] = _expect_str_list(
            answer_source["recommended_actions"],
            field="recommended_actions",
            context="answer",
        )

    blinded_evidence = _build_evidence(agent_answer.get("evidence"))
    blinded_excerpts = _build_excerpts(excerpts)
    profile_brief = _build_profile_brief(profile)

    # --- Audit digests (§9.2 / §20) ---------------------------------------- #
    # Digests are over the canonical form of the *full* source artifacts, so any
    # change to the answer or GT - including dropped fields like task_details -
    # changes the digest and invalidates the Judge cache (conservative by design).
    agent_answer_digest = digest_json(dict(agent_answer))
    ground_truth_digest = digest_json(dict(ground_truth))

    blind: dict[str, Any] = {
        "schema_version": JUDGE_INPUT_SCHEMA_VERSION,
        "judge_protocol": profile.get("judge_protocol", JUDGE_PROTOCOL),
        "scoring_profile": scoring_profile,
        "task_type": task_type,
        "case_id": case_id,
        "question": question,
        "rubric_items": blinded_rubric,
        "answer": blinded_answer,
        "digests": {
            "agent_answer_digest": agent_answer_digest,
            "ground_truth_digest": ground_truth_digest,
            "judge_prompt_digest": judge_prompt_digest,
            "profile_version": profile_version,
            "blinding_protocol_version": blinding_protocol_version,
        },
    }
    if blinded_evidence:
        blind["evidence"] = blinded_evidence
    if blinded_excerpts:
        blind["excerpts"] = blinded_excerpts
    if profile_brief:
        blind["profile_brief"] = profile_brief
    return blind
