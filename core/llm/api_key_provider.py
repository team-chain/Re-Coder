"""
API 키 Provider — AWS 없이 Claude(Anthropic)·ChatGPT(OpenAI)·OpenAI 호환 API(DeepSeek 등) 키로 AI 를 쓴다.

활성화 (확장이 VS Code 보안 저장소의 키로 Core 를 띄울 때 넣는다):
  RECODER_AI_PROVIDER      = "anthropic" | "openai"
  RECODER_ANTHROPIC_API_KEY / RECODER_OPENAI_API_KEY
  RECODER_ANTHROPIC_MODEL / RECODER_OPENAI_MODEL        (선택)
  RECODER_ANTHROPIC_FAST_MODEL / RECODER_OPENAI_FAST_MODEL (선택)

OpenAI 호환(2.0.36): RECODER_COMPATIBLE_API_KEY + RECODER_COMPATIBLE_BASE_URL(필수, https —
  내 PC 의 localhost 만 http 허용) + RECODER_COMPATIBLE_MODEL(비우면 deepseek-chat).
  강제 도구 호출을 거절하는 서버면 JSON 모드(response_format)로 한 번 다시 보낸다.

일부러 ANTHROPIC_API_KEY/OPENAI_API_KEY 같은 흔한 이름을 읽지 않는다. 다른 도구 때문에
이미 설정된 키가 있다고 Bedrock 사용자의 경로가 몰래 바뀌면 안 된다.

인터페이스(model_id, provider_name, async converse, call, estimate_cost)는
BedrockProvider·GatewayProvider 와 같다. 구조화 출력은 강제 도구 호출로 받는다.
키는 로그·예외 메시지에 넣지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Optional

try:
    from llm.base import LLMProvider, LLMRequest, LLMResponse, LLMError, LLMErrorType
except ImportError:  # pragma: no cover
    from core.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMError, LLMErrorType

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "compatible")
DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-5", "claude-sonnet-5"),
    "openai": ("gpt-6-sol", "gpt-6-sol"),
    "compatible": ("deepseek-chat", "deepseek-chat"),
}
ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
}
LABELS = {"anthropic": "Claude API", "openai": "OpenAI API", "compatible": "OpenAI 호환 API"}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
TOOL_NAME = "output"


def compatible_endpoint(raw: str) -> str:
    """OpenAI 호환 주소 → chat/completions 주소. 안전하지 않은 주소는 ValueError.

    https 만 받는다(키가 평문으로 나가지 않게). 내 PC 에서 도는 서버(localhost)만 http 를 허용한다.
    주소 안의 사용자 정보(user:pass@)·쿼리·조각은 받지 않는다 — 키는 헤더로만 보낸다.
    """
    from urllib.parse import urlsplit
    value = (raw or "").strip()
    if not value:
        raise ValueError("OpenAI 호환 API 주소가 비어 있습니다.")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise ValueError("OpenAI 호환 API 주소는 https:// 로 시작해야 합니다.")
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ValueError("OpenAI 호환 API 주소는 https 여야 합니다(http 는 내 PC 의 localhost 만 허용).")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("OpenAI 호환 API 주소에 사용자 정보·쿼리를 넣지 마세요.")
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    netloc = parts.netloc
    return f"{parts.scheme}://{netloc}{path}"


def provider_ready(name: str) -> bool:
    """이 제공자를 쓸 키(호환 API 는 주소까지)가 있는가."""
    if name not in PROVIDERS or not os.environ.get(f"RECODER_{name.upper()}_API_KEY", "").strip():
        return False
    if name == "compatible":
        try:
            compatible_endpoint(os.environ.get("RECODER_COMPATIBLE_BASE_URL", ""))
        except ValueError:
            return False
    return True


def selected_provider() -> str:
    """키까지 있는 경우에만 제공자 이름을 돌려준다. 아니면 ''."""
    name = (os.environ.get("RECODER_AI_PROVIDER") or "").strip().lower()
    return name if provider_ready(name) else ""


def api_key_enabled() -> bool:
    return bool(selected_provider())


def model_for(provider: str, fast: bool = False) -> str:
    env = f"RECODER_{provider.upper()}_{'FAST_' if fast else ''}MODEL"
    return (os.environ.get(env) or "").strip() or ((os.environ.get(f"RECODER_{provider.upper()}_MODEL") or "").strip() if fast else "") or DEFAULT_MODELS[provider][1 if fast else 0]


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:  # 배포본(PyInstaller)에서도 공인 CA 를 확실히 갖도록 certifi 를 더한다.
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:  # pragma: no cover
        pass
    return ctx


def _is_reasoning_model(model: str) -> bool:
    return bool(re.match(r"^(o\d|gpt-[56])", model))


class ApiKeyProvider(LLMProvider):
    #: converse(usage=...) 로 실제 토큰 수를 돌려준다 — 라우터가 원장에 어림값 대신 적는다.
    reports_usage = True

    def __init__(self, provider: str, model_id: str = "") -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        self._provider = provider
        self._key = os.environ.get(f"RECODER_{provider.upper()}_API_KEY", "").strip()
        if provider == "compatible":
            self._endpoint = compatible_endpoint(os.environ.get("RECODER_COMPATIBLE_BASE_URL", ""))
        else:
            self._endpoint = os.environ.get(f"RECODER_{provider.upper()}_BASE_URL", "").strip() or ENDPOINTS[provider]
        self._timeout = float(os.environ.get("RECODER_AI_TIMEOUT", "") or "150")
        self.model_id = model_id or model_for(provider)
        self._model_id = self.model_id
        self._ctx = _ssl_context()

    @property
    def provider_name(self) -> str:
        return self._provider

    # ── HTTP ─────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        if self._provider == "anthropic":
            return {"Content-Type": "application/json", "x-api-key": self._key, "anthropic-version": "2023-06-01"}
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"}

    def _post_once(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._endpoint, data=data, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except urllib.error.URLError as e:
            raise LLMError(f"{LABELS[self._provider]} 연결 실패: {e.reason}", LLMErrorType.SERVICE_ERROR, True) from None
        except TimeoutError:
            raise LLMError(f"{LABELS[self._provider]} 응답 시간 초과", LLMErrorType.SERVICE_ERROR, True) from None

    def _http_error(self, e: urllib.error.HTTPError) -> LLMError:
        body: dict = {}
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            pass
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        message = str(err.get("message") or body.get("message") or e.reason or "")
        message = message.replace(self._key, "***") if self._key else message
        code = str(err.get("code") or err.get("type") or "")
        label = LABELS[self._provider]
        lowered = f"{message} {code}".lower()
        # 크레딧 소진은 제공자마다 코드가 다르다 — Anthropic 400 "credit balance is too low",
        # OpenAI 429 insufficient_quota, 일부 게이트웨이 402. 전부 "결제·한도" 로 모은다.
        if e.code == 402 or any(t in lowered for t in ("credit balance", "insufficient_quota", "billing", "exceeded your current quota")):
            return LLMError(f"{label} 크레딧·사용 한도가 부족합니다. {label} 콘솔에서 결제·한도를 확인하세요: {message}",
                            LLMErrorType.QUOTA_EXCEEDED, False)
        # Gemini 는 잘못된 키를 400 "API key not valid" 로 돌려준다.
        if e.code in (401, 403) or (e.code == 400 and ("api key not valid" in lowered or "api_key_invalid" in lowered)):
            return LLMError(f"{label} 키가 거부되었습니다({e.code}). 키를 다시 입력하세요.", LLMErrorType.ACCESS_DENIED, False)
        if e.code == 429:
            if "insufficient_quota" in code or "credit" in message.lower() or "billing" in message.lower():
                return LLMError(f"{label} 사용 한도·결제를 확인하세요: {message}", LLMErrorType.QUOTA_EXCEEDED, False)
            return LLMError(f"{label} 요청이 많습니다. 잠시 후 다시 시도하세요.", LLMErrorType.THROTTLING, True)
        if e.code == 404:
            return LLMError(f"{label} 모델을 찾을 수 없습니다({self.model_id}): {message}", LLMErrorType.MODEL_NOT_FOUND, False)
        if e.code in (400, 413, 422):
            kind = LLMErrorType.CONTEXT_TOO_LONG if ("too long" in message.lower() or e.code == 413) else LLMErrorType.VALIDATION_ERROR
            return LLMError(f"{label} 요청 오류({e.code}): {message}", kind, False)
        return LLMError(f"{label} 서버 오류({e.code}): {message}", LLMErrorType.SERVICE_ERROR, True)

    def _post(self, payload: dict) -> dict:
        """일시 오류는 한 번 재시도. 모델이 모르는 선택 파라미터는 빼고 한 번 더 보낸다."""
        for attempt in range(3):
            try:
                return self._post_once(payload)
            except LLMError as exc:
                text = str(exc)
                if self._provider == "compatible" and exc.error_type == LLMErrorType.VALIDATION_ERROR and "tools" in payload:
                    #: 강제 도구 호출을 모르는 호환 서버 — 같은 요청을 JSON 모드로 한 번 더.
                    payload = _json_mode(payload)
                    continue
                if exc.error_type == LLMErrorType.VALIDATION_ERROR and "reasoning_effort" in payload and "reasoning_effort" in text:
                    payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
                    continue
                if attempt == 0 and exc.retryable:
                    import time
                    time.sleep(random.uniform(0.5, 1.5))
                    continue
                raise
        raise LLMError(f"{LABELS[self._provider]} 호출 실패", LLMErrorType.SERVICE_ERROR, True)  # pragma: no cover

    # ── 요청·응답 변환 ───────────────────────────────────────────────
    @staticmethod
    def _texts(messages: list[dict]) -> list[tuple[str, str]]:
        out = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(str(b.get("text", "")) for b in (content or []) if isinstance(b, dict) and "text" in b)
            out.append((m.get("role", "user"), text))
        return out

    def _payload(self, messages: list[dict], system: Optional[str], schema: Optional[dict], max_tokens: int) -> dict:
        texts = self._texts(messages)
        if self._provider == "anthropic":
            payload: dict[str, Any] = {
                "model": self.model_id, "max_tokens": max_tokens,
                "messages": [{"role": r, "content": [{"type": "text", "text": t}]} for r, t in texts],
            }
            if system:
                payload["system"] = system
            if schema:
                payload["tools"] = [{"name": TOOL_NAME, "description": "Return structured JSON output matching the provided schema.", "input_schema": schema}]
                payload["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
            return payload
        chat = ([{"role": "system", "content": system}] if system else []) + [{"role": r, "content": t} for r, t in texts]
        if self._provider == "compatible":
            #: 호환 서버 대부분은 max_completion_tokens·reasoning_effort 를 모른다.
            payload = {"model": self.model_id, "messages": chat, "max_tokens": max_tokens}
        else:
            payload = {"model": self.model_id, "messages": chat, "max_completion_tokens": max_tokens}
        if self._provider == "openai" and _is_reasoning_model(self.model_id):
            # GPT-6 Sol/Luna의 Chat Completions 도구 호출은 none만 지원한다.
            # https://developers.openai.com/api/docs/guides/latest-model
            payload["reasoning_effort"] = "none" if schema and re.match(r"^gpt-6-(sol|luna)(?:$|-)", self.model_id) else "low"
        if schema:
            payload["tools"] = [{"type": "function", "function": {"name": TOOL_NAME, "description": "Return structured JSON output matching the provided schema.", "parameters": schema}}]
            payload["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
        return payload

    def _parse(self, result: dict, schema: Optional[dict]) -> tuple[dict, str, int, int]:
        """(파싱 결과, 원문, 입력 토큰, 출력 토큰). 잘린 응답은 STRUCTURED_OUTPUT 오류."""
        if self._provider == "anthropic":
            if result.get("stop_reason") == "max_tokens":
                raise LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)
            blocks = result.get("content") or []
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            tool = next((b.get("input") for b in blocks if b.get("type") == "tool_use"), None)
            usage = result.get("usage") or {}
            tokens = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        else:
            choice = (result.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)
            message = choice.get("message") or {}
            text = message.get("content") or ""
            tool = None
            calls = message.get("tool_calls") or []
            if calls:
                try:
                    tool = json.loads(calls[0].get("function", {}).get("arguments") or "{}")
                except ValueError:
                    raise LLMError("모델이 올바르지 않은 JSON 을 돌려주었습니다.", LLMErrorType.STRUCTURED_OUTPUT) from None
            usage = result.get("usage") or {}
            tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        if isinstance(tool, dict):
            return tool, text, *tokens
        return _parse_json_text(text), text, *tokens

    # ── BedrockProvider 호환 async converse ──────────────────────────
    async def converse(self, messages, system=None, output_schema=None, *, max_tokens=4096, temperature=0.0,
                       raw: bool = False, usage: Optional[dict] = None) -> dict:
        payload = self._payload(messages, system, None if raw else output_schema, max_tokens)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._post, payload)
        if usage is not None:
            usage.update(zip(("input_tokens", "output_tokens"), self._usage(result)))
        if raw:
            return self._raw_text(result)
        parsed, _text, _i, _o = self._parse(result, output_schema)
        return parsed

    def _usage(self, result: dict) -> tuple[int, int]:
        usage = result.get("usage") or {}
        if self._provider == "anthropic":
            return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
        return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)

    def _raw_text(self, result: dict) -> dict:
        """글자 그대로 + 끊김 여부(JSON 추출·잘림 오류 없음)."""
        if self._provider == "anthropic":
            blocks = result.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return {"text": text, "truncated": result.get("stop_reason") == "max_tokens"}
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return {"text": message.get("content") or "", "truncated": choice.get("finish_reason") == "length"}

    def call(self, request: LLMRequest) -> LLMResponse:
        messages = [{"role": "user", "content": [{"text": request.prompt}]}]
        result = self._post(self._payload(messages, request.system, request.json_schema, request.max_tokens))
        parsed, text, input_tokens, output_tokens = self._parse(result, request.json_schema)
        return LLMResponse(
            text=text or json.dumps(parsed, ensure_ascii=False), parsed=parsed if request.json_schema else None,
            model_used=self.model_id, provider=self._provider,
            input_tokens=input_tokens, output_tokens=output_tokens, token_source="api",
        )

    def ping(self) -> None:
        """구조화 도구 호출 1회. 평문/JSON 모드 폴백은 호환성 성공으로 보지 않는다."""
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                  "required": ["ok"], "additionalProperties": False}
        messages = [{"role": "user", "content": [{"text": 'Call the output tool with {"ok": true}.'}]}]
        result = self._post_once(self._payload(messages, None, schema, 1024))
        if self._provider == "anthropic":
            called = any(b.get("type") == "tool_use" and b.get("name") == TOOL_NAME for b in result.get("content", []))
        else:
            message = ((result.get("choices") or [{}])[0].get("message") or {})
            called = any(c.get("function", {}).get("name") == TOOL_NAME for c in message.get("tool_calls", []))
        parsed, _, _, _ = self._parse(result, schema)
        if not called or set(parsed) != {"ok"} or parsed.get("ok") is not True:
            raise LLMError("모델의 구조화 도구 호출 확인에 실패했습니다.", LLMErrorType.STRUCTURED_OUTPUT)

    def available_models(self) -> list[str]:
        """같은 공급자의 모델 목록만 조회한다. 실패하면 설정된 모델을 확인할 수 있게 빈 목록."""
        endpoint = re.sub(r"/(chat/completions|messages)/?$", "/models", self._endpoint)
        if endpoint == self._endpoint:
            return []
        req = urllib.request.Request(endpoint, method="GET", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=min(self._timeout, 10), context=self._ctx) as response:
                data = json.loads(response.read(1_000_000).decode("utf-8"))
            return [m["id"] for m in data.get("data", []) if isinstance(m, dict)
                    and isinstance(m.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", m["id"])]
        except Exception:
            return []  # 오류 본문에 키가 들어 있을 수 있어 화면·로그로 보내지 않는다.

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0


def _json_mode(payload: dict) -> dict:
    """도구 호출 대신 JSON 모드 — 스키마는 마지막 사용자 메시지에 붙인다."""
    out = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
    schema = None
    try:
        schema = payload["tools"][0]["function"]["parameters"]
    except (KeyError, IndexError, TypeError):
        pass
    messages = [dict(m) for m in out.get("messages") or []]
    hint = "\n\nReturn only one JSON object" + (
        " matching this JSON schema:\n" + json.dumps(schema, ensure_ascii=False) if schema else ".")
    for m in reversed(messages):
        if m.get("role") == "user":
            m["content"] = str(m.get("content") or "") + hint
            break
    out["messages"] = messages
    out["response_format"] = {"type": "json_object"}
    return out


def _parse_json_text(text: str) -> dict:
    """Bedrock 평문 경로와 같은 규칙: JSON 블록이면 dict, 아니면 raw_response."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for candidate in ((fence.group(1),) if fence else ()) + ((m.group(0),) if (m := re.search(r"\{.*\}", text, re.DOTALL)) else ()) + (text,):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            continue
    return {"raw_response": text}
