"""
Local Core — PolicyBundle 로컬 캐시

설계서 §Q2-B:
- Control Plane에서 Rego 다운로드 후 sha256 검증
- bundle_version / downloaded_at / expires_at 저장
- 오프라인 시 캐시된 정책 사용 (Level 3: 만료 안 된 캐시만)
- 캐시 파일: ~/.recoder/policy_cache.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(os.environ.get("RECODER_HOME", Path.home() / ".recoder"))
_CACHE_FILE = _CACHE_DIR / "policy_cache.json"
_REGO_FILE = _CACHE_DIR / "policy.rego"
_CACHE_TTL_HOURS = int(os.environ.get("POLICY_CACHE_TTL_HOURS", "1"))
_CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:18000")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PolicyCache:
    """
    PolicyBundle 로컬 캐시 관리자.

    Heartbeat 응답에 최신 policy_bundle_version이 오면
    캐시와 비교해서 구버전이면 자동으로 다운로드한다.
    """

    def __init__(self) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._meta: dict = self._load_meta()

    # ------------------------------------------------------------------
    # 다운로드 + 검증
    # ------------------------------------------------------------------

    async def ensure_fresh(
        self,
        device_token: str,
        org_id: str,
        latest_version: Optional[str],
    ) -> bool:
        """
        최신 버전과 캐시를 비교해 구버전이면 다운로드한다.
        반환: True=캐시 유효, False=다운로드 실패
        """
        if latest_version is None:
            return True  # 정책 없음 — allow

        cached_version = self._meta.get("bundle_version")

        if cached_version == latest_version and not self._is_expired():
            logger.debug("Policy cache fresh: %s", latest_version)
            return True

        logger.info("Policy cache stale (%s → %s) — downloading", cached_version, latest_version)
        return await self._download(device_token, org_id, latest_version)

    async def _download(
        self, device_token: str, org_id: str, version: str
    ) -> bool:
        url = f"{_CONTROL_PLANE_URL}/policy/{org_id}/bundles/{version}/rego"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {device_token}"}
                )
                resp.raise_for_status()

            rego_content = resp.text
            expected_sha256 = resp.headers.get("X-SHA256", "")

            # sha256 검증
            actual_sha256 = hashlib.sha256(rego_content.encode("utf-8")).hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256:
                logger.error(
                    "PolicyBundle sha256 mismatch: expected=%s actual=%s",
                    expected_sha256[:12], actual_sha256[:12],
                )
                return False

            # 파일 저장
            _REGO_FILE.write_text(rego_content, encoding="utf-8")

            # 메타 업데이트
            now = _now()
            self._meta = {
                "bundle_version": version,
                "sha256": actual_sha256,
                "downloaded_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=_CACHE_TTL_HOURS)).isoformat(),
            }
            self._save_meta()

            logger.info("PolicyBundle downloaded and verified: %s (%s…)", version, actual_sha256[:12])
            return True

        except Exception as exc:
            logger.error("PolicyBundle download failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # OPA에 Rego 로드
    # ------------------------------------------------------------------

    async def load_to_opa(self, opa_url: str) -> bool:
        """
        캐시된 Rego를 OPA에 PUT /v1/policies/recoder 로 로드한다.
        OPA가 처음 실행됐을 때나 정책 갱신 시 호출한다.
        """
        if not _REGO_FILE.exists():
            logger.warning("No cached rego file to load")
            return False
        rego_content = _REGO_FILE.read_text(encoding="utf-8")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.put(
                    f"{opa_url}/v1/policies/recoder",
                    content=rego_content.encode("utf-8"),
                    headers={"Content-Type": "text/plain"},
                )
                resp.raise_for_status()
            logger.info("Rego loaded to OPA successfully")
            return True
        except Exception as exc:
            logger.error("Failed to load rego to OPA: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 캐시 상태 조회
    # ------------------------------------------------------------------

    def get_cached_version(self) -> Optional[str]:
        return self._meta.get("bundle_version")

    def is_valid(self) -> bool:
        """캐시가 존재하고 만료되지 않았는지"""
        return bool(self._meta.get("bundle_version")) and not self._is_expired()

    def _is_expired(self) -> bool:
        expires_at_str = self._meta.get("expires_at")
        if not expires_at_str:
            return True
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            return _now() > expires_at
        except ValueError:
            return True

    # ------------------------------------------------------------------
    # 오프라인 허용 여부 (Level 3 전용)
    # ------------------------------------------------------------------

    def offline_level3_allowed(self) -> tuple[bool, str]:
        """
        설계서 §오프라인 모드 정책:
        Level 3: 정책 캐시 유효 + 마지막 heartbeat 1시간 이내
        """
        if not self.is_valid():
            return False, "정책 캐시가 만료됐거나 없습니다. 온라인 상태에서 재연결하세요."
        return True, f"정책 캐시 유효 (version={self.get_cached_version()})"

    # ------------------------------------------------------------------
    # 직렬화
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict:
        if _CACHE_FILE.exists():
            try:
                return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_meta(self) -> None:
        _CACHE_FILE.write_text(
            json.dumps(self._meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# 싱글톤
policy_cache = PolicyCache()
