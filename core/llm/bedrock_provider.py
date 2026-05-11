"""
AWS Bedrock Provider (설계서 v5.7 §3.2 ~ §3.3).

- Converse / ConverseStream API 사용
- Structured Output 우선순위: outputConfig → toolConfig → JSON 추출 → schema repair → fallback
- 폴백 체인: Primary(Sonnet) → Secondary(Sonnet 구버전) → Fast(Haiku)
- AWS 자격증명: CLI profile / IAM Identity Center → 환경변수 → Access Key (시연 전용)

NOTE: Bedrock Converse API는 Structured Output(outputConfig.textFormat)을 지원하기 위해
모델별 미지원 확인 필요. 일부 모델은 toolConfig strict mode를 통한 JSON 스키마 제약만 지원.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .base import LLMError, LLMErrorType, LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

# ── 기본 모델 식별자 (설계서 §3.4) ──────────────────────────────────────
# 설계서 §3.2 — 기본 모델 식별자 (.env 으로 재정의 가능)
#
# ★ Bedrock 최신 모델은 on-demand 직접 호출 불가 → cross-region inference profile 사용
#   형식: us.anthropic.<model-id>  (us-east-1 기준)
#         ap.anthropic.<model-id>  (ap-northeast-1 등 아시아)
#         eu.anthropic.<model-id>  (eu-west-1 등 유럽)
#
# 사용 가능한 모델 확인:
#   aws bedrock list-inference-profiles --region us-east-1
#
# 고성능 옵션:
#   BEDROCK_PRIMARY_MODEL_IDENTIFIER=us.anthropic.claude-opus-4-20250514-v1:0
DEFAULT_PRIMARY_MODEL   = os.getenv(
    "BEDROCK_PRIMARY_MODEL_IDENTIFIER",
    "us.anthropic.claude-sonnet-4-6",
)
DEFAULT_SECONDARY_MODEL = os.getenv(
    "BEDROCK_SECONDARY_MODEL_IDENTIFIER",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)
DEFAULT_FAST_MODEL      = os.getenv(
    "BEDROCK_FAST_MODEL_IDENTIFIER",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")


# ── 오류 분류 ────────────────────────────────────────────────────────────

def _classify_boto_error(exc: Exception) -> tuple[LLMErrorType, bool]:
    """botocore 예외를 LLMErrorType으로 분류. (error_type, retryable) 반환."""
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
    if "too many tokens" in msg or "too long" in msg or "context" in msg and "length" in msg:
        return LLMErrorType.CONTEXT_TOO_LONG, False
    if code and code.startswith("5"):
        return LLMErrorType.SERVICE_ERROR, True
    return LLMErrorType.UNKNOWN, False


# ── JSON 추출/복구 헬퍼 ──────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    """텍스트에서 JSON 블록 추출 (```json ... ``` 또는 { ... })."""
    # 코드 블록 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 첫 번째 { ... } 블록
    start = text.find("{")
    end   = text.rfind("}")
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
    props    = schema.get("properties", {})
    for key in required:
        if key not in parsed:
            prop_type = props.get(key, {}).get("type", "string")
            parsed[key] = "" if prop_type == "string" else (
                False if prop_type == "boolean" else (0 if prop_type in ("integer", "number") else None)
            )
    return parsed


class BedrockProvider(LLMProvider):
    """AWS Bedrock Converse API 기반 Provider (설계서 §3.2 ~ §3.3)."""

    def __init__(self, model_id: str, region: str = BEDROCK_REGION):
        self._model_id = model_id
        self._region   = region
        self._client   = None

    @property
    def provider_name(self) -> str:
        return "bedrock"

    def _get_client(self):
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

        self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    # ── Structured Output 우선순위 구현 (설계서 §3.3) ─────────────────────

    def _call_with_output_config(self, client, messages: list, system: str,
                                  schema: dict, max_tokens: int) -> str | None:
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

    def _call_with_tool_config(self, client, messages: list, system: str,
                                schema: dict, max_tokens: int) -> str | None:
        """2순위: toolConfig strict tool use."""
        try:
            tool_spec = {
                "toolSpec": {
                    "name":        "structured_output",
                    "description": "Return structured JSON output.",
                    "inputSchema": {"json": schema},
                }
            }
            body: dict[str, Any] = {
                "messages":      messages,
                "toolConfig":    {"tools": [tool_spec], "toolChoice": {"tool": {"name": "structured_output"}}},
                "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
            }
            if system:
                body["system"] = [{"text": system}]
            resp  = client.converse(modelId=self._model_id, **body)
            block = resp["output"]["message"]["content"][0]
            if block.get("toolUse"):
                return json.dumps(block["toolUse"]["input"])
            return None
        except Exception:
            return None

    def _call_plain(self, client, messages: list, system: str, max_tokens: int) -> str:
        """3~5순위 폴백용 일반 Converse 호출."""
        body: dict[str, Any] = {
            "messages":      messages,
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
        client = self._get_client()

        # 메시지 구성
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
        token_source = "estimate"

        # ── Structured Output 우선순위 ──
        if request.json_schema:
            # 1순위: outputConfig
            raw_text = self._call_with_output_config(
                client, messages, request.system, request.json_schema, request.max_tokens
            )
            # 2순위: toolConfig
            if raw_text is None:
                raw_text = self._call_with_tool_config(
                    client, messages, request.system, request.json_schema, request.max_tokens
                )
            # 3순위: 일반 호출 + JSON 추출
            if raw_text is None:
                raw_text = self._call_plain(client, messages, request.system, request.max_tokens)

            # JSON 파싱
            parsed: dict | None = None
            try:
                parsed = json.loads(raw_text)
            except Exception:
                # 4순위: JSON 추출
                parsed = _extract_json(raw_text)
                if parsed is None:
                    # 5순위: schema repair 1회
                    parsed = _repair_json(raw_text or "", request.json_schema)
        else:
            raw_text = self._call_plain(client, messages, request.system, request.max_tokens)
            parsed   = None

        # 토큰 수 추정 (설계서 §3.7)
        in_tokens  = self._count_tokens_estimate(request.prompt)
        out_tokens = self._count_tokens_estimate(raw_text or "")

        return LLMResponse(
            text         = raw_text or "",
            parsed       = parsed,
            model_used   = self._model_id,
            provider     = "bedrock",
            input_tokens  = in_tokens,
            output_tokens = out_tokens,
            token_source  = token_source,
        )


# ── 편의 팩토리 ──────────────────────────────────────────────────────────

def make_primary()   -> BedrockProvider: return BedrockProvider(DEFAULT_PRIMARY_MODEL)
def make_secondary() -> BedrockProvider: return BedrockProvider(DEFAULT_SECONDARY_MODEL)
def make_fast()      -> BedrockProvider: return BedrockProvider(DEFAULT_FAST_MODEL)
