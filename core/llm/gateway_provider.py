"""
GatewayProvider — 학생 Local Core 가 운영자 게이트웨이를 통해 Bedrock 을 쓰는 클라이언트.

학생 PC 에는 AWS 자격증명이 없다. 대신 발급받은 학생 토큰으로 운영자 게이트웨이
( API Gateway + Lambda )에 HTTPS 로 프롬프트만 보내고, 게이트웨이가 운영자 계정의
Bedrock 을 대신 호출한다.

활성화: 환경변수
  RECODER_LLM_GATEWAY_URL = https://xxxx.execute-api.<region>.amazonaws.com
  RECODER_STUDENT_TOKEN   = rcdr_<student_id>_<secret>

이 값이 있으면 provider_router 가 Bedrock 직접호출 대신 본 Provider 를 사용한다.
인터페이스(model_id, async converse)는 BedrockProvider 와 호환된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import urllib.error

try:
    from llm.base import LLMProvider, LLMRequest, LLMResponse, LLMError, LLMErrorType
except ImportError:  # pragma: no cover
    from core.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMError, LLMErrorType

log = logging.getLogger(__name__)


def gateway_enabled() -> bool:
    return bool(os.environ.get("RECODER_LLM_GATEWAY_URL") and os.environ.get("RECODER_STUDENT_TOKEN"))


class GatewayProvider(LLMProvider):
    def __init__(self, model_id: str = "anthropic.claude-3-haiku-20240307-v1:0") -> None:
        base = os.environ.get("RECODER_LLM_GATEWAY_URL", "").rstrip("/")
        self._endpoint = base + "/llm/invoke"
        self._token = os.environ.get("RECODER_STUDENT_TOKEN", "")
        self._timeout = float(os.environ.get("RECODER_GATEWAY_TIMEOUT", "60"))
        self.model_id = model_id
        self._model_id = model_id  # bedrock 호환 별칭

    @property
    def provider_name(self) -> str:
        return "gateway"

    # ── 내부 HTTP ───────────────────────────────────────────────────
    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = {}
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                pass
            code = body.get("error", "")
            etype = LLMErrorType.QUOTA_EXCEEDED if e.code == 429 else (
                LLMErrorType.ACCESS_DENIED if e.code in (401, 403) else LLMErrorType.SERVICE_ERROR)
            raise LLMError(f"gateway {e.code}: {body.get('message', code)}",
                           error_type=etype, retryable=(e.code >= 500), raw=e)
        except urllib.error.URLError as e:
            raise LLMError(f"gateway 연결 실패: {e}", error_type=LLMErrorType.SERVICE_ERROR,
                           retryable=True, raw=e)

    @staticmethod
    def _prompt_from_messages(messages: list[dict]) -> str:
        try:
            return messages[-1]["content"][0]["text"]
        except Exception:
            return ""

    # ── BedrockProvider 호환 async converse ─────────────────────────
    async def converse(self, messages, system=None, output_schema=None) -> dict:
        loop = asyncio.get_running_loop()
        payload = {"messages": messages, "system": system or "",
                   "output_schema": output_schema, "max_tokens": 2048}
        result = await loop.run_in_executor(None, self._post, payload)
        if output_schema is not None and isinstance(result.get("parsed"), dict):
            return result["parsed"]
        if isinstance(result.get("parsed"), dict):
            return result["parsed"]
        return {"text": result.get("text", "")}

    # ── 동기 call (ABC 충족) ────────────────────────────────────────
    def call(self, request: LLMRequest) -> LLMResponse:
        messages = [{"role": "user", "content": [{"text": request.prompt}]}]
        payload = {"messages": messages, "system": request.system or "",
                   "output_schema": request.json_schema, "max_tokens": request.max_tokens}
        result = self._post(payload)
        return LLMResponse(
            text=result.get("text", ""),
            parsed=result.get("parsed"),
            model_used=result.get("model_used", self.model_id),
            provider="gateway",
            input_tokens=int(result.get("input_tokens", 0)),
            output_tokens=int(result.get("output_tokens", 0)),
            token_source="gateway",
        )

    # bedrock 호환용 보조 (router 가 estimate_cost 를 부를 수 있어 방어적으로 제공)
    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0
