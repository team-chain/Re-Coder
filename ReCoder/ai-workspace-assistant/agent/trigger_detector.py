"""
Trigger Detector (3단계) — 점수 계산으로 분석 트리거 여부 결정.
API 없음. 순수 규칙 기반.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

# ── 크리티컬 에러 (단독으로 즉시 트리거) ─────────────────────────────
# 이 패턴에 매칭되면 다른 조건 없이 score >= TRIGGER_THRESHOLD 보장
_CRITICAL_ERRORS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'Traceback \(most recent call last\)',
        r'\bFATAL\b',
        r'\bPanic\b',                           # Go / Rust panic
        r'panic: ',
        r'fatal error:',
        r'\bSegmentation fault\b',
        r'\bcore dumped\b',
        r'SIGSEGV',
        r'OutOfMemoryError',
        r'OOMKilled',
        r'CrashLoopBackOff',
    ]
]

# ── 고가중치 에러 (다른 신호와 결합해 트리거) ─────────────────────────
_HIGH_WEIGHT_ERRORS = [
    re.compile(p, re.IGNORECASE) for p in [
        # Python
        r'\bModuleNotFoundError\b',
        r'\bImportError\b',
        r'\bSyntaxError\b',
        r'\bTypeError\b',
        r'\bValueError\b',
        r'\bAttributeError\b',
        r'\bNameError\b',
        r'\bKeyError\b',
        r'\bIndexError\b',
        r'\bRuntimeError\b',
        r'No module named',
        # JavaScript / Node / TypeScript
        r'\bReferenceError\b',
        r'\bUnhandledPromiseRejection\b',
        r'\bERR_\w+\b',
        r'Cannot find module',
        r'npm ERR!',
        r'error TS\d+',                         # TypeScript 컴파일 에러
        r'\bcompilation failed\b',
        r'Module not found: Error',
        # HTTP 4xx / 5xx
        r'HTTP/\d(?:\.\d)?\s+[45]\d{2}',
        r'(?:status|code|error)\s*:?\s*[45]\d{2}\b',
        r'(?:GET|POST|PUT|DELETE|PATCH)\s+\S+\s+[45]\d{2}\b',
        # Java / Kotlin / Scala
        r'\bNullPointerException\b',
        r'\bClassNotFoundException\b',
        r'\bStackOverflowError\b',
        r'BUILD FAILED',
        # Rust
        r'error\[E\d+\]',
        # Go
        r'undefined:',
        # Docker / k8s
        r'Error response from daemon',
        r'\bImagePullBackOff\b',
        r'ErrImagePull',
        # Database
        r'(?i)OperationalError',
        r'(?i)IntegrityError',
        r'FATAL:\s+role',
        r'Connection refused',
        # 한국어
        r'에러',
        r'오류',
        r'실패',
        # 공통
        r'FAILED',
        r'\bFAILED\b',
        r'command not found',
        r'Permission denied',
        r'No such file or directory',
    ]
]

# ── 점수 가중치 ────────────────────────────────────────────────────────
_SCORE_CRITICAL_ERROR  = 80   # 크리티컬 에러 → 단독 트리거 가능
_SCORE_HIGH_ERROR      = 45   # 고가중치 에러
_SCORE_NEW_COMMAND     = 25   # 새 터미널 명령
_SCORE_REPEAT_ERROR    = 25   # 동일 에러 반복
_SCORE_TEXT_CHANGE     = 15   # 화면 텍스트 변화
_SCORE_WINDOW_SWITCH   = 10   # 윈도우 전환

TRIGGER_THRESHOLD = 70        # score >= 70 시 분석 트리거

# Cool-down: 동일 에러 N초 이내 재트리거 차단
_COOLDOWN_SECONDS = 60

# 반복 에러 카운터 자동 리셋 주기 (초) — 오래된 에러가 쌓이는 것 방지
_OCCURRENCE_TTL_SECONDS = 300

_last_trigger_times:     dict[str, float] = defaultdict(float)
_error_occurrence:       dict[str, int]   = defaultdict(int)
_error_first_seen:       dict[str, float] = defaultdict(float)  # 발생 시각 추적


def _error_fingerprint(errors: list[str]) -> str:
    return "|".join(sorted(set(e.strip()[:50] for e in errors)))


def _is_critical_error(errors: list[str]) -> bool:
    for err in errors:
        for pattern in _CRITICAL_ERRORS:
            if pattern.search(err):
                return True
    return False


def _is_high_weight_error(errors: list[str]) -> bool:
    for err in errors:
        for pattern in _HIGH_WEIGHT_ERRORS:
            if pattern.search(err):
                return True
    return False


def _in_cooldown(fingerprint: str) -> bool:
    last = _last_trigger_times.get(fingerprint, 0.0)
    return (time.time() - last) < _COOLDOWN_SECONDS


def _cleanup_stale_occurrences() -> None:
    """오래된 에러 발생 카운터를 정리해 메모리 누수와 오탐 방지."""
    now = time.time()
    stale = [
        fp for fp, first in _error_first_seen.items()
        if (now - first) > _OCCURRENCE_TTL_SECONDS
    ]
    for fp in stale:
        _error_occurrence.pop(fp, None)
        _error_first_seen.pop(fp, None)


def compute_score(
    errors:          list[str],
    new_commands:    list[str],
    text_changed:    bool,
    window_switched: bool,
) -> int:
    _cleanup_stale_occurrences()
    score = 0

    if errors:
        fp = _error_fingerprint(errors)

        # 첫 발생 시각 기록
        if fp not in _error_first_seen:
            _error_first_seen[fp] = time.time()
        _error_occurrence[fp] += 1

        if _is_critical_error(errors):
            score += _SCORE_CRITICAL_ERROR      # 80 → 단독으로 임계값 초과
        elif _is_high_weight_error(errors):
            score += _SCORE_HIGH_ERROR          # 45
        if _error_occurrence[fp] >= 2:
            score += _SCORE_REPEAT_ERROR        # 반복 에러 추가 가산

    if new_commands:
        score += _SCORE_NEW_COMMAND
    if text_changed:
        score += _SCORE_TEXT_CHANGE
    if window_switched:
        score += _SCORE_WINDOW_SWITCH

    return score


def should_trigger(
    errors:          list[str],
    new_commands:    list[str],
    text_changed:    bool,
    window_switched: bool,
    uia_failure:     bool,
) -> tuple[bool, int, bool]:
    """
    Returns:
        (trigger, score, need_capture)
        trigger      — True면 분석 실행
        score        — 계산된 점수
        need_capture — True면 Capture Manager 필요 (UIA 실패 시)
    """
    score = compute_score(errors, new_commands, text_changed, window_switched)

    if score < TRIGGER_THRESHOLD:
        return False, score, False

    fp = _error_fingerprint(errors) if errors else "_no_error_"
    if _in_cooldown(fp):
        return False, score, False

    _last_trigger_times[fp] = time.time()
    need_capture = uia_failure  # UIA 실패 시만 캡처 필요

    return True, score, need_capture


def notify_resolved(errors: list[str]) -> None:
    """에러가 해결됐을 때 호출 — 쿨다운과 카운터를 즉시 리셋해 다음 에러를 빠르게 감지."""
    if not errors:
        return
    fp = _error_fingerprint(errors)
    _last_trigger_times.pop(fp, None)
    _error_occurrence.pop(fp, None)
    _error_first_seen.pop(fp, None)


def reset_cooldown(fingerprint: str) -> None:
    """테스트 또는 수동 리셋용."""
    _last_trigger_times.pop(fingerprint, None)
    _error_occurrence.pop(fingerprint, None)
    _error_first_seen.pop(fingerprint, None)
