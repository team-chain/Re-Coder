"""
ReCoder Core — LLM Provider Router

Routing strategy
----------------
Primary   : Bedrock Claude Sonnet  — code patches, Dockerfile, complex ops analysis
Fast      : Bedrock Claude Haiku   — error classification, log summary, scanner results
Fallback  : Gemini Flash           — Bedrock failure or cost-reduction mode

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

        self._call_records: list[Any] = []  # list[LLMCallRecord]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
                        provider="bedrock", model=self._bedrock_sonnet.model_id,
                        input_tokens=input_tok, output_tokens=output_tok,
                        latency_ms=latency, fallback_used=False, retry_count=0,
                    )
                    return result

                except Exception as exc:
                    br.record_failure()
                    log.warning("Bedrock Sonnet failed: %s — trying Gemini fallback", exc)
                    retry_count = 1

        # Gemini fallback (별도 span)
        with _instrument_llm_span(
            provider="gemini",
            model="gemini-2.5-flash",
            operation="call_primary_fallback",
        ) as span:
            result = await self._gemini.generate(prompt, schema=schema)
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
                    provider="bedrock", model=self._bedrock_haiku.model_id,
                    input_tokens=input_tok, output_tokens=output_tok,
                    latency_ms=latency, fallback_used=False, retry_count=0,
                )
                return result

            except Exception as exc:
                br.record_failure()
                log.warning("Bedrock Haiku failed: %s — trying Gemini fallback", exc)
                retry_count = 1

        result = await self._gemini.generate(prompt, schema=schema)
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
        """
        LLMError, LLMResponse = _get_base_types()
        breaker_mod, _ = _get_shared()

        provider = self._bedrock_haiku if prefer == "fast" else self._bedrock_sonnet
        schema = getattr(request, "json_schema", None)
        system = (getattr(request, "system", "") or "").strip()
        prompt = f"{system}\n\n{request.prompt}" if system else request.prompt

        start = time.monotonic()
        retry = 0
        fallback = False
        result: Any = None
        used_provider = provider.provider_name
        used_model = provider.model_id

        br = breaker_mod.breaker_for(f"{provider.provider_name}/{provider.model_id}")
        if br.is_open:
            log.info("[CB] open — %s 건너뜀 (call_llm)", provider.model_id)
            retry = 1
        else:
            try:
                messages = [{"role": "user", "content": [{"text": prompt}]}]
                result = await provider.converse(messages, output_schema=schema)
                br.record_success()
            except Exception as exc:
                br.record_failure()
                retry = 1
                log.warning("call_llm %s 실패: %s — Gemini 폴백", used_model, exc)

        if result is None:
            fallback = True
            used_provider, used_model = "gemini", "gemini-2.5-flash"
            try:
                result = await self._gemini.generate(prompt, schema=schema)
            except Exception as exc:
                #: 전부 실패 — 레거시 계약대로 LLMError 를 던진다.
                raise LLMError(f"모든 LLM Provider 실패: {exc}", retryable=True, raw=exc) from exc

        latency = int((time.monotonic() - start) * 1000)
        input_tok, output_tok = self._estimate_tokens(prompt, result)
        record = self._record_call(
            agent=agent or "router", operation=operation or "call_llm",
            provider="bedrock" if used_provider != "gemini" else "gemini",
            model=used_model,
            input_tokens=input_tok, output_tokens=output_tok,
            latency_ms=latency, fallback_used=fallback, retry_count=retry,
        )

        #: 레거시 호출자는 .text 에서 JSON 을 뽑는다. converse 는 파싱된 dict 를
        #: 주므로 되돌려 직렬화한다. {"text": ...} 한 장짜리는 원문 그대로.
        if isinstance(result, dict) and set(result.keys()) == {"text"}:
            text = str(result["text"])
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
        )
        resp.metadata["llm_call_record"] = {
            "call_id": record.call_id,
            "estimated_cost_usd": record.estimated_cost_usd,
            "provider": used_provider,
            "model": used_model,
        }
        return resp

    @property
    def call_records(self) -> list[Any]:
        """Return a copy of all recorded LLMCallRecord entries."""
        return list(self._call_records)

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
    ) -> Any:
        """Create and store an LLMCallRecord for cost/telemetry tracking."""
        LLMCallRecord, ProviderType = _get_schemas()

        cost = self._estimate_cost(provider, model, input_tokens, output_tokens)

        try:
            provider_enum = ProviderType(provider)
        except ValueError:
            provider_enum = ProviderType.ANTHROPIC

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
        ledger.record({
            "call_id": record.call_id,
            "agent": agent,
            "operation": operation,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": int(latency_ms),
            "fallback_used": fallback_used,
            "retry_count": retry_count,
            "estimated_cost_usd": cost,
        })
        return record

    @staticmethod
    def _estimate_cost(
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Estimate USD cost for the given provider/model/token counts.

        Falls back to Sonnet pricing when the model is not in the table.
        """
        try:
            from llm.bedrock_provider import COST_PER_1K_TOKENS as BEDROCK_COSTS
        except ImportError:
            try:
                from core.llm.bedrock_provider import COST_PER_1K_TOKENS as BEDROCK_COSTS
            except ImportError:
                BEDROCK_COSTS = {}

        if provider == "gemini":
            return 0.0

        rates = BEDROCK_COSTS.get(model, {"input": 0.003, "output": 0.015})
        return (
            (input_tokens / 1000.0) * rates["input"]
            + (output_tokens / 1000.0) * rates["output"]
        )

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
