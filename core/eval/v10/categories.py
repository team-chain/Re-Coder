"""
v10 Eval — 6 evaluation categories (§38).
"""

from __future__ import annotations

from enum import Enum


class V10EvalCategory(str, Enum):
    """v10 백본 평가 카테고리."""
    PREFLIGHT_ACCURACY      = "preflight_accuracy"
    REMEDIATION_DETERMINISM = "remediation_determinism"
    REMEDIATION_APPLY       = "remediation_apply"
    INCIDENT_FINGERPRINT    = "incident_fingerprint"
    INCIDENT_MATCH          = "incident_match"
    SAFETY_REGRESSIONS      = "safety_regressions"


# 카테고리별 기본 가중치 (CI gate 시 weighted pass rate 계산용).
# SAFETY_REGRESSIONS 는 단 1건 실패해도 즉시 게이트 fail (별도 처리).
CATEGORY_WEIGHTS: dict[V10EvalCategory, float] = {
    V10EvalCategory.PREFLIGHT_ACCURACY:      1.0,
    V10EvalCategory.REMEDIATION_DETERMINISM: 1.2,   # 결정성은 중요
    V10EvalCategory.REMEDIATION_APPLY:       1.0,
    V10EvalCategory.INCIDENT_FINGERPRINT:    0.8,
    V10EvalCategory.INCIDENT_MATCH:          0.8,
    V10EvalCategory.SAFETY_REGRESSIONS:      2.0,   # absolute gate
}


CATEGORIES: list[V10EvalCategory] = list(V10EvalCategory)
