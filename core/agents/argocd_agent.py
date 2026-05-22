"""
Local Core — Q4: ArgoCD GitOps 배포 에이전트

설계서 §Q4-A (Must):
- ArgoCD REST API를 통해 Application 동기화
- 배포 상태 폴링 (Healthy/Degraded/SyncFailed)
- ADR-009: Q4 배포는 EKS + ArgoCD. ECS(Q3)와 동시 운영 없음
- ADR-005: 동기화 실패 시 rollback = Git revert PR 기본
- ADR-006: ArgoCD 직접 rollback은 Severity 1만 허용
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import httpx

from core.schemas import (
    ArgoHealthStatus,
    ArgoSyncPhase,
    ArgoSyncRecord,
    ArgoSyncRequest,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10      # 폴링 간격 (초)
_MAX_POLL_ATTEMPTS = 60  # 최대 10분
_DEFAULT_TIMEOUT = 30    # HTTP 요청 타임아웃


class ArgoCDAgent:
    """
    ArgoCD REST API 클라이언트 + 배포 오케스트레이터.

    ADR-009: Q4 표준 배포 경로. ECS(Q3)는 Q3 전용.
    LLM 미사용 — 결정론적 API 호출.
    """

    async def sync(self, request: ArgoSyncRequest) -> ArgoSyncRecord:
        """
        ArgoCD Application 동기화 실행.

        1. 현재 상태 조회 (pre-sync)
        2. sync 요청
        3. 상태 폴링 (Healthy 확인)
        4. 실패 시 rollback_triggered = True
        """
        record = ArgoSyncRecord(
            project_id=request.project_id,
            app_name=request.app_name,
            argocd_server=request.argocd_server,
            target_revision=request.target_revision,
        )

        base_url = f"https://{request.argocd_server}"
        headers = {
            "Authorization": f"Bearer {request.argocd_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            verify=False,  # 사설 인증서 환경 대응 (프로덕션에서는 verify=True)
            timeout=_DEFAULT_TIMEOUT,
        ) as client:
            try:
                # 1. pre-sync 상태 조회
                pre_state = await self._get_app_state(client, base_url, headers, request.app_name)
                record.live_revision = pre_state.get("status", {}).get("sync", {}).get("revision")
                logger.info(
                    "ArgoCD pre-sync: app=%s phase=%s health=%s revision=%s",
                    request.app_name,
                    pre_state.get("status", {}).get("sync", {}).get("status"),
                    pre_state.get("status", {}).get("health", {}).get("status"),
                    record.live_revision,
                )

                # 2. sync 요청
                if not request.dry_run:
                    await self._trigger_sync(client, base_url, headers, request)

                # 3. 폴링
                success = await self._poll_until_healthy(
                    client, base_url, headers, request.app_name, record
                )

                if success:
                    record.sync_phase = ArgoSyncPhase.SYNCED
                    record.health_status = ArgoHealthStatus.HEALTHY
                    record.completed_at = datetime.now(timezone.utc)
                    logger.info("ArgoCD sync succeeded: app=%s", request.app_name)
                else:
                    record.sync_phase = ArgoSyncPhase.SYNC_FAILED
                    record.health_status = ArgoHealthStatus.DEGRADED
                    record.rollback_triggered = True
                    record.completed_at = datetime.now(timezone.utc)
                    logger.warning(
                        "ArgoCD sync failed: app=%s — rollback triggered", request.app_name
                    )

            except httpx.HTTPStatusError as exc:
                record.sync_phase = ArgoSyncPhase.SYNC_FAILED
                record.error_message = f"ArgoCD API 오류 {exc.response.status_code}: {exc.response.text[:200]}"
                record.completed_at = datetime.now(timezone.utc)
                logger.error("ArgoCD HTTP error: %s", exc)
            except Exception as exc:
                record.sync_phase = ArgoSyncPhase.SYNC_FAILED
                record.error_message = str(exc)
                record.completed_at = datetime.now(timezone.utc)
                logger.error("ArgoCD agent error: %s", exc, exc_info=True)

        return record

    async def get_app_status(
        self,
        app_name: str,
        argocd_server: str,
        argocd_token: str,
    ) -> dict:
        """단일 Application의 현재 상태 조회."""
        base_url = f"https://{argocd_server}"
        headers = {"Authorization": f"Bearer {argocd_token}"}

        async with httpx.AsyncClient(verify=False, timeout=_DEFAULT_TIMEOUT) as client:
            return await self._get_app_state(client, base_url, headers, app_name)

    async def rollback_app(
        self,
        app_name: str,
        argocd_server: str,
        argocd_token: str,
        target_id: int,
    ) -> bool:
        """
        ArgoCD Application 직접 rollback.

        ADR-006: Severity 1 장애만 허용. 일반적으로는 Git revert PR 우선.
        target_id: ArgoCD history의 id (이전 배포 revision 번호)
        """
        base_url = f"https://{argocd_server}"
        headers = {
            "Authorization": f"Bearer {argocd_token}",
            "Content-Type": "application/json",
        }
        url = urljoin(base_url, f"/api/v1/applications/{app_name}/rollback")

        async with httpx.AsyncClient(verify=False, timeout=_DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.post(url, headers=headers, json={"id": target_id})
                resp.raise_for_status()
                logger.warning(
                    "ArgoCD direct rollback triggered: app=%s target_id=%d (ADR-006: Sev1 only)",
                    app_name, target_id,
                )
                return True
            except Exception as exc:
                logger.error("ArgoCD rollback failed: %s", exc)
                return False

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    async def _get_app_state(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict,
        app_name: str,
    ) -> dict:
        url = urljoin(base_url, f"/api/v1/applications/{app_name}")
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _trigger_sync(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict,
        request: ArgoSyncRequest,
    ) -> None:
        url = urljoin(base_url, f"/api/v1/applications/{request.app_name}/sync")
        body: dict = {
            "revision": request.target_revision,
            "prune": request.prune,
            "dryRun": request.dry_run,
            "force": request.force,
            "strategy": {"hook": {"force": request.force}},
        }
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        logger.info("ArgoCD sync triggered: app=%s revision=%s", request.app_name, request.target_revision)

    async def _poll_until_healthy(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict,
        app_name: str,
        record: ArgoSyncRecord,
    ) -> bool:
        """
        Healthy + Synced 상태가 될 때까지 폴링.
        Degraded / SyncFailed 는 실패로 처리.
        """
        for attempt in range(_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_POLL_INTERVAL)

            try:
                state = await self._get_app_state(client, base_url, headers, app_name)
                sync_status = state.get("status", {}).get("sync", {}).get("status", "Unknown")
                health_status = state.get("status", {}).get("health", {}).get("status", "Unknown")
                resources = state.get("status", {}).get("resources", [])

                record.resources_synced = sum(
                    1 for r in resources if r.get("status") == "Synced"
                )
                record.resources_failed = sum(
                    1 for r in resources if r.get("health", {}).get("status") == "Degraded"
                )
                record.live_revision = state.get("status", {}).get("sync", {}).get("revision")

                logger.debug(
                    "Poll %d/%d: app=%s sync=%s health=%s resources=%d failed=%d",
                    attempt + 1, _MAX_POLL_ATTEMPTS,
                    app_name, sync_status, health_status,
                    record.resources_synced, record.resources_failed,
                )

                # 성공
                if sync_status == "Synced" and health_status == "Healthy":
                    return True

                # 명시적 실패
                if health_status == "Degraded" or sync_status == "Unknown":
                    msg = state.get("status", {}).get("conditions", [{}])[0].get("message", "")
                    record.error_message = f"sync={sync_status} health={health_status}: {msg}"
                    return False

            except Exception as exc:
                logger.warning("ArgoCD poll error (attempt %d): %s", attempt + 1, exc)

        record.error_message = f"폴링 타임아웃 ({_MAX_POLL_ATTEMPTS * _POLL_INTERVAL}초)"
        return False


# 모듈 레벨 싱글톤
argocd_agent = ArgoCDAgent()
