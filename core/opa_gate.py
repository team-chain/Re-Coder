"""
opa_gate.py — OPA (Open Policy Agent) REST API 게이트 (설계서 §Q2-B / §Q3)

설계 원칙:
  - OPA server 는 Local Core 와 같은 머신에서 독립 프로세스로 실행 (ADR-003)
  - REST API 로 질의: POST http://localhost:{OPA_PORT}/v1/data/{policy_path}
  - OPA unavailable → fail-closed: Level 3~4 전체 차단
  - Level 1~2 정책 불필요 작업은 항상 허용 (오프라인 모드 정책)

평가 결과 5단계 (설계서 §Q2-B):
  allow                 → 즉시 진행
  allow_with_approval   → Approval Level 3~4 UI 진입
  deny                  → 자동 차단
  deny_with_fix_suggestion → 차단 + 수정 제안
  escalate_to_security  → AuditLog + 보안 담당자 알림

Q3 배포 게이트 정책:
  - Trivy critical 취약점 → 차단
  - SBOM 없는 배포 → 차단
  - gitleaks secret 감지 → 항상 차단
  - Hadolint error → 차단

환경변수:
  OPA_URL   — OPA 서버 URL (기본: http://localhost:8181)
  OPA_TOKEN — OPA bearer 토큰 (없으면 미사용)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── OPA 서버 설정 ──────────────────────────────────────────────────────

_OPA_URL_DEFAULT = "http://localhost:8181"
_OPA_TIMEOUT     = 5   # 초 (짧게 — unavailable 시 빠르게 fail-closed)

# ── 평가 결과 Enum ────────────────────────────────────────────────────


class OPADecision(str, Enum):
    ALLOW                   = "allow"
    ALLOW_WITH_APPROVAL     = "allow_with_approval"
    DENY                    = "deny"
    DENY_WITH_FIX           = "deny_with_fix_suggestion"
    ESCALATE_TO_SECURITY    = "escalate_to_security"
    FAIL_CLOSED             = "fail_closed"   # OPA unavailable


@dataclass
class OPAResult:
    """OPA 평가 결과."""
    decision:       OPADecision
    reason:         str = ""
    fix_suggestion: str = ""
    approval_level: int = 0    # allow_with_approval 시 필요 레벨
    policy_version: str = ""   # 어떤 정책 버전으로 판정됐는지
    raw_result:     dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision in (OPADecision.ALLOW, OPADecision.ALLOW_WITH_APPROVAL)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict:
        return {
            "decision":       self.decision.value,
            "allowed":        self.allowed,
            "reason":         self.reason,
            "fix_suggestion": self.fix_suggestion,
            "approval_level": self.approval_level,
            "policy_version": self.policy_version,
        }


# ── OPA 클라이언트 ────────────────────────────────────────────────────

class OPAGate:
    """
    OPA REST API 게이트.

    모든 public 메서드는 동기. server.py 에서 asyncio.to_thread 로 호출.
    """

    def __init__(self, opa_url: Optional[str] = None, token: Optional[str] = None):
        self._url   = (opa_url or os.getenv("OPA_URL", _OPA_URL_DEFAULT)).rstrip("/")
        self._token = token or os.getenv("OPA_TOKEN", "")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _post(self, path: str, input_data: dict) -> Optional[dict]:
        """
        POST {opa_url}/v1/data/{path}  →  OPA 응답 dict.
        실패·타임아웃 시 None 반환 (caller 가 fail-closed 처리).
        """
        try:
            import urllib.request, urllib.error, json as _json

            url     = f"{self._url}/v1/data/{path}"
            payload = _json.dumps({"input": input_data}).encode()
            req     = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
            with urllib.request.urlopen(req, timeout=_OPA_TIMEOUT) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"[opa] OPA 서버 연결 실패: {e}")
            return None

    def is_available(self) -> bool:
        """OPA 서버 헬스 체크."""
        try:
            import urllib.request
            with urllib.request.urlopen(f"{self._url}/health", timeout=_OPA_TIMEOUT) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ── 핵심 평가 메서드 ─────────────────────────────────────────────

    def evaluate(
        self,
        policy_path: str,
        input_data: dict,
        approval_level: int = 3,
    ) -> OPAResult:
        """
        범용 OPA 정책 평가.

        Args:
            policy_path:    OPA 정책 경로 (예: "recoder/deploy/allow")
            input_data:     OPA input 객체
            approval_level: OPA unavailable 시 차단할 기준 레벨 (기본 3)

        Returns:
            OPAResult
        """
        raw = self._post(policy_path, input_data)

        if raw is None:
            # OPA unavailable → fail-closed (Level 3~4 차단)
            logger.warning("[opa] OPA unavailable → fail-closed")
            if approval_level >= 3:
                return OPAResult(
                    decision=OPADecision.FAIL_CLOSED,
                    reason="OPA 서버에 연결할 수 없습니다. Level 3~4 작업은 차단됩니다. (fail-closed)",
                )
            else:
                # Level 1~2 는 오프라인 모드 허용
                return OPAResult(
                    decision=OPADecision.ALLOW,
                    reason="OPA 서버 미연결 — Level 1~2 작업은 오프라인 허용",
                )

        result = raw.get("result", {})
        if not result:
            return OPAResult(
                decision=OPADecision.DENY,
                reason="OPA 정책이 정의되지 않았거나 빈 결과를 반환했습니다.",
                raw_result=raw,
            )

        decision_str    = result.get("decision",       "deny")
        reason          = result.get("reason",         "")
        fix_suggestion  = result.get("fix_suggestion", "")
        appr_level      = result.get("approval_level", 3)
        policy_version  = result.get("policy_bundle_version", "")

        try:
            decision = OPADecision(decision_str)
        except ValueError:
            decision = OPADecision.DENY
            reason = f"알 수 없는 OPA 결과: {decision_str}"

        return OPAResult(
            decision=decision,
            reason=reason,
            fix_suggestion=fix_suggestion,
            approval_level=appr_level,
            policy_version=policy_version,
            raw_result=raw,
        )

    # ── Q3 배포 게이트 ───────────────────────────────────────────────

    def evaluate_ecs_deploy(
        self,
        image_uri: str,
        trivy_result: Optional[dict] = None,
        sbom_result: Optional[dict] = None,
        gitleaks_result: Optional[dict] = None,
        hadolint_result: Optional[dict] = None,
        branch: str = "",
        environment: str = "staging",
    ) -> OPAResult:
        """
        ECS Fargate 배포 OPA 게이트 평가.

        OPA 서버가 없는 경우 로컬 폴백 정책(built-in rules)으로 평가한다.
        OPA 서버 연결 성공 시에는 서버 정책이 우선한다.

        Q3 차단 규칙:
          - SBOM 없는 배포 → deny
          - Trivy critical 취약점 → deny
          - gitleaks secret 감지 → deny
          - Hadolint error → deny
          - production 배포 + main 아닌 브랜치 → deny (기본 preset)
        """
        input_data = {
            "image_uri":   image_uri,
            "environment": environment,
            "branch":      branch,
            "sbom": {
                "present":        sbom_result is not None and sbom_result.get("success", False),
                "package_count":  sbom_result.get("package_count", 0) if sbom_result else 0,
                "sbom_hash":      sbom_result.get("sbom_hash", "")    if sbom_result else "",
            },
            "trivy": {
                "passed":         trivy_result.get("passed", True)         if trivy_result else True,
                "critical_count": trivy_result.get("critical_count", 0)    if trivy_result else 0,
                "high_count":     trivy_result.get("high_count", 0)        if trivy_result else 0,
            },
            "gitleaks": {
                "passed":         gitleaks_result.get("passed", True)      if gitleaks_result else True,
            },
            "hadolint": {
                "passed":         hadolint_result.get("passed", True)      if hadolint_result else True,
            },
        }

        # OPA 서버 호출 시도
        raw = self._post("recoder/deploy/allow", input_data)

        if raw is not None:
            # OPA 서버 응답이 있으면 서버 정책 우선
            return self.evaluate("recoder/deploy/allow", input_data, approval_level=3)

        # OPA unavailable → 로컬 폴백 규칙 적용
        logger.info("[opa] OPA 서버 없음 — 로컬 폴백 규칙 적용")
        return self._local_deploy_gate(input_data)

    def _local_deploy_gate(self, inp: dict) -> OPAResult:
        """
        OPA 서버 없을 때 적용하는 로컬 내장 배포 게이트.
        설계서 Preset Policy 5개 중 Q3 관련 규칙만 적용.
        """
        # 규칙 1: SBOM 없는 배포 차단
        if not inp["sbom"]["present"]:
            return OPAResult(
                decision=OPADecision.DENY,
                reason="SBOM이 생성되지 않은 이미지는 배포할 수 없습니다. (Preset: SBOM 필수)",
                fix_suggestion="ECS 배포 파이프라인에서 SBOM 생성 단계를 확인하세요.",
            )

        # 규칙 2: Trivy critical 차단
        critical = inp["trivy"]["critical_count"]
        if critical > 0:
            return OPAResult(
                decision=OPADecision.DENY,
                reason=f"Trivy critical 취약점 {critical}건 감지. (Preset: Trivy critical 취약점 차단)",
                fix_suggestion="베이스 이미지를 최신 버전으로 업데이트하거나 취약한 패키지를 제거하세요.",
            )

        # 규칙 3: gitleaks secret 감지 → 항상 차단
        if not inp["gitleaks"]["passed"]:
            return OPAResult(
                decision=OPADecision.ESCALATE_TO_SECURITY,
                reason="gitleaks: secret 이 감지됐습니다. 소스코드에 자격증명이 포함돼 있을 수 있습니다.",
                fix_suggestion="git history 에서 secret 을 제거하고 해당 자격증명을 즉시 교체하세요.",
            )

        # 규칙 4: Hadolint error 차단
        if not inp["hadolint"]["passed"]:
            return OPAResult(
                decision=OPADecision.DENY,
                reason="Hadolint: Dockerfile 에서 오류가 감지됐습니다.",
                fix_suggestion="hadolint 오류를 수정한 후 다시 배포하세요.",
            )

        # 규칙 5: production + main 아닌 브랜치 차단
        env    = inp.get("environment", "")
        branch = inp.get("branch", "")
        if env == "production" and branch and branch not in ("main", "master", ""):
            return OPAResult(
                decision=OPADecision.DENY,
                reason=f"프로덕션 배포는 main 브랜치에서만 허용됩니다. (현재: {branch})",
                fix_suggestion="main 브랜치로 머지 후 다시 배포하세요.",
            )

        return OPAResult(
            decision=OPADecision.ALLOW,
            reason="모든 배포 게이트를 통과했습니다.",
        )


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[OPAGate] = None


def get_opa_gate() -> OPAGate:
    global _instance
    if _instance is None:
        _instance = OPAGate()
    return _instance


__all__ = ["OPAGate", "OPADecision", "OPAResult", "get_opa_gate"]
