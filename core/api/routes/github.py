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

from typing import Any, Optional

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
    #: 3.0.2 — 저장소를 만들고 올리므로 사용자 확인이 필요하다. `True` 가 아니면 400.
    approved: Any = None


class GhSetSecretRequest(BaseModel):
    repo: str
    name: str
    value: str
    #: 3.0.2 — Actions Secret 을 쓰거나 덮어쓰므로 사용자 확인이 필요하다. `True` 가 아니면 400.
    approved: Any = None


#: 승인 없는 GitHub 변경 거절 — 403 이 아니라 400 이다(확장 ApiClient 는 403 을 토큰 갱신 뒤 재시도한다).
PUSH_APPROVAL_REQUIRED = "GitHub 에 올리는 작업은 사용자 확인이 필요합니다. 확인 창에서 승인한 뒤 다시 시도하세요."
REPO_APPROVAL_REQUIRED = "GitHub 저장소를 만들고 올리는 작업은 사용자 확인이 필요합니다. 확인 창에서 승인한 뒤 다시 시도하세요."
SECRET_APPROVAL_REQUIRED = "GitHub Actions Secret 을 저장하는 작업은 사용자 확인이 필요합니다. 확인 창에서 승인한 뒤 다시 시도하세요."


def _require_approved(value: object, message: str) -> None:
    if value is not True:
        raise HTTPException(status_code=400, detail=message)


class GhRepositoryConnectRequest(BaseModel):
    repository: str = Field(..., pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
    create: bool = False
    #: 새로 만들 때만 쓴다. 기본은 예전처럼 비공개.
    private: bool = True


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
    _require_approved(body.approved, REPO_APPROVAL_REQUIRED)
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
    _require_approved(body.approved, SECRET_APPROVAL_REQUIRED)
    try:
        from github_agent import get_github_agent  # type: ignore
        return await asyncio.to_thread(
            get_github_agent().set_secret, body.repo, body.name, body.value,
        )
    except Exception as exc:
        logger.exception("[github] set_secret failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/github/repository/connect")
async def github_connect_repository(body: GhRepositoryConnectRequest) -> dict:
    """Validate access or create an empty (private by default, or public) repository; never commit/push."""
    from github_agent import get_github_agent
    return await asyncio.to_thread(get_github_agent().connect_repository, body.repository, body.create, body.private)


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
    """원격 push. 로그인 없이 존재하지 않는 GitAgent.push를 호출하지 않는다.

    3.0.2 — `approved: true` 가 없으면 400. 강제 push 는 `force: true` 를 정확히 줄 때만
    `--force-with-lease` 로 한다. 브랜치 이름은 `git check-ref-format --branch` 기준으로 검사한다.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="요청 형식이 올바르지 않습니다.")
    _require_approved(body.get("approved"), PUSH_APPROVAL_REQUIRED)
    workspace_path = str(body.get("workspace_path", ""))
    branch = str(body.get("branch", "") or "")
    force = body.get("force") is True
    # Canvas approvals can explicitly push existing commits only. Preserve legacy defaults.
    auto_commit = body.get("auto_commit", True) is not False
    try:
        from github_agent import validate_branch_name  # type: ignore
    except ImportError:  # pragma: no cover
        from core.github_agent import validate_branch_name  # type: ignore
    if branch:
        problem = validate_branch_name(branch)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
    try:
        from github_agent import get_github_agent  # type: ignore
        gh = get_github_agent()
        if getattr(gh, "_token", None):
            return await asyncio.to_thread(gh.push, workspace_path, branch, force, auto_commit=auto_commit)
        return {"status": "error", "message": "GitHub 로그인이 필요합니다. GitHub 패널에서 로그인한 뒤 다시 푸시하세요."}
    except Exception as exc:
        logger.exception("[github] git_push failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
