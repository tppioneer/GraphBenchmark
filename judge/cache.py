"""Judge-result cache keyed by the complete Judge-input identity
(docs/ai-scoring-design.md §13.4, §20).

A Judge result is a deterministic function of the blind input the Judge sees
(answer, GT, prompt, profile, blinding) together with the Judge execution
identity (provider, requested+effective model, CLI version, generation
parameters). The cache key is a SHA-256 digest over the canonical form of all
of these, so that *any* input or version change affecting the Judge result
invalidates the cache (invariant), while the same complete input always yields
the same key (invariant: "相同语义输入的规范序列化和 digest 稳定"). Per §13.4,
regenerating a report for the same input must not implicitly re-score.

Integrity: every cached entry stores a digest of the Judge result it holds. On
read the digest is re-verified; a tampered or incomplete entry is rejected
(acceptance: "损坏/不完整缓存被拒绝") rather than served silently.

Non-rewriting: a cache hit returns a deep copy of the stored verdict. The cache
holds no artifact paths and exposes no API to write the cached result back to
an original Judge artifact, so a hit never overwrites the original
``judge-a.json`` / ``judge-b.json`` / ``judge-c.json`` audit trail (acceptance:
"缓存命中不重写原始 Judge artifact"). Persistent disk storage and artifact-path
management belong to the runner/integration layer and are out of scope for
AIS-007; this module delivers the key derivation and the integrity-verified
cache contract.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import digest_json, is_valid_digest

#: Cache format version. Bumped when the cache entry shape or key derivation
#: changes; a bump invalidates every prior entry (invariant).
CACHE_PROTOCOL_VERSION = "judge-cache-v1"

#: The input digests that must be valid sha256 for a reproducible cache key.
_DIGEST_FIELDS: tuple[str, ...] = (
    "judge_prompt_digest",
    "ground_truth_digest",
    "agent_answer_digest",
)

#: Fields required in every cached entry for it to be considered complete.
#: ``key_components`` is always present (``None`` when no ``key_input`` was
#: supplied); requiring it ensures a tamperer cannot delete it to bypass the
#: key-to-components integrity check in :meth:`JudgeCache._verify`.
_REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "cache_protocol_version",
    "judge_result",
    "result_digest",
    "key_components",
)

#: The digest fields read from a blind payload's ``digests`` block.
_BLIND_DIGEST_FIELDS: tuple[str, ...] = (
    "judge_prompt_digest",
    "ground_truth_digest",
    "agent_answer_digest",
    "profile_version",
    "blinding_protocol_version",
)


class CacheCorruptedError(Exception):
    """A cache entry exists but is tampered, incomplete, or format-mismatched."""


@dataclass(frozen=True)
class CacheKeyInput:
    """The complete set of inputs that determine a Judge result.

    Each field is a cache-key component per the acceptance criterion
    "缓存键包含模型、Provider、生成参数、Prompt、GT、答案、Profile、盲化和协议版本".
    The model/provider/CLI/parameter identity is supplied by the Judge caller
    and never appears in the blind payload itself (it would leak experiment
    identity to the Judge, §9.2).
    """

    judge_protocol: str
    judge_provider: str
    judge_requested_model: str
    judge_model: str
    judge_cli_version: str
    generation_params: Mapping[str, Any]
    judge_prompt_digest: str
    ground_truth_digest: str
    agent_answer_digest: str
    profile_version: str
    blinding_protocol_version: str

    @classmethod
    def from_blind_input(
        cls,
        blind_input: Mapping[str, Any],
        *,
        judge_provider: str,
        judge_requested_model: str,
        judge_model: str,
        judge_cli_version: str,
        generation_params: Mapping[str, Any],
    ) -> "CacheKeyInput":
        """Build a key input from a blind payload plus Judge execution identity.

        The answer/GT/prompt/profile/blinding digests and versions are read from
        the blind payload's ``digests`` block (recorded by
        :mod:`judge.blind_payload`); the execution identity is supplied by the
        caller and stays out of the blind payload.

        Raises :class:`CacheCorruptedError` if the blind payload's ``digests``
        block is incomplete - the payload is not a usable cache identity.
        """
        digests = blind_input.get("digests") or {}
        missing = [f for f in _BLIND_DIGEST_FIELDS if f not in digests]
        if missing:
            raise CacheCorruptedError(f"blind input digests block is missing: {missing}")
        return cls(
            judge_protocol=blind_input.get("judge_protocol", ""),
            judge_provider=judge_provider,
            judge_requested_model=judge_requested_model,
            judge_model=judge_model,
            judge_cli_version=judge_cli_version,
            generation_params=dict(generation_params),
            judge_prompt_digest=digests["judge_prompt_digest"],
            ground_truth_digest=digests["ground_truth_digest"],
            agent_answer_digest=digests["agent_answer_digest"],
            profile_version=digests["profile_version"],
            blinding_protocol_version=digests["blinding_protocol_version"],
        )

    def key_payload(self) -> dict[str, Any]:
        """The canonical object hashed to form the cache key."""
        return {
            "cache_protocol_version": CACHE_PROTOCOL_VERSION,
            "judge_protocol": self.judge_protocol,
            "judge_provider": self.judge_provider,
            "judge_requested_model": self.judge_requested_model,
            "judge_model": self.judge_model,
            "judge_cli_version": self.judge_cli_version,
            "generation_params": dict(self.generation_params),
            "judge_prompt_digest": self.judge_prompt_digest,
            "ground_truth_digest": self.ground_truth_digest,
            "agent_answer_digest": self.agent_answer_digest,
            "profile_version": self.profile_version,
            "blinding_protocol_version": self.blinding_protocol_version,
        }


def compute_cache_key(key_input: CacheKeyInput) -> str:
    """Return the ``sha256:<hex>`` cache key for ``key_input``.

    Raises :class:`ValueError` if any input digest is malformed, since a bad
    digest would make the key non-reproducible and could mask input changes
    (invariant: "任一影响 Judge 结果的输入或版本变化都必须缓存失效").
    """
    for name in _DIGEST_FIELDS:
        value = getattr(key_input, name)
        if not is_valid_digest(value):
            raise ValueError(f"{name} is not a valid sha256 digest: {value!r}")
    return digest_json(key_input.key_payload())


class JudgeCache:
    """In-memory, integrity-verified cache of Judge results.

    The cache stores deep-copied snapshots of Judge verdicts keyed by
    :func:`compute_cache_key`. Reads re-verify a stored digest before serving; a
    tampered or incomplete entry raises :class:`CacheCorruptedError` instead of
    returning stale data.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def put(
        self,
        key: str,
        judge_result: Mapping[str, Any],
        *,
        key_input: CacheKeyInput | None = None,
    ) -> None:
        """Store ``judge_result`` under ``key`` as a deep-copied snapshot.

        When ``key_input`` is supplied, ``key`` **must** equal
        :func:`compute_cache_key` applied to it; a malformed or mismatched key
        is rejected so an entry can never be stored under a key unrelated to
        its components (AIS007-R2). ``key_input`` is retained as audit metadata
        (``key_components``) and re-verified on read to prevent stale or
        poisoned hits.
        """
        if key_input is not None:
            expected_key = compute_cache_key(key_input)
            if key != expected_key:
                raise ValueError(
                    f"key {key!r} does not match compute_cache_key(key_input) "
                    f"({expected_key!r}); refusing to store under an unrelated key"
                )
        result_copy = copy.deepcopy(judge_result)
        entry: dict[str, Any] = {
            "cache_protocol_version": CACHE_PROTOCOL_VERSION,
            "judge_result": result_copy,
            "result_digest": digest_json(result_copy),
            "key_components": key_input.key_payload() if key_input is not None else None,
        }
        self._entries[key] = entry

    def has(self, key: str) -> bool:
        """True only when an entry exists for ``key`` (not yet integrity-verified)."""
        return key in self._entries

    def get(self, key: str) -> dict[str, Any] | None:
        """Return a deep copy of the cached Judge result, or ``None`` on a miss.

        Raises :class:`CacheCorruptedError` if an entry exists but is tampered
        or incomplete (acceptance: "损坏/不完整缓存被拒绝"). The returned copy is
        independent of the stored snapshot, so callers cannot mutate the cache,
        and the cache never writes to an original Judge artifact (acceptance:
        "缓存命中不重写原始 Judge artifact").
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._verify(entry, key)
        return copy.deepcopy(entry["judge_result"])

    def evict(self, key: str) -> bool:
        """Remove the entry for ``key``; return ``True`` if one was present."""
        return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        """Remove every cached entry."""
        self._entries.clear()

    @staticmethod
    def _verify(entry: Mapping[str, Any], key: str) -> None:
        for field_name in _REQUIRED_ENTRY_FIELDS:
            if field_name not in entry:
                raise CacheCorruptedError(
                    f"cache entry {key!r} is incomplete: missing {field_name!r}"
                )
        if entry["cache_protocol_version"] != CACHE_PROTOCOL_VERSION:
            raise CacheCorruptedError(
                f"cache entry {key!r} has protocol version "
                f"{entry['cache_protocol_version']!r}, expected "
                f"{CACHE_PROTOCOL_VERSION!r}"
            )
        expected = digest_json(entry["judge_result"])
        if entry["result_digest"] != expected:
            raise CacheCorruptedError(
                f"cache entry {key!r} is tampered: result digest mismatch "
                f"(stored {entry['result_digest']!r}, recomputed {expected!r})"
            )
        # Key-to-components integrity (AIS007-R2): when ``key_components`` were
        # recorded they must hash back to the lookup key. A mismatch means the
        # entry was stored under a wrong key or its components were tampered;
        # serving it would be a stale or poisoned hit.
        key_components = entry.get("key_components")
        if key_components is not None:
            expected_key = digest_json(key_components)
            if key != expected_key:
                raise CacheCorruptedError(
                    f"cache entry {key!r} key_components do not hash to key "
                    f"(expected {expected_key!r}); refusing stale or poisoned hit"
                )
