"""
ReCoder Core — GitHub Routes (modular)

main.py 의 app 에서 사용할 GitHub 관련 라우트.
이전엔 server.py 에 정의되어 있었으나 main.py 가 server.py 의 라우트를 include 하지
않아서 /api/github/* 가 404 → middleware 가 401 로 회신하던 버그를 수정.

엔드포인트:
  GET  /api/github/status         — 인증 상태
  POST /api/github/token          — VS Code OAuth 토큰 등록 (부트스트랩)
  GET  /api/github/repos          — 인증된 사용자 레포 목록
  POST /api/github/repo           — 새 레포 생성 + 초기 push
  POST /api/github/secret         — Actions Secret 등록 (PyNaCl sealed-box)
  GET  /api/github/runs           — 워크플로 실행 이력
  GET  /api/github/branches       — 로컬 워크스페이스 브랜치 목록
  POST /api/github/logout         — 토큰 제거
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["github"])


# ── Pydantic 요청 모델 ─────────────────────────────────────────────────


class GhTokenRequest(BaseModel):
    token: str = Field(..., min_length=10)


class GhRepoCreateRequest(BaseModel):
    workspace_path: str
    name: str
    private: bool = True
    description: str = ""


class GhSetSecretRequest(BaseModel):
    repo: str
    name: str
    value: str


# ── 엔드포인트 ──────────────────────────────────────────────────────────


@router.get("/api/github/status")
async def github_status(force: bool = False) -> dict:
    """GitHub 인증 상태 — Sidebar/Workbench 진입 시 호출.

    force=true 면 캐시 무시하고 새로 조회 (수동 새로고침용).
    """
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(get_github_agent().status, force)
    except Exception as exc:
        logger.exception("[github] status failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/github/token")
async def github_set_token(body: GhTokenRequest) -> dict:
    """Extension 이 VS Code OAuth 로 획득한 GitHub 토큰을 Core 에 저장.

    저장 즉시 /user API 로 유효성 검증 → { status, user } 반환.
    """
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(get_github_agent().set_token, body.token)
    except Exception as exc:
        logger.exception("[github] set_token failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/github/repos")
async def github_list_repos() -> dict:
    """인증된 사용자의 GitHub 레포지토리 목록."""
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(get_github_agent().list_repos)
    except Exception as exc:
        logger.exception("[github] list_repos failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/github/repo")
async def github_repo_create(body: GhRepoCreateRequest) -> dict:
    """새 레포 생성 + 워크스페이스 초기 push."""
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(
            get_github_agent().repo_create_and_push,
            body.workspace_path, body.name, body.private, body.description,
        )
    except Exception as exc:
        logger.exception("[github] repo_create failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/github/secret")
async def github_set_secret(body: GhSetSecretRequest) -> dict:
    """GitHub Actions Secret 등록 (PyNaCl sealed-box 암호화)."""
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(
            get_github_agent().set_secret, body.repo, body.name, body.value,
        )
    except Exception as exc:
        logger.exception("[github] set_secret failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/github/runs")
async def github_list_runs(repo: str) -> dict:
    """워크플로 실행 이력 (?repo=owner/name)."""
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(get_github_agent().list_runs, repo)
    except Exception as exc:
        logger.exception("[github] list_runs failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/github/branches")
async def github_list_branches(workspace_path: str = "") -> dict:
    """로컬 워크스페이스의 git 브랜치 목록."""
    try:
        from github_agent import get_github_agent  # type: ignore
        result = await asyncio.to_thread(
            get_github_agent().list_branches, workspace_path,
        )
        return {"branches": result} if isinstance(result, list) else result
    except Exception as exc:
        logger.exception("[github] list_branches failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/github/logout")
async def github_logout() -> dict:
    """저장된 GitHub 토큰 제거."""
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(get_github_agent().logout)
    except Exception as exc:
        logger.exception("[github] logout failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/git/push")
async def git_push_route(body: dict) -> dict:
    """원격 push. github_agent 토큰이 있으면 사용, 없으면 git_agent 폴백."""
    workspace_path = str(body.get("workspace_path", ""))
    branch = str(body.get("branch", ""))
    force = bool(body.get("force", False))
    try:
        from github_agent import get_github_agent  # type: ignore
        gh = get_github_agent()
        if getattr(gh, "_token", None):
            return await asyncio.to_thread(gh.push, workspace_path, branch, force)
        from git_agent import get_git_agent  # type: ignore
        return await asyncio.to_thread(
            get_git_agent().push, workspace_path, branch, force,
        )
    except Exception as exc:
        logger.exception("[github] git_push failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
