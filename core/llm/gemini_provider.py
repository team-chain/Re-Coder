"""
ReCoder Core — Gemini Flash Fallback Provider

Uses google-generativeai SDK to call gemini-2.5-flash as a cost-free
fallback when Bedrock is unavailable or in cost-reduction mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
