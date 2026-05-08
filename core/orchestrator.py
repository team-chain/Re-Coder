"""
Orchestrator FSM (설계서 v6.4 §7) — 규칙 기반 상태 머신.

핵심 정책:
  - 외부 LLM 판단 제외 (Groq 등). 모든 전이는 명시적 규칙으로만.
  - DEPLOY_FAILED 무한 루프 방지 (§7.1) — 3회 실패 시 IDLE 복귀.
  - diff 적용 실패는 hash_mismatch / context_mismatch / unknown 으로 구분 (§7.2).
  - 전역 이벤트(USER_QUESTION/IGNORE/CANCEL/RESOLVED) 는 모든 상태에서 처리 가능.
  - v6.4 Stage 2 추가: SECURITY_SCANNING, DOCKER_BUILDING, HEALTH_CHECKING 상태.

설계서의 상태는 schemas.OrchestratorState에 정의되어 있다. 이 모듈은
허용 전이표(_VALID_TRANSITIONS)와 사이드이펙트 없는 transition() 함수를 제공한다.
"""

from __future__ import annotations

from typing import Optional

from schemas import OrchestratorState as S


class IllegalTransition(Exception):
    """허용되지 않은 상태 전이 시도."""

    def __init__(self, src: S, event: str, dst: Optional[S] = None) -> None:
        self.src = src
        self.event = event
        self.dst = dst
        super().__init__(f"illegal transition: {src.value} --{event}--> {dst.value if dst else '?'}")


# ── 전이표 ────────────────────────────────────────────────────────────
# (src, event) -> dst
# event는 추상 명령 — 모니터/에이전트가 이 이벤트를 발생시키면 FSM이 새 상태를 반환.
_VALID_TRANSITIONS: dict[tuple[S, str], S] = {
    # IDLE
    (S.IDLE,                "ERROR_FOUND"):              S.ERROR_DETECTED,
    (S.IDLE,                "TASK_CHANGED"):             S.IDLE,
    (S.IDLE,                "USER_QUESTION"):            S.ANALYZING,

    # ERROR_DETECTED
    (S.ERROR_DETECTED,      "USER_REQUEST_FIX"):         S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_EXPLAIN"):     S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_REQUEST_DEPLOY"):      S.ANALYZING,
    (S.ERROR_DETECTED,      "USER_IGNORE"):              S.IDLE,
    (S.ERROR_DETECTED,      "RESOLVED"):                 S.IDLE,

    # ANALYZING
    (S.ANALYZING,           "PATCH_PROPOSED"):           S.CODE_PATCH_PROPOSED,
    (S.ANALYZING,           "INFRA_PROPOSED"):           S.INFRA_PROPOSED,
    (S.ANALYZING,           "DEPLOY_PROPOSED"):          S.DEPLOY_PROPOSED,
    (S.ANALYZING,           "WAIT_USER_INPUT"):          S.WAITING_USER_ACTION,
    (S.ANALYZING,           "ANALYSIS_FAILED"):          S.IDLE,
    (S.ANALYZING,           "USER_IGNORE"):              S.IDLE,

    # WAITING_USER_ACTION (설계서 v6.4 §7.1 — fix_code / infra / deploy / explain / ignore)
    (S.WAITING_USER_ACTION, "USER_REQUEST_FIX"):         S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_EXPLAIN"):     S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_REQUEST_DEPLOY"):      S.ANALYZING,
    (S.WAITING_USER_ACTION, "USER_IGNORE"):              S.IDLE,
    (S.WAITING_USER_ACTION, "RESOLVED"):                 S.IDLE,

    # CODE_PATCH_PROPOSED
    (S.CODE_PATCH_PROPOSED, "USER_APPROVE_PATCH"):       S.APPLYING_PATCH,
    (S.CODE_PATCH_PROPOSED, "USER_REJECT_PATCH"):        S.IDLE,
    (S.CODE_PATCH_PROPOSED, "USER_IGNORE"):              S.IDLE,

    # APPLYING_PATCH
    (S.APPLYING_PATCH,      "PATCH_APPLIED"):            S.CODE_READY,
    (S.APPLYING_PATCH,      "PATCH_HASH_MISMATCH"):      S.ANALYZING,   # 재분석
    (S.APPLYING_PATCH,      "PATCH_CONTEXT_MISMATCH"):   S.CODE_PATCH_PROPOSED,  # 사용자가 수동 편집 후 재시도
    (S.APPLYING_PATCH,      "PATCH_UNKNOWN_ERROR"):      S.ROLLBACK,

    # CODE_READY
    (S.CODE_READY,          "USER_REQUEST_INFRA"):       S.ANALYZING,
    (S.CODE_READY,          "USER_REQUEST_DEPLOY"):      S.DEPLOY_PROPOSED,
    (S.CODE_READY,          "USER_IGNORE"):              S.IDLE,
    (S.CODE_READY,          "RESOLVED"):                 S.IDLE,

    # INFRA_PROPOSED
    (S.INFRA_PROPOSED,      "USER_APPROVE_INFRA"):       S.INFRA_READY,
    (S.INFRA_PROPOSED,      "USER_REJECT_INFRA"):        S.IDLE,

    # INFRA_READY (Stage 2 security scanning 추가)
    (S.INFRA_READY,         "START_SECURITY_SCAN"):      S.SECURITY_SCANNING,
    (S.INFRA_READY,         "USER_REQUEST_DEPLOY"):      S.DEPLOY_PROPOSED,
    (S.INFRA_READY,         "USER_IGNORE"):              S.IDLE,

    # SECURITY_SCANNING (v6.4 신규: Trivy/Hadolint)
    (S.SECURITY_SCANNING,   "SCAN_PASSED"):              S.INFRA_READY,
    (S.SECURITY_SCANNING,   "SCAN_FAILED"):              S.WAITING_USER_ACTION,

    # DEPLOY_PROPOSED (Stage 2 진입점)
    (S.DEPLOY_PROPOSED,     "USER_APPROVE_DEPLOY"):      S.DOCKER_BUILDING,
    (S.DEPLOY_PROPOSED,     "USER_REJECT_DEPLOY"):       S.IDLE,

    # DOCKER_BUILDING (v6.4 신규: docker build)
    (S.DOCKER_BUILDING,     "BUILD_SUCCESS"):            S.HEALTH_CHECKING,
    (S.DOCKER_BUILDING,     "BUILD_FAILED"):             S.DEPLOY_FAILED,

    # HEALTH_CHECKING (v6.4 신규: Health Check)
    (S.HEALTH_CHECKING,     "HEALTH_OK"):                S.DEPLOYED,
    (S.HEALTH_CHECKING,     "HEALTH_FAILED"):            S.ROLLBACK,

    # DEPLOY_FAILED
    (S.DEPLOY_FAILED,       "RETRY"):                    S.ANALYZING,   # 새 해결책 생성
    (S.DEPLOY_FAILED,       "GIVE_UP"):                  S.IDLE,
    (S.DEPLOY_FAILED,       "USER_IGNORE"):              S.IDLE,

    # DEPLOYED
    (S.DEPLOYED,            "RESOLVED"):                 S.IDLE,
    (S.DEPLOYED,            "ROLLBACK_REQUESTED"):       S.ROLLBACK,

    # ROLLBACK
    (S.ROLLBACK,            "ROLLBACK_OK"):              S.IDLE,
    (S.ROLLBACK,            "ROLLBACK_FAILED"):          S.IDLE,
}


# 전역 이벤트 — 모든 상태에서 IDLE로 갈 수 있는 escape hatch들 (§7.3)
_GLOBAL_EVENTS = {"USER_CANCEL", "GLOBAL_RESET"}


def transition(src: S, event: str) -> S:
    """
    설계서 §7 — 상태 전이.
    허용되지 않은 전이는 IllegalTransition 예외.
    USER_CANCEL/GLOBAL_RESET 은 어느 상태에서나 IDLE로 갈 수 있다 (§7.3).
    """
    if event in _GLOBAL_EVENTS:
        return S.IDLE
    key = (src, event)
    if key not in _VALID_TRANSITIONS:
        raise IllegalTransition(src, event)
    return _VALID_TRANSITIONS[key]


def is_terminal(state: S) -> bool:
    """완료/대기 상태인지 — 위젯이 다음 입력을 받을 준비가 됐는지 판단."""
    return state in {S.IDLE, S.CODE_READY, S.DEPLOYED, S.WAITING_USER_ACTION}


# ── DEPLOY_FAILED 무한 루프 가드 (§7.1) ───────────────────────────────
# Deploy Agent가 자체 카운터를 가지고 있지만, FSM 레벨에서도 한 번 더 보호.

_MAX_DEPLOY_RETRIES = 3
_deploy_retry_counter: dict[str, int] = {}


def record_deploy_failure(plan_id: str) -> bool:
    """
    실패 기록. 임계값 도달 시 True 반환 (이후 RETRY는 거부해야 함).
    """
    if not plan_id:
        return False
    _deploy_retry_counter[plan_id] = _deploy_retry_counter.get(plan_id, 0) + 1
    return _deploy_retry_counter[plan_id] >= _MAX_DEPLOY_RETRIES


def can_retry_deploy(plan_id: str) -> bool:
    return _deploy_retry_counter.get(plan_id, 0) < _MAX_DEPLOY_RETRIES


def reset_deploy_retries(plan_id: str = "") -> None:
    if not plan_id:
        _deploy_retry_counter.clear()
        return
    _deploy_retry_counter.pop(plan_id, None)
