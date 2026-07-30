"""Canonical JSON serialization and SHA-256 digests.

Stable serialization is required by two AIS-007 invariants
(docs/ai-scoring-design.md §9.2, §13.4):

* "相同语义输入的规范序列化和 digest 稳定" - the same semantic input must always
  produce the same canonical bytes, and therefore the same digest, so that audit
  digests and Judge cache keys are reproducible across platforms, processes and
  Python dict orderings.
* "任一影响 Judge 结果的输入或版本变化都必须缓存失效" - because the digest is
  taken over the canonical form of the *complete* input, any semantic change
  changes the digest and invalidates the cache.

The canonical form is JSON with recursively sorted keys, no insignificant
whitespace, and UTF-8 encoding (non-ASCII emitted directly, not escaped). Only
the standard library is used so production code stays dependency-free:
``jsonschema`` is a dev-only dependency and schema validation is test-only,
mirroring the AIS-002 contract (see memory [[ais-002-contracts-layout]]).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Prefix for every digest string produced here and recorded in artifacts.
DIGEST_PREFIX = "sha256:"

#: Length of the hex part of a SHA-256 digest (64 lowercase hex chars).
DIGEST_HEX_LEN = 64

#: The full length of a ``sha256:<hex>`` string.
DIGEST_STR_LEN = len(DIGEST_PREFIX) + DIGEST_HEX_LEN

_HEX = "0123456789abcdef"


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to deterministic JSON bytes.

    Keys are sorted recursively, insignificant whitespace is stripped, and
    non-ASCII characters are emitted as UTF-8 rather than ``\\u`` escapes, so
    the byte output depends only on the semantic content of ``obj`` and never
    on dict insertion order, locale or platform.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of raw bytes."""
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    """Return the ``sha256:<hex>`` digest of a UTF-8 text string."""
    return digest_bytes(text.encode("utf-8"))


def digest_json(obj: Any) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical JSON form of ``obj``."""
    return digest_bytes(canonical_json_bytes(obj))


def is_valid_digest(value: object) -> bool:
    """True when ``value`` is a ``sha256:`` string followed by 64 lowercase hex chars.

    :func:`hashlib.sha256.hexdigest` always produces lowercase hex, so the
    lowercase check rejects any tampered or accidentally-uppercased digest.
    """
    if not isinstance(value, str):
        return False
    if len(value) != DIGEST_STR_LEN:
        return False
    if not value.startswith(DIGEST_PREFIX):
        return False
    return all(c in _HEX for c in value[len(DIGEST_PREFIX) :])
