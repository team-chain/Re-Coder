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


#: 이보다 작은 상한은 연결 확인용 탐침 — 잘림 검사를 하지 않는다.
_PROBE_MAX_TOKENS = 64
#: stop_reason 없이 출력 토큰 수로 잘림을 추정하는 최소 요청 상한(코드 생성 규모).
_INFER_MIN_TOKENS = 1024


def gateway_output_cap() -> int:
    """게이트웨이가 호출 한 번에 허용하는 출력 토큰 상한(서버 GW_MAX_TOKENS_CEILING 과 같게 둔다)."""
    try:
        return max(256, int(os.environ.get("RECODER_GATEWAY_MAX_OUTPUT", "4096")))
    except ValueError:
        return 4096


def gateway_enabled() -> bool:
    return bool(os.environ.get("RECODER_LLM_GATEWAY_URL") and os.environ.get("RECODER_STUDENT_TOKEN"))


class GatewayProvider(LLMProvider):
    def __init__(self, model_id: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0") -> None:
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
    async def converse(self, messages, system=None, output_schema=None, *, max_tokens=4096, temperature=0.0) -> dict:
        loop = asyncio.get_running_loop()
        payload = {"messages": messages, "system": system or "",
                   "output_schema": output_schema, "max_tokens": max_tokens, "temperature": temperature}
        result = await loop.run_in_executor(None, self._post, payload)
        self._require_complete(result, max_tokens, structured=output_schema is not None)
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
        self._require_complete(result, request.max_tokens, structured=request.json_schema is not None)
        return LLMResponse(
            text=result.get("text", ""),
            parsed=result.get("parsed"),
            model_used=result.get("model_used", self.model_id),
            provider="gateway",
            input_tokens=int(result.get("input_tokens", 0)),
            output_tokens=int(result.get("output_tokens", 0)),
            token_source="gateway",
        )

    @staticmethod
    def _require_complete(result: dict, max_tokens: int | None = None, structured: bool = True) -> None:
        """잘린 응답이면 STRUCTURED_OUTPUT 오류 — 호출자가 나눠서 다시 만들게 한다.

        게이트웨이는 출력 토큰을 GW_MAX_TOKENS_CEILING(기본 4096)으로 **조용히 깎는다**
        (HTTP API 30초 제한 안에 끝나야 해서). 배포된 게이트웨이는 stop_reason 을 돌려주지
        않으므로, 쓴 출력 토큰이 실제 상한에 닿았으면 잘린 것으로 본다. 이걸 못 알아채면
        잘린 JSON 을 "형식 오류"로 오인해 같은 큰 요청을 한 번 더 보내고 결국 실패했다
        (실기기: 결제까지 넣은 쇼핑몰 요청).
        """
        #: 아주 작은 상한(연결 확인 ping 등)은 원래 잘리는 게 정상이다 — 잘림으로 보면 AI 연결 확인이 실패한다.
        if max_tokens is not None and int(max_tokens) < _PROBE_MAX_TOKENS:
            return
        #: 평문 응답(요약·설명)은 예전처럼 잘린 그대로 돌려준다 — 배포·운영 쪽 요약 기능이 갑자기 실패하면 안 된다.
        #: 잘림을 오류로 보는 것은 JSON 구조를 요구한 호출(코드 생성 등)뿐이다.
        if not structured:
            return
        reason = str(result.get("stop_reason") or result.get("stopReason") or "").lower()
        if reason in {"max_tokens", "length"}:
            raise LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)
        #: stop_reason 이 없는(배포된) 게이트웨이: 생성 규모의 요청에서만 '상한에 닿음 = 잘림'으로 추정한다.
        if reason or not max_tokens or int(max_tokens) < _INFER_MIN_TOKENS:
            return
        cap = min(int(max_tokens), gateway_output_cap())
        try:
            used = int(result.get("output_tokens") or 0)
        except (TypeError, ValueError):
            return
        if cap > 0 and used >= cap - 8:
            raise LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0
