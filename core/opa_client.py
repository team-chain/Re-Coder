"""
Local Core — OPA REST API 클라이언트

설계서 §Q2-B:
- OPA를 독립 프로세스로 실행하고 REST API로 질의 (ADR-003)
- OPA unavailable → fail-closed: Level 3~4 전체 차단
- Level 1~2는 OPA 없이도 허용

사용:
    client = OPAClient()
    result = await client.evaluate(action="deployment:request", level=3, context={...})
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_OPA_BASE_URL = os.environ.get("OPA_URL", "http://localhost:8181")
_OPA_POLICY_PATH = "/v1/data/recoder/policy"
_OPA_TIMEOUT = float(os.environ.get("OPA_TIMEOUT_SECONDS", "3.0"))


@dataclass
class OPAResult:
    """OPA 평가 결과"""
    decision: str                        # allow / allow_with_approval / deny / deny_with_fix_suggestion / escalate_to_security
    reason: str
    fix_suggestion: Optional[str] = None
    required_approvers: int = 0
    deny_reasons: list[str] = field(default_factory=list)
    opa_available: bool = True


class OPAClient:
    """
    OPA REST API 클라이언트 (fail-closed).

    ADR-003: OPA를 독립 프로세스로 실행하고 REST API로 질의한다.
    Python 프로세스에 Go 라이브러리를 직접 임베딩하지 않는다.
    """

    def __init__(
        self,
        base_url: str = _OPA_BASE_URL,
        timeout: float = _OPA_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def evaluate(
        self,
        action: str,
        level: int,
        context: dict[str, Any],
        org_id: str = "",
        resource_type: str = "",
        resource_id: Optional[str] = None,
        policy_bundle_version: str = "unknown",
    ) -> OPAResult:
        """
        OPA에 정책 평가를 요청한다.

        fail-closed 원칙:
        - OPA 연결 실패 or 오류 시 Level 3~4는 deny 반환
        - Level 1~2는 allow 반환 (로컬 작업 허용)
        """
        input_data = {
            "input": {
                "org_id": org_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "level": level,
                "context": context,
                "policy_bundle_version": policy_bundle_version,
            }
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}{_OPA_POLICY_PATH}",
                    json=input_data,
                )
                resp.raise_for_status()
                result = resp.json().get("result", {})

            decision_str = result.get("decision", "deny")
            deny_reasons: list[str] = list(result.get("deny_reasons", []))
            required_approvers = int(result.get("required_approvers", 0))

            reason = "; ".join(deny_reasons) if deny_reasons else _default_reason(decision_str)
            fix_suggestion = _build_fix_suggestion(deny_reasons)

            return OPAResult(
                decision=decision_str,
                reason=reason,
                fix_suggestion=fix_suggestion,
                required_approvers=required_approvers,
                deny_reasons=deny_reasons,
                opa_available=True,
            )

        except httpx.ConnectError:
            logger.warning("OPA not reachable at %s — fail-closed applied (level=%d)", self._base_url, level)
            return self._fail_closed(level, "OPA 서버에 연결할 수 없습니다")

        except httpx.TimeoutException:
            logger.warning("OPA timeout — fail-closed applied (level=%d)", level)
            return self._fail_closed(level, "OPA 응답 시간 초과")

        except Exception as exc:
            logger.error("OPA evaluation error: %s", exc)
            return self._fail_closed(level, f"정책 평가 중 오류: {exc}")

    async def health_check(self) -> bool:
        """OPA 서버 가용성 확인"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _fail_closed(level: int, reason: str) -> OPAResult:
        """
        설계서 §Failure Mode: OPA unavailable → fail-closed.
        Level 3~4: deny. Level 1~2: allow (로컬 작업 허용).
        """
        if level >= 3:
            return OPAResult(
                decision="deny",
                reason=f"{reason}. Level {level} 작업은 OPA 연결 필수 (fail-closed).",
                opa_available=False,
            )
        return OPAResult(
            decision="allow",
            reason=f"OPA 오프라인 — Level {level} 로컬 작업 허용",
            opa_available=False,
        )


def _default_reason(decision: str) -> str:
    return {
        "allow": "정책 검사 통과",
        "allow_with_approval": "승인이 필요한 작업입니다",
        "deny": "정책에 의해 차단됩니다",
        "deny_with_fix_suggestion": "정책 위반 — 수정 가이드를 확인하세요",
        "escalate_to_security": "보안팀 에스컬레이션이 필요합니다",
    }.get(decision, "알 수 없는 결정")


def _build_fix_suggestion(deny_reasons: list[str]) -> Optional[str]:
    for r in deny_reasons:
        if "trivy_critical_block" in r:
            return (
                "이미지에 CRITICAL 취약점이 있습니다. "
                "`trivy image <image>` 로 취약한 패키지를 확인하고 업데이트하세요."
            )
        if "port_22_block" in r:
            return "22번 포트를 0.0.0.0/0으로 열지 마세요. Security Group에서 특정 IP만 허용하세요."
        if "prod_main_branch_only" in r:
            return "프로덕션 배포는 main 브랜치에서만 가능합니다."
        if "secret_env_escalate" in r or "SECRET" in r or "PASSWORD" in r or "TOKEN" in r:
            return "환경 변수에 시크릿이 포함돼 있습니다. AWS Secrets Manager 또는 .env 파일을 사용하세요."
    return None


# 싱글톤 인스턴스 (Local Core가 import해서 사용)
opa_client = OPAClient()
