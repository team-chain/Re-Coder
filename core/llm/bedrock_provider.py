"""
ReCoder Core — AWS Bedrock LLM Provider

Wraps boto3 Bedrock Converse API with:
  - Structured output via tool_choice (first-class)
  - Tool Use fallback
  - Plain JSON extraction fallback
  - Model ID resolution from diagnostics.json
  - Per-model cost estimation
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model / cost tables
# ---------------------------------------------------------------------------

SONNET_MODELS: list[str] = [
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
]

HAIKU_MODELS: list[str] = [
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]

COST_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 0.003,  "output": 0.015},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 0.003,  "output": 0.015},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 0.0008, "output": 0.004},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.00025,"output": 0.00125},
}

_DIAGNOSTICS_PATH = Path.home() / ".recoder" / "diagnostics.json"


# ---------------------------------------------------------------------------
# BedrockProvider
# ---------------------------------------------------------------------------


class BedrockProvider:
    """
    Async wrapper around boto3 Bedrock Runtime's ``converse`` endpoint.

    The heavy boto3 I/O is executed in a thread-pool executor so the
    asyncio event loop is never blocked.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> None:
        resolved_region, resolved_model = self._load_diagnostics()

        self.region = region or resolved_region or "us-east-1"
        self.model_id = model_id or resolved_model or SONNET_MODELS[0]

        # Lazy boto3 import -- not required if only running unit tests
        try:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        except Exception as exc:  # pragma: no cover
            log.warning("boto3 unavailable: %s", exc)
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def converse(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str] = None,
        output_schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Call Bedrock Converse API.

        Strategy
        --------
        1. If *output_schema* provided: attempt structured output via tool_choice.
        2. On failure: retry as plain Tool Use.
        3. On second failure: extract JSON from the text response.

        Returns a dict (parsed from the model response).
        """
        if output_schema:
            try:
                return await self._converse_structured(messages, system, output_schema)
            except Exception as exc:
                log.warning("Structured output failed (%s), trying tool use", exc)
                try:
                    return await self._converse_tool_use(messages, system, output_schema)
                except Exception as exc2:
                    log.warning("Tool use failed (%s), falling back to text JSON", exc2)

        return await self._converse_plain(messages, system)

    async def validate_access(self) -> tuple[bool, str, str]:
        """
        Probe Bedrock access by sending a minimal message.

        Returns (is_valid, model_id, region).
        """
        probe = [{"role": "user", "content": [{"text": "ping"}]}]
        try:
            result = await self._converse_plain(probe, system=None)
            return True, self.model_id, self.region
        except Exception as exc:
            log.error("Bedrock access validation failed: %s", exc)
            return False, self.model_id, self.region

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Return estimated USD cost for the given token counts."""
        rates = COST_PER_1K_TOKENS.get(self.model_id, {"input": 0.003, "output": 0.015})
        return (input_tokens / 1000.0) * rates["input"] + (output_tokens / 1000.0) * rates["output"]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _converse_structured(
        self,
        messages: list[dict],
        system: Optional[str],
        schema: dict,
    ) -> dict[str, Any]:
        """Structured output via tool_choice={"type": "tool", "name": "output"}."""
        tool_spec = {
            "toolSpec": {
                "name": "output",
                "description": "Return structured JSON output matching the provided schema.",
                "inputSchema": {"json": schema},
            }
        }
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "toolConfig": {
                "tools": [tool_spec],
                "toolChoice": {"tool": {"name": "output"}},
            },
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = await self._invoke_sync(kwargs)
        return self._extract_tool_input(response)

    async def _converse_tool_use(
        self,
        messages: list[dict],
        system: Optional[str],
        schema: dict,
    ) -> dict[str, Any]:
        """Tool use fallback — let model choose when to call the tool."""
        tool_spec = {
            "toolSpec": {
                "name": "output",
                "description": "Return structured JSON output matching the provided schema.",
                "inputSchema": {"json": schema},
            }
        }
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "toolConfig": {"tools": [tool_spec]},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = await self._invoke_sync(kwargs)
        return self._extract_tool_input(response)

    async def _converse_plain(
        self,
        messages: list[dict],
        system: Optional[str],
    ) -> dict[str, Any]:
        """Plain text call — extract any JSON block from the response text."""
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = await self._invoke_sync(kwargs)
        text = self._extract_text(response)
        return self._parse_json_from_text(text)

    async def _invoke_sync(self, kwargs: dict) -> dict:
        """Run the blocking boto3 call in a thread-pool executor."""
        if self._client is None:
            raise RuntimeError("boto3 client not initialised")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._client.converse(**kwargs)
        )

    @staticmethod
    def _extract_tool_input(response: dict) -> dict[str, Any]:
        """Pull the tool input payload out of a Converse API response."""
        for block in response.get("output", {}).get("message", {}).get("content", []):
            if block.get("toolUse"):
                return block["toolUse"].get("input", {})
        # Fallback: try extracting text JSON
        text = BedrockProvider._extract_text(response)
        return BedrockProvider._parse_json_from_text(text)

    @staticmethod
    def _extract_text(response: dict) -> str:
        """Extract concatenated text blocks from a Converse API response."""
        parts: list[str] = []
        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)

    @staticmethod
    def _parse_json_from_text(text: str) -> dict[str, Any]:
        """
        Attempt to parse JSON from model text output.

        Tries:
        1. Fenced ```json ... ``` blocks
        2. First {...} block (greedy outer braces)
        3. Raw parse
        """
        # 1. Fenced block
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        # 2. First {...} block
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # 3. Raw
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_response": text}

    @staticmethod
    def _load_diagnostics() -> tuple[Optional[str], Optional[str]]:
        """
        Read diagnostics.json and return (resolved_region, resolved_model_id).
        Returns (None, None) if the file is absent or malformed.
        """
        try:
            if _DIAGNOSTICS_PATH.exists():
                data = json.loads(_DIAGNOSTICS_PATH.read_text(encoding="utf-8"))
                return data.get("resolved_region"), data.get("resolved_model_id")
        except Exception:
            pass
        return None, None
