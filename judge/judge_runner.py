"""Judge Runner: orchestrates A/B/C Judge calls, retry, audit and consensus
(docs/ai-scoring-design.md §13, §13.5, §13.6).

The runner is the entry point for scoring an answer. It:

* Decides whether to call Judge A only (development) or A/B/C (formal) based on
  the run mode and the consensus module's arbiter decision.
* Calls each Judge via the configured :class:`JudgeProvider`, with at most one
  retry per Judge (§13.5).
* Collects audit information (digests, parameters, timings, retries).
* Produces an ordered ``JudgeRunResult`` that the benchmark runner can persist
  and feed to :func:`judge.consensus.form_consensus` /
  :func:`judge.consensus.build_effective_score`.

Frozen invariants enforced by the runner:

* Retry does not change blind input, prompt, model or generation parameters.
* Invalid/missing Judge output does not produce a formal score.
* A/B then conditionally C based on ``judge.consensus.should_call_arbiter``.
* All secrets are redacted before output is persisted.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any, Mapping

from judge.blind_payload import BlindPayloadError, build_blind_input
from judge.cache import CacheKeyInput, JudgeCache, compute_cache_key
from judge.canonical import digest_json, digest_text
from judge.consensus import (
    ConsensusError,
    should_call_arbiter,
)
from judge.prompts import PROMPT_VERSION, build_judge_prompt, compute_prompt_digest
from judge.provider import (
    DEFAULT_GENERATION_PARAMS,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_JUDGE_TIMEOUT_MS,
    DEFAULT_PROMPT_DIGEST,
    UNVERIFIABLE_MODEL,
    JudgeCallParams,
    JudgeCallResult,
    JudgeProvider,
)

#: Frozen run modes (§13.2).
RUN_MODES = ("development", "formal")

#: Judge labels in call order.
_JUDGE_LABELS = ("A", "B", "C")


# --- Exceptions ------------------------------------------------------------ #

class JudgeRunnerError(Exception):
    """Runner-level error: configuration, input validation or orchestration failure."""


class JudgeFailedError(JudgeRunnerError):
    """A Judge call failed after retry exhaustion (§13.5)."""


class JudgeUnavailableError(JudgeRunnerError):
    """Provider authentication or availability failure (§13.6)."""


# --- Data model ------------------------------------------------------------ #

@dataclasses.dataclass(frozen=True)
class JudgeRunConfig:
    """Configuration for a single Judge run."""

    judge_model: str = DEFAULT_JUDGE_MODEL
    timeout_ms: int = DEFAULT_JUDGE_TIMEOUT_MS
    generation_params: Mapping[str, Any] = dataclasses.field(
        default_factory=lambda: dict(DEFAULT_GENERATION_PARAMS)
    )
    run_mode: str = "development"
    prompt_version: str = PROMPT_VERSION


@dataclasses.dataclass(frozen=True)
class JudgeAuditEntry:
    """Audit record for a single Judge A/B/C call."""

    label: str
    success: bool
    judge_output: dict[str, Any] | None
    raw_stdout_digest: str
    raw_stderr_digest: str
    cli_version: str
    requested_model: str
    effective_model: str
    generation_params: Mapping[str, Any]
    prompt_digest: str
    elapsed_ms: int
    retry_count: int
    failure_reason: str | None
    retry_exhausted: bool


@dataclasses.dataclass(frozen=True)
class JudgeRunResult:
    """Complete result of a Judge run."""

    success: bool
    run_mode: str
    judge_model: str
    judge_a: JudgeCallResult | None
    judge_b: JudgeCallResult | None
    judge_c: JudgeCallResult | None
    arbiter_required: bool
    arbiter_called: bool
    blind_input: dict[str, Any] | None
    blind_input_digest: str
    prompt_digest: str
    agent_answer_digest: str
    ground_truth_digest: str
    provider_name: str
    cli_version: str
    elapsed_ms: int
    status: str
    failure_reason: str | None
    audits: tuple[JudgeAuditEntry, ...]
    cache_key: str | None


# --- Runner ---------------------------------------------------------------- #

class JudgeRunner:
    """Orchestrates Judge A/B/C calls with retry, audit and consensus gating.

    Usage::

        runner = JudgeRunner(provider, cache=JudgeCache())
        result = runner.run(case, profile, ground_truth, agent_answer)
        if result.success:
            # feed result.judge_a, result.judge_b, result.judge_c to
            # judge.consensus.form_consensus()
    """

    def __init__(
        self,
        provider: JudgeProvider,
        config: JudgeRunConfig | None = None,
        cache: JudgeCache | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or JudgeRunConfig()
        self._cache = cache or JudgeCache()

    @property
    def provider(self) -> JudgeProvider:
        return self._provider

    @property
    def config(self) -> JudgeRunConfig:
        return self._config

    def run(
        self,
        case: Mapping[str, Any],
        profile: Mapping[str, Any],
        ground_truth: Mapping[str, Any],
        agent_answer: Mapping[str, Any],
        *,
        excerpts: list[Mapping[str, Any]] | None = None,
        task_profile: dict[str, Any] | None = None,
        common_profile: dict[str, Any] | None = None,
    ) -> JudgeRunResult:
        """Execute the full Judge run for the given case/answer.

        Returns a ``JudgeRunResult`` with the status of every Judge call.
        On success, ``judge_a`` / ``judge_b`` / ``judge_c`` carry the raw results
        for the consensus layer. On failure, ``status`` is ``judge_failed`` or
        ``judge_unavailable`` and no formal score should be produced.
        """
        start = time.monotonic()
        run_mode = self._config.run_mode
        if run_mode not in RUN_MODES:
            return self._fail_result(f"invalid run_mode: {run_mode}", start)

        # Build blind input.
        prompt_digest = DEFAULT_PROMPT_DIGEST
        try:
            blind_input = build_blind_input(
                case=case,
                profile=profile,
                ground_truth=ground_truth,
                agent_answer=agent_answer,
                judge_prompt_digest=prompt_digest,
                excerpts=excerpts,
            )
        except BlindPayloadError as exc:
            return self._fail_result(f"blind input error: {exc}", start)

        # Build the actual prompt and compute its digest.
        prompt_text = build_judge_prompt(blind_input)
        actual_prompt_digest = compute_prompt_digest(blind_input)
        blind_input["digests"]["judge_prompt_digest"] = actual_prompt_digest
        prompt_digest = actual_prompt_digest

        blind_input_digest = digest_json(blind_input)
        agent_answer_digest = blind_input.get("digests", {}).get("agent_answer_digest", "")
        ground_truth_digest = blind_input.get("digests", {}).get("ground_truth_digest", "")

        gen_params = dict(self._config.generation_params)

        # Check cache first.
        cache_key = self._check_cache(
            blind_input, prompt_digest, agent_answer_digest, ground_truth_digest, gen_params
        )
        if cache_key and cache_key in self._cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return self._build_cached_result(cached, run_mode, blind_input, prompt_digest,
                                                  agent_answer_digest, ground_truth_digest, start)

        # Judge A call.
        judge_a = self._call_single("A", blind_input, prompt_text, prompt_digest)
        if judge_a is None:
            return self._fail_result("judge_unavailable", start, blind_input, prompt_digest,
                                      agent_answer_digest, ground_truth_digest)
        if judge_a.failed:
            return self._build_result(
                success=False, run_mode=run_mode, blind_input=blind_input,
                prompt_digest=prompt_digest, agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest, start=start,
                status="judge_failed", failure_reason=judge_a.failure_reason,
                judge_a=judge_a, audits=[self._to_audit(judge_a)],
            )

        # R3: model-consistency check for formal mode.
        model_check = self._reject_on_model_mismatch(
            judge_a, run_mode, start,
            blind_input=blind_input, prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest, judge_a=judge_a,
        )
        if model_check is not None:
            return model_check

        if run_mode == "development":
            elapsed = int((time.monotonic() - start) * 1000)
            result = JudgeRunResult(
                success=True,
                run_mode=run_mode,
                judge_model=self._config.judge_model,
                judge_a=judge_a,
                judge_b=None,
                judge_c=None,
                arbiter_required=False,
                arbiter_called=False,
                blind_input=blind_input,
                blind_input_digest=blind_input_digest,
                prompt_digest=prompt_digest,
                agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest,
                provider_name=self._provider.provider_name,
                cli_version=self._provider.cli_version,
                elapsed_ms=elapsed,
                status="completed",
                failure_reason=None,
                audits=(self._to_audit(judge_a),),
                cache_key=cache_key,
            )
            self._maybe_cache(cache_key, result)
            return result

        # Judge B call.
        judge_b = self._call_single("B", blind_input, prompt_text, prompt_digest)
        if judge_b is None:
            return self._fail_result("judge_unavailable", start, blind_input, prompt_digest,
                                      agent_answer_digest, ground_truth_digest, judge_a=judge_a)
        if judge_b.failed:
            return self._build_result(
                success=False, run_mode=run_mode, blind_input=blind_input,
                prompt_digest=prompt_digest, agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest, start=start,
                status="judge_failed", failure_reason=judge_b.failure_reason,
                judge_a=judge_a, judge_b=judge_b,
                audits=[self._to_audit(judge_a), self._to_audit(judge_b)],
            )

        # R3: model-consistency check for formal mode.
        model_check = self._reject_on_model_mismatch(
            judge_b, run_mode, start,
            blind_input=blind_input, prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest,
            judge_a=judge_a, judge_b=judge_b,
        )
        if model_check is not None:
            return model_check

        # Check if arbiter is needed.
        arbiter_required = False
        arbiter_called = False
        judge_c = None

        if judge_a.judge_output and judge_b.judge_output:
            try:
                decision = should_call_arbiter(
                    judge_a.judge_output,
                    judge_b.judge_output,
                    ground_truth,
                    task_profile=task_profile,
                    common_profile=common_profile,
                )
                arbiter_required = decision.call_arbiter
            except ConsensusError:
                arbiter_required = True

        if arbiter_required:
            judge_c = self._call_single("C", blind_input, prompt_text, prompt_digest)
            if judge_c is None:
                return self._build_result(
                    success=False, run_mode=run_mode, blind_input=blind_input,
                    prompt_digest=prompt_digest, agent_answer_digest=agent_answer_digest,
                    ground_truth_digest=ground_truth_digest, start=start,
                    status="judge_unavailable", failure_reason="judge_unavailable",
                    judge_a=judge_a, judge_b=judge_b,
                    audits=[self._to_audit(judge_a), self._to_audit(judge_b)],
                )
            if judge_c.failed:
                return self._build_result(
                    success=False, run_mode=run_mode, blind_input=blind_input,
                    prompt_digest=prompt_digest, agent_answer_digest=agent_answer_digest,
                    ground_truth_digest=ground_truth_digest, start=start,
                    status="judge_failed", failure_reason=judge_c.failure_reason,
                    judge_a=judge_a, judge_b=judge_b, judge_c=judge_c,
                    audits=[self._to_audit(judge_a),
                            self._to_audit(judge_b),
                            self._to_audit(judge_c)],
                )
            # R3: model-consistency check for formal mode.
            model_check = self._reject_on_model_mismatch(
                judge_c, run_mode, start,
                blind_input=blind_input, prompt_digest=prompt_digest,
                agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest,
                judge_a=judge_a, judge_b=judge_b,
            )
            if model_check is not None:
                return model_check
            arbiter_called = True

        audits = [self._to_audit(judge_a), self._to_audit(judge_b)]
        if judge_c:
            audits.append(self._to_audit(judge_c))

        elapsed = int((time.monotonic() - start) * 1000)
        result = JudgeRunResult(
            success=True,
            run_mode=run_mode,
            judge_model=self._config.judge_model,
            judge_a=judge_a,
            judge_b=judge_b,
            judge_c=judge_c,
            arbiter_required=arbiter_required,
            arbiter_called=arbiter_called,
            blind_input=blind_input,
            blind_input_digest=blind_input_digest,
            prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest,
            provider_name=self._provider.provider_name,
            cli_version=self._provider.cli_version,
            elapsed_ms=elapsed,
            status="completed",
            failure_reason=None,
            audits=tuple(audits),
            cache_key=cache_key,
        )
        self._maybe_cache(cache_key, result)
        return result

    def _call_single(
        self,
        label: str,
        blind_input: Mapping[str, Any],
        prompt_text: str,
        prompt_digest: str,
    ) -> JudgeCallResult | None:
        params = JudgeCallParams(
            label=label,
            blind_input=blind_input,
            prompt_text=prompt_text,
            prompt_digest=prompt_digest,
            judge_model=self._config.judge_model,
            generation_params=dict(self._config.generation_params),
            timeout_ms=self._config.timeout_ms,
        )
        try:
            return self._provider.call(params)
        except Exception as exc:
            return JudgeCallResult(
                success=False,
                label=label,
                judge_output=None,
                raw_stdout="",
                raw_stderr=str(exc),
                cli_version=self._provider.cli_version,
                requested_model=self._config.judge_model,
                effective_model=self._config.judge_model,
                generation_params=dict(self._config.generation_params),
                prompt_digest=prompt_digest,
                elapsed_ms=0,
                retry_count=0,
                failed=True,
                failure_reason=str(exc),
                retry_exhausted=True,
            )

    def _reject_on_model_mismatch(
        self,
        result: JudgeCallResult,
        run_mode: str,
        start: float,
        blind_input: Mapping[str, Any] | None = None,
        prompt_digest: str = "",
        agent_answer_digest: str = "",
        ground_truth_digest: str = "",
        judge_a: JudgeCallResult | None = None,
        judge_b: JudgeCallResult | None = None,
    ) -> JudgeRunResult | None:
        if run_mode != "formal":
            return None
        audits = []
        for j in (judge_a, judge_b):
            if j is not None:
                audits.append(self._to_audit(j))
        if result.effective_model == UNVERIFIABLE_MODEL:
            return self._build_result(
                success=False, run_mode=run_mode,
                blind_input=blind_input, prompt_digest=prompt_digest,
                agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest, start=start,
                status="judge_failed", failure_reason="model_unverifiable",
                judge_a=judge_a, judge_b=judge_b, audits=audits,
            )
        if result.effective_model != result.requested_model:
            return self._build_result(
                success=False, run_mode=run_mode,
                blind_input=blind_input, prompt_digest=prompt_digest,
                agent_answer_digest=agent_answer_digest,
                ground_truth_digest=ground_truth_digest, start=start,
                status="judge_failed", failure_reason="model_mismatch",
                judge_a=judge_a, judge_b=judge_b, audits=audits,
            )
        return None

    def _check_cache(
        self,
        blind_input: Mapping[str, Any],
        prompt_digest: str,
        agent_answer_digest: str,
        ground_truth_digest: str,
        gen_params: Mapping[str, Any],
    ) -> str | None:
        key_input = CacheKeyInput.from_blind_input(
            blind_input,
            judge_provider=self._provider.provider_name,
            judge_requested_model=self._config.judge_model,
            judge_model=self._config.judge_model,
            judge_cli_version=self._provider.cli_version,
            generation_params=gen_params,
        )
        return compute_cache_key(key_input)

    def _maybe_cache(self, cache_key: str | None, result: JudgeRunResult) -> None:
        if cache_key is None or not result.success:
            return
        outputs = []
        for j in (result.judge_a, result.judge_b, result.judge_c):
            if j is not None and j.judge_output is not None:
                outputs.append(j.judge_output)
        if outputs:
            self._cache.put(cache_key, {"judge_outputs": outputs})

    def _build_cached_result(
        self,
        cached: dict[str, Any],
        run_mode: str,
        blind_input: Mapping[str, Any],
        prompt_digest: str,
        agent_answer_digest: str,
        ground_truth_digest: str,
        start: float,
    ) -> JudgeRunResult:
        elapsed = int((time.monotonic() - start) * 1000)
        outputs = cached.get("judge_outputs", [])
        judge_a = None
        judge_b = None
        judge_c = None
        audits = []

        for i, out in enumerate(outputs):
            label = _JUDGE_LABELS[i]
            result = JudgeCallResult(
                success=True,
                label=label,
                judge_output=out,
                raw_stdout=json.dumps(out),
                raw_stderr="",
                cli_version=self._provider.cli_version,
                requested_model=self._config.judge_model,
                effective_model=self._config.judge_model,
                generation_params=dict(self._config.generation_params),
                prompt_digest=prompt_digest,
                elapsed_ms=0,
                retry_count=0,
                failed=False,
                failure_reason=None,
                retry_exhausted=False,
            )
            audits.append(self._to_audit(result))
            if label == "A":
                judge_a = result
            elif label == "B":
                judge_b = result
            elif label == "C":
                judge_c = result

        return JudgeRunResult(
            success=True,
            run_mode=run_mode,
            judge_model=self._config.judge_model,
            judge_a=judge_a,
            judge_b=judge_b,
            judge_c=judge_c,
            arbiter_required=judge_c is not None,
            arbiter_called=judge_c is not None,
            blind_input=dict(blind_input),
            blind_input_digest=digest_json(blind_input),
            prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest,
            provider_name=self._provider.provider_name,
            cli_version=self._provider.cli_version,
            elapsed_ms=elapsed,
            status="completed",
            failure_reason=None,
            audits=tuple(audits),
            cache_key=None,
        )

    def _to_audit(self, result: JudgeCallResult) -> JudgeAuditEntry:
        return JudgeAuditEntry(
            label=result.label,
            success=result.success,
            judge_output=result.judge_output,
            raw_stdout_digest=digest_text(result.raw_stdout),
            raw_stderr_digest=digest_text(result.raw_stderr),
            cli_version=result.cli_version,
            requested_model=result.requested_model,
            effective_model=result.effective_model,
            generation_params=result.generation_params,
            prompt_digest=result.prompt_digest,
            elapsed_ms=result.elapsed_ms,
            retry_count=result.retry_count,
            failure_reason=result.failure_reason,
            retry_exhausted=result.retry_exhausted,
        )

    def _fail_result(
        self,
        reason: str,
        start: float,
        blind_input: Mapping[str, Any] | None = None,
        prompt_digest: str = "",
        agent_answer_digest: str = "",
        ground_truth_digest: str = "",
        judge_a: JudgeCallResult | None = None,
        judge_b: JudgeCallResult | None = None,
    ) -> JudgeRunResult:
        status = "judge_unavailable" if "unavailable" in reason else "judge_failed"
        audits = []
        for j in (judge_a, judge_b):
            if j is not None:
                audits.append(self._to_audit(j))
        return JudgeRunResult(
            success=False,
            run_mode=self._config.run_mode,
            judge_model=self._config.judge_model,
            judge_a=judge_a,
            judge_b=judge_b,
            judge_c=None,
            arbiter_required=False,
            arbiter_called=False,
            blind_input=dict(blind_input) if blind_input else None,
            blind_input_digest=digest_json(blind_input) if blind_input else "",
            prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest,
            provider_name=self._provider.provider_name,
            cli_version=self._provider.cli_version,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            status=status,
            failure_reason=reason,
            audits=tuple(audits),
            cache_key=None,
        )

    def _build_result(
        self,
        *,
        success: bool,
        run_mode: str,
        blind_input: Mapping[str, Any] | None,
        prompt_digest: str,
        agent_answer_digest: str,
        ground_truth_digest: str,
        start: float,
        status: str,
        failure_reason: str | None,
        judge_a: JudgeCallResult | None = None,
        judge_b: JudgeCallResult | None = None,
        judge_c: JudgeCallResult | None = None,
        audits: list[JudgeAuditEntry] | None = None,
    ) -> JudgeRunResult:
        elapsed = int((time.monotonic() - start) * 1000)
        effective_status = status
        if failure_reason and "unavailable" in failure_reason:
            effective_status = "judge_unavailable"
        return JudgeRunResult(
            success=success,
            run_mode=run_mode,
            judge_model=self._config.judge_model,
            judge_a=judge_a,
            judge_b=judge_b,
            judge_c=judge_c,
            arbiter_required=judge_c is not None,
            arbiter_called=judge_c is not None,
            blind_input=dict(blind_input) if blind_input else None,
            blind_input_digest=digest_json(blind_input) if blind_input else "",
            prompt_digest=prompt_digest,
            agent_answer_digest=agent_answer_digest,
            ground_truth_digest=ground_truth_digest,
            provider_name=self._provider.provider_name,
            cli_version=self._provider.cli_version,
            elapsed_ms=elapsed,
            status=effective_status,
            failure_reason=failure_reason,
            audits=tuple(audits) if audits else (),
            cache_key=None,
        )