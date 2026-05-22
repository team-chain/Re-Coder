"""
LLM Router (설계서 v5.7 §3.1 · §3.6).

Provider 선택 및 폴백 체인 관리.
폴백 순서: Bedrock Primary -> Secondary -> Fast -> Gemini -> WAITING_USER_ACTION

각 Agent는 이 모듈의 get_router().call(request) 만 사용한다.
LLMResponse.metadata["llm_call_record"] 에 LLMCallRecord(dict) 를 포함시켜 반환한다.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from .base import LLMError, LLMErrorType, LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

# -- Circuit Breaker 설정 --------------------------------------------------
CB_FAILURE_THRESHOLD = int(os.getenv("LLM_CB_THRESHOLD", "3"))
CB_RESET_SECONDS     = int(os.getenv("LLM_CB_RESET_SEC", "60"))


class _CircuitBreaker:
    """개별 Provider 모델의 Circuit Breaker."""

    def __init__(self, name: str):
        self.name       = name
        self._failures  = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at > CB_RESET_SECONDS:
                self._failures  = 0
                self._opened_at = None
                return False
            return True

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= CB_FAILURE_THRESHOLD:
                self._opened_at = time.monotonic()
                logger.warning("[CircuitBreaker] %s OPEN (failures=%d)", self.name, self._failures)

    def record_success(self) -> None:
        with self._lock:
            self._failures  = 0
            self._opened_at = None


# -- 비용 추정 (설계서 §3.8 estimated_cost_usd) ----------------------------
# 가격: Bedrock On-Demand 기준 (1M 토큰당 USD). 실제 청구액과 다를 수 있음.
# 참고: https://aws.amazon.com/bedrock/pricing/
_PRICING_TABLE: dict[str, tuple[float, float]] = {
    # Claude 4 시리즈
    "claude-sonnet-4-5": (3.0,   15.0),
    "claude-sonnet-4":   (3.0,   15.0),
    "claude-haiku-4-5":  (0.8,    4.0),
    "claude-haiku-4":    (0.8,    4.0),
    # Claude 3.x 시리즈 (MVP 기본값)
    "claude-3-5-sonnet": (3.0,   15.0),
    "claude-3-5-haiku":  (0.8,    4.0),
    "claude-3-sonnet":   (3.0,   15.0),
    "claude-3-haiku":    (0.25,   1.25),   # 가장 저렴 — MVP 분석용 기본
    "claude-3-opus":     (15.0,  75.0),
    # Gemini (fallback)
    "gemini-flash":      (0.075,  0.30),
}


def _estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """모델 ID 접두사 매칭으로 추정 비용(USD)을 계산한다."""
    ml = model_id.lower()
    for key, (inp, out) in _PRICING_TABLE.items():
        if key in ml:
            return round(inp * input_tokens / 1_000_000 + out * output_tokens / 1_000_000, 8)
    return round(3.0 * input_tokens / 1_000_000 + 15.0 * output_tokens / 1_000_000, 8)


def _make_call_record(
    call_id: str,
    agent: str,
    operation: str,
    provider_name: str,
    model_id: str,
    region: str,
    input_tokens: int,
    output_tokens: int,
    token_source: str,
    latency_ms: int,
    status: str,
    fallback_used: bool,
    retry_count: int,
    error_type: str | None,
) -> dict:
    return {
        "call_id":            call_id,
        "agent":              agent,
        "operation":          operation,
        "provider":           provider_name,
        "model_identifier":   model_id,
        "region":             region,
        "input_tokens":       input_tokens,
        "output_tokens":      output_tokens,
        "total_tokens":       input_tokens + output_tokens,
        "token_source":       token_source,
        "estimated_cost_usd": _estimate_cost(model_id, input_tokens, output_tokens),
        "latency_ms":         latency_ms,
        "status":             status,
        "fallback_used":      fallback_used,
        "retry_count":        retry_count,
        "error_type":         error_type,
    }


class LLMRouter:
    """
    폴백 체인을 관리하는 Router (설계서 §3.6).

    circuit breaker 가 열린 provider 는 건너뛴다.
    """

    def __init__(self, chain: list[LLMProvider]):
        self._chain    = chain
        self._breakers = {
            p.provider_name + "/" + getattr(p, "_model_id", p.provider_name):
            _CircuitBreaker(p.provider_name + "/" + getattr(p, "_model_id", p.provider_name))
            for p in chain
        }

    def call(self, request: LLMRequest, agent: str = "", operation: str = "") -> LLMResponse:
        """폴백 체인을 순서대로 시도. 전체 실패 시 LLMError raise.

        성공 시 response.metadata["llm_call_record"] 에 §3.8 비용 추적 dict 를 포함한다.
        agent / operation 은 호출 측 식별자 (예: "context_analyzer", "analyze_context").
        """
        last_err:     LLMError | None = None
        call_id       = uuid.uuid4().hex
        retry_count   = 0
        fallback_used = False
        first_key     = ""

        for idx, provider in enumerate(self._chain):
            key = provider.provider_name + "/" + getattr(provider, "_model_id", provider.provider_name)
            cb  = self._breakers.get(key)

            if idx == 0:
                first_key = key
            elif key != first_key:
                fallback_used = True

            if cb and cb.is_open:
                logger.info("[Router] CB open -- skip %s", key)
                retry_count += 1
                continue

            t0 = time.monotonic()
            try:
                response   = provider.call(request)
                latency_ms = int((time.monotonic() - t0) * 1000)
                if cb:
                    cb.record_success()
                logger.debug("[Router] success via %s (%dms)", key, latency_ms)

                # §3.8 LLM 비용 추적 레코드를 metadata 에 포함
                response.latency_ms    = latency_ms
                response.fallback_used = fallback_used
                response.retry_count   = retry_count
                _agent     = agent     or request.metadata.get("agent",     "")
                _operation = operation or request.metadata.get("operation", "")
                response.metadata["llm_call_record"] = _make_call_record(
                    call_id       = call_id,
                    agent         = _agent,
                    operation     = _operation,
                    provider_name = response.provider or provider.provider_name,
                    model_id      = response.model_used,
                    region        = response.region,
                    input_tokens  = response.input_tokens,
                    output_tokens = response.output_tokens,
                    token_source  = response.token_source,
                    latency_ms    = latency_ms,
                    status        = "success",
                    fallback_used = fallback_used,
                    retry_count   = retry_count,
                    error_type    = None,
                )
                return response

            except LLMError as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if cb:
                    cb.record_failure()
                logger.warning("[Router] %s failed: %s (%s)", key, e, e.error_type)
                last_err    = e
                retry_count += 1
                if provider.is_retryable_error(e):
                    continue
                # 재시도 불가 오류 (validation, context too long) -- 즉시 실패
                e.llm_call_record = _make_call_record(  # type: ignore[attr-defined]
                    call_id       = call_id,
                    agent         = agent or request.metadata.get("agent", ""),
                    operation     = operation or request.metadata.get("operation", ""),
                    provider_name = provider.provider_name,
                    model_id      = getattr(provider, "_model_id", ""),
                    region        = getattr(provider, "_region", ""),
                    input_tokens  = 0,
                    output_tokens = 0,
                    token_source  = "local_estimate",
                    latency_ms    = latency_ms,
                    status        = "failed",
                    fallback_used = fallback_used,
                    retry_count   = retry_count,
                    error_type    = e.error_type.value,
                )
                raise

            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if cb:
                    cb.record_failure()
                logger.warning("[Router] %s unexpected error: %s", key, e)
                last_err    = LLMError(str(e), LLMErrorType.UNKNOWN, retryable=True, raw=e)
                retry_count += 1

        # 전체 폴백 실패
        final_err = last_err or LLMError(
            "모든 LLM Provider 폴백 실패 -- WAITING_USER_ACTION 으로 복귀",
            LLMErrorType.UNKNOWN,
        )
        final_err.llm_call_record = _make_call_record(  # type: ignore[attr-defined]
            call_id       = call_id,
            agent         = agent,
            operation     = operation,
            provider_name = "",
            model_id      = "",
            region        = "",
            input_tokens  = 0,
            output_tokens = 0,
            token_source  = "local_estimate",
            latency_ms    = 0,
            status        = "failed",
            fallback_used = fallback_used,
            retry_count   = retry_count,
            error_type    = final_err.error_type.value,
        )
        raise final_err


# -- 싱글턴 router 인스턴스 ------------------------------------------------
_router_instance: LLMRouter | None = None
_router_lock = threading.Lock()


def _build_default_chain() -> list[LLMProvider]:
    """
    설계서 §3.6 기본 폴백 체인.
    Primary(Sonnet) -> Secondary(Sonnet 구버전) -> Fast(Haiku) -> Gemini
    Bedrock 자격증명이 없으면 Bedrock 항목을 건너뛰고 Gemini 로 직행.
    """
    providers: list[LLMProvider] = []

    if _bedrock_available():
        from .bedrock_provider import make_fast, make_primary, make_secondary
        providers.extend([make_primary(), make_secondary(), make_fast()])

    if os.getenv("GEMINI_API_KEY", "").strip():
        from .gemini_provider import GeminiProvider
        providers.append(GeminiProvider())

    if not providers:
        logger.warning(
            "[Router] 사용 가능한 LLM Provider 없음. "
            "AWS 자격증명 또는 GEMINI_API_KEY 를 설정하세요."
        )

    return providers


def _bedrock_available() -> bool:
    """boto3 존재 + AWS 자격증명이 설정되어 있는지 확인."""
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False

    has_profile = bool(os.getenv("AWS_PROFILE", "").strip())
    has_key     = bool(os.getenv("AWS_ACCESS_KEY_ID", "").strip())
    has_sso     = bool(os.getenv("AWS_SSO_START_URL", "").strip())

    from pathlib import Path
    aws_creds  = Path.home() / ".aws" / "credentials"
    aws_config = Path.home() / ".aws" / "config"

    return has_profile or has_key or has_sso or aws_creds.exists() or aws_config.exists()


def get_router(force_rebuild: bool = False) -> LLMRouter:
    """싱글턴 Router 반환. 환경변수 변경 시 force_rebuild=True 로 재생성."""
    global _router_instance
    if _router_instance is not None and not force_rebuild:
        return _router_instance
    with _router_lock:
        if _router_instance is not None and not force_rebuild:
            return _router_instance
        _router_instance = LLMRouter(_build_default_chain())
        return _router_instance
