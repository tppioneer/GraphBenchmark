"""Judge cache key and store tests (AIS-007, docs/ai-scoring-design.md §13.4, §20).

Covers the cache acceptance criteria:

* the cache key includes model, Provider, generation parameters, Prompt, GT,
  answer, Profile, blinding and protocol version, and any one of them changing
  invalidates the key;
* a cache hit returns the stored verdict without rewriting the original Judge
  artifact (deep-copy isolation, no artifact-write API);
* a corrupted or incomplete cache entry is rejected rather than served.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

import pytest

# A real blind payload (built by judge.blind_payload) is used for the
# integration test; the rest of the suite uses the minimal dict below so the
# cache logic is exercised independently of the payload builder.
from judge.blind_payload import build_blind_input
from judge.cache import (
    CacheCorruptedError,
    CacheKeyInput,
    JudgeCache,
    compute_cache_key,
)
from judge.canonical import is_valid_digest

_PROMPT = "sha256:" + "1" * 64
_GT = "sha256:" + "2" * 64
_ANSWER = "sha256:" + "3" * 64


def _base_key_input() -> CacheKeyInput:
    return CacheKeyInput(
        judge_protocol="semantic_outcome_v1",
        judge_provider="claude-code-cli",
        judge_requested_model="glm-5.2",
        judge_model="glm-5.2",
        judge_cli_version="2.1.220",
        generation_params={"temperature": 0.0, "seed": 42, "top_p": 1.0},
        judge_prompt_digest=_PROMPT,
        ground_truth_digest=_GT,
        agent_answer_digest=_ANSWER,
        profile_version="bug-localization-v1",
        blinding_protocol_version="blind-v1",
    )


def _minimal_blind_input() -> dict[str, Any]:
    return {
        "schema_version": "judge-input-v1",
        "judge_protocol": "semantic_outcome_v1",
        "scoring_profile": "bug_localization_v1",
        "task_type": "bug_localization",
        "case_id": "case-1",
        "question": "q",
        "rubric_items": [
            {"id": "i", "dimension": "core_correctness", "points": 35, "criterion": "c"}
        ],
        "answer": {"summary": "s", "explanation": "e"},
        "digests": {
            "agent_answer_digest": _ANSWER,
            "ground_truth_digest": _GT,
            "judge_prompt_digest": _PROMPT,
            "profile_version": "bug-localization-v1",
            "blinding_protocol_version": "blind-v1",
        },
    }


def _sample_judge_result() -> dict[str, Any]:
    return {
        "schema_version": "judge-output-v1",
        "judge_protocol": "semantic_outcome_v1",
        "scoring_profile": "bug_localization_v1",
        "items": [
            {
                "item_id": "i",
                "credit": 1,
                "verdict": "correct",
                "reason": "ok",
                "confidence": 0.9,
            }
        ],
        "unsupported_claims": [],
        "critical_errors": [],
        "overall_confidence": 0.9,
        "requires_human_review": False,
    }


# ----------------------------- key derivation ------------------------------ #


def test_compute_cache_key_returns_valid_sha256() -> None:
    key = compute_cache_key(_base_key_input())
    assert is_valid_digest(key)


def test_key_stable_for_identical_input() -> None:
    assert compute_cache_key(_base_key_input()) == compute_cache_key(_base_key_input())


def test_key_stable_for_equal_copy() -> None:
    a = _base_key_input()
    b = dataclasses.replace(a)  # equal field values, new instance
    assert compute_cache_key(a) == compute_cache_key(b)


@pytest.mark.parametrize(
    "field,value",
    [
        ("judge_protocol", "pairwise_v1"),
        ("judge_provider", "other-provider"),
        ("judge_requested_model", "claude-sonnet-4"),
        ("judge_model", "claude-sonnet-4"),
        ("judge_cli_version", "2.2.0"),
        ("generation_params", {"temperature": 0.1, "seed": 42, "top_p": 1.0}),
        ("judge_prompt_digest", "sha256:" + "9" * 64),
        ("ground_truth_digest", "sha256:" + "8" * 64),
        ("agent_answer_digest", "sha256:" + "7" * 64),
        ("profile_version", "flow-tracing-v1"),
        ("blinding_protocol_version", "blind-v2"),
    ],
)
def test_key_invalidates_on_any_component_change(field: str, value: Any) -> None:
    base_key = compute_cache_key(_base_key_input())
    mutated = dataclasses.replace(_base_key_input(), **{field: value})
    assert compute_cache_key(mutated) != base_key


def test_generation_params_key_order_does_not_affect_key() -> None:
    a = dataclasses.replace(_base_key_input(), generation_params={"temperature": 0.0, "seed": 42})
    b = dataclasses.replace(_base_key_input(), generation_params={"seed": 42, "temperature": 0.0})
    assert compute_cache_key(a) == compute_cache_key(b)


def test_requested_and_effective_model_both_keyed() -> None:
    # Changing only the effective model (judge_model) while keeping the
    # requested model must still invalidate the key (§13.3: A/B/C must share
    # both requested and effective model).
    base = compute_cache_key(_base_key_input())
    mismatched = dataclasses.replace(_base_key_input(), judge_model="claude-sonnet-4")
    assert compute_cache_key(mismatched) != base


def test_compute_cache_key_rejects_malformed_digest() -> None:
    bad = dataclasses.replace(_base_key_input(), agent_answer_digest="not-a-digest")
    with pytest.raises(ValueError, match="agent_answer_digest"):
        compute_cache_key(bad)


# ----------------------------- from_blind_input ---------------------------- #


def test_from_blind_input_extracts_digests_and_versions() -> None:
    blind = _minimal_blind_input()
    key_input = CacheKeyInput.from_blind_input(
        blind,
        judge_provider="claude-code-cli",
        judge_requested_model="glm-5.2",
        judge_model="glm-5.2",
        judge_cli_version="2.1.220",
        generation_params={"temperature": 0.0, "seed": 42},
    )
    assert key_input.judge_protocol == "semantic_outcome_v1"
    assert key_input.judge_prompt_digest == _PROMPT
    assert key_input.ground_truth_digest == _GT
    assert key_input.agent_answer_digest == _ANSWER
    assert key_input.profile_version == "bug-localization-v1"
    assert key_input.blinding_protocol_version == "blind-v1"
    # The extracted input yields a valid key.
    assert is_valid_digest(compute_cache_key(key_input))


def test_from_blind_input_rejects_incomplete_digests_block() -> None:
    blind = _minimal_blind_input()
    del blind["digests"]["agent_answer_digest"]
    with pytest.raises(CacheCorruptedError, match="agent_answer_digest"):
        CacheKeyInput.from_blind_input(
            blind,
            judge_provider="claude-code-cli",
            judge_requested_model="glm-5.2",
            judge_model="glm-5.2",
            judge_cli_version="2.1.220",
            generation_params={"temperature": 0.0},
        )


def test_cache_key_integration_with_blind_payload() -> None:
    # End-to-end: a blind payload built by judge.blind_payload feeds the cache
    # key, and the same payload rebuilds to the same key.
    from tests.schemas import examples as ex

    def _build_blind() -> dict[str, Any]:
        from pathlib import Path

        import yaml

        profile_dir = Path(__file__).resolve().parent.parent.parent / "profiles"
        with (profile_dir / "bug-localization-v1.yaml").open(encoding="utf-8") as fh:
            profile = yaml.safe_load(fh)
        return build_blind_input(
            case=ex.FULL_CASE,
            profile=profile,
            ground_truth=ex.FULL_GT,
            agent_answer=ex.FULL_AGENT_ANSWER,
            judge_prompt_digest=_PROMPT,
        )

    blind = _build_blind()
    ki = CacheKeyInput.from_blind_input(
        blind,
        judge_provider="claude-code-cli",
        judge_requested_model="glm-5.2",
        judge_model="glm-5.2",
        judge_cli_version="2.1.220",
        generation_params={"temperature": 0.0, "seed": 1},
    )
    key_a = compute_cache_key(ki)
    key_b = compute_cache_key(
        CacheKeyInput.from_blind_input(
            _build_blind(),
            judge_provider="claude-code-cli",
            judge_requested_model="glm-5.2",
            judge_model="glm-5.2",
            judge_cli_version="2.1.220",
            generation_params={"temperature": 0.0, "seed": 1},
        )
    )
    assert key_a == key_b
    assert is_valid_digest(key_a)


# ----------------------------- store: hit / miss --------------------------- #


def test_put_then_get_returns_result() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    result = _sample_judge_result()
    cache.put(key, result)
    assert cache.has(key)
    assert key in cache
    assert cache.get(key) == result


def test_get_returns_none_on_miss() -> None:
    cache = JudgeCache()
    assert cache.get("sha256:" + "0" * 64) is None
    assert not cache.has("sha256:" + "0" * 64)


def test_put_stores_independent_snapshot() -> None:
    # The cache must not hold a reference to the caller's original dict; mutating
    # the original after put must not change the cached entry.
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    result = _sample_judge_result()
    cache.put(key, result)
    result["items"][0]["credit"] = 0  # mutate the caller's original
    assert cache.get(key)["items"][0]["credit"] == 1


def test_hit_does_not_rewrite_original_artifact() -> None:
    # A cache hit returns a deep copy; mutating it must not rewrite the stored
    # snapshot (the "original Judge artifact" held by the cache). The cache
    # exposes no artifact-path write API, so a hit can never overwrite an
    # on-disk judge-a/b/c.json either.
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    hit = cache.get(key)
    assert hit is not None
    hit["items"][0]["credit"] = 0
    hit["tampered"] = True
    # The stored snapshot is unchanged.
    again = cache.get(key)
    assert again is not None
    assert again["items"][0]["credit"] == 1
    assert "tampered" not in again
    # No artifact-writing surface exists on the cache.
    for attr in ("write_artifact", "save", "write", "dump", "store_artifact"):
        assert not hasattr(cache, attr)


def test_put_records_key_components_for_audit() -> None:
    cache = JudgeCache()
    ki = _base_key_input()
    key = compute_cache_key(ki)
    cache.put(key, _sample_judge_result(), key_input=ki)
    entry = cache._entries[key]  # noqa: SLF001 - white-box audit check
    assert entry["key_components"] == ki.key_payload()


def test_evict_and_clear() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    assert cache.evict(key) is True
    assert not cache.has(key)
    assert cache.evict(key) is False
    cache.put(key, _sample_judge_result())
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


# ----------------------------- corruption rejection ------------------------ #


def test_tampered_result_rejected() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    # Tamper with the stored result without updating its digest.
    cache._entries[key]["judge_result"]["items"][0]["credit"] = 0  # noqa: SLF001
    with pytest.raises(CacheCorruptedError, match="tampered"):
        cache.get(key)


def test_incomplete_entry_missing_result_rejected() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    del cache._entries[key]["judge_result"]  # noqa: SLF001
    with pytest.raises(CacheCorruptedError, match="incomplete"):
        cache.get(key)


def test_incomplete_entry_missing_digest_rejected() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    del cache._entries[key]["result_digest"]  # noqa: SLF001
    with pytest.raises(CacheCorruptedError, match="incomplete"):
        cache.get(key)


def test_protocol_version_mismatch_rejected() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    cache._entries[key]["cache_protocol_version"] = "judge-cache-v0"  # noqa: SLF001
    with pytest.raises(CacheCorruptedError, match="protocol version"):
        cache.get(key)


def test_corrupt_entry_does_not_serve_stale_data() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    cache.put(key, _sample_judge_result())
    cache._entries[key]["result_digest"] = "sha256:" + "0" * 64  # noqa: SLF001
    # The corrupt entry must raise, not return the (possibly tampered) result.
    with pytest.raises(CacheCorruptedError):
        cache.get(key)
    # After eviction the entry is gone and behaves as a clean miss.
    cache.evict(key)
    assert cache.get(key) is None


def test_put_overwrite_is_idempotent_for_same_result() -> None:
    cache = JudgeCache()
    key = compute_cache_key(_base_key_input())
    result = _sample_judge_result()
    cache.put(key, result)
    cache.put(key, copy.deepcopy(result))
    assert len(cache) == 1
    assert cache.get(key) == result
