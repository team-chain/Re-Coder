"""레거시 LLM Router 진입점 — 이제 **provider_router 위의 얇은 어댑터**다.

보드 이슈 「LLM 라우터가 두 벌 — 비용 집계와 폴백이 갈라짐」.

무엇이 사고였나
    이 파일이 자체 폴백 체인·브레이커·비용 기록을 따로 갖고 있었다.
    code_agent/analyzer 는 이쪽을, 에이전트·라우트 들은 provider_router 를
    써서 — 게이트웨이 모드는 절반의 경로에만 적용되고, 브레이커는 반대쪽
    절반에만 있었고, 비용은 두 군데로 갈라져 어느 쪽을 읽어도 전체가
    아니었다.

지금 구조
    실제 구현은 llm/provider_router.py 하나다(브레이커·게이트웨이·비용 원장
    포함). 이 파일은 기존 호출 계약(동기 `get_router().call(LLMRequest)` →
    `LLMResponse`)을 유지하는 어댑터만 남긴다 — 호출부 6곳을 안 고치고
    라우터를 하나로 만드는 가장 작은 수술이다.

동기→비동기 브리지
    레거시 호출자는 동기다(대개 FastAPI 의 async 라우트 안에서 블로킹으로
    불렀다 — 그건 예전부터의 성질이고 여기서 바꾸지 않는다). provider_router
    는 async 라서, 전용 워커 스레드에서 asyncio.run 으로 돌리고 결과를
    기다린다. 워커 스레드에는 이벤트 루프가 없으므로 안전하다.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    from llm.base import (  # noqa: F401 — 재수출 (레거시 import 호환)
        LLMError,
        LLMErrorType,
        LLMRequest,
        LLMResponse,
    )
    from llm.breaker import (  # noqa: F401 — 재수출
        CB_FAILURE_THRESHOLD,
        CB_RESET_SECONDS,
        CircuitBreaker as _CircuitBreaker,
    )
except ImportError:  # 패키지 상대 배치
    from core.llm.base import (  # type: ignore # noqa: F401
        LLMError,
        LLMErrorType,
        LLMRequest,
        LLMResponse,
    )
    from core.llm.breaker import (  # type: ignore # noqa: F401
        CB_FAILURE_THRESHOLD,
        CB_RESET_SECONDS,
        CircuitBreaker as _CircuitBreaker,
    )

logger = logging.getLogger(__name__)

#: 동기 호출자를 위한 브리지 스레드. LLM 호출은 어차피 수 초 단위라
#: 스레드 몇 개면 충분하고, 호출자는 지금까지처럼 블로킹으로 기다린다.
_BRIDGE = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-bridge")


class LLMRouter:
    """레거시 계약 유지용 어댑터 — 폴백·브레이커·비용은 provider_router 가 한다."""

    def __init__(self) -> None:
        try:
            from llm.provider_router import get_provider_router
        except ImportError:
            from core.llm.provider_router import get_provider_router  # type: ignore
        self._pr = get_provider_router()

    def call(
        self,
        request: LLMRequest,
        agent: str = "",
        operation: str = "",
        prefer: str = "primary",
    ) -> LLMResponse:
        """폴백 체인을 시도하고, 전부 실패하면 LLMError 를 던진다.

        성공 시 response.metadata["llm_call_record"] 에 비용 추적 dict 가
        들어간다 — 같은 기록이 llm/cost_ledger 원장에도 적힌다.
        """
        future = _BRIDGE.submit(
            asyncio.run,
            self._pr.call_llm(request, agent=agent, operation=operation, prefer=prefer),
        )
        return future.result()


_router_instance: LLMRouter | None = None
_router_lock = threading.Lock()


def get_router(force_rebuild: bool = False) -> LLMRouter:
    """싱글턴 Router 반환. force_rebuild 는 provider_router 까지 다시 만든다."""
    global _router_instance
    if _router_instance is not None and not force_rebuild:
        return _router_instance
    with _router_lock:
        if _router_instance is not None and not force_rebuild:
            return _router_instance
        if force_rebuild:
            try:
                from llm.provider_router import get_provider_router
            except ImportError:
                from core.llm.provider_router import get_provider_router  # type: ignore
            get_provider_router(force_rebuild=True)
        _router_instance = LLMRouter()
        return _router_instance
