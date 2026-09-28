"""LLM 라우터 통합 — 보드 이슈 「LLM 라우터가 두 벌 — 비용 집계와 폴백이 갈라짐」.

무엇이 사고였나
    llm/router.py(브레이커 있음·게이트웨이 없음)와 llm/provider_router.py
    (게이트웨이 있음·브레이커 없음)가 각자 폴백과 비용 기록을 갖고 있었다.
    code_agent 경로와 에이전트 경로가 서로 다른 라우터를 타서, 어떤 보호도
    전 경로에 적용되지 않았고 비용은 두 군데로 갈라졌다.

여기서 고정하는 것 (DoD)
    1. 모든 LLM 호출이 한 라우터(provider_router)를 거친다 — 레거시
       get_router().call 은 어댑터일 뿐이다.
    2. 비용 집계가 한 곳(llm/cost_ledger)으로 모인다 — 어느 진입점이든.
    3. circuit breaker 가 전 경로에 적용된다 — 상태는 프로세스 전역이라
       인스턴스를 새로 만들어도 유지된다.
    4. 게이트웨이 모드가 레거시 경로에도 적용된다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from llm import breaker as breaker_mod  # noqa: E402
from llm import cost_ledger  # noqa: E402
from llm import provider_router as pr_mod  # noqa: E402
from llm import router as router_mod  # noqa: E402
from llm.base import LLMError, LLMRequest  # noqa: E402


class _FakeProvider:
    """converse 만 있으면 되는 가짜 Bedrock/Gateway."""

    def __init__(self, name: str, model_id: str, fail: bool = False, result=None):
        self._name = name
        self.model_id = model_id
        self.fail = fail
        self.result = result if result is not None else {"text": "ok"}
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def converse(self, messages, system=None, output_schema=None, *, max_tokens=4096, temperature=0.0):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return self.result


class _FakeGemini:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def generate(self, prompt, schema=None, *, max_tokens=4096, temperature=0.0):
        self.calls += 1
        if self.fail:
            raise RuntimeError("gemini down")
        return {"text": "gemini-ok"}


@pytest.fixture(autouse=True)
def _clean_state():
    breaker_mod.reset_all()
    cost_ledger.reset()
    router_mod._router_instance = None
    pr_mod._singleton = None
    yield
    breaker_mod.reset_all()
    cost_ledger.reset()
    router_mod._router_instance = None
    pr_mod._singleton = None


def _router_with(sonnet=None, haiku=None, gemini=None) -> pr_mod.LLMProviderRouter:
    r = pr_mod.get_provider_router(force_rebuild=True)
    if sonnet is not None:
        r._bedrock_sonnet = sonnet
    if haiku is not None:
        r._bedrock_haiku = haiku
    if gemini is not None:
        r._gemini = gemini
    return r


# ---------------------------------------------------------------------------
# 1. 한 라우터
# ---------------------------------------------------------------------------


def test_레거시_call_은_provider_router_를_그대로_탄다() -> None:
    sonnet = _FakeProvider("bedrock", "sonnet-x")
    _router_with(sonnet=sonnet, gemini=_FakeGemini())

    resp = router_mod.get_router().call(
        LLMRequest(prompt="hello"), agent="code_agent", operation="t",
    )

    assert sonnet.calls == 1, "레거시 경로가 provider_router 의 provider 를 안 탔다"
    assert resp.text == "ok"
    assert resp.model_used == "sonnet-x"
    assert resp.metadata["llm_call_record"]["provider"] == "bedrock"


def test_prefer_fast_는_haiku_로_간다() -> None:
    haiku = _FakeProvider("bedrock", "haiku-x")
    _router_with(sonnet=_FakeProvider("bedrock", "sonnet-x"), haiku=haiku, gemini=_FakeGemini())

    resp = router_mod.get_router().call(LLMRequest(prompt="hi"), prefer="fast")
    assert haiku.calls == 1
    assert resp.model_used == "haiku-x"


def test_전부_실패하면_LLMError() -> None:
    _router_with(
        sonnet=_FakeProvider("bedrock", "s", fail=True),
        gemini=_FakeGemini(fail=True),
    )
    with pytest.raises(LLMError):
        router_mod.get_router().call(LLMRequest(prompt="x"))


# ---------------------------------------------------------------------------
# 2. 비용 한 곳
# ---------------------------------------------------------------------------


def test_두_진입점의_비용이_같은_원장에_쌓인다() -> None:
    r = _router_with(
        sonnet=_FakeProvider("bedrock", "sonnet-x"),
        haiku=_FakeProvider("bedrock", "haiku-x"),
        gemini=_FakeGemini(),
    )

    #: 진입점 1 — 에이전트 스타일
    asyncio.run(r.complete("p1", model_preference="haiku"))
    #: 진입점 2 — 레거시 라우터 스타일
    router_mod.get_router().call(LLMRequest(prompt="p2"))

    records = cost_ledger.all_records()
    assert len(records) == 2, "두 진입점 중 하나가 원장을 비껴갔다"
    assert {r_["model"] for r_ in records} == {"haiku-x", "sonnet-x"}
    assert cost_ledger.total_cost_usd() >= 0.0


# ---------------------------------------------------------------------------
# 3. 브레이커 전 경로 + 전역 상태
# ---------------------------------------------------------------------------


def test_한_경로에서_열린_브레이커가_다른_경로에도_적용된다() -> None:
    sonnet = _FakeProvider("bedrock", "sonnet-x", fail=True)
    gemini = _FakeGemini()
    r = _router_with(sonnet=sonnet, gemini=gemini)

    #: 에이전트 경로에서 임계까지 실패시켜 브레이커를 연다.
    for _ in range(breaker_mod.CB_FAILURE_THRESHOLD):
        asyncio.run(r.call_primary("p"))
    opened_calls = sonnet.calls

    #: 레거시 경로 — 열린 브레이커 때문에 Bedrock 을 건드리지 않아야 한다.
    resp = router_mod.get_router().call(LLMRequest(prompt="p"))
    assert sonnet.calls == opened_calls, "열린 브레이커인데 레거시 경로가 다시 두드렸다"
    assert resp.fallback_used is True
    assert resp.provider == "gemini"


def test_브레이커_상태는_새_인스턴스에도_유지된다() -> None:
    sonnet = _FakeProvider("bedrock", "sonnet-x", fail=True)
    r1 = _router_with(sonnet=sonnet, gemini=_FakeGemini())
    for _ in range(breaker_mod.CB_FAILURE_THRESHOLD):
        asyncio.run(r1.call_primary("p"))
    calls_after_open = sonnet.calls

    #: 라우트가 새로 만든 인스턴스 — 예전 구현이라면 상태가 초기화됐다.
    r2 = pr_mod.LLMProviderRouter()
    r2._bedrock_sonnet = sonnet
    r2._gemini = _FakeGemini()
    asyncio.run(r2.call_primary("p"))
    assert sonnet.calls == calls_after_open, "인스턴스를 새로 만들자 죽은 모델을 다시 두드렸다"


# ---------------------------------------------------------------------------
# 4. 게이트웨이 전 경로
# ---------------------------------------------------------------------------


def test_게이트웨이_모드가_레거시_경로에도_적용된다(monkeypatch) -> None:
    monkeypatch.setenv("RECODER_LLM_GATEWAY_URL", "https://gw.example/llm")
    #: 게이트웨이 provider 를 가짜로 — 네트워크 없이 전환 여부만 본다.
    import llm.gateway_provider as gw

    created: list[str] = []

    class _FakeGateway(_FakeProvider):
        def __init__(self, model_id: str = "gw-model"):
            super().__init__("gateway", model_id)
            created.append(model_id)

    monkeypatch.setattr(gw, "GatewayProvider", _FakeGateway)
    monkeypatch.setattr(gw, "gateway_enabled", lambda: True)

    r = pr_mod.get_provider_router(force_rebuild=True)
    r._gemini = _FakeGemini()
    assert created, "게이트웨이 모드인데 GatewayProvider 로 전환되지 않았다"
    assert r._bedrock_sonnet.provider_name == "gateway"

    resp = router_mod.get_router().call(LLMRequest(prompt="p"))
    assert resp.provider == "gateway", "레거시 경로가 게이트웨이를 안 탔다"
