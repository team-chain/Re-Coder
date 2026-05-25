"""
discord-bot/recoder_client.py — ReCoder Local Core API 클라이언트

Discord Bot이 각 서버의 ReCoder Core API를 호출하는 HTTP 래퍼.

SaaS 봇 모드에서는 서버마다 다른 API 엔드포인트를 사용하므로
get_client_for_guild(guild_id) 팩토리 함수로 서버별 클라이언트를 가져온다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

import guild_store

log = logging.getLogger(__name__)
_TIMEOUT = 30.0


# ── 예외 ────────────────────────────────────────────────────────────────────

class GuildNotConfiguredError(RuntimeError):
    """서버의 ReCoder API 설정이 없을 때 발생한다."""

    def __init__(self, guild_id: int):
        super().__init__(
            f"서버({guild_id})의 ReCoder API 설정이 없습니다. "
            "서버 관리자가 `/recoder setup api <url> <token>` 으로 설정해주세요."
        )


# ── 팩토리 ──────────────────────────────────────────────────────────────────

def get_client_for_guild(guild_id: int) -> "RecoderClient":
    """
    서버 ID로 해당 서버의 RecoderClient를 반환한다.

    설정이 없으면 GuildNotConfiguredError를 발생시킨다.
    슬래시 커맨드 핸들러에서 try/except로 잡아 사용자에게 안내 메시지를 보낸다.
    """
    cfg = guild_store.get_api(guild_id)
    if cfg is None:
        raise GuildNotConfiguredError(guild_id)
    api_base, api_token = cfg
    return RecoderClient(base=api_base, token=api_token)


# ── 클라이언트 ──────────────────────────────────────────────────────────────

class RecoderClient:
    """비동기 ReCoder Local Core API 클라이언트."""

    def __init__(self, base: str, token: str):
        self._base = base.rstrip("/")
        self._headers: Dict[str, str] = {
            "X-Session-Token": token,
            "Content-Type": "application/json",
        }

    # ── Preflight ─────────────────────────────────────────────────────────

    async def preflight(
        self,
        cluster: str,
        service: str,
        region: str = "ap-northeast-2",
        task_definition: str = "",
    ) -> Dict[str, Any]:
        """§37.3 /recoder preflight — ECS 배포 사전 점검."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/api/preflight/run",
                headers=self._headers,
                json={
                    "cluster": cluster,
                    "service": service,
                    "region": region,
                    "task_definition_family": task_definition,
                },
            )
            r.raise_for_status()
            return r.json()

    # ── Status ────────────────────────────────────────────────────────────

    async def status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """§37.3 /recoder status — 현재 배포/오케스트레이터 상태."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            params = {}
            if session_id:
                params["session_id"] = session_id
            r = await c.get(
                f"{self._base}/api/health",
                headers=self._headers,
                params=params,
            )
            r.raise_for_status()
            return r.json()

    # ── Deploy ────────────────────────────────────────────────────────────

    async def deploy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """§37.3 /recoder deploy — ECS 배포 트리거."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/api/ecs/deploy",
                headers=self._headers,
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    # ── Rollback ──────────────────────────────────────────────────────────

    async def rollback(
        self, cluster: str, service: str, target_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        """§37.3 /recoder rollback — ECS 이전 태스크 정의로 롤백."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            body: Dict[str, Any] = {"cluster": cluster, "service": service}
            if target_revision is not None:
                body["target_revision"] = target_revision
            r = await c.post(
                f"{self._base}/api/ecs/rollback",
                headers=self._headers,
                json=body,
            )
            r.raise_for_status()
            return r.json()

    # ── Code ──────────────────────────────────────────────────────────────

    async def code(self, prompt: str, project_path: str = ".") -> Dict[str, Any]:
        """§37.3 /recoder code — 코드 생성/분석 요청."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/api/analyze",
                headers=self._headers,
                json={"prompt": prompt, "project_path": project_path},
            )
            r.raise_for_status()
            return r.json()

    # ── Standup data ──────────────────────────────────────────────────────

    async def get_standup_data(self) -> Dict[str, Any]:
        """Daily Standup 생성을 위한 배포/인시던트 요약 데이터 조회."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{self._base}/api/session/history",
                headers=self._headers,
                params={"hours": 24},
            )
            r.raise_for_status()
            return r.json()

    # ── Replay data ───────────────────────────────────────────────────────

    async def get_replay_timeline(self, deploy_id: str) -> Dict[str, Any]:
        """Deploy Replay를 위한 타임라인 데이터 조회."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{self._base}/api/incident/timeline",
                headers=self._headers,
                params={"deploy_id": deploy_id},
            )
            r.raise_for_status()
            return r.json()

    # ── Workbench (Discord ↔ Core ↔ VSCode 양방향 sync) ────────────────────

    async def workbench_state(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /workbench/state — 통합 state."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            params = {"project_id": project_id} if project_id else {}
            r = await c.get(
                f"{self._base}/workbench/state",
                headers=self._headers,
                params=params,
            )
            r.raise_for_status()
            return r.json()

    async def workbench_change_mode(
        self, mode: str, source: str = "discord"
    ) -> Dict[str, Any]:
        """POST /workbench/mode — 활성 모드 전환."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/workbench/mode",
                headers=self._headers,
                json={"mode": mode, "source": source},
            )
            r.raise_for_status()
            return r.json()

    async def workbench_preflight_run(
        self,
        project_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        source: str = "discord",
    ) -> Dict[str, Any]:
        """POST /workbench/preflight/run — Preflight 실행."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/workbench/preflight/run",
                headers=self._headers,
                json={
                    "project_id": project_id,
                    "workspace_path": workspace_path,
                    "source": source,
                },
            )
            r.raise_for_status()
            return r.json()

    async def workbench_deployment_start(
        self,
        project_id: Optional[str] = None,
        image_digest: Optional[str] = None,
        git_commit: Optional[str] = None,
        target_env: str = "local",
        source: str = "discord",
    ) -> Dict[str, Any]:
        """POST /workbench/deployment/start — 배포 시작."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/workbench/deployment/start",
                headers=self._headers,
                json={
                    "project_id": project_id,
                    "image_digest": image_digest,
                    "git_commit": git_commit,
                    "target_env": target_env,
                    "source": source,
                },
            )
            r.raise_for_status()
            return r.json()

    async def workbench_rollback(
        self, deployment_id: str, source: str = "discord"
    ) -> Dict[str, Any]:
        """POST /workbench/deployment/{id}/rollback."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base}/workbench/deployment/{deployment_id}/rollback",
                headers=self._headers,
                params={"source": source},
            )
            r.raise_for_status()
            return r.json()

    async def workbench_list_deployments(
        self, project_id: Optional[str] = None, limit: int = 10
    ) -> list:
        """GET /workbench/deployments — 최근 배포 N건."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            params = {"limit": limit}
            if project_id:
                params["project_id"] = project_id
            r = await c.get(
                f"{self._base}/workbench/deployments",
                headers=self._headers,
                params=params,
            )
            r.raise_for_status()
            return r.json()

    async def workbench_events(self, since: int = 0) -> Dict[str, Any]:
        """GET /workbench/events?since=N — 양방향 sync 이벤트 polling."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{self._base}/workbench/events",
                headers=self._headers,
                params={"since": since},
            )
            r.raise_for_status()
            return r.json()
