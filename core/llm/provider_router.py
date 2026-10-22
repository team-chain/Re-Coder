"""
ReCoder Core — LLM Provider Router

Routing strategy
----------------
Primary   : Bedrock Claude Sonnet  — code patches, Dockerfile, complex ops analysis
Fast      : Bedrock Claude Haiku   — error classification, log summary, scanner results
Fallback  : Gemini Flash           — Bedrock failure or cost-reduction mode

2.0.36 작업 등급(llm/task_tier.py)
Quality   : 코드 생성·보안 수정·교정 — 위 Primary 그대로
Economy   : 계획 주제·요약·오류 분류·테스트 초안 — RECODER_ECONOMY_PROVIDER 로 따로 고를 수 있다
            (fast | anthropic | openai | compatible). 고르지 않으면 품질 공급자와 같다.

All calls are recorded as LLMCallRecord entries for cost tracking.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger(__name__)


# Lazy imports -- resolved at first use to avoid circular dependencies
def _get_schemas():
    try:
        from schemas import LLMCallRecord, ProviderType
    except ImportError:
        from core.schemas import LLMCallRecord, ProviderType
    return LLMCallRecord, ProviderType


def _get_providers():
    try:
        from llm.bedrock_provider import BedrockProvider, SONNET_MODELS, HAIKU_MODELS
        from llm.gemini_provider import GeminiProvider
    except ImportError:
        from core.llm.bedrock_provider import BedrockProvider, SONNET_MODELS, HAIKU_MODELS
        from core.llm.gemini_provider import GeminiProvider
    return BedrockProvider, SONNET_MODELS, HAIKU_MODELS, GeminiProvider


def _get_shared():
    """브레이커·비용 원장 — 두 진입점(라우터 스타일)이 공유하는 단일 구현."""
    try:
        from llm import breaker as _breaker_mod
        from llm import cost_ledger as _ledger
    except ImportError:
        from core.llm import breaker as _breaker_mod  # type: ignore
        from core.llm import cost_ledger as _ledger  # type: ignore
    return _breaker_mod, _ledger


def _get_tiers():
    try:
        from llm import task_tier as _tiers
    except ImportError:
        from core.llm import task_tier as _tiers  # type: ignore
    return _tiers


def _get_pricing():
    try:
        from llm import pricing as _pricing
    except ImportError:
        from core.llm import pricing as _pricing  # type: ignore
    return _pricing


def _get_base_types():
    try:
        from llm.base import LLMError, LLMResponse
    except ImportError:
        from core.llm.base import LLMError, LLMResponse  # type: ignore
    return LLMError, LLMResponse


def _instrument_llm_span(**kwargs):
    """v5.0 Q4 — OTel Span 계측. observability 패키지가 import 실패하면 no-op."""
    try:
        try:
            from observability.otel_adapter import instrument_llm_span
        except ImportError:
            from core.observability.otel_adapter import instrument_llm_span
        return instrument_llm_span(**kwargs)
    except Exception:  # pragma: no cover
        import contextlib

        class _NoopSpan:
            def set_attribute(self, *_a, **_k):
                return None

        @contextlib.contextmanager
        def _noop():
            yield _NoopSpan()

        return _noop()


# ---------------------------------------------------------------------------
# LLMProviderRouter
# ---------------------------------------------------------------------------


class LLMProviderRouter:
    """
    Single entry-point for all LLM calls within the ReCoder Core.

    Maintains a list of LLMCallRecord entries that can be consumed by
    SessionLogger for cost tracking and telemetry.
    """

    def __init__(self) -> None:
        BedrockProvider, SONNET_MODELS, HAIKU_MODELS, GeminiProvider = _get_providers()

        self._bedrock_sonnet = BedrockProvider(model_id=SONNET_MODELS[0])
        self._bedrock_haiku = BedrockProvider(model_id=HAIKU_MODELS[0])
        self._gemini = GeminiProvider()

        # 학생 배포 모드: 게이트웨이 env 가 있으면 Bedrock 직접호출 대신
        # 운영자 게이트웨이를 통해 호출(학생 PC 에 AWS 키 불필요).
        try:
            try:
                from llm.gateway_provider import GatewayProvider, gateway_enabled
            except ImportError:
                from core.llm.gateway_provider import GatewayProvider, gateway_enabled
            if gateway_enabled():
                self._bedrock_sonnet = GatewayProvider(model_id=SONNET_MODELS[0])
                self._bedrock_haiku = GatewayProvider(model_id=HAIKU_MODELS[0])
                log.info("LLM gateway mode enabled — Bedrock 호출을 운영자 게이트웨이로 라우팅")
        except Exception as exc:  # pragma: no cover
            log.debug("gateway provider 비활성: %s", exc)

        # AWS 없이 쓰는 경로: 사용자가 확장에서 Claude/OpenAI API 키를 골라 넣은 경우.
        # 명시적으로 고른 선택이므로 게이트웨이·Bedrock 보다 우선한다.
        try:
            try:
                from llm.api_key_provider import ApiKeyProvider, model_for, selected_provider
            except ImportError:
                from core.llm.api_key_provider import ApiKeyProvider, model_for, selected_provider
            chosen = selected_provider()
            if chosen:
                self._bedrock_sonnet = ApiKeyProvider(chosen, model_for(chosen))
                self._bedrock_haiku = ApiKeyProvider(chosen, model_for(chosen, fast=True))
                log.info("LLM API key mode enabled — provider=%s model=%s", chosen, self._bedrock_sonnet.model_id)
        except Exception as exc:  # pragma: no cover
            log.warning("API key provider 비활성: %s", exc)

        self._call_records: list[Any] = []  # list[LLMCallRecord]

        #: 2.0.36 절약 등급 공급자 — None 이면 품질 공급자를 그대로 쓴다(기존 동작).
        self._extra: dict = {}
        self._economy_spec = ""
        self._economy_note = ""
        try:
            self._economy = self._build_economy()
        except Exception as exc:  # pragma: no cover — 설정 오류로 Core 가 못 뜨면 안 된다
            log.warning("economy provider 비활성: %s", exc)
            self._economy = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def _fallback(self, prompt, schema, primary_error, **settings):
        """Do not replace the actual failure with an unconfigured fallback error."""
        from .base import LLMError, LLMErrorType
        error = primary_error or LLMError(
            "AI 서비스의 반복 오류로 잠시 대기 중입니다. 잠시 후 다시 요청해 주세요.",
            LLMErrorType.SERVICE_ERROR, True,
        )
        if not getattr(self._gemini, "available", True):
            if isinstance(error, LLMError):
                raise error
            raise LLMError(f"Bedrock 호출 실패: {error}", raw=error) from error
        try:
            return await self._gemini.generate(prompt, schema=schema, **settings)
        except Exception as secondary:
            raise LLMError(
                f"Bedrock 호출 실패: {error}; 대체 AI 호출 실패: {secondary}",
                error.error_type if isinstance(error, LLMError) else LLMErrorType.UNKNOWN,
                error.retryable if isinstance(error, LLMError) else False,
                raw=error,
            ) from error

    async def call_primary(
        self,
        prompt: str,
        schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Call Bedrock Sonnet (primary).
        On failure falls back to Gemini Flash.

        브레이커: 같은 provider/model 이 연속 실패하면 한동안 건너뛴다.
        상태는 프로세스 전역이라, 라우트마다 라우터를 새로 만들어도
        죽은 모델을 다시 두드리지 않는다.
        """
        LLMCallRecord, ProviderType = _get_schemas()
        breaker_mod, _ = _get_shared()
        start = time.monotonic()
        retry_count = 0
        primary_error = None

        br = breaker_mod.breaker_for(
            f"{self._bedrock_sonnet.provider_name}/{self._bedrock_sonnet.model_id}"
        )
        if br.is_open:
            log.info("[CB] open — %s 건너뜀 (call_primary)", self._bedrock_sonnet.model_id)
            retry_count = 1
        else:
            with _instrument_llm_span(
                provider="bedrock",
                model=self._bedrock_sonnet.model_id,
                operation="call_primary",
            ) as span:
                try:
                    messages = [{"role": "user", "content": [{"text": prompt}]}]
                    result = await self._bedrock_sonnet.converse(messages, output_schema=schema)
                    br.record_success()
                    input_tok, output_tok = self._estimate_tokens(prompt, result)
                    latency = int((time.monotonic() - start) * 1000)
                    span.set_attribute("input_tokens", input_tok)
                    span.set_attribute("output_tokens", output_tok)
                    span.set_attribute("latency_ms", latency)
                    self._record_call(
                        agent="primary", operation="call_primary",
                        provider=self._bedrock_sonnet.provider_name, model=self._bedrock_sonnet.model_id,
                        input_tokens=input_tok, output_tokens=output_tok,
                        latency_ms=latency, fallback_used=False, retry_count=0,
                    )
                    return result

                except Exception as exc:
                    primary_error = exc
                    br.record_failure()
                    log.warning("Bedrock Sonnet failed: %s — trying Gemini fallback", exc)
                    retry_count = 1

        # Gemini fallback (별도 span)
        with _instrument_llm_span(
            provider="gemini",
            model="gemini-2.5-flash",
            operation="call_primary_fallback",
        ) as span:
            result = await self._fallback(prompt, schema, primary_error)
            input_tok, output_tok = self._estimate_tokens(prompt, result)
            latency = int((time.monotonic() - start) * 1000)
            span.set_attribute("input_tokens", input_tok)
            span.set_attribute("output_tokens", output_tok)
            span.set_attribute("latency_ms", latency)
            span.set_attribute("fallback_used", True)
            self._record_call(
                agent="primary", operation="call_primary",
                provider="gemini", model="gemini-2.5-flash",
                input_tokens=input_tok, output_tokens=output_tok,
                latency_ms=latency, fallback_used=True, retry_count=retry_count,
            )
            return result

    async def call_fast(
        self,
        prompt: str,
        schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Call Bedrock Haiku (fast path).
        On failure falls back to Gemini Flash. 브레이커는 call_primary 와 동일.
        """
        breaker_mod, _ = _get_shared()
        start = time.monotonic()
        retry_count = 0
        primary_error = None

        br = breaker_mod.breaker_for(
            f"{self._bedrock_haiku.provider_name}/{self._bedrock_haiku.model_id}"
        )
        if br.is_open:
            log.info("[CB] open — %s 건너뜀 (call_fast)", self._bedrock_haiku.model_id)
            retry_count = 1
        else:
            try:
                messages = [{"role": "user", "content": [{"text": prompt}]}]
                result = await self._bedrock_haiku.converse(messages, output_schema=schema)
                br.record_success()
                input_tok, output_tok = self._estimate_tokens(prompt, result)
                latency = int((time.monotonic() - start) * 1000)
                self._record_call(
                    agent="fast", operation="call_fast",
                    provider=self._bedrock_haiku.provider_name, model=self._bedrock_haiku.model_id,
                    input_tokens=input_tok, output_tokens=output_tok,
                    latency_ms=latency, fallback_used=False, retry_count=0,
                )
                return result

            except Exception as exc:
                primary_error = exc
                br.record_failure()
                log.warning("Bedrock Haiku failed: %s — trying Gemini fallback", exc)
                retry_count = 1

        result = await self._fallback(prompt, schema, primary_error)
        input_tok, output_tok = self._estimate_tokens(prompt, result)
        latency = int((time.monotonic() - start) * 1000)
        self._record_call(
            agent="fast", operation="call_fast",
            provider="gemini", model="gemini-2.5-flash",
            input_tokens=input_tok, output_tokens=output_tok,
            latency_ms=latency, fallback_used=True, retry_count=retry_count,
        )
        return result

    async def call_with_fallback(
        self,
        prompt: str,
        provider_type: Any,  # ProviderType
        schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Call a specific provider with Gemini as universal fallback.

        *provider_type* must be a ProviderType enum value.
        """
        _, ProviderType = _get_schemas()

        if provider_type == ProviderType.BEDROCK:
            return await self.call_primary(prompt, schema=schema)

        # Direct Gemini call
        start = time.monotonic()
        result = await self._gemini.generate(prompt, schema=schema)
        input_tok, output_tok = self._estimate_tokens(prompt, result)
        latency = int((time.monotonic() - start) * 1000)
        self._record_call(
            agent="direct", operation="call_with_fallback",
            provider="gemini", model="gemini-2.5-flash",
            input_tokens=input_tok, output_tokens=output_tok,
            latency_ms=latency, fallback_used=False, retry_count=0,
        )
        return result

    async def complete(
        self,
        prompt: str,
        model_preference: str = "sonnet",
        agent: str = "unknown",
        operation: str = "complete",
        max_tokens: int = 4096,
        schema: Optional[dict] = None,
    ) -> str:
        """
        Unified completion entry point used by all agents.

        model_preference:
          - "sonnet"  → call_primary (Bedrock Sonnet → Gemini fallback)
          - "haiku"   → call_fast    (Bedrock Haiku  → Gemini fallback)
          - anything else → call_primary
        """
        tiers = _get_tiers()
        if tiers.forced_provider() or (self._economy is not None and tiers.tier_for(operation) == tiers.ECONOMY):
            #: 절약 등급·비교 실행은 등급 라우팅이 있는 call_llm 으로 보낸다.
            try:
                from llm.base import LLMRequest
            except ImportError:  # pragma: no cover
                from core.llm.base import LLMRequest  # type: ignore
            resp = await self.call_llm(
                LLMRequest(prompt=prompt, json_schema=schema, max_tokens=max_tokens, temperature=0.0),
                agent=agent, operation=operation, prefer="fast" if model_preference == "haiku" else "primary",
            )
            return resp.text
        if model_preference == "haiku":
            result = await self.call_fast(prompt, schema=schema)
        else:
            result = await self.call_primary(prompt, schema=schema)

        # Agents expect a string; serialise dict results
        if isinstance(result, str):
            return result
        import json as _json
        return _json.dumps(result, ensure_ascii=False)

    async def call_llm(
        self,
        request: Any,          # llm.base.LLMRequest
        agent: str = "",
        operation: str = "",
        prefer: str = "primary",
    ) -> Any:                  # llm.base.LLMResponse
        """레거시 get_router().call() 경로의 실제 구현 — **라우터는 이제 하나다.**

        code_agent/analyzer 가 쓰던 LLMRequest→LLMResponse 계약을 유지하면서,
        provider 선택·게이트웨이 전환·브레이커·비용 기록을 전부 이 클래스의
        한 벌로 태운다. 보드 이슈 「LLM 라우터가 두 벌」의 본체 수술.

        2.0.36 작업 등급
            절약 등급 작업(task_tier.ECONOMY_OPERATIONS)은 절약 공급자가 정해져 있으면 그쪽으로 먼저
            보낸다. 실패하면(잘린 응답 제외) 품질 공급자로 이어 간다 — 비용 때문에 기능이 멈추지 않게.
            비교 도구가 공급자를 고정한 실행(task_tier.pinned)에서는 다른 공급자로 바꾸지 않는다.
            품질 공급자 아닌 곳·OpenAI 호환 API 로 나가는 문맥은 비밀값을 가린다.
        """
        LLMError, LLMResponse = _get_base_types()
        from .base import LLMErrorType
        tiers = _get_tiers()
        breaker_mod, _ = _get_shared()

        tier = tiers.tier_for(operation)
        forced = tiers.forced_provider()
        if forced:
            pinned = self._spec_provider(forced)
            if pinned is None:
                raise LLMError(f"비교할 AI 공급자를 쓸 수 없습니다({forced}) — 키·주소를 확인하세요.",
                               LLMErrorType.ACCESS_DENIED, False)
            chain = [pinned]
        else:
            base = self._bedrock_haiku if prefer == "fast" else self._bedrock_sonnet
            economy = self._economy if tier == tiers.ECONOMY else None
            chain = [economy, base] if economy is not None and economy is not base else [base]
            #: 2.0.45 역할별 모델 — 팀 구성 창에서 이 역할에 고른 공급자를 먼저 쓴다. 실패하면(키 만료·장애)
            #: 기본 공급자로 이어 간다 — 결과의 model_used 가 실제로 쓴 모델을 보여 준다(몰래 바꾼 것으로 숨기지 않음).
            spec = tiers.role_spec()
            if spec:
                chosen = self._spec_provider(spec)
                if chosen is not None and chosen is not chain[0]:
                    chain = [chosen] + [p for p in chain if p is not chosen]
                elif chosen is None:
                    log.warning("역할 모델 %s 를 쓸 수 없어 기본 공급자를 씁니다(%s)", spec, tiers.current_role())

        schema = getattr(request, "json_schema", None)
        raw = bool(getattr(request, "raw_text", False))
        if raw:
            schema = None
        system = (getattr(request, "system", "") or "").strip()
        full_prompt = f"{system}\n\n{request.prompt}" if system else request.prompt
        code_output = operation not in tiers.ECONOMY_OPERATIONS or operation == "generate_test_draft"

        start = time.monotonic()
        retry = 0
        result: Any = None
        primary_error = None
        provider = chain[0]
        prompt = full_prompt
        masked = 0
        usage: dict = {}

        for provider in chain:
            prompt, masked = self._outgoing(provider, full_prompt, code_output)
            usage = {}
            br = breaker_mod.breaker_for(f"{provider.provider_name}/{provider.model_id}")
            if br.is_open:
                log.info("[CB] open — %s 건너뜀 (call_llm)", provider.model_id)
                retry += 1
                continue
            try:
                messages = [{"role": "user", "content": [{"text": prompt}]}]
                extra: dict = {"raw": True} if raw else {}
                if getattr(provider, "reports_usage", False):
                    extra["usage"] = usage
                result = await provider.converse(
                    messages, output_schema=schema,
                    max_tokens=request.max_tokens, temperature=request.temperature, **extra,
                )
                br.record_success()
                break
            except Exception as exc:
                # A clipped model response is a generation failure, not a provider
                # outage. Let the caller repair its output without switching models.
                if isinstance(exc, LLMError) and exc.error_type == LLMErrorType.STRUCTURED_OUTPUT:
                    br.record_success()
                    self._record_call(
                        agent=agent or "router", operation=operation or "call_llm",
                        provider=provider.provider_name, model=provider.model_id,
                        input_tokens=max(1, len(prompt) // 4), output_tokens=request.max_tokens,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        fallback_used=False, retry_count=retry, tier=tier, masked=masked,
                    )
                    raise
                br.record_failure()
                retry += 1
                primary_error = exc
                log.warning("call_llm %s/%s 실패: %s", provider.provider_name, provider.model_id, exc)

        fallback = result is None or provider is not chain[0]
        used_provider, used_model = provider.provider_name, provider.model_id
        if result is None:
            if forced:
                #: 비교 실험의 조건을 몰래 바꾸지 않는다 — 실패는 실패로 남긴다.
                if isinstance(primary_error, LLMError):
                    raise primary_error
                raise LLMError(f"{forced} 공급자 호출 실패: {primary_error or '연속 실패로 잠시 대기 중'}",
                               LLMErrorType.SERVICE_ERROR, True, raw=primary_error)
            used_provider, used_model = "gemini", "gemini-2.5-flash"
            prompt, masked = self._outgoing(self._gemini, full_prompt, code_output)
            usage = {}
            result = await self._fallback(
                prompt, schema, primary_error, max_tokens=request.max_tokens,
                temperature=request.temperature, **({"raw": True} if raw else {}),
            )

        latency = int((time.monotonic() - start) * 1000)
        if usage.get("input_tokens") or usage.get("output_tokens"):
            input_tok, output_tok, source = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), "api"
        else:
            (input_tok, output_tok), source = self._estimate_tokens(prompt, result), "estimate"
        record = self._record_call(
            agent=agent or "router", operation=operation or "call_llm",
            provider=used_provider, model=used_model,
            input_tokens=input_tok, output_tokens=output_tok,
            latency_ms=latency, fallback_used=fallback, retry_count=retry,
            tier=tier, token_source=source, masked=masked,
        )

        #: 레거시 호출자는 .text 에서 JSON 을 뽑는다. converse 는 파싱된 dict 를
        #: 주므로 되돌려 직렬화한다. {"text": ...} 한 장짜리는 원문 그대로.
        truncated = None
        if raw and isinstance(result, dict):
            text = str(result.get("text") or "")
            truncated = result.get("truncated")
        elif isinstance(result, dict) and len(result) == 1 and ("text" in result or "raw_response" in result):
            text = str(result.get("text", result.get("raw_response", "")))
        else:
            import json as _json
            text = _json.dumps(result, ensure_ascii=False)

        resp = LLMResponse(
            text=text,
            parsed=result if isinstance(result, dict) else None,
            model_used=used_model,
            provider=used_provider,
            latency_ms=latency,
            fallback_used=fallback,
            retry_count=retry,
            input_tokens=input_tok,
            output_tokens=output_tok,
            token_source="api" if source == "api" else "local_estimate",
        )
        if truncated is not None:
            resp.metadata["truncated"] = bool(truncated)
        resp.metadata["llm_call_record"] = {
            "call_id": record.call_id,
            "estimated_cost_usd": record.estimated_cost_usd,
            "provider": used_provider,
            "model": used_model,
            "tier": tier,
        }
        return resp

    # ------------------------------------------------------------------
    # 2.0.36 작업 등급 — 공급자 고르기·비밀값 가리기·상태
    # ------------------------------------------------------------------

    def _outgoing(self, provider: Any, prompt: str, code_output: bool) -> tuple[str, int]:
        """품질 공급자가 아닌 곳·OpenAI 호환 API 로 나가는 글은 비밀값을 가린다."""
        name = getattr(provider, "provider_name", "")
        if name != "compatible" and name == self._bedrock_sonnet.provider_name:
            return prompt, 0
        if name == "gemini" and not getattr(provider, "available", True):
            return prompt, 0
        try:
            from .scrub import scrub_for_external
        except ImportError:  # pragma: no cover
            from llm.scrub import scrub_for_external  # type: ignore
        return scrub_for_external(prompt, code=code_output)

    def _spec_provider(self, spec: str, *, economy: bool = False) -> Any:
        """공급자 이름 → provider. 쓸 수 없으면 None.

        quality(지금 품질 공급자) · fast(그 빠른 모델) · economy(지금 절약 설정) ·
        anthropic/openai/compatible(키가 있는 API 공급자).
        """
        spec = (spec or "").strip().lower()
        if spec in ("", "quality", "same"):
            return self._bedrock_sonnet
        if spec == "fast":
            return self._bedrock_haiku
        if spec == "economy":
            return self._economy or self._bedrock_sonnet
        key = (spec, economy)
        if key in self._extra:
            return self._extra[key]
        try:
            from .api_key_provider import ApiKeyProvider, model_for, provider_ready
        except ImportError:  # pragma: no cover
            from llm.api_key_provider import ApiKeyProvider, model_for, provider_ready  # type: ignore
        import os
        if not provider_ready(spec):
            return None
        model = (os.environ.get("RECODER_ECONOMY_MODEL") or "").strip() if economy else ""
        try:
            made = ApiKeyProvider(spec, model or model_for(spec, fast=economy))
        except ValueError as exc:
            log.warning("AI 공급자 %s 를 만들 수 없습니다: %s", spec, exc)
            return None
        self._extra[key] = made
        return made

    def _build_economy(self) -> Any:
        import os
        spec = (os.environ.get("RECODER_ECONOMY_PROVIDER") or "").strip().lower()
        self._economy_spec = spec
        self._economy_note = ""
        if spec in ("", "quality", "same"):
            return None
        if spec not in ("fast", "anthropic", "openai", "compatible"):
            self._economy_note = f"알 수 없는 절약 공급자({spec}) — 품질 공급자를 씁니다."
            return None
        provider = self._spec_provider(spec, economy=True)
        if provider is None:
            self._economy_note = "절약 공급자의 키(호환 API 는 주소까지)가 없어 품질 공급자를 씁니다."
            log.warning("economy provider %s 비활성: 키 없음", spec)
            return None
        log.info("economy tier → %s/%s", provider.provider_name, provider.model_id)
        return provider

    def routing_status(self) -> dict:
        """확장 설정 화면이 보여 줄 등급별 공급자(키 값은 넣지 않는다)."""
        tiers = _get_tiers()
        quality = self._bedrock_sonnet
        economy = self._economy or quality
        separate = self._economy is not None and self._economy is not quality
        choices = []
        for spec, label in (("quality", "지금 품질 공급자"), ("fast", "품질 공급자의 빠른 모델"),
                            ("anthropic", "Claude API"), ("openai", "OpenAI API"), ("compatible", "OpenAI 호환 API")):
            p = self._spec_provider(spec)
            choices.append({"spec": spec, "label": label, "ready": p is not None,
                            "provider": getattr(p, "provider_name", ""), "model": getattr(p, "model_id", "")})
        return {
            "quality": {"provider": quality.provider_name, "model": quality.model_id},
            "economy": {"spec": self._economy_spec or "quality", "provider": economy.provider_name,
                        "model": economy.model_id, "separate": separate,
                        "scrubbed": separate and (economy.provider_name != quality.provider_name
                                                  or economy.provider_name == "compatible"),
                        "note": self._economy_note},
            "economy_operations": sorted(tiers.ECONOMY_OPERATIONS),
            "choices": choices,
        }

    @property
    def call_records(self) -> list[Any]:
        """Return a copy of all recorded LLMCallRecord entries."""
        return list(self._call_records)

    def repair_profile(self) -> dict:
        """Include actual tier identities and configured prices in repair cache keys."""
        import os
        return {"fast": self._bedrock_haiku.model_id,
                "primary": self._bedrock_sonnet.model_id,
                "provider": self._bedrock_sonnet.provider_name,
                "prices": os.getenv("RECODER_REPAIR_PRICES", "{}")}

    async def call_repair(self, request: Any, *, tier: str, run_id: str) -> Any:
        """Measured single-tier call. Never silently switch experimental conditions.

        Providers return their native usage when available. Unknown prices/failed
        usage remain null; they must not become zero-dollar experimental results.
        Generation calls are measured here; SDK transport retries are not counted.
        """
        import json
        import math
        import os
        from dataclasses import replace
        from .base import LLMError, LLMErrorType

        if tier not in {"fast", "primary"}:
            raise ValueError("Unknown repair tier")
        provider = self._bedrock_haiku if tier == "fast" else self._bedrock_sonnet
        prices = json.loads(os.getenv("RECODER_REPAIR_PRICES", "{}"))
        if not isinstance(prices, dict):
            raise ValueError("Repair prices must be a model-to-rates object")
        for rate in prices.values():
            for direction in ("input", "output"):
                price = float(rate[direction])
                if not math.isfinite(price) or price < 0:
                    raise ValueError("Repair model prices must be finite and non-negative")
        breaker_mod, ledger = _get_shared()
        br = breaker_mod.breaker_for(f"{provider.provider_name}/{provider.model_id}")
        if br.is_open:
            raise LLMError("Model circuit breaker is open", LLMErrorType.SERVICE_ERROR)
        # A schema-capability fallback can make multiple model calls. Use one
        # JSON text generation and let the pipeline validate its schema instead.
        prompt = request.prompt + "\nReturn JSON matching this schema:\n" + json.dumps(request.json_schema)
        plain = replace(request, prompt=prompt, json_schema=None)
        entry = {"call_id": str(uuid.uuid4()), "run_id": run_id, "agent": "grounded_repair",
                 "operation": "repair_" + tier, "provider": provider.provider_name,
                 "model": provider.model_id, "input_tokens": None, "output_tokens": None,
                 "token_source": "unknown", "estimated_cost_usd": None,
                 "fallback_used": False, "transport_retries": "not_measured", "tier": "quality"}
        if _get_tiers().run_tag():
            entry["run"] = _get_tiers().run_tag()
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(getattr(provider, "call_measured", provider.call), plain)
            br.record_success()
            entry.update(input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                         token_source=response.token_source, model=response.model_used, status="succeeded")
            entry["transport_retries"] = response.metadata.get("transport_retries", "not_measured")
            rate = prices.get(response.model_used)
            if rate is not None:
                incoming, outgoing = float(rate["input"]), float(rate["output"])
                if incoming < 0 or outgoing < 0:
                    raise ValueError("Repair model prices cannot be negative")
                entry["estimated_cost_usd"] = (response.input_tokens * incoming + response.output_tokens * outgoing) / 1_000_000
                entry["pricing_source"] = "configured_usd_per_million"
            else:
                entry["pricing_source"] = "unconfigured"
            return response
        except Exception as exc:
            br.record_failure()
            error = exc if isinstance(exc, LLMError) else LLMError(str(exc), raw=exc)
            entry["status"] = "failed"
            error.llm_call_record = entry
            raise error
        finally:
            entry["latency_ms"] = round((time.monotonic() - started) * 1000)
            ledger.record(entry)
            if "response" in locals():
                response.metadata["llm_call_record"] = entry

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_call(
        self,
        agent: str,
        operation: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        fallback_used: bool,
        retry_count: int,
        tier: str = "quality",
        token_source: str = "estimate",
        masked: int = 0,
    ) -> Any:
        """Create and store an LLMCallRecord for cost/telemetry tracking."""
        LLMCallRecord, ProviderType = _get_schemas()
        pricing = _get_pricing()

        known, pricing_source = pricing.cost_usd(provider, model, input_tokens, output_tokens)
        cost = known if known is not None else 0.0
        quality_eq = None
        if tier == "economy":
            quality_eq, _ = pricing.cost_usd(self._bedrock_sonnet.provider_name, self._bedrock_sonnet.model_id,
                                             input_tokens, output_tokens)

        provider_enum = {
            "bedrock": ProviderType.BEDROCK, "gateway": ProviderType.BEDROCK,
            "openai": ProviderType.OPENAI, "compatible": ProviderType.OPENAI,
            "anthropic": ProviderType.ANTHROPIC,
        }.get(provider, ProviderType.ANTHROPIC)

        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            agent=agent,
            operation=operation,
            provider=provider_enum,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            latency_ms=int(latency_ms),
            fallback_used=fallback_used,
            retry_count=retry_count,
            timestamp=datetime.utcnow(),
        )
        self._call_records.append(record)

        #: 비용 집계는 **한 곳** — 어느 진입점으로 불렀든 원장에 남는다.
        _, ledger = _get_shared()
        entry = {
            "call_id": record.call_id,
            "agent": agent,
            "operation": operation,
            "tier": tier,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "token_source": token_source,
            "latency_ms": int(latency_ms),
            "fallback_used": fallback_used,
            "retry_count": retry_count,
            #: 단가를 모르면 None — 0 달러로 적지 않는다(비교 도구가 "단가 미설정"으로 보여 준다).
            "estimated_cost_usd": known,
            "pricing_source": pricing_source,
            "quality_equivalent_usd": quality_eq,
            "masked": masked,
        }
        run = _get_tiers().run_tag()
        if run:
            entry["run"] = run
        ledger.record(entry)
        return record

    @staticmethod
    def _estimate_cost(
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """USD 비용 어림(호환용). 단가 규칙은 llm/pricing.py 한 곳 — 모르는 단가는 0."""
        cost, _ = _get_pricing().cost_usd(provider, model, input_tokens, output_tokens)
        return cost or 0.0

    @staticmethod
    def _estimate_tokens(prompt: str, result: Any) -> tuple[int, int]:
        """
        Rough token estimate: 1 token ~ 4 characters.

        Used when the API response does not include usage metadata.
        """
        input_chars = len(prompt)
        if isinstance(result, dict):
            output_chars = len(str(result))
        else:
            output_chars = len(str(result)) if result else 0
        return max(1, input_chars // 4), max(1, output_chars // 4)


# ---------------------------------------------------------------------------
# 싱글턴 — 레거시 라우터(llm/router.py)와 라우트들이 같은 인스턴스를 쓴다.
# 브레이커·원장은 어차피 전역이지만, 게이트웨이 전환 로그가 한 번만 찍히고
# call_records 프로퍼티도 한 곳에 모이는 이점이 있다.
# ---------------------------------------------------------------------------
import threading as _threading

_singleton: LLMProviderRouter | None = None
_singleton_lock = _threading.Lock()


def get_provider_router(force_rebuild: bool = False) -> LLMProviderRouter:
    global _singleton
    if _singleton is not None and not force_rebuild:
        return _singleton
    with _singleton_lock:
        if _singleton is not None and not force_rebuild:
            return _singleton
        _singleton = LLMProviderRouter()
        return _singleton
