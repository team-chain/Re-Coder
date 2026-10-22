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


@contextlib.contextmanager
def tagged(tag: str) -> Iterator[None]:
    """공급자는 그대로 두고 이 문맥의 AI 호출 원장 기록에만 tag 를 붙인다(2.0.45 팀 벤치마크 — 실행마다 비용을 나눠 센다)."""
    b = _RUN_TAG.set(tag)
    try:
        yield
    finally:
        _RUN_TAG.reset(b)


# ── 역할별 모델(2.0.45) ─────────────────────────────────────────────────
#: 팀 구성 창에서 사용자가 역할마다 고른 공급자. 한 생성 요청 동안만 유효하다(contextvars — 다른 요청에 새지 않음).
ROLES = ("planner", "dev", "review", "test")
ROLE_LABEL = {"planner": "설계", "dev": "개발", "review": "검토", "test": "테스트"}
#: 역할 모델로 고를 수 있는 공급자 이름(provider_router._spec_provider 와 같은 이름).
ROLE_SPECS = ("quality", "fast", "anthropic", "openai", "compatible")
_ROLE_MODELS: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("recoder_role_models", default=None)
_ROLE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("recoder_role", default=None)


def normalize_role_models(raw: Any) -> dict[str, str]:
    """비신뢰 입력 → {역할: 공급자}. 모르는 역할·공급자는 버린다. quality 는 기본과 같아 적지 않는다."""
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for role, spec in raw.items():
        r = str(role or "").strip().lower()
        v = str(spec or "").strip().lower()
        if r in ROLES and v in ROLE_SPECS and v != "quality":
            out[r] = v
    return out


def role_of(agent: str) -> Optional[str]:
    """엔진의 에이전트 이름 → 역할. planner·review·test 는 그대로, agent-N·dev 는 개발."""
    a = (agent or "").strip().lower()
    if a in ("planner", "review", "test"):
        return a
    if a.startswith("agent-") or a.startswith("dev") or a == "code_agent":
        return "dev"
    return None


@contextlib.contextmanager
def role_models(models: Any) -> Iterator[dict]:
    """이 문맥(한 생성 요청) 동안 역할별 모델을 쓴다. 빈 값이면 기존과 같다."""
    norm = normalize_role_models(models)
    token = _ROLE_MODELS.set(norm or None)
    try:
        yield norm
    finally:
        _ROLE_MODELS.reset(token)


@contextlib.contextmanager
def acting_as(role: Optional[str]) -> Iterator[None]:
    """이 문맥의 AI 호출은 role 역할로 본다(역할 모델을 고를 때·사용량을 셀 때)."""
    token = _ROLE.set(role if role in ROLES else None)
    try:
        yield
    finally:
        _ROLE.reset(token)


def current_role() -> Optional[str]:
    return _ROLE.get()


def role_spec() -> Optional[str]:
    """지금 역할에 사용자가 고른 공급자(없으면 None — 기본 경로)."""
    models = _ROLE_MODELS.get()
    role = _ROLE.get()
    return (models or {}).get(role) if role else None


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
