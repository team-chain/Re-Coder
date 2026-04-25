"""
Trigger Detector (3단계) — 점수 계산으로 분석 트리거 여부 결정.
API 없음. 순수 규칙 기반.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

# 고가중치 에러 키워드 (즉각 반응)
_HIGH_WEIGHT_ERRORS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'Traceback \(most recent call last\)',
        r'\bModuleNotFoundError\b',
        r'\bImportError\b',
        r'\bSyntaxError\b',
        r'\bFATAL\b',
        r'FAILED',
        r'No module named',
    ]
]

# 점수 가중치 (기획서 2.4)
_SCORE_HIGH_ERROR      = 50
_SCORE_NEW_COMMAND     = 30
_SCORE_REPEAT_ERROR    = 30
_SCORE_TEXT_CHANGE     = 20
_SCORE_WINDOW_SWITCH   = 10

TRIGGER_THRESHOLD = 70  # score >= 70 시 분석 트리거

# Cool-down: 동일 에러 N초 이내 재트리거 차단
_COOLDOWN_SECONDS = 60

_last_trigger_times: dict[str, float] = defaultdict(float)
_error_occurrence:   dict[str, int]   = defaultdict(int)


def _error_fingerprint(errors: list[str]) -> str:
    return "|".join(sorted(set(e.strip()[:50] for e in errors)))


def _is_high_weight_error(errors: list[str]) -> bool:
    for err in errors:
        for pattern in _HIGH_WEIGHT_ERRORS:
            if pattern.search(err):
                return True
    return False


def _in_cooldown(fingerprint: str) -> bool:
    last = _last_trigger_times.get(fingerprint, 0.0)
    return (time.time() - last) < _COOLDOWN_SECONDS


def compute_score(
    errors:          list[str],
    new_commands:    list[str],
    text_changed:    bool,
    window_switched: bool,
) -> int:
    score = 0

    if errors:
        _fp = _error_fingerprint(errors)
        _error_occurrence[_fp] += 1

        if _is_high_weight_error(errors):
            score += _SCORE_HIGH_ERROR
        if _error_occurrence[_fp] >= 2:
            score += _SCORE_REPEAT_ERROR

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


def reset_cooldown(fingerprint: str) -> None:
    """테스트 또는 수동 리셋용."""
    _last_trigger_times.pop(fingerprint, None)
    _error_occurrence.pop(fingerprint, None)
