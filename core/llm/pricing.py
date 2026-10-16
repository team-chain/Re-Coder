"""모델 단가(2.0.36) — 원장이 비용을 적을 때 쓰는 한 곳.

순서
    1. 사용자가 정한 단가 `RECODER_MODEL_PRICES` (JSON {모델 ID: {"input": 달러/백만 토큰, "output": …}})
       확장 설정 `recoder.ai.prices` 가 이 값으로 들어온다.
    2. Bedrock 단가표(bedrock_provider.COST_PER_1K_TOKENS) — Claude 모델.
    3. Claude 계열(bedrock·gateway·anthropic)인데 표에 없는 모델 → 기존처럼 Sonnet 단가로 어림(assumed).
    4. 그 밖(OpenAI·OpenAI 호환)은 **모른다** — 0 달러로 적지 않고 None 으로 남긴다.
       값이 자주 바뀌는 외부 단가를 코드에 박아 두면 틀린 비용을 "정확한 값"처럼 보여 주게 된다.

Gemini 대체 경로는 예전부터 0 으로 적었다(무료 한도 안에서 쓰는 비상 경로). 그대로 둔다.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

#: Sonnet 기준 어림 단가(달러/백만 토큰) — 기존 _estimate_cost 의 기본값과 같다.
_ASSUMED_CLAUDE = (3.0, 15.0)
_CLAUDE_FAMILY = ("bedrock", "gateway", "anthropic")


def configured_prices() -> dict[str, tuple[float, float]]:
    """사용자 단가. 형식이 틀린 항목은 버린다(비용 계산이 통째로 실패하지 않게)."""
    raw = os.environ.get("RECODER_MODEL_PRICES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, tuple[float, float]] = {}
    if not isinstance(data, dict):
        return out
    for model, rate in data.items():
        if not isinstance(model, str) or not isinstance(rate, dict):
            continue
        try:
            incoming, outgoing = float(rate["input"]), float(rate["output"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) and 0 <= v < 10_000 for v in (incoming, outgoing)):
            out[model.strip()] = (incoming, outgoing)
    return out


def _bedrock_table() -> dict:
    try:
        from llm.bedrock_provider import COST_PER_1K_TOKENS
    except ImportError:  # pragma: no cover
        try:
            from core.llm.bedrock_provider import COST_PER_1K_TOKENS  # type: ignore
        except ImportError:
            return {}
    return COST_PER_1K_TOKENS


def price_for(provider: str, model: str) -> tuple[Optional[tuple[float, float]], str]:
    """((입력, 출력) 달러/백만 토큰 또는 None, 출처)."""
    provider = (provider or "").lower()
    model = (model or "").strip()
    if provider == "gemini":
        return (0.0, 0.0), "free_fallback"
    user = configured_prices()
    if model in user:
        return user[model], "configured"
    table = _bedrock_table()
    if model in table:
        rate = table[model]
        return (rate["input"] * 1000, rate["output"] * 1000), "list_price"
    if provider in _CLAUDE_FAMILY:
        return _ASSUMED_CLAUDE, "assumed"
    return None, "unconfigured"


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> tuple[Optional[float], str]:
    rate, source = price_for(provider, model)
    if rate is None:
        return None, source
    return (max(0, input_tokens) * rate[0] + max(0, output_tokens) * rate[1]) / 1_000_000, source
