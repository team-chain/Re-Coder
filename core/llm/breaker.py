"""Circuit Breaker — 모든 LLM 경로가 공유하는 단 하나의 구현.

보드 이슈 「LLM 라우터가 두 벌 — 비용 집계와 폴백이 갈라짐」의 일부.
예전에는 이 클래스가 llm/router.py 안에만 있어서, provider_router 를 쓰는
에이전트 경로(deploy/ops/incident/agents)에는 브레이커가 없었다 — 죽은
Bedrock 을 경로마다 다시 두드렸다. 이제 여기 한 벌을 두 진입점이 공유한다.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: 연속 실패가 이 횟수에 닿으면 연다.
CB_FAILURE_THRESHOLD = 3
#: 열린 뒤 이 시간이 지나면 반쯤 닫고 다시 시도한다.
CB_RESET_SECONDS = 60.0


class CircuitBreaker:
    """개별 Provider/모델의 Circuit Breaker."""

    def __init__(self, name: str):
        self.name = name
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at > CB_RESET_SECONDS:
                self._failures = 0
                self._opened_at = None
                return False
            return True

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= CB_FAILURE_THRESHOLD:
                self._opened_at = time.monotonic()
                logger.warning("[CircuitBreaker] %s OPEN (failures=%d)", self.name, self._failures)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None


#: 프로세스 전역 브레이커 저장소 — 라우터 인스턴스가 몇 개든 상태는 한 벌.
#: (라우트마다 LLMProviderRouter() 를 새로 만들어도 죽은 모델을 다시
#:  두드리지 않게 하기 위해서다.)
_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def breaker_for(key: str) -> CircuitBreaker:
    with _BREAKERS_LOCK:
        br = _BREAKERS.get(key)
        if br is None:
            br = CircuitBreaker(key)
            _BREAKERS[key] = br
        return br


def reset_all() -> None:
    """테스트 전용 — 전역 상태를 깨끗하게."""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()
