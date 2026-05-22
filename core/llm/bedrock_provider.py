"""
ReCoder Core — AWS Bedrock LLM Provider (설계서 v6.4-final §5.1, §13).

Wraps boto3 Bedrock Converse / ConverseStream API with:
  - Structured output 우선순위:
      1) outputConfig.textFormat JSON Schema (일부 모델만)
      2) toolConfig strict tool use (tool_choice)
      3) Tool Use (자유 선택)
      4) Plain JSON 추출
      5) schema repair (1회)
  - 폴백 체인: Primary(Sonnet) → Secondary(Sonnet 구버전) → Fast(Haiku)
  - Model ID resolution from diagnostics.json (region/model)
  - AWS 자격증명: CLI profile / IAM Identity Center → 환경변수 → Access Key
  - Per-model cost estimation
  - botocore 예외의 LLMErrorType 분류

NOTE: Bedrock Converse API의 Structured Output(outputConfig.textFormat)은
      모델별로 미지원일 수 있으므로 toolConfig strict mode 폴백이 필요하다.

이 Provider는 동기 ``call(LLMRequest) -> LLMResponse`` (LLMProvider 인터페이스)와
비동기 ``converse(...)`` 두 가지 호출 경로를 모두 제공한다.  비동기 경로는
boto3 블로킹 호출을 thread-pool executor 에서 실행하여 asyncio 이벤트
루프를 차단하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .base import LLMError, LLMErrorType, LLMProvider, LLMRequest, LLMResponse

log = logging.getLogger(__name__)
logger = log  # alias for legacy callers

# ---------------------------------------------------------------------------
# Model / cost tables
# ---------------------------------------------------------------------------

# ── 기본 모델 식별자 (설계서 §5.1, §13) ──────────────────────────────────
# MVP/시연 기본값: 3.5 Sonnet v2 (코드 패치) + Haiku 3 (분석, 저비용)
# 고성능 옵션: BEDROCK_PRIMARY_MODEL_IDENTIFIER=global.anthropic.claude-sonnet-4-5-20250929-v1:0
DEFAULT_PRIMARY_MODEL = os.getenv(
    "BEDROCK_PRIMARY_MODEL_IDENTIFIER",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
)
DEFAULT_SECONDARY_MODEL = os.getenv(
    "BEDROCK_SECONDARY_MODEL_IDENTIFIER",
    "anthropic.claude-3-sonnet-20240229-v1:0",
)
DEFAULT_FAST_MODEL = os.getenv(
    "BEDROCK_FAST_MODEL_IDENTIFIER",
    "anthropic.claude-3-haiku-20240307-v1:0",
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")

SONNET_MODELS: list[str] = [
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
]

HAIKU_MODELS: list[str] = [
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]

COST_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 0.003,   "output": 0.015},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 0.003,   "output": 0.015},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 0.0008,  "output": 0.004},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.00025, "output": 0.00125},
}

_DIAGNOSTICS_PATH = Path.home() / ".recoder" / "diagnostics.json"


# ---------------------------------------------------------------------------
# 오류 분류
# ---------------------------------------------------------------------------

def _classify_boto_error(exc: Exception) -> tuple[LLMErrorType, bool]:
    """botocore 예외를 LLMErrorType 으로 분류. (error_type, retryable) 반환."""
    code = ""
    try:
        code = exc.response["Error"]["Code"]  # type: ignore[attr-defined]
    except Exception:
        pass
    msg = str(exc).lower()

    if code in ("ResourceNotFoundException", "ModelNotReadyException") or "not found" in msg:
        return LLMErrorType.MODEL_NOT_FOUND, True
    if code in ("AccessDeniedException", "UnauthorizedException") or "access denied" in msg:
        return LLMErrorType.ACCESS_DENIED, True
    if code in ("ThrottlingException", "TooManyRequestsException") or "throttl" in msg:
        return LLMErrorType.THROTTLING, True
    if "quota" in msg or "exceeded" in msg:
        return LLMErrorType.QUOTA_EXCEEDED, True
    if code == "ValidationException" or "validat" in msg:
        return LLMErrorType.VALIDATION_ERROR, False
    if "too many tokens" in msg or "too long" in msg or ("context" in msg and "length" in msg):
        return LLMErrorType.CONTEXT_TOO_LONG, False
    if code and code.startswith("5"):
        return LLMErrorType.SERVICE_ERROR, True
    return LLMErrorType.UNKNOWN, False


# ---------------------------------------------------------------------------
# JSON 추출 / 복구 헬퍼
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """텍스트에서 JSON 블록 추출 (```json ... ``` 또는 { ... })."""
    # 코드 블록 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 첫 번째 { ... } 블록 (greedy outer braces)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


def _repair_json(text: str, schema: dict) -> dict | None:
    """단순 schema repair: 누락된 required 필드를 기본값으로 채운다."""
    parsed = _extract_json(text)
    if parsed is None:
        return None
    required = schema.get("required", [])
    props = schema.get("properties", {})
    for key in required:
        if key not in parsed:
            prop_type = props.get(key, {}).get("type", "string")
            parsed[key] = "" if prop_type == "string" else (
                False if prop_type == "boolean" else (0 if prop_type in ("integer", "number") else None)
            )
    return parsed


# ---------------------------------------------------------------------------
# BedrockProvider
# ---------------------------------------------------------------------------


class BedrockProvider(LLMProvider):
    """
    AWS Bedrock Converse API 기반 Provider (설계서 §5.1, §13).

    동기 진입점 ``call(LLMRequest) -> LLMResponse`` 와 비동기 진입점
    ``converse(messages, system, output_schema)`` 를 모두 제공한다.
    동기 경로는 router 및 LLMProvider 인터페이스를 따르고, 비동기 경로는
    boto3 호출을 thread-pool executor 에서 실행하여 asyncio 이벤트 루프를
    차단하지 않는다.
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        resolved_region, resolved_model = self._load_diagnostics()

        # 정밀한 우선순위: 명시 인자 > diagnostics.json > 환경변수/기본값
        self._region = region or resolved_region or BEDROCK_REGION
        self._model_id = model_id or resolved_model or DEFAULT_PRIMARY_MODEL

        # 공개 별칭 (legacy)
        self.region = self._region
        self.model_id = self._model_id

        self._client = None

    # ------------------------------------------------------------------
    # LLMProvider 인터페이스
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "bedrock"

    def _get_client(self):
        """boto3 클라이언트를 lazy 하게 생성/캐시."""
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as e:
            raise LLMError(
                "boto3가 설치되지 않았습니다. pip install boto3",
                LLMErrorType.UNKNOWN,
            ) from e

        profile = os.getenv("AWS_PROFILE", "").strip()
        kwargs: dict[str, Any] = {"region_name": self._region}
        if profile:
            kwargs["profile_name"] = profile

        try:
            if profile:
                # boto3.Session(profile_name=...) 경유가 안전
                session = boto3.Session(profile_name=profile, region_name=self._region)
                self._client = session.client("bedrock-runtime")
            else:
                self._client = boto3.client("bedrock-runtime", region_name=self._region)
        except Exception as exc:  # pragma: no cover
            log.warning("boto3 unavailable: %s", exc)
            self._client = None
            raise

        return self._client

    # ------------------------------------------------------------------
    # Structured Output 우선순위 구현 (설계서 §5.1, §13)
    # ------------------------------------------------------------------

    def _call_with_output_config(
        self,
        client,
        messages: list,
        system: str,
        schema: dict,
        max_tokens: int,
    ) -> str | None:
        """1순위: outputConfig.textFormat JSON Schema (일부 모델만 지원)."""
        try:
            body: dict[str, Any] = {
                "messages": messages,
                "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
                "outputConfig": {"textFormat": {"schema": schema}},
            }
            if system:
                body["system"] = [{"text": system}]
            resp = client.converse(modelId=self._model_id, **body)
            return resp["output"]["message"]["content"][0]["text"]
        except Exception:
            return None

    def _call_with_tool_config(
        self,
        client,
        messages: list,
        system: str,
        schema: dict,
        max_tokens: int,
    ) -> str | None:
        """2순위: toolConfig strict tool use (tool_choice 강제)."""
        try:
            tool_spec = {
                "toolSpec": {
                    "name":        "structured_output",
                    "description": "Return structured JSON output matching the provided schema.",
                    "inputSchema": {"json": schema},
                }
            }
            body: dict[str, Any] = {
                "messages": messages,
                "toolConfig": {
                    "tools": [tool_spec],
                    "toolChoice": {"tool": {"name": "structured_output"}},
                },
                "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
            }
            if system:
                body["system"] = [{"text": system}]
            resp = client.converse(modelId=self._model_id, **body)
            block = resp["output"]["message"]["content"][0]
            if block.get("toolUse"):
                return json.dumps(block["toolUse"]["input"])
            return None
        except Exception:
            return None

    def _call_plain(
        self,
        client,
        messages: list,
        system: str,
        max_tokens: int,
    ) -> str:
        """3~5순위 폴백용 일반 Converse 호출."""
        body: dict[str, Any] = {
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        }
        if system:
            body["system"] = [{"text": system}]
        try:
            resp = client.converse(modelId=self._model_id, **body)
        except Exception as exc:
            error_type, retryable = _classify_boto_error(exc)
            raise LLMError(str(exc), error_type, retryable, raw=exc) from exc
        return resp["output"]["message"]["content"][0]["text"]

    def _count_tokens_estimate(self, text: str) -> int:
        """설계서 §3.7 — 로컬 추정 (1 토큰 ≈ 4 글자)."""
        return max(1, len(text) // 4)

    def call(self, request: LLMRequest) -> LLMResponse:
        """동기 LLMProvider 진입점. router 가 이 메서드를 호출한다."""
        client = self._get_client()

        # 메시지 구성 (멀티모달 image 지원)
        content: list[dict] = []
        if request.image_bytes:
            content.append({
                "image": {
                    "format": request.image_mime.split("/")[-1],
                    "source": {"bytes": request.image_bytes},
                }
            })
        content.append({"text": request.prompt})
        messages = [{"role": "user", "content": content}]

        raw_text: str | None = None
        parsed: dict | None = None
        token_source = "estimate"

        # ── Structured Output 우선순위 ──
        if request.json_schema:
            # 1순위: outputConfig
            raw_text = self._call_with_output_config(
                client, messages, request.system, request.json_schema, request.max_tokens,
            )
            # 2순위: toolConfig
            if raw_text is None:
                raw_text = self._call_with_tool_config(
                    client, messages, request.system, request.json_schema, request.max_tokens,
                )
            # 3순위: 일반 호출 + JSON 추출
            if raw_text is None:
                raw_text = self._call_plain(client, messages, request.system, request.max_tokens)

            # JSON 파싱
            try:
                parsed = json.loads(raw_text)
            except Exception:
                # 4순위: JSON 추출
                parsed = _extract_json(raw_text or "")
                if parsed is None:
                    # 5순위: schema repair 1회
                    parsed = _repair_json(raw_text or "", request.json_schema)
        else:
            raw_text = self._call_plain(client, messages, request.system, request.max_tokens)

        # 토큰 수 추정 (설계서 §3.7)
        in_tokens = self._count_tokens_estimate(request.prompt)
        out_tokens = self._count_tokens_estimate(raw_text or "")

        return LLMResponse(
            text          = raw_text or "",
            parsed        = parsed,
            model_used    = self._model_id,
            provider      = "bedrock",
            region        = self._region,
            input_tokens  = in_tokens,
            output_tokens = out_tokens,
            token_source  = token_source,
        )

    # ------------------------------------------------------------------
    # 비동기 진입점 (legacy / asyncio 호출자용)
    # ------------------------------------------------------------------

    async def converse(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str] = None,
        output_schema: Optional[dict] = None,
    ) -> dict[str, Any]:
        """
        Bedrock Converse API 비동기 호출.

        Strategy
        --------
        1. *output_schema* 가 제공되면 structured output (tool_choice) 시도.
        2. 실패 시 plain Tool Use 시도.
        3. 그래도 실패하면 텍스트 응답에서 JSON 추출.

        Returns parsed dict.
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
        Bedrock 접근 권한 검증.  최소한의 ping 메시지를 전송한다.

        Returns (is_valid, model_id, region).
        """
        probe = [{"role": "user", "content": [{"text": "ping"}]}]
        try:
            await self._converse_plain(probe, system=None)
            return True, self._model_id, self._region
        except Exception as exc:
            log.error("Bedrock access validation failed: %s", exc)
            return False, self._model_id, self._region

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Return estimated USD cost for the given token counts."""
        rates = COST_PER_1K_TOKENS.get(self._model_id, {"input": 0.003, "output": 0.015})
        return (input_tokens / 1000.0) * rates["input"] + (output_tokens / 1000.0) * rates["output"]

    # ------------------------------------------------------------------
    # 비동기 internal helpers
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
            "modelId": self._model_id,
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
            "modelId": self._model_id,
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
            "modelId": self._model_id,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{"text": system}]

        response = await self._invoke_sync(kwargs)
        text = self._extract_text(response)
        return self._parse_json_from_text(text)

    async def _invoke_sync(self, kwargs: dict) -> dict:
        """Run the blocking boto3 call in a thread-pool executor."""
        client = self._client
        if client is None:
            try:
                client = self._get_client()
            except Exception:
                client = None
        if client is None:
            raise RuntimeError("boto3 client not initialised")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: client.converse(**kwargs)
        )

    # ------------------------------------------------------------------
    # 정적 유틸리티
    # ------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 편의 팩토리 (설계서 §5.1 폴백 체인)
# ---------------------------------------------------------------------------

def make_primary() -> BedrockProvider:
    """Primary: Claude 3.5 Sonnet v2 (코드 패치 / Dockerfile / 운영 분석)."""
    return BedrockProvider(DEFAULT_PRIMARY_MODEL)


def make_secondary() -> BedrockProvider:
    """Secondary: Claude 3 Sonnet (구버전 폴백)."""
    return BedrockProvider(DEFAULT_SECONDARY_MODEL)


def make_fast() -> BedrockProvider:
    """Fast: Claude 3 Haiku (에러 분류 / 요약, 저비용)."""
    return BedrockProvider(DEFAULT_FAST_MODEL)
