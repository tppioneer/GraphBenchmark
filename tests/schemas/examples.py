"""Valid and invalid example data for AIS-002 schema tests.

A single bug-localization case (``qwenpaw-case-z-corrupt-inbox-recovery-bug``
from docs/ai-scoring-design.md §7.1) is used as the running example so that the
ground truth, agent answer, judge input, judge output and score examples are
mutually consistent for cross-document tests.

For every schema we provide:

* ``FULL_*``  - a complete structure exercising every optional field;
* ``MINIMAL_*`` - the smallest legal structure (only required fields).

Invalid builders deep-copy a valid example and mutate a single field so each
negative test isolates one rejection reason and a locatable JSON Pointer.
"""

from __future__ import annotations

import copy
from typing import Any

# Valid sha256-style digest strings (64 hex chars) for fixture use only.
DIGEST_PROMPT = "sha256:" + "a" * 64
DIGEST_GT = "sha256:" + "b" * 64
DIGEST_ANSWER = "sha256:" + "c" * 64
DIGEST_EXCERPT = "sha256:" + "d" * 64

CASE_ID = "qwenpaw-case-z-corrupt-inbox-recovery-bug"

TASK_TYPES = ["flow_tracing", "bug_localization", "impact_analysis"]
PROFILES = ["flow_tracing_v1", "bug_localization_v1", "impact_analysis_v1"]
DIMENSIONS = [
    "core_correctness",
    "reasoning_correctness",
    "completeness",
    "scope_precision",
    "evidence_actionability",
]

# Finding-kind enums, kept in sync with agent-answer.schema.json allOf branches.
FINDING_KINDS = {
    "flow_tracing": [
        "entrypoint",
        "flow_relation",
        "data_flow",
        "branch",
        "async_boundary",
        "terminal_behavior",
        "scope_exclusion",
    ],
    "bug_localization": [
        "symptom",
        "root_cause",
        "trigger_condition",
        "failure_chain",
        "blast_radius",
        "false_cause_exclusion",
        "fix_direction",
    ],
    "impact_analysis": [
        "target",
        "direct_impact",
        "indirect_impact",
        "dependency_relation",
        "risk",
        "scope_exclusion",
        "validation",
    ],
}


# --------------------------------------------------------------------------- #
# case.schema.json
# --------------------------------------------------------------------------- #

FULL_CASE: dict[str, Any] = {
    "schema_version": "case-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "question": "定位导致 inbox 列表接口和 append 写入同时失败的根因，并说明传播路径。",
    "repo": {
        "url": "https://example.invalid/qwenpaw.git",
        "revision": "abc1234",
        "root_path": "src/qwenpaw",
    },
}

MINIMAL_CASE: dict[str, Any] = {
    "schema_version": "case-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "question": "定位导致 inbox 列表接口失败的根因。",
}


# --------------------------------------------------------------------------- #
# ground-truth.schema.json
# --------------------------------------------------------------------------- #
# Dimension point sums: core 35, reasoning 25, completeness 20, scope 10,
# evidence 10; total 100 (DEC-001 #1, #3).


def _rubric_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 20,
            "criterion": "正确识别共享读取函数无法处理损坏 JSON 的根因。",
            "full_credit": "根因位置、异常类型、触发条件和主要结果均正确。",
            "partial_credit": "找到共享读取路径，但只描述了 HTTP 500 等表面症状。",
            "zero_credit": "把正常写入的临时文件替换机制判断为主要根因。",
            "critical": True,
            "references": [
                {
                    "symbol": "_load_events",
                    "file": "src/qwenpaw/app/inbox_store.py",
                    "lines": [40, 60],
                }
            ],
        },
        {
            "id": "outcome.trigger",
            "dimension": "core_correctness",
            "points": 15,
            "criterion": "说明触发损坏的条件。",
        },
        {
            "id": "reasoning.failure-chain",
            "dimension": "reasoning_correctness",
            "points": 12,
            "criterion": "正确说明损坏文件如何经共享读取路径传播到列表接口。",
            "zero_credit": "因果方向错误。",
            "critical": True,
        },
        {
            "id": "reasoning.propagation",
            "dimension": "reasoning_correctness",
            "points": 13,
            "criterion": "说明传播如何阻止后续 append 完成。",
        },
        {
            "id": "completeness.blast-radius",
            "dimension": "completeness",
            "points": 10,
            "criterion": "覆盖依赖同一读取函数的主要读写路径。",
        },
        {
            "id": "completeness.recovery",
            "dimension": "completeness",
            "points": 10,
            "criterion": "覆盖恢复行为。",
        },
        {
            "id": "precision.atomic-write",
            "dimension": "scope_precision",
            "points": 5,
            "criterion": "不应把已存在的原子替换机制正面归因于本次问题。",
        },
        {
            "id": "precision.unrelated",
            "dimension": "scope_precision",
            "points": 5,
            "criterion": "排除无关模块。",
        },
        {
            "id": "evidence.validation",
            "dimension": "evidence_actionability",
            "points": 5,
            "criterion": "提供可定位源码证据并提出测试方向。",
        },
        {
            "id": "evidence.repro",
            "dimension": "evidence_actionability",
            "points": 5,
            "criterion": "提供可复现的验证建议。",
        },
    ]


FULL_GT: dict[str, Any] = {
    "schema_version": "ground-truth-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "scoring_profile": "bug_localization_v1",
    "rubric_items": _rubric_items(),
}

MINIMAL_GT: dict[str, Any] = {
    "schema_version": "ground-truth-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "scoring_profile": "bug_localization_v1",
    "rubric_items": [
        {
            "id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 35,
            "criterion": "识别根因。",
        }
    ],
}


# --------------------------------------------------------------------------- #
# agent-answer.schema.json
# --------------------------------------------------------------------------- #

FULL_AGENT_ANSWER: dict[str, Any] = {
    "schema_version": "agent-answer-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "status": "completed",
    "answer": {
        "summary": "损坏的 inbox JSON 会在共享读取函数中触发解析异常。",
        "explanation": (
            "列表接口和 append 路径都依赖同一个读取函数，因此损坏文件不仅影响读取，"
            "也会阻止后续事件写入。"
        ),
        "findings": [
            {
                "id": "finding-1",
                "kind": "root_cause",
                "claim": "_load_events 未处理损坏 JSON，是直接根因。",
                "evidence_ids": ["evidence-1"],
            },
            {
                "id": "finding-2",
                "kind": "failure_chain",
                "claim": "append_event 在写入前读取已有数据，因此损坏文件会阻止后续事件写入。",
                "evidence_ids": ["evidence-1", "evidence-2"],
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
        },
        {
            "id": "evidence-2",
            "file": "src/qwenpaw/app/inbox_store.py",
            "symbol": "append_event",
            "line": 88,
            "reason": "写入前读取已有数据。",
        },
    ],
    "task_details": {
        "root_causes": ["_load_events"],
        "failure_chain": ["_load_events", "append_event"],
        "affected_paths": ["/inbox", "/inbox/append"],
    },
}

MINIMAL_AGENT_ANSWER: dict[str, Any] = {
    "schema_version": "agent-answer-v1",
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "status": "completed",
    "answer": {
        "summary": "损坏的 inbox JSON 触发解析异常。",
        "explanation": "共享读取函数未处理损坏 JSON，影响列表与 append 路径。",
    },
}


def agent_answer_with_identity_leak() -> dict[str, Any]:
    """Identity leak: a Runner-collected field smuggled into the agent answer."""
    bad = copy.deepcopy(FULL_AGENT_ANSWER)
    bad["agent_model"] = "glm-5.2"
    return bad


def agent_answer_with_tool_policy_leak() -> dict[str, Any]:
    """Identity leak: tool_policy smuggled into the answer sub-object."""
    bad = copy.deepcopy(FULL_AGENT_ANSWER)
    bad["answer"]["tool_policy"] = "graph"
    return bad


def agent_answer_with_bad_finding_kind() -> dict[str, Any]:
    """A finding kind not permitted for bug_localization."""
    bad = copy.deepcopy(FULL_AGENT_ANSWER)
    bad["answer"]["findings"][0]["kind"] = "entrypoint"
    return bad


def agent_answer_with_bad_evidence_reference() -> dict[str, Any]:
    """A finding references an evidence id that does not exist."""
    bad = copy.deepcopy(FULL_AGENT_ANSWER)
    bad["answer"]["findings"][0]["evidence_ids"] = ["evidence-missing"]
    return bad


# Status-conditional answer representation (design §8.8, R1).
# completed_with_schema_warning allows an empty summary but must carry the raw
# Markdown in explanation; empty/refused/invalid must have a legal representation
# without fabricated non-empty answer text; completed stays non-empty.


def agent_answer_schema_warning_fallback() -> dict[str, Any]:
    """The §8.8 degradation wrapper: empty summary, raw-Markdown explanation."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": CASE_ID,
        "task_type": "bug_localization",
        "status": "completed_with_schema_warning",
        "answer": {
            "summary": "",
            "explanation": "模型返回的原始 Markdown。",
            "findings": [],
            "limitations": [],
            "recommended_actions": [],
        },
        "evidence": [],
    }


def agent_answer_empty() -> dict[str, Any]:
    """An empty run: legal representation with no fabricated answer text."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": CASE_ID,
        "task_type": "bug_localization",
        "status": "empty",
        "answer": {"summary": "", "explanation": ""},
    }


def agent_answer_refused() -> dict[str, Any]:
    """A refused run: legal representation with no fabricated answer text."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": CASE_ID,
        "task_type": "bug_localization",
        "status": "refused",
        "answer": {"summary": "", "explanation": ""},
    }


def agent_answer_invalid() -> dict[str, Any]:
    """An invalid run: legal representation with no fabricated answer text."""
    return {
        "schema_version": "agent-answer-v1",
        "case_id": CASE_ID,
        "task_type": "bug_localization",
        "status": "invalid",
        "answer": {"summary": "", "explanation": ""},
    }


def agent_answer_schema_warning_empty_explanation() -> dict[str, Any]:
    """Schema-warning fallback whose explanation is empty (must be rejected)."""
    bad = agent_answer_schema_warning_fallback()
    bad["answer"]["explanation"] = ""
    return bad


def agent_answer_completed_empty_summary() -> dict[str, Any]:
    """A completed answer with an empty summary (must be rejected)."""
    bad = copy.deepcopy(MINIMAL_AGENT_ANSWER)
    bad["answer"]["summary"] = ""
    return bad


# --------------------------------------------------------------------------- #
# run-metadata.schema.json
# --------------------------------------------------------------------------- #

FULL_RUN_METADATA: dict[str, Any] = {
    "schema_version": "run-metadata-v1",
    "agent": "claude-code",
    "agent_model": "glm-5.2",
    "tool_policy": "graph",
    "policy_enforced": True,
    "started_at": "2025-01-15T10:30:00Z",
    "ended_at": "2025-01-15T10:33:17Z",
    "metrics": {
        "tool_call_count": 14,
        "files_read_count": 6,
        "graph_query_count": 8,
        "search_query_count": 2,
        "elapsed_ms": 197000,
        "input_tokens": 12000,
        "output_tokens": 2300,
    },
}

MINIMAL_RUN_METADATA: dict[str, Any] = {
    "schema_version": "run-metadata-v1",
    "agent": "claude-code",
    "agent_model": "glm-5.2",
    "tool_policy": "graph",
    "policy_enforced": True,
    "started_at": "2025-01-15T10:30:00Z",
    "ended_at": "2025-01-15T10:33:17Z",
    "metrics": {
        "tool_call_count": 0,
        "files_read_count": 0,
        "graph_query_count": 0,
        "search_query_count": 0,
        "elapsed_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    },
}


# --------------------------------------------------------------------------- #
# policy-result.schema.json
# --------------------------------------------------------------------------- #

FULL_POLICY_RESULT: dict[str, Any] = {
    "schema_version": "policy-result-v1",
    "valid": True,
    "violations": [],
    "observations": ["Graph policy produced 8 verified Graph queries."],
}

MINIMAL_POLICY_RESULT: dict[str, Any] = {
    "schema_version": "policy-result-v1",
    "valid": True,
    "violations": [],
    "observations": [],
}


# --------------------------------------------------------------------------- #
# judge-input.schema.json
# --------------------------------------------------------------------------- #

FULL_JUDGE_INPUT: dict[str, Any] = {
    "schema_version": "judge-input-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "task_type": "bug_localization",
    "case_id": CASE_ID,
    "question": "定位导致 inbox 列表接口和 append 写入同时失败的根因，并说明传播路径。",
    "profile_brief": "Bug localization: identify root cause, failure chain and blast radius.",
    "rubric_items": [
        {
            "id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 20,
            "criterion": "正确识别共享读取函数无法处理损坏 JSON 的根因。",
            "zero_credit": "把正常写入的临时文件替换机制判断为主要根因。",
            "critical": True,
        },
        {
            "id": "reasoning.failure-chain",
            "dimension": "reasoning_correctness",
            "points": 12,
            "criterion": "正确说明损坏文件如何经共享读取路径传播到列表接口。",
            "critical": True,
        },
    ],
    "answer": {
        "summary": "损坏的 inbox JSON 会在共享读取函数中触发解析异常。",
        "explanation": "列表接口和 append 路径都依赖同一个读取函数。",
        "findings": [
            {
                "id": "finding-1",
                "kind": "root_cause",
                "claim": "_load_events 未处理损坏 JSON，是直接根因。",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "limitations": [],
        "recommended_actions": [],
    },
    "evidence": [
        {
            "id": "evidence-1",
            "file": "src/qwenpaw/app/inbox_store.py",
            "symbol": "_load_events",
            "reason": "该函数直接调用 json.loads，且没有处理解析异常。",
        }
    ],
    "excerpts": [
        {
            "file": "src/qwenpaw/app/inbox_store.py",
            "symbol": "_load_events",
            "revision": "abc1234",
            "start_line": 40,
            "end_line": 60,
            "digest": DIGEST_EXCERPT,
            "content": "def _load_events(path):\n    return json.loads(path.read_text())\n",
        }
    ],
    "digests": {
        "agent_answer_digest": DIGEST_ANSWER,
        "ground_truth_digest": DIGEST_GT,
        "judge_prompt_digest": DIGEST_PROMPT,
        "profile_version": "bug-localization-v1",
        "blinding_protocol_version": "blind-v1",
    },
}

MINIMAL_JUDGE_INPUT: dict[str, Any] = {
    "schema_version": "judge-input-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "task_type": "bug_localization",
    "case_id": CASE_ID,
    "question": "定位导致 inbox 列表接口失败的根因。",
    "rubric_items": [
        {
            "id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 35,
            "criterion": "识别根因。",
        }
    ],
    "answer": {
        "summary": "损坏的 inbox JSON 触发解析异常。",
        "explanation": "共享读取函数未处理损坏 JSON。",
    },
    "digests": {
        "agent_answer_digest": DIGEST_ANSWER,
        "ground_truth_digest": DIGEST_GT,
        "judge_prompt_digest": DIGEST_PROMPT,
        "profile_version": "bug-localization-v1",
        "blinding_protocol_version": "blind-v1",
    },
}


def judge_input_with_identity_leak() -> dict[str, Any]:
    """Blinding leak: agent_model smuggled into the blind payload."""
    bad = copy.deepcopy(FULL_JUDGE_INPUT)
    bad["agent_model"] = "glm-5.2"
    return bad


def judge_input_with_bad_digest() -> dict[str, Any]:
    """A malformed digest that violates the sha256 pattern."""
    bad = copy.deepcopy(FULL_JUDGE_INPUT)
    bad["digests"]["ground_truth_digest"] = "not-a-digest"
    return bad


# --------------------------------------------------------------------------- #
# judge-output.schema.json
# --------------------------------------------------------------------------- #
# Items align 1:1 with FULL_GT.rubric_items ids for cross-document tests.

FULL_JUDGE_OUTPUT: dict[str, Any] = {
    "schema_version": "judge-output-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "items": [
        {
            "item_id": "outcome.root-cause",
            "credit": 1,
            "verdict": "correct",
            "answer_evidence": [{"json_pointer": "/answer/summary", "quote": "损坏的 inbox JSON"}],
            "reason": "根因正确。",
            "confidence": 0.9,
        },
        {
            "item_id": "outcome.trigger",
            "credit": 0.75,
            "verdict": "mostly_correct",
            "reason": "触发条件基本正确。",
            "confidence": 0.8,
        },
        {
            "item_id": "reasoning.failure-chain",
            "credit": 0.75,
            "verdict": "mostly_correct",
            "answer_evidence": [
                {"json_pointer": "/answer/findings/1/claim", "quote": "append_event"}
            ],
            "reason": "主要因果方向正确，但遗漏了 append 路径细节。",
            "confidence": 0.86,
        },
        {
            "item_id": "reasoning.propagation",
            "credit": 0.5,
            "verdict": "partial",
            "reason": "传播说明不完整。",
            "confidence": 0.7,
        },
        {
            "item_id": "completeness.blast-radius",
            "credit": 0.5,
            "verdict": "partial",
            "reason": "覆盖部分路径。",
            "confidence": 0.6,
        },
        {
            "item_id": "completeness.recovery",
            "credit": 0.25,
            "verdict": "weak",
            "reason": "恢复行为描述不足。",
            "confidence": 0.55,
        },
        {
            "item_id": "precision.atomic-write",
            "credit": 1,
            "verdict": "correct",
            "reason": "正确排除。",
            "confidence": 0.9,
        },
        {
            "item_id": "precision.unrelated",
            "credit": 1,
            "verdict": "correct",
            "reason": "正确排除。",
            "confidence": 0.85,
        },
        {
            "item_id": "evidence.validation",
            "credit": 0.75,
            "verdict": "mostly_correct",
            "reason": "证据基本充分。",
            "confidence": 0.8,
        },
        {
            "item_id": "evidence.repro",
            "credit": 0.5,
            "verdict": "partial",
            "reason": "复现建议不完整。",
            "confidence": 0.6,
        },
    ],
    "unsupported_claims": [],
    "critical_errors": [],
    "overall_confidence": 0.84,
    "requires_human_review": False,
}

MINIMAL_JUDGE_OUTPUT: dict[str, Any] = {
    "schema_version": "judge-output-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "items": [
        {
            "item_id": "outcome.root-cause",
            "credit": 1,
            "verdict": "correct",
            "reason": "根因正确。",
            "confidence": 0.9,
        }
    ],
    "unsupported_claims": [],
    "critical_errors": [],
    "overall_confidence": 0.9,
    "requires_human_review": False,
}


def judge_output_with_illegal_credit() -> dict[str, Any]:
    """Credit outside the frozen {0, 0.25, 0.5, 0.75, 1} set."""
    bad = copy.deepcopy(MINIMAL_JUDGE_OUTPUT)
    bad["items"][0]["credit"] = 0.6
    return bad


def judge_output_with_unknown_item() -> dict[str, Any]:
    """An item_id that does not exist in the ground-truth rubric."""
    bad = copy.deepcopy(FULL_JUDGE_OUTPUT)
    bad["items"][0]["item_id"] = "outcome.does-not-exist"
    return bad


def judge_output_with_bad_answer_pointer() -> dict[str, Any]:
    """An answer_evidence json_pointer that does not resolve in the answer."""
    bad = copy.deepcopy(FULL_JUDGE_OUTPUT)
    bad["items"][0]["answer_evidence"][0]["json_pointer"] = "/answer/no-such-field"
    return bad


def judge_output_with_mismatched_quote() -> dict[str, Any]:
    """A correct json_pointer but a quote absent from the referenced text (R5)."""
    bad = copy.deepcopy(FULL_JUDGE_OUTPUT)
    # /answer/summary resolves, but this quote is not a substring of the summary.
    bad["items"][0]["answer_evidence"][0]["json_pointer"] = "/answer/summary"
    bad["items"][0]["answer_evidence"][0]["quote"] = "this quote is not in the summary"
    return bad


def judge_output_with_non_text_pointer() -> dict[str, Any]:
    """A json_pointer that resolves to a non-text node (R5)."""
    bad = copy.deepcopy(FULL_JUDGE_OUTPUT)
    # /answer/findings resolves to an array, not referenceable text.
    bad["items"][0]["answer_evidence"][0]["json_pointer"] = "/answer/findings"
    bad["items"][0]["answer_evidence"][0]["quote"] = "finding-1"
    return bad


# --------------------------------------------------------------------------- #
# score.schema.json
# --------------------------------------------------------------------------- #
# item_score = points * consensus_credit; dimension_totals and raw_total match.

FULL_SCORE: dict[str, Any] = {
    "schema_version": "score-v1",
    "benchmark_version": "ai-score-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "judge_provider": "claude-code-cli",
    "judge_requested_model": "glm-5.2",
    "judge_model": "glm-5.2",
    "judge_cli_version": "2.1.220",
    "judge_prompt_digest": DIGEST_PROMPT,
    "ground_truth_digest": DIGEST_GT,
    "agent_answer_digest": DIGEST_ANSWER,
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "items": [
        {
            "item_id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 20,
            "consensus_credit": 1.0,
            "item_score": 20.0,
        },
        {
            "item_id": "outcome.trigger",
            "dimension": "core_correctness",
            "points": 15,
            "consensus_credit": 0.75,
            "item_score": 11.25,
        },
        {
            "item_id": "reasoning.failure-chain",
            "dimension": "reasoning_correctness",
            "points": 12,
            "consensus_credit": 0.75,
            "item_score": 9.0,
        },
        {
            "item_id": "reasoning.propagation",
            "dimension": "reasoning_correctness",
            "points": 13,
            "consensus_credit": 0.5,
            "item_score": 6.5,
        },
        {
            "item_id": "completeness.blast-radius",
            "dimension": "completeness",
            "points": 10,
            "consensus_credit": 0.5,
            "item_score": 5.0,
        },
        {
            "item_id": "completeness.recovery",
            "dimension": "completeness",
            "points": 10,
            "consensus_credit": 0.25,
            "item_score": 2.5,
        },
        {
            "item_id": "precision.atomic-write",
            "dimension": "scope_precision",
            "points": 5,
            "consensus_credit": 1.0,
            "item_score": 5.0,
        },
        {
            "item_id": "precision.unrelated",
            "dimension": "scope_precision",
            "points": 5,
            "consensus_credit": 1.0,
            "item_score": 5.0,
        },
        {
            "item_id": "evidence.validation",
            "dimension": "evidence_actionability",
            "points": 5,
            "consensus_credit": 0.75,
            "item_score": 3.75,
        },
        {
            "item_id": "evidence.repro",
            "dimension": "evidence_actionability",
            "points": 5,
            "consensus_credit": 0.5,
            "item_score": 2.5,
        },
    ],
    "dimension_totals": {
        "core_correctness": 31.25,
        "reasoning_correctness": 15.5,
        "completeness": 7.5,
        "scope_precision": 10.0,
        "evidence_actionability": 6.25,
    },
    "raw_total": 70.5,
    "critical_cap": None,
    "capped_total": 70.5,
    "consensus": {
        "mode": "mean",
        "judges": 2,
        "arbiter_used": False,
        "human_review_triggered": False,
    },
    "requires_human_review": False,
    "run_mode": "formal",
}

MINIMAL_SCORE: dict[str, Any] = {
    "schema_version": "score-v1",
    "benchmark_version": "ai-score-v1",
    "judge_protocol": "semantic_outcome_v1",
    "scoring_profile": "bug_localization_v1",
    "judge_provider": "claude-code-cli",
    "judge_requested_model": "glm-5.2",
    "judge_model": "glm-5.2",
    "judge_cli_version": "2.1.220",
    "judge_prompt_digest": DIGEST_PROMPT,
    "ground_truth_digest": DIGEST_GT,
    "agent_answer_digest": DIGEST_ANSWER,
    "case_id": CASE_ID,
    "task_type": "bug_localization",
    "items": [
        {
            "item_id": "outcome.root-cause",
            "dimension": "core_correctness",
            "points": 35,
            "consensus_credit": 1.0,
            "item_score": 35.0,
        }
    ],
    "dimension_totals": {
        "core_correctness": 35.0,
        "reasoning_correctness": 0.0,
        "completeness": 0.0,
        "scope_precision": 0.0,
        "evidence_actionability": 0.0,
    },
    "raw_total": 35.0,
    "critical_cap": None,
    "capped_total": 35.0,
    "consensus": {"mode": "single", "judges": 1, "arbiter_used": False},
    "requires_human_review": False,
    "run_mode": "development",
}


def score_missing_digest() -> dict[str, Any]:
    """A formal score missing a required input digest."""
    bad = copy.deepcopy(MINIMAL_SCORE)
    del bad["ground_truth_digest"]
    return bad


def score_with_bad_benchmark_version() -> dict[str, Any]:
    """A score carrying an incompatible benchmark version."""
    bad = copy.deepcopy(MINIMAL_SCORE)
    bad["benchmark_version"] = "ai-score-v2"
    return bad


def score_with_model_mismatch() -> dict[str, Any]:
    """Requested and effective Judge models differ (must be rejected, R2)."""
    bad = copy.deepcopy(MINIMAL_SCORE)
    bad["judge_requested_model"] = "glm-5.2"
    bad["judge_model"] = "claude-sonnet-4"
    return bad


def score_with_human_review() -> dict[str, Any]:
    """A formal score flagged for human review with frozen trigger reasons (R3)."""
    bad = copy.deepcopy(MINIMAL_SCORE)
    bad["requires_human_review"] = True
    bad["human_review_reasons"] = ["critical_consensus_confidence", "overall_confidence"]
    bad["consensus"]["human_review_triggered"] = True
    return bad


# --------------------------------------------------------------------------- #
# manifest.schema.json
# --------------------------------------------------------------------------- #
# 64-hex digest for fixture use only (distinct from the score digests above).
DIGEST_ARTIFACT = "sha256:" + "e" * 64

MANIFEST_ARTIFACT_NAMES = [
    "raw_response",
    "agent_answer",
    "run_metadata",
    "policy_result",
    "blind_input",
    "judge_a",
    "judge_b",
    "judge_c",
    "judge_score",
    "adjudication",
    "effective_score",
]
MANIFEST_STATUSES = ["present", "absent", "failed", "not_applicable"]


FULL_MANIFEST: dict[str, Any] = {
    "schema_version": "manifest-v1",
    "artifacts": [
        {
            "name": "raw_response",
            "status": "present",
            "path": "run/raw-response.txt",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "agent_answer",
            "status": "present",
            "path": "run/agent-answer.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "run_metadata",
            "status": "present",
            "path": "run/run-metadata.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "policy_result",
            "status": "present",
            "path": "run/policy-result.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "blind_input",
            "status": "present",
            "path": "run/judge/blind-input.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "judge_a",
            "status": "present",
            "path": "run/judge/judge-a.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {
            "name": "judge_b",
            "status": "present",
            "path": "run/judge/judge-b.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {"name": "judge_c", "status": "absent"},
        {
            "name": "judge_score",
            "status": "present",
            "path": "run/judge-score.json",
            "sha256": DIGEST_ARTIFACT,
        },
        {"name": "adjudication", "status": "not_applicable"},
        {
            "name": "effective_score",
            "status": "present",
            "path": "run/effective-score.json",
            "sha256": DIGEST_ARTIFACT,
        },
    ],
}

MINIMAL_MANIFEST: dict[str, Any] = {
    "schema_version": "manifest-v1",
    "artifacts": [
        {
            "name": "raw_response",
            "status": "present",
            "path": "run/raw-response.txt",
            "sha256": DIGEST_ARTIFACT,
        }
    ],
}


def manifest_with_unknown_name() -> dict[str, Any]:
    """An artifact name outside the allowed set."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0]["name"] = "raw_response_bad"
    return bad


def manifest_with_unknown_status() -> dict[str, Any]:
    """A status outside the allowed set."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0]["status"] = "missing"
    return bad


def manifest_present_missing_path() -> dict[str, Any]:
    """A present entry without a path."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    del bad["artifacts"][0]["path"]
    return bad


def manifest_present_missing_digest() -> dict[str, Any]:
    """A present entry without a sha256 digest."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    del bad["artifacts"][0]["sha256"]
    return bad


def manifest_present_invalid_path() -> dict[str, Any]:
    """A present entry with an absolute (non-repository-relative) path."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0]["path"] = "/abs/run/raw-response.txt"
    return bad


def manifest_present_invalid_digest() -> dict[str, Any]:
    """A present entry with a malformed sha256 digest."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0]["sha256"] = "not-a-digest"
    return bad


def manifest_adjudication_wrong_status() -> dict[str, Any]:
    """v1 adjudication with a status other than not_applicable."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0] = {"name": "adjudication", "status": "absent"}
    return bad


def manifest_non_present_with_placeholder() -> dict[str, Any]:
    """A non-present entry carrying a path/digest placeholder."""
    bad = copy.deepcopy(MINIMAL_MANIFEST)
    bad["artifacts"][0] = {
        "name": "judge_c",
        "status": "absent",
        "path": "run/judge/judge-c.json",
        "sha256": DIGEST_ARTIFACT,
    }
    return bad
