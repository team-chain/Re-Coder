"""
Control Plane — Q2-B: PolicyBundle 서비스

설계서 §Q2-B:
- Preset Policy Template 방식 (자유 Rego 빌더 아님)
- 5개 Preset → 고정 Rego 템플릿 파라미터로 변환
- sha256 + version 부여
- Local Core가 sha256 검증 후 OPA에 로드
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.db.models import PolicyBundle
from control_plane.models.schemas import (
    OPADecisionStatus,
    PolicyBundleCreate,
    PolicyBundleResponse,
    PolicyPresetConfig,
    PolicyPresetKey,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Preset → Rego 템플릿
# ---------------------------------------------------------------------------

# 각 Preset의 Rego 스니펫. 최종 bundle은 이들을 조합해 생성한다.
_PRESET_REGO_SNIPPETS: dict[PolicyPresetKey, str] = {
    PolicyPresetKey.TRIVY_CRITICAL_BLOCK: """\
# Preset: Trivy critical 취약점 차단
deny_reasons["trivy_critical_block: CRITICAL 취약점이 발견된 이미지는 배포할 수 없습니다"] {
    input.context.trivy_critical_count > 0
}""",

    PolicyPresetKey.PROD_MAIN_BRANCH_ONLY: """\
# Preset: 프로덕션 배포는 main 브랜치만 허용
deny_reasons["prod_main_branch_only: 프로덕션 배포는 main 브랜치에서만 허용됩니다"] {
    input.context.environment == "production"
    input.context.branch != "main"
}""",

    PolicyPresetKey.PORT_22_BLOCK: """\
# Preset: 22번 포트 외부 노출 차단
deny_reasons["port_22_block: 22번 포트(SSH)의 외부 노출(0.0.0.0/0)은 허용되지 않습니다"] {
    port := input.context.exposed_ports[_]
    port.number == 22
    port.cidr == "0.0.0.0/0"
}""",

    PolicyPresetKey.SECRET_ENV_ESCALATE: """\
# Preset: SECRET/PASSWORD/TOKEN env 감지 시 Level 4 격상
escalate_to_security {
    env_key := input.context.env_keys[_]
    contains(upper(env_key), "SECRET")
}
escalate_to_security {
    env_key := input.context.env_keys[_]
    contains(upper(env_key), "PASSWORD")
}
escalate_to_security {
    env_key := input.context.env_keys[_]
    contains(upper(env_key), "TOKEN")
}""",

    PolicyPresetKey.LEVEL3_TWO_APPROVERS: """\
# Preset: Level 3 이상 2인 승인 필요
required_approvers = 2 {
    input.level >= 3
} else = 0""",

    # Q3-A: SBOM 없는 배포 차단
    PolicyPresetKey.SBOM_REQUIRED_BLOCK: """\
# Preset: SBOM 없는 배포 차단 (§Q3-A)
deny_reasons["sbom_required_block: SBOM이 생성되지 않은 이미지는 배포할 수 없습니다"] {
    input.context.generate_sbom == false
}
deny_reasons["sbom_required_block: SBOM이 생성되지 않은 이미지는 배포할 수 없습니다"] {
    not input.context.generate_sbom
}""",

    # Q3-A: Hadolint error 차단
    PolicyPresetKey.HADOLINT_ERROR_BLOCK: """\
# Preset: Hadolint Dockerfile lint error 차단 (§Q3-A)
deny_reasons["hadolint_error_block: Hadolint error가 발견된 Dockerfile은 배포할 수 없습니다"] {
    input.context.hadolint_error_count > 0
}""",
}

_REGO_HEADER = '''\
package recoder.policy

import future.keywords.in

default allow = false
default required_approvers = 0
default escalate_to_security = false

deny_reasons := set()

'''

_REGO_FOOTER = '''
# ---------------------------------------------------------------------------
# 최종 판정 (설계서 §Q2-B 5단계)
# ---------------------------------------------------------------------------
allow {
    count(deny_reasons) == 0
    not escalate_to_security
    required_approvers == 0
}

allow_with_approval {
    count(deny_reasons) == 0
    not escalate_to_security
    required_approvers > 0
}

decision = "allow"                { allow }
decision = "allow_with_approval"  { allow_with_approval }
decision = "deny"                 { count(deny_reasons) > 0; not escalate_to_security }
decision = "deny_with_fix_suggestion" {
    count(deny_reasons) > 0
    deny_reasons[r]
    startswith(r, "trivy_")
}
decision = "escalate_to_security" { escalate_to_security }
'''


def _generate_rego(presets: list[PolicyPresetConfig]) -> str:
    """활성화된 Preset들을 조합해 Rego 소스를 생성한다."""
    snippets: list[str] = []
    for preset in presets:
        if preset.enabled:
            snippet = _PRESET_REGO_SNIPPETS.get(preset.key)
            if snippet:
                snippets.append(snippet)
    return _REGO_HEADER + "\n\n".join(snippets) + _REGO_FOOTER


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _next_version(current: Optional[str]) -> str:
    """v1.0.0 → v1.0.1 단순 patch 증가"""
    if current is None:
        return "v1.0.0"
    parts = current.lstrip("v").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        return "v1.0.0"
    return "v" + ".".join(parts)


# ---------------------------------------------------------------------------
# PolicyService
# ---------------------------------------------------------------------------

class PolicyService:

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_bundle(
        self,
        request: PolicyBundleCreate,
        creator_user_id: str,
    ) -> PolicyBundleResponse:
        """Preset 설정을 받아 Rego를 생성하고 PolicyBundle을 저장한다."""
        # 현재 최신 버전 조회
        result = await self._db.execute(
            select(PolicyBundle)
            .where(PolicyBundle.org_id == request.org_id, PolicyBundle.is_active == True)
            .order_by(PolicyBundle.created_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        new_version = _next_version(latest.version if latest else None)

        # Rego 생성
        rego_content = _generate_rego(request.presets)
        sha256 = _sha256(rego_content)

        # 기존 active bundle 비활성화
        if latest:
            latest.is_active = False

        bundle = PolicyBundle(
            org_id=request.org_id,
            version=new_version,
            display_name=request.display_name,
            rego_content=rego_content,
            sha256=sha256,
            preset_config=[p.model_dump() for p in request.presets],
            is_active=True,
            created_by=creator_user_id,
        )
        self._db.add(bundle)
        await self._db.flush()

        logger.info(
            "PolicyBundle created: org=%s version=%s sha256=%s…",
            request.org_id, new_version, sha256[:12],
        )
        return self._to_response(bundle)

    async def get_active_bundle(self, org_id: str) -> Optional[PolicyBundleResponse]:
        result = await self._db.execute(
            select(PolicyBundle)
            .where(PolicyBundle.org_id == org_id, PolicyBundle.is_active == True)
            .order_by(PolicyBundle.created_at.desc())
            .limit(1)
        )
        bundle = result.scalar_one_or_none()
        return self._to_response(bundle) if bundle else None

    async def get_bundle_rego(self, org_id: str, version: str) -> Optional[tuple[str, str]]:
        """(rego_content, sha256) 반환. Local Core가 다운로드할 때 사용."""
        result = await self._db.execute(
            select(PolicyBundle)
            .where(PolicyBundle.org_id == org_id, PolicyBundle.version == version)
        )
        bundle = result.scalar_one_or_none()
        if bundle is None:
            return None
        return bundle.rego_content, bundle.sha256

    async def list_bundles(self, org_id: str) -> list[PolicyBundleResponse]:
        result = await self._db.execute(
            select(PolicyBundle)
            .where(PolicyBundle.org_id == org_id)
            .order_by(PolicyBundle.created_at.desc())
        )
        return [self._to_response(b) for b in result.scalars().all()]

    async def get_latest_version(self, org_id: str) -> Optional[str]:
        """identity_service._get_latest_policy_version()의 실제 구현"""
        bundle = await self.get_active_bundle(org_id)
        return bundle.version if bundle else None


    @staticmethod
    def _to_response(bundle: PolicyBundle) -> PolicyBundleResponse:
        from control_plane.models.schemas import PolicyPresetConfig
        presets = [PolicyPresetConfig(**p) for p in (bundle.preset_config or [])]
        return PolicyBundleResponse(
            bundle_id=bundle.bundle_id,
            org_id=bundle.org_id,
            version=bundle.version,
            display_name=bundle.display_name,
            sha256=bundle.sha256,
            presets=presets,
            is_active=bundle.is_active,
            created_at=bundle.created_at,
        )
