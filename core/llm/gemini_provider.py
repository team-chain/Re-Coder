"""
Google Gemini Provider (설계서 v5.7 §3.2 보조 fallback).

Bedrock 전체 체인 실패 시 최후 수단으로 사용.
GEMINI_API_KEY 환경변수 필수.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

from .base import LLMError, LLMErrorType, LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

_DEFAULT_FALLBACK_CHAIN = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
]

_client_lock = threading.Lock()
_client_cache: dict[str, object] = {}


def _get_client(api_key: str):
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


class GeminiProvider(LLMProvider):
    """Google Gemini API Provider (설계서 §3 보조 fallback)."""

    @property
    def provider_name(self) -> str:
        return "gemini"

    def call(self, request: LLMRequest) -> LLMResponse:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY가 설정되지 않았습니다.",
                LLMErrorType.ACCESS_DENIED,
                retryable=False,
            )

        from google.genai import types

        client = _get_client(api_key)
        chain  = _resolve_chain()

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
                    config=types.GenerateContentConfig(**{k: v for k, v in config_kwargs.items() if v is not None}),
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

                in_tokens  = max(1, len(request.prompt) // 4)
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
