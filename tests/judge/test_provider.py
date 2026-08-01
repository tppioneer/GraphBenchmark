"""Tests for ``judge.provider`` (AIS-008, docs/ai-scoring-design.md §13.3, §13.5, §13.6).

Tests cover:

* FakeCliProvider returns configured outputs for all fail modes.
* FakeCliProvider records calls for audit.
* Secret redaction.
* Output schema construction.
"""

from __future__ import annotations

from typing import Any

from judge.provider import (
    DEFAULT_GENERATION_PARAMS,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_PROMPT_DIGEST,
    UNVERIFIABLE_MODEL,
    ClaudeCodeCliProvider,
    FakeCliProvider,
    JudgeCallParams,
    JudgeProviderConfig,
    _build_output_schema,
    redact_secrets,
)


def _params(label: str = "A") -> JudgeCallParams:
    return JudgeCallParams(
        label=label,
        blind_input={"case_id": "test"},
        prompt_text="Evaluate this answer.",
        prompt_digest=DEFAULT_PROMPT_DIGEST,
        judge_model=DEFAULT_JUDGE_MODEL,
        generation_params=dict(DEFAULT_GENERATION_PARAMS),
        timeout_ms=300000,
    )


def _sample_judge_output() -> dict[str, Any]:
    return {
        "schema_version": "judge-output-v1",
        "judge_protocol": "semantic_outcome_v1",
        "scoring_profile": "bug_localization_v1",
        "items": [
            {
                "item_id": "outcome.root-cause",
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


# --------------------------------------------------------------------------- #
# FakeCliProvider basic operation
# --------------------------------------------------------------------------- #


def test_fake_provider_returns_configured_output() -> None:
    output = _sample_judge_output()
    provider = FakeCliProvider(judge_output=output)
    result = provider.call(_params())
    assert result.success is True
    assert result.judge_output == output
    assert result.label == "A"
    assert result.requested_model == DEFAULT_JUDGE_MODEL
    assert result.effective_model == DEFAULT_JUDGE_MODEL
    assert result.cli_version == "2.1.220"
    assert result.failed is False
    assert result.retry_count == 0


def test_fake_provider_records_calls() -> None:
    provider = FakeCliProvider(judge_output=_sample_judge_output())
    provider.call(_params("A"))
    provider.call(_params("B"))
    assert len(provider.calls) == 2
    assert provider.calls[0].label == "A"
    assert provider.calls[1].label == "B"


def test_fake_provider_properties() -> None:
    provider = FakeCliProvider()
    assert provider.provider_name == "fake-cli"
    assert provider.cli_version == "2.1.220"
    assert provider.effective_model == "glm-5.2"
    info = provider.cli_info
    assert "--model" in info.supported_flags
    assert "--print" in info.supported_flags


# --------------------------------------------------------------------------- #
# FakeCliProvider fail modes
# --------------------------------------------------------------------------- #


def test_fake_provider_auth_failure() -> None:
    provider = FakeCliProvider(fail_mode="auth")
    result = provider.call(_params())
    assert result.success is False
    assert result.failure_reason == "judge_unavailable"
    assert result.retry_count == 0


def test_fake_provider_timeout() -> None:
    provider = FakeCliProvider(fail_mode="timeout")
    result = provider.call(_params())
    assert result.success is False
    assert result.failure_reason == "timeout"
    assert result.retry_exhausted is True
    assert result.retry_count == 1


def test_fake_provider_retry_exhausted() -> None:
    provider = FakeCliProvider(fail_mode="retry_exhausted")
    result = provider.call(_params())
    assert result.success is False
    assert result.failure_reason is not None
    assert result.retry_exhausted is True
    assert result.retry_count == 1


def test_fake_provider_invalid_json() -> None:
    provider = FakeCliProvider(fail_mode="invalid_json")
    result = provider.call(_params())
    assert result.success is True
    assert result.failed is True
    assert result.failure_reason == "invalid_json"


def test_fake_provider_non_dict_output() -> None:
    provider = FakeCliProvider(fail_mode="non_dict_output")
    result = provider.call(_params())
    assert result.success is True
    assert result.failed is True
    assert result.failure_reason == "expected JSON object"


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #


def test_redact_api_key() -> None:
    text = "api_key=sk-abc123 and token=xyz"
    result = redact_secrets(text)
    assert "<REDACTED>" in result
    assert "sk-abc123" not in result


def test_redact_authorization_header() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
    result = redact_secrets(text)
    assert "<REDACTED>" in result


def test_redact_password() -> None:
    text = "password=super_secret_value"
    result = redact_secrets(text)
    assert "<REDACTED>" in result
    assert "super_secret" not in result


def test_redact_preserves_normal_text() -> None:
    text = "This is normal text with no secrets."
    result = redact_secrets(text)
    assert result == text


def test_redact_jwt_fully_removed() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_part"
    text = f"token {jwt} here"
    result = redact_secrets(text)
    assert jwt not in result
    assert "<REDACTED>" in result


# --------------------------------------------------------------------------- #
# Output schema
# --------------------------------------------------------------------------- #


def test_output_schema_has_required_fields() -> None:
    schema = _build_output_schema()
    assert schema["type"] == "object"
    required = set(schema["required"])
    for field in ("schema_version", "judge_protocol", "scoring_profile", "items",
                  "unsupported_claims", "critical_errors", "overall_confidence",
                  "requires_human_review"):
        assert field in required


def test_output_schema_credit_enum() -> None:
    schema = _build_output_schema()
    item_schema = schema["properties"]["items"]["items"]
    credit = item_schema["properties"]["credit"]
    assert credit["enum"] == [0, 0.25, 0.5, 0.75, 1]


def test_output_schema_no_additional_properties() -> None:
    schema = _build_output_schema()
    assert schema.get("additionalProperties") is False


# --------------------------------------------------------------------------- #
# R1: Unverifiable model when --model is not supported
# --------------------------------------------------------------------------- #


def test_unverifiable_model_when_no_model_flag() -> None:
    provider = ClaudeCodeCliProvider.__new__(ClaudeCodeCliProvider)
    provider._config = JudgeProviderConfig()
    provider._cli_version = "0.0.0"
    provider._supported_flags = frozenset({"--print", "--output-format"})
    provider._unsupported_params = ("model",)
    provider._effective_model = provider._config.judge_model
    params = _params()
    provider._build_cli_args(params)
    assert provider.effective_model == UNVERIFIABLE_MODEL


# --------------------------------------------------------------------------- #
# Provider config
# --------------------------------------------------------------------------- #


def test_default_config() -> None:
    config = JudgeProviderConfig()
    assert config.judge_model == DEFAULT_JUDGE_MODEL
    assert config.timeout_ms == 300000
    assert config.generation_params["temperature"] == 0.0
    assert config.generation_params["seed"] == 42


def test_custom_config() -> None:
    config = JudgeProviderConfig(
        judge_model="claude-sonnet-4",
        timeout_ms=60000,
        generation_params={"temperature": 0.5},
    )
    assert config.judge_model == "claude-sonnet-4"
    assert config.timeout_ms == 60000
    assert config.generation_params["temperature"] == 0.5