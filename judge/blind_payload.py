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


def _pick(source: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """Project only ``allowed`` keys present in ``source`` (allowlist copy)."""
    return {k: source[k] for k in allowed if k in source}


def _pick_findings(findings: object) -> list[dict[str, Any]]:
    """Allowlist-copy each finding; drop any smuggled per-finding field."""
    if not isinstance(findings, list):
        return []
    return [_pick(item, _FINDING_FIELDS) for item in findings if isinstance(item, Mapping)]


def _pick_evidence(evidence: object) -> list[dict[str, Any]]:
    """Allowlist-copy each evidence entry; drop any smuggled per-entry field."""
    if not isinstance(evidence, list):
        return []
    return [_pick(item, _EVIDENCE_FIELDS) for item in evidence if isinstance(item, Mapping)]


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
    """Allowlist-copy excerpts and verify each carries a matching content digest.

    The excerpt ``digest`` (§9.1) lets the Judge and auditors verify that the
    excerpt content has not been altered. If absent it is computed from the
    content; if present it must be a valid ``sha256:`` digest that matches the
    content, otherwise the payload is rejected.
    """
    if not isinstance(excerpts, list) or not excerpts:
        return None
    built: list[dict[str, Any]] = []
    for i, exc in enumerate(excerpts):
        if not isinstance(exc, Mapping):
            raise BlindPayloadError(f"excerpt {i} is not an object")
        projected = _pick(exc, _EXCERPT_FIELDS)
        content = projected.get("content")
        if not isinstance(content, str):
            raise BlindPayloadError(f"excerpt {i} is missing string content")
        digest = projected.get("digest")
        computed = digest_text(content)
        if digest is None:
            projected["digest"] = computed
        elif not is_valid_digest(digest):
            raise BlindPayloadError(f"excerpt {i} has malformed digest {digest!r}")
        elif digest != computed:
            raise BlindPayloadError(f"excerpt {i} digest {digest!r} does not match its content")
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
    # Only the closed field sets above are copied. ``references``, ``task_details``
    # and any unknown extension field are dropped here by construction, so nested
    # metadata, policy tags, filenames or candidate-identity fields cannot leak.
    blinded_rubric = [_pick(item, _RUBRIC_ITEM_FIELDS) for item in rubric_items]

    blinded_answer: dict[str, Any] = {"summary": summary, "explanation": explanation}
    findings = _pick_findings(answer_source.get("findings"))
    if findings:
        blinded_answer["findings"] = findings
    if "limitations" in answer_source:
        blinded_answer["limitations"] = list(answer_source["limitations"])
    if "recommended_actions" in answer_source:
        blinded_answer["recommended_actions"] = list(answer_source["recommended_actions"])

    blinded_evidence = _pick_evidence(agent_answer.get("evidence"))
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
