"""
ReCoder Core — Google Gemini Provider (설계서 v6.4-final §5.1 보조 fallback).

- Bedrock 전체 체인 실패 시 최후 수단으로 사용 (cost-free / 저비용 폴백).
- GEMINI_API_KEY 환경변수 필수.
- 모델 폴백 체인: gemini-2.5-flash-lite → gemini-2.5-flash → gemini-2.0-flash-lite
  → gemini-1.5-flash-8b → gemini-1.5-flash
- 두 가지 호출 경로를 모두 제공한다:
    1) 동기 ``call(LLMRequest) -> LLMResponse`` — router/LLMProvider 인터페이스
       (``google.genai`` 신 SDK 사용, 모델 폴백 체인 + structured output 지원)
    2) 비동기 ``generate(prompt, schema)`` — legacy / asyncio 호출자용
       (``google.generativeai`` 구 SDK 사용, thread-pool executor 경유)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Optional

from .base import LLMError, LLMErrorType, LLMProvider, LLMRequest, LLMResponse

log = logging.getLogger(__name__)
logger = log  # alias for legacy callers

# ---------------------------------------------------------------------------
# 상수 / 환경설정
# ---------------------------------------------------------------------------

# Legacy 비동기 경로용 단일 모델 (google-generativeai SDK)
MODEL = "gemini-2.5-flash"
API_KEY_ENV = "GEMINI_API_KEY"

# Free-tier pricing (0.0 USD) — 비용 추정용 (설계서 §13)
COST_PER_1K_TOKENS: dict[str, float] = {"input": 0.0, "output": 0.0}

# 동기 경로용 모델 폴백 체인 (google.genai 신 SDK)
_DEFAULT_FALLBACK_CHAIN: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
]

# 신 SDK 클라이언트 캐시 (thread-safe)
_client_lock = threading.Lock()
_client_cache: dict[str, object] = {}


# ---------------------------------------------------------------------------
# 신 SDK (google.genai) helper
# ---------------------------------------------------------------------------

def _get_client(api_key: str):
    """google.genai 클라이언트를 캐시하여 반환."""
    if api_key in _client_cache:
        return _client_cache[api_key]
    with _client_lock:
        if api_key in _client_cache:
            return _client_cache[api_key]
        from google import genai
        client = genai.Client(api_key=api_key)
        _client_cache[api_key] = client
        return client


def _resolve_chain() -> list[str]:
    """환경변수를 반영한 모델 폴백 체인 해석."""
    explicit = os.getenv("GEMINI_MODEL_FALLBACKS", "").strip()
    if explicit:
        chain = [m.strip() for m in explicit.split(",") if m.strip()]
        if chain:
            return chain
    primary = os.getenv("GEMINI_MODEL", "").strip()
    if primary:
        return [primary] + [m for m in _DEFAULT_FALLBACK_CHAIN if m != primary]
    return list(_DEFAULT_FALLBACK_CHAIN)


def _is_model_unavailable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (404, 400):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("not found", "unsupported", "invalid model", "404"))


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None)
    if resp and getattr(resp, "status_code", None) == 429:
        return True
    name = type(exc).__name__.lower()
    return "toomanyrequests" in name or "ratelimit" in name


# ---------------------------------------------------------------------------
# GeminiProvider
# ---------------------------------------------------------------------------


class GeminiProvider(LLMProvider):
    """
    Google Gemini API Provider (설계서 §5.1 보조 fallback).

    동기 진입점 ``call(LLMRequest)`` 는 신 SDK(``google.genai``)와 모델
    폴백 체인을 사용하고, 비동기 진입점 ``generate(prompt, schema)`` 는
    구 SDK(``google.generativeai``)를 사용한다.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        # 비동기 경로용 구 SDK 초기화
        self._api_key = api_key or os.environ.get(API_KEY_ENV, "")
        self._genai_legacy = None
        self._model = None
        self._initialise_legacy()

    # ------------------------------------------------------------------
    # LLMProvider 인터페이스
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "gemini"

    def call(self, request: LLMRequest) -> LLMResponse:
        """동기 호출 경로 — google.genai 신 SDK + 모델 폴백 체인."""
        api_key = (self._api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY가 설정되지 않았습니다.",
                LLMErrorType.ACCESS_DENIED,
                retryable=False,
            )

        from google.genai import types

        client = _get_client(api_key)
        chain = _resolve_chain()

        # 컨텐츠 구성 (멀티모달 image 지원)
        contents: list = []
        if request.image_bytes:
            contents.append(
                types.Part.from_bytes(data=request.image_bytes, mime_type=request.image_mime)
            )
        contents.append(request.prompt)

        config_kwargs: dict = {
            "system_instruction": request.system or None,
        }
        if request.json_schema:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_json_schema"] = request.json_schema

        last_exc: Exception | None = None
        for idx, model_name in enumerate(chain):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        **{k: v for k, v in config_kwargs.items() if v is not None}
                    ),
                )
                raw_text = resp.text or ""
                parsed: dict | None = None
                if request.json_schema:
                    try:
                        parsed = json.loads(raw_text)
                    except Exception:
                        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
                        if m:
                            try:
                                parsed = json.loads(m.group())
                            except Exception:
                                parsed = None

                if idx > 0:
                    logger.info("[GeminiProvider] fallback model used: %s", model_name)

                in_tokens = max(1, len(request.prompt) // 4)
                out_tokens = max(1, len(raw_text) // 4)

                return LLMResponse(
                    text          = raw_text,
                    parsed        = parsed,
                    model_used    = model_name,
                    provider      = "gemini",
                    input_tokens  = in_tokens,
                    output_tokens = out_tokens,
                    token_source  = "estimate",
                )
            except Exception as exc:
                last_exc = exc
                if _is_model_unavailable(exc) or _is_rate_limit(exc):
                    logger.warning("[GeminiProvider] model %s unavailable: %s", model_name, exc)
                    continue
                raise LLMError(str(exc), LLMErrorType.UNKNOWN, raw=exc) from exc

        raise LLMError(
            f"Gemini 전체 폴백 체인 실패: {last_exc}",
            LLMErrorType.THROTTLING,
            retryable=True,
            raw=last_exc,
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """무료 티어 — 0.0 USD 반환 (설계서 §13)."""
        return (input_tokens / 1000.0) * COST_PER_1K_TOKENS["input"] + \
               (output_tokens / 1000.0) * COST_PER_1K_TOKENS["output"]

    # ------------------------------------------------------------------
    # 비동기 진입점 (legacy / asyncio 호출자용)
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Gemini Flash 비동기 호출 (legacy google-generativeai SDK).

        *schema* 가 제공되면 모델이 schema 에 맞는 JSON 을 반환하도록 지시한다.
        응답 텍스트를 파싱하여 dict 로 반환한다.
        """
        import asyncio

        if self._model is None:
            raise RuntimeError(
                "Gemini SDK not initialised. "
                f"Set the {API_KEY_ENV!r} environment variable and install "
                "google-generativeai."
            )

        full_prompt = self._build_prompt(prompt, schema)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._model.generate_content(full_prompt)
        )
        text = response.text if hasattr(response, "text") else str(response)
        return self._parse_json(text)

    async def validate_access(self) -> bool:
        """Gemini API 키가 존재하고 동작하는지 확인."""
        if not self._api_key or self._model is None:
            return False
        try:
            await self.generate("ping")
            return True
        except Exception as exc:
            log.warning("Gemini access validation failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _initialise_legacy(self) -> None:
        """비동기 경로용 google.generativeai 구 SDK 초기화 (실패해도 무해)."""
        if not self._api_key:
            log.debug("GEMINI_API_KEY not set — GeminiProvider legacy async path disabled")
            return
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=self._api_key)
            self._genai_legacy = genai
            self._model = genai.GenerativeModel(MODEL)
        except ImportError:
            log.warning(
                "google-generativeai package not installed. "
                "Run: pip install google-generativeai"
            )
        except Exception as exc:
            log.error("Failed to initialise Gemini legacy client: %s", exc)

    @staticmethod
    def _build_prompt(prompt: str, schema: Optional[dict]) -> str:
        """Prepend JSON instruction when a schema is requested."""
        if schema is None:
            return prompt
        schema_str = json.dumps(schema, indent=2)
        return (
            "You must respond with valid JSON only — no markdown, no explanation.\n"
            f"Your response must conform to this JSON schema:\n{schema_str}\n\n"
            f"{prompt}"
        )

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """
        Extract and parse JSON from the model response.

        Tries:
        1. Fenced ```json ... ``` block
        2. First {...} block
        3. Raw parse
        """
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_response": text}
