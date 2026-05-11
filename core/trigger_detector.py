"""
Trigger Detector (5단계) — 점수 계산으로 분석 트리거 여부 결정.
API 없음. 순수 규칙 기반.

설계서 v5 §6.4 — Cool-down + Circuit Breaker.
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict


# ── 크리티컬 에러 (단독으로 즉시 트리거) ─────────────────────────────
_CRITICAL_ERRORS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'Traceback \(most recent call last\)',
        r'\bFATAL\b',
        r'\bPanic\b',
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

# ── 고가중치 에러 ─────────────────────────────────────────────────────
_HIGH_WEIGHT_ERRORS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\bModuleNotFoundError\b', r'\bImportError\b', r'\bSyntaxError\b',
        r'\bTypeError\b', r'\bValueError\b', r'\bAttributeError\b',
        r'\bNameError\b', r'\bKeyError\b', r'\bIndexError\b',
        r'\bRuntimeError\b', r'\b\w+Error\b', r'\b\w+Exception\b',
        r'No module named',
        r'\bReferenceError\b', r'\bUnhandledPromiseRejection\b',
        r'\bERR_\w+\b', r'Cannot find module', r'npm ERR!',
        r'error TS\d+', r'\bcompilation failed\b', r'Module not found: Error',
        r'HTTP/\d(?:\.\d)?\s+[45]\d{2}',
        r'(?:status|code|error)\s*:?\s*[45]\d{2}\b',
        r'(?:GET|POST|PUT|DELETE|PATCH)\s+\S+\s+[45]\d{2}\b',
        r'\bNullPointerException\b', r'\bClassNotFoundException\b',
        r'\bStackOverflowError\b', r'BUILD FAILED',
        r'error\[E\d+\]', r'undefined:',
        r'Error response from daemon', r'\bImagePullBackOff\b', r'ErrImagePull',
        r'(?i)OperationalError', r'(?i)IntegrityError',
        r'FATAL:\s+role', r'Connection refused',
        r'에러', r'오류', r'실패',
        r'FAILED', r'\bFAILED\b',
        r'command not found', r'Permission denied', r'No such file or directory',
    ]
]

# ── 점수 가중치 (설계서 v5 §6.4) ───────────────────────────────────────
# 기획서 명세 그대로:
#   - 크리티컬 에러: 단독으로 trigger_score >= 70 보장 (80으로 안전 마진)
#   - 고가중치 에러 키워드: +50
#   - 새 터미널 명령 실행: +30
#   - 동일 에러 2회 이상 반복: +30
#   - 텍스트 변화량 큼: +20
#   - 업무 앱 창 전환: +10
_SCORE_CRITICAL_ERROR  = 80
_SCORE_HIGH_ERROR      = 50
_SCORE_NEW_COMMAND     = 30
_SCORE_REPEAT_ERROR    = 30
_SCORE_TEXT_CHANGE     = 20
_SCORE_WINDOW_SWITCH   = 10

TRIGGER_THRESHOLD = 70

_COOLDOWN_SECONDS = 60
_OCCURRENCE_TTL_SECONDS = 300

# ── Circuit Breaker (설계서 v5 §6.4) ──────────────────────────────────
# 연속 실패 N회 → Circuit Breaker 작동, 호출 일시 차단.
_CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get('RECODER_CIRCUIT_FAILURE_THRESHOLD', '3'))
_CIRCUIT_OPEN_SECONDS      = int(os.environ.get('RECODER_CIRCUIT_OPEN_SECONDS', '600'))

_last_trigger_times:     dict[str, float] = defaultdict(float)
_error_occurrence:       dict[str, int]   = defaultdict(int)
_error_first_seen:       dict[str, float] = defaultdict(float)
_consecutive_failures:   dict[str, int]   = defaultdict(int)
_circuit_open_until:     dict[str, float] = defaultdict(float)


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


# ── Circuit Breaker API ───────────────────────────────────────────────

def _is_circuit_open(fingerprint: str) -> bool:
    open_until = _circuit_open_until.get(fingerprint, 0.0)
    if open_until == 0.0:
        return False
    if time.time() >= open_until:
        _circuit_open_until.pop(fingerprint, None)
        return False
    return True


def record_failure(fingerprint: str) -> bool:
    """AI/패치/배포 실패 시 호출. 임계값 도달 시 회로 차단기 열림."""
    if not fingerprint:
        return False
    _consecutive_failures[fingerprint] += 1
    if _consecutive_failures[fingerprint] >= _CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until[fingerprint] = time.time() + _CIRCUIT_OPEN_SECONDS
        return True
    return False


def record_success(fingerprint: str) -> None:
    if not fingerprint:
        return
    _consecutive_failures.pop(fingerprint, None)
    _circuit_open_until.pop(fingerprint, None)


def manual_reset_circuit(fingerprint: str = "") -> int:
    if not fingerprint:
        n = len(_circuit_open_until) + len(_consecutive_failures)
        _circuit_open_until.clear()
        _consecutive_failures.clear()
        return n
    cleared = 0
    if fingerprint in _circuit_open_until:
        _circuit_open_until.pop(fingerprint, None)
        cleared += 1
    if fingerprint in _consecutive_failures:
        _consecutive_failures.pop(fingerprint, None)
        cleared += 1
    return cleared


def is_circuit_open_for(errors: list[str]) -> bool:
    if not errors:
        return False
    return _is_circuit_open(_error_fingerprint(errors))


def _cleanup_stale_occurrences() -> None:
    now = time.time()
    stale = [fp for fp, first in _error_first_seen.items()
             if (now - first) > _OCCURRENCE_TTL_SECONDS]
    for fp in stale:
        _error_occurrence.pop(fp, None)
        _error_first_seen.pop(fp, None)


def compute_score(errors, new_commands, text_changed, window_switched) -> int:
    score, _ = compute_score_with_reasons(errors, new_commands, text_changed, window_switched)
    return score


def compute_score_with_reasons(errors, new_commands, text_changed, window_switched):
    """설계서 v5 §6.4 — 점수와 reasons 배열 반환."""
    _cleanup_stale_occurrences()
    score = 0
    reasons: list[dict] = []

    if errors:
        fp = _error_fingerprint(errors)
        if fp not in _error_first_seen:
            _error_first_seen[fp] = time.time()
        _error_occurrence[fp] += 1

        matched_token = ""
        for err in errors:
            for pattern in _CRITICAL_ERRORS:
                m = pattern.search(err)
                if m:
                    matched_token = m.group()
                    break
            if matched_token:
                break

        if _is_critical_error(errors):
            score += _SCORE_CRITICAL_ERROR
            reasons.append({
                "type": "critical_error",
                "weight": _SCORE_CRITICAL_ERROR,
                "matched": matched_token or "critical pattern",
            })
        elif _is_high_weight_error(errors):
            score += _SCORE_HIGH_ERROR
            reasons.append({
                "type": "high_weight_error",
                "weight": _SCORE_HIGH_ERROR,
                "matched": (errors[0][:60] if errors else ""),
            })
        if _error_occurrence[fp] >= 2:
            score += _SCORE_REPEAT_ERROR
            reasons.append({
                "type": "repeat_error",
                "weight": _SCORE_REPEAT_ERROR,
                "matched": f"{_error_occurrence[fp]}회 반복",
            })

    if new_commands:
        score += _SCORE_NEW_COMMAND
        reasons.append({
            "type": "new_terminal_command",
            "weight": _SCORE_NEW_COMMAND,
            "matched": (new_commands[0][:60] if new_commands else ""),
        })
    if text_changed:
        score += _SCORE_TEXT_CHANGE
        reasons.append({"type": "text_changed", "weight": _SCORE_TEXT_CHANGE, "matched": ""})
    if window_switched:
        score += _SCORE_WINDOW_SWITCH
        reasons.append({"type": "window_switched", "weight": _SCORE_WINDOW_SWITCH, "matched": ""})

    if score > 100:
        score = 100
    elif score < 0:
        score = 0

    return score, reasons


def should_trigger(errors, new_commands, text_changed, window_switched, uia_failure):
    trigger, score, need_capture, _reasons = should_trigger_with_reasons(
        errors, new_commands, text_changed, window_switched, uia_failure)
    return trigger, score, need_capture


def should_trigger_with_reasons(errors, new_commands, text_changed, window_switched, uia_failure):
    """Returns: (trigger, score, need_capture, reasons)"""
    score, reasons = compute_score_with_reasons(errors, new_commands, text_changed, window_switched)

    if score < TRIGGER_THRESHOLD:
        return False, score, False, reasons

    fp = _error_fingerprint(errors) if errors else "_no_error_"
    if _in_cooldown(fp):
        return False, score, False, reasons

    # 설계서 v5 §6.4 — Circuit Breaker
    if _is_circuit_open(fp):
        reasons.append({
            "type": "circuit_open",
            "weight": 0,
            "matched": "이 에러는 연속 실패로 일시 차단되었습니다 (수동 리셋 필요)",
        })
        return False, score, False, reasons

    _last_trigger_times[fp] = time.time()
    return True, score, uia_failure, reasons


def notify_resolved(errors: list[str]) -> None:
    if not errors:
        return
    fp = _error_fingerprint(errors)
    _last_trigger_times.pop(fp, None)
    _error_occurrence.pop(fp, None)
    _error_first_seen.pop(fp, None)


def reset_cooldown(fingerprint: str) -> None:
    _last_trigger_times.pop(fingerprint, None)
    _error_occurrence.pop(fingerprint, None)
    _error_first_seen.pop(fingerprint, None)
