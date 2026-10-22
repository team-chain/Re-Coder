"""작업 등급(2.0.36) — 같은 AI 라도 일의 무게에 따라 공급자를 나눈다.

두 등급
    품질(quality)  코드 생성·보안 수정·교정처럼 결과가 곧 코드가 되는 일. 기존 공급자 그대로.
    절약(economy)  계획 주제·요약·오류 분류·테스트 초안처럼 사람이 다시 확인하는 가벼운 일.

정하지 않은 작업 이름은 언제나 품질 등급이다(새 호출이 몰래 싼 모델로 가지 않게).
절약 공급자를 따로 고르지 않으면 두 등급은 같은 공급자를 쓴다 — 기존 동작과 같다.

비교 도구는 `forced_provider` 로 한 실행의 **모든** 호출을 한 공급자로 고정하고, `run_tag` 로
그 실행의 원장 기록만 골라낸다. 두 값은 contextvars 라서 다른 요청에 새지 않는다.
작업 스레드로 넘길 때는 `bind` 로 감싸야 같은 값을 본다(ThreadPoolExecutor 는 문맥을 복사하지 않는다).
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Callable, Iterator, Optional

ECONOMY = "economy"
QUALITY = "quality"
TIERS = (ECONOMY, QUALITY)
TIER_LABEL = {ECONOMY: "절약", QUALITY: "품질"}

#: 절약 등급으로 보내도 되는 작업 — 결과를 사람이 다시 보거나, 틀려도 품질 등급 작업이 바로잡는 일.
ECONOMY_OPERATIONS: frozenset[str] = frozenset({
    "generate_plan_topics",      # 설계 질문 주제 뽑기
    "summarize_request",         # 요청 한 줄 요약
    "summarize_scan",            # 스캔 결과 요약
    "summarize_scan_results",
    "classify_error",            # 오류 종류 분류
    "augment_candidates",        # 원인 후보 문장 보강
    "generate_test_draft",       # 테스트 파일 초안
})

#: 비교 도구가 "교정 횟수"로 세는 작업(첫 생성 뒤에 고치려고 다시 부른 호출).
REPAIR_OPERATIONS: frozenset[str] = frozenset({
    "generate_code_consistency",
    "generate_code_fix",
    "review_code_manifest",
    "repair_fast",
    "repair_primary",
})


def tier_for(operation: str) -> str:
    return ECONOMY if (operation or "") in ECONOMY_OPERATIONS else QUALITY


_FORCED: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("recoder_forced_provider", default=None)
_RUN_TAG: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("recoder_run_tag", default=None)


def forced_provider() -> Optional[str]:
    return _FORCED.get()


def run_tag() -> Optional[str]:
    return _RUN_TAG.get()


@contextlib.contextmanager
def pinned(spec: str, tag: str) -> Iterator[None]:
    """이 문맥 안의 모든 AI 호출을 spec 공급자로 고정하고 원장 기록에 tag 를 붙인다."""
    a = _FORCED.set(spec)
    b = _RUN_TAG.set(tag)
    try:
        yield
    finally:
        _RUN_TAG.reset(b)
        _FORCED.reset(a)


def bind(fn: Callable[..., Any]) -> Callable[..., Any]:
    """지금 문맥(고정 공급자·실행 표시)을 작업 스레드로 가져가는 감싸개.

    같은 Context 객체는 두 스레드가 동시에 들어갈 수 없으므로 호출마다 복사본을 쓴다.
    """
    snapshot = contextvars.copy_context()

    def run(*args: Any, **kwargs: Any) -> Any:
        return snapshot.copy().run(fn, *args, **kwargs)

    return run


def is_test_file(path: str) -> bool:
    """테스트 파일 경로인가 — 생성 엔진이 테스트 초안을 절약 등급으로 쓸지 정할 때."""
    p = (path or "").replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if not name:
        return False
    if any(seg in ("tests", "test", "__tests__", "e2e") for seg in p.split("/")[:-1]):
        return True
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py") \
        or name.endswith("_test.go") or any(f".{k}." in name for k in ("test", "spec"))
