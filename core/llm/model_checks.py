"""설정 화면의 모델 확인. 키/응답 본문은 저장하지 않고 실제 도구 호출 결과만 유지한다."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .api_key_provider import ApiKeyProvider, DEFAULT_MODELS, PROVIDERS
from .base import LLMRequest

# 공식 대체 모델은 후보일 뿐이다. 실제 키로 도구 호출을 통과해야 선택할 수 있다.
# https://developers.openai.com/api/docs/deprecations
_CANDIDATES = {
    "openai": ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-5-mini"],
    "anthropic": ["claude-sonnet-5", "claude-haiku-4-5"],
}
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_CHECK_LOCK = threading.Lock()


def retirement(provider: str, model: str) -> str:
    if provider == "openai" and re.fullmatch(r"(?:gpt-5(?:-mini|-nano|-pro)?|o3(?:-pro)?)(?:-\d{4}-\d{2}-\d{2})?", model):
        return "2026-12-11"
    return ""


def _candidates(router) -> list[str]:
    provider = router._bedrock_sonnet.provider_name
    ids = [router._bedrock_sonnet.model_id, router._bedrock_haiku.model_id]
    ids += list(DEFAULT_MODELS.get(provider, ())) + _CANDIDATES.get(provider, [])
    return list(dict.fromkeys(m for m in ids if m))


def status(router) -> dict:
    provider = router._bedrock_sonnet.provider_name
    cached = getattr(router, "_model_check_status", None)
    if cached:
        return cached
    return {"checked_at": None, "configurable": provider in PROVIDERS,
            "catalog": [{"model": m, "ready": False, "checked": False, "retirement": retirement(provider, m)}
                        for m in _candidates(router)]}


def check(router) -> dict:
    """동시 중복 확인은 직렬화하고, 후보당 1회(최대 12개)만 호출한다."""
    with _CHECK_LOCK:
        provider = router._bedrock_sonnet.provider_name
        candidates = _candidates(router)
        if provider in PROVIDERS:
            source = ApiKeyProvider(provider, router._bedrock_sonnet.model_id)
            listed = source.available_models()
            if provider == "openai":
                listed = [m for m in listed if re.fullmatch(r"gpt-(?:5\.6|6(?:\.1)?)-(?:sol|terra|luna|astra)", m)]
            elif provider == "anthropic":
                listed = [m for m in listed if m.startswith("claude-")]
            candidates = list(dict.fromkeys(candidates + listed))[:12]

        def probe(model: str) -> dict:
            end = retirement(provider, model)
            row = {"model": model, "ready": False, "checked": False, "retirement": end}
            if end:
                return row
            row["checked"] = True
            try:
                if provider in PROVIDERS:
                    p = ApiKeyProvider(provider, model)
                    p._timeout = min(p._timeout, 20)
                    p.ping()
                else:
                    p = next(p for p in (router._bedrock_sonnet, router._bedrock_haiku) if p.model_id == model)
                    response = p.call(LLMRequest(prompt='Return {"ok": true} using the output tool.', json_schema=_SCHEMA, max_tokens=256))
                    if response.parsed != {"ok": True}:
                        return row
                row["ready"] = True
            except Exception:
                pass  # 공급자 예외/본문은 키를 포함할 수 있으므로 노출하지 않는다.
            return row

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="model-check") as pool:
            catalog = list(pool.map(probe, candidates))
        router._model_check_status = {"checked_at": int(time.time() * 1000), "catalog": catalog,
                                      "configurable": provider in PROVIDERS}
        return router._model_check_status
