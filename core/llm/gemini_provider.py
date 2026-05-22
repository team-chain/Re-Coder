"""
<<<<<<< HEAD
ReCoder Core — Gemini Flash Fallback Provider

Uses google-generativeai SDK to call gemini-2.5-flash as a cost-free
fallback when Bedrock is unavailable or in cost-reduction mode.
"""

=======
Google Gemini Provider (설계서 v5.7 §3.2 보조 fallback).

Bedrock 전체 체인 실패 시 최후 수단으로 사용.
GEMINI_API_KEY 환경변수 필수.
"""
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
from __future__ import annotations

import json
import logging
import os
import re
<<<<<<< HEAD
from typing import Any, Optional

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
API_KEY_ENV = "GEMINI_API_KEY"

# Free-tier pricing (0.0 USD)
COST_PER_1K_TOKENS: dict[str, float] = {"input": 0.0, "output": 0.0}


class GeminiProvider:
    """
    Async wrapper for the Google Generative AI (Gemini) SDK.

    Falls back gracefully when the SDK is not installed or the API key
    is absent.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get(API_KEY_ENV, "")
        self._client = None
        self._model = None
        self._initialise()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Generate a response from Gemini Flash.

        If *schema* is provided the model is instructed to return
        well-formed JSON matching that schema.  The response text is
        parsed and returned as a dict.

        Runs synchronous SDK calls in a thread-pool executor.
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
        """Return True if the Gemini API key is present and functional."""
        if not self._api_key or self._model is None:
            return False
        try:
            await self.generate("ping")
            return True
        except Exception as exc:
            log.warning("Gemini access validation failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _initialise(self) -> None:
        """Attempt to initialise the Google Generative AI client."""
        if not self._api_key:
            log.debug("GEMINI_API_KEY not set — GeminiProvider disabled")
            return
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=self._api_key)
            self._client = genai
            self._model = genai.GenerativeModel(MODEL)
        except ImportError:
            log.warning(
                "google-generativeai package not installed. "
                "Run: pip install google-generativeai"
            )
        except Exception as exc:
            log.error("Failed to initialise Gemini client: %s", exc)

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
=======
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
>>>>>>> 74cf4369799da45d0fa49de67d56e58e01a2cc27
