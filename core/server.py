"""
server.py — ReCoder v6.4 Local Core FastAPI 서버 (LEGACY MONOLITH — DEPRECATED)

⚠️ 이 모듈은 더 이상 진입점(entry point)이 아닙니다.

현재 실제 entry point: core/main.py
실제 라우터 위치: core/api/routes/*.py (health · analyze · deploy · ecs · ops ·
                  session · policy · gitops · incident)

본 파일에 정의된 60여 개의 @app.* 라우트는 main.py 가 import 하지 않으며,
프로젝트 내 어떤 코드도 server.py 를 직접 import 하지 않습니다 (확인됨,
2026-05-17). 따라서 본 파일은 backward-compat 또는 점진 마이그레이션 참고용
스냅샷으로만 보존됩니다.

설계서 §4 (Local Core) / §5 (보안) / §6 (Lifecycle) / §10~17 (Agents) 결선체로
작성된 원본이며, P0-1~P0-13 적용 이후 모듈식 api/routes/* 구조로 이전됐습니다.
새 라우트 추가는 api/routes/ 에서 하고, server.py 는 수정하지 마십시오.
단계적 제거 예정 (잔여 권고 §4.1).

본 모듈은 FastAPI 라우팅·미들웨어·요청-응답 모델만 책임지며,
실제 비즈니스 로직은 모두 core/* 의 agent/registry 모듈에 위임한다.

레거시 변경 이력 (2026-05-08, P0-1~P0-13 적용):
- Mock 응답 전면 제거 → analyzer/code_agent/infra_agent/local_deploy_agent 실배선
- /api/deploy/status, /api/security/scan, /api/ready 신규
- /api/cost 를 SessionLogger SQLite 누적치와 결선
- /api/project/scan 을 ProjectScanner 와 결선
- background task 로 docker build/run 실행, 진행 상태 메모리 보관
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from schemas import (
    AnalyzeRequest, DeploymentPlan, DeployMethod, FilePatch, InfraFileProposal,
    OrchestratorState, OrchestratorUpdate, PatchProposal, ProjectProfile,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# ── 경로 / 기본 설정 ──────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path.home() / ".recoder"
RUNTIME_FILE = RUNTIME_DIR / "runtime.json"

# ── 포트 / 토큰 ───────────────────────────────────────────────────────

SESSION_TOKEN: str = os.getenv("SESSION_TOKEN", uuid.uuid4().hex)
DEFAULT_PORT = 17894
PORT: int = int(os.getenv("LOCAL_PORT", str(DEFAULT_PORT)))


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _find_available_port(start_port: int = DEFAULT_PORT, max_port: int = 17910) -> int:
    for port in range(start_port, max_port + 1):
        if _port_available(port):
            return port
    return start_port


if not _port_available(PORT):
    PORT = _find_available_port(PORT + 1)


# ── FastAPI 앱 ────────────────────────────────────────────────────────

app = FastAPI(title="ReCoder v6.4 Local Core", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Origin 검증은 _OriginHostMiddleware 에서 단일 처리
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEV_MODE: bool = os.getenv("DEV_MODE", "0").strip() in ("1", "true", "yes")

# 허용할 추가 호스트 (EC2 IP 등) - 환경변수 ALLOWED_HOSTS 에 콤마로 구분해 입력
_EXTRA_HOSTS: set[str] = {
    h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()
}


class _OriginHostMiddleware(BaseHTTPMiddleware):
    """Origin / Host 헤더 검증 (§5.2)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/" or path == "/dashboard":
            return await call_next(request)

        # DEV_MODE 이거나 ALLOWED_HOSTS 가 "*" 이면 Host 검증 스킵
        if not _DEV_MODE and "*" not in _EXTRA_HOSTS:
            host = request.headers.get("host", "")
            host_base = host.split(":")[0]  # 포트 제거
            allowed = (
                not host
                or host.startswith("127.0.0.1")
                or host.startswith("localhost")
                or host_base in _EXTRA_HOSTS
                or host in _EXTRA_HOSTS
            )
            if not allowed:
                return Response(
                    content='{"detail": "Invalid Host header"}',
                    status_code=403,
                    media_type="application/json",
                )

        if request.method in _WRITE_METHODS:
            origin = request.headers.get("origin", "")
            if origin in ("", "null"):
                if not _DEV_MODE:
                    return Response(
                        content='{"detail": "Origin header required"}',
                        status_code=403,
                        media_type="application/json",
                    )
            elif origin not in _SAFE_ORIGINS and "*" not in _EXTRA_HOSTS:
                # DEV_MODE 이거나 vscode-webview Origin 이면 허용
                if not _DEV_MODE and not origin.startswith("vscode-webview://"):
                    return Response(
                        content='{"detail": "Origin not allowed"}',
                        status_code=403,
                        media_type="application/json",
                    )

        return await call_next(request)


app.add_middleware(_OriginHostMiddleware)


# ── 토큰 검증 ─────────────────────────────────────────────────────────

def _verify_token(request: Request) -> None:
    token = request.headers.get("X-Session-Token", "")
    if token != SESSION_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid session token")


# ── 상태 저장소 ───────────────────────────────────────────────────────

_session_ref: dict = {}
_session_id: str = ""              # SessionLogger.create_session() 결과 캐시
_orchestrator_state: OrchestratorState = OrchestratorState.IDLE

_current_patch: Optional[PatchProposal] = None
_current_infra: Optional[InfraFileProposal] = None
_current_plan: Optional[DeploymentPlan] = None
_current_project: Optional[ProjectProfile] = None
_current_deploy_record = None  # S-9: 롤백용 마지막 성공 배포 기록

# 비동기 docker build/run 진행 상태
_deploy_progress: dict = {
    "stage": "idle",          # idle | building | running | health | done | failed
    "log_tail": [],           # 최근 줄 모음 (최대 200)
    "health": None,           # bool | None
    "finished": True,
    "error": "",
    "started_at": "",
    "finished_at": "",
}
_deploy_task: Optional[asyncio.Task] = None

# EC2 배포 진행 상태
_ec2_deploy_state: dict = {
    "running":     False,
    "stage":       "idle",   # idle | building | ecr_login | ecr_push | ec2_deploy | done | failed
    "log_tail":    [],
    "image_uri":   "",
    "error":       "",
    "started_at":  "",
    "finished_at": "",
}

# ECS Fargate 배포 진행 상태
_ecs_deploy_state: dict = {
    "running":        False,
    "stage":          "idle",   # idle | building | ecr_push | task_def | svc_update | deploying | done | failed
    "log_tail":       [],
    "image_uri":      "",
    "task_def_arn":   "",
    "error":          "",
    "started_at":     "",
    "finished_at":    "",
    "rollback_proposal": None,  # Circuit Breaker 발동 시 채워짐
}

_server_ready = asyncio.Event()


def _ensure_session() -> str:
    """첫 호출 시 SessionLogger 세션 생성, 이후 캐시된 ID 재사용."""
    global _session_id
    if _session_id:
        return _session_id
    try:
        from session_logger import get_session_logger
        project_id = _current_project.project_id if _current_project else "default"
        record = get_session_logger().create_session(project_id)
        _session_id = record.session_id
    except Exception as e:
        logger.warning(f"[server] SessionLogger 초기화 실패: {e}")
        _session_id = uuid.uuid4().hex
    return _session_id


# ── 에러 텍스트 추출 ─────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(r"^(Traceback \(most recent call last\):.*?)(?=\n\S|\Z)", re.S | re.M),
    re.compile(r"^(\w+(?:Error|Exception):.*?)$", re.M),
    re.compile(r"^(error\s+TS\d+:.*?)$", re.M | re.I),
    re.compile(r"^(npm ERR!.*?)$", re.M),
    re.compile(r"^(yarn run.*?ERROR.*?)$", re.M | re.I),
]


def _extract_error_text(terminal_output: str) -> str:
    """터미널 출력에서 에러 본문만 추출. 못 찾으면 마지막 30줄을 반환."""
    if not terminal_output:
        return ""
    for pat in _ERROR_PATTERNS:
        m = pat.search(terminal_output)
        if m:
            return m.group(1).strip()
    lines = terminal_output.strip().splitlines()
    return "\n".join(lines[-30:])


# ── runtime.json ──────────────────────────────────────────────────────

def _save_runtime_config() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    config = {
        "port": PORT,
        "session_token": SESSION_TOKEN,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    RUNTIME_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


# ── Pydantic 요청 모델 ────────────────────────────────────────────────

class AnalyzeRequestBody(BaseModel):
    """Extension → Core 분석 요청 (FastAPI body)."""
    workspace_path: str = ""
    terminal_output: str = ""
    project_id: str = ""
    active_file_path: str = ""
    selected_text: str = ""
    command: str = ""
    project_files_summary: str = ""
    error_text: str = ""
    file_context: str = ""
    related_files: list[str] = Field(default_factory=list)


class ScanWorkspaceRequest(BaseModel):
    workspace_path: str


class PatchApproveRequest(BaseModel):
    proposal_id: str


class PatchRejectRequest(BaseModel):
    proposal_id: str


class InfraGenerateRequest(BaseModel):
    project_id: str = ""
    file_type: str = "dockerfile"
    workspace_path: str = ""


class InfraApproveRequest(BaseModel):
    proposal_id: str


class DeployLocalRequest(BaseModel):
    plan_id: str = ""
    project_id: str = ""
    workspace_path: str = ""
    image: str = "recoder-app:latest"
    container_name: str = "recoder-app"
    host_port: int = 8000
    container_port: int = 8000
    health_check_path: str = "/health"


class SecurityScanRequest(BaseModel):
    image: str = ""              # Trivy 대상
    dockerfile_path: str = ""    # Hadolint 대상
    workspace_path: str = ""     # Gitleaks 대상


class GitCommitRequest(BaseModel):
    workspace_path: str
    message: str
    session_id: str = ""


class GitCheckoutRequest(BaseModel):
    workspace_path: str
    branch: str


class GitBranchCreateRequest(BaseModel):
    workspace_path: str
    branch_name: str
    checkout: bool = True


class GitPushRequest(BaseModel):
    workspace_path: str
    branch: str = ""
    force: bool = False


class DeployRollbackRequest(BaseModel):
    plan_id: str


class EC2DeployRequest(BaseModel):
    """EC2 배포 요청."""
    workspace_path: str = ""
    image_name:     str = "recoder-app"
    repo_name:      str = "recoder-app"
    tag:            str = "latest"
    container_name: str = "recoder-app"
    host_port:      int = 8000
    container_port: int = 8000
    health_check_path: str = "/health"
    env_vars:       list[str] = Field(default_factory=list)
    # 아래 값은 미전달 시 환경변수(ECR_REGISTRY, EC2_HOST, EC2_SSH_KEY)에서 자동 로드
    ecr_registry:   str = ""
    ec2_host:       str = ""
    ec2_ssh_key:    str = ""
    aws_region:     str = ""
    ec2_user:       str = "ec2-user"


class ECSDeployRequest(BaseModel):
    """ECS Fargate 배포 요청 (Q3-A Rolling Update)."""
    workspace_path:  str = ""
    image_name:      str = "recoder-app"
    repo_name:       str = "recoder-app"
    tag:             str = "latest"
    # 아래 값은 미전달 시 환경변수(ECR_REGISTRY, ECS_CLUSTER, ECS_SERVICE)에서 자동 로드
    ecr_registry:    str = ""
    ecs_cluster:     str = ""
    ecs_service:     str = ""
    aws_region:      str = ""
    container_name:  str = "app"
    container_port:  int = 8000
    cpu:             str = "256"
    memory:          str = "512"
    env_vars:        list[dict] = Field(default_factory=list)  # [{"name":"K","value":"V"}]
    task_family:     str = "recoder-task"
    environment:     str = "staging"   # staging | production (OPA 정책 평가용)
    branch:          str = ""          # Git 브랜치 (production + main 규칙용)
    skip_sbom:       bool = False      # 테스트용 SBOM 생성 건너뜀
    skip_opa:        bool = False      # 테스트용 OPA 게이트 건너뜀


class OPAEvaluateRequest(BaseModel):
    """범용 OPA 정책 평가 요청."""
    policy_path:    str = "recoder/deploy/allow"
    input_data:     dict = Field(default_factory=dict)
    approval_level: int = 3


class SBOMGenerateRequest(BaseModel):
    """SBOM 생성 요청."""
    image_uri: str
    tag:       str = "latest"


class GhLoginRequest(BaseModel):
    """deprecated — VS Code OAuth 방식으로 대체됨. 호환성 유지용."""
    pass


class GhTokenRequest(BaseModel):
    """Extension 이 VS Code OAuth 토큰을 Core 에 전달하는 요청."""
    token: str


class GhRepoCreateRequest(BaseModel):
    workspace_path: str
    name: str  # OWNER/NAME 또는 NAME
    private: bool = True
    description: str = ""


class GhSetSecretRequest(BaseModel):
    repo: str
    name: str
    value: str


class ShipGitHubRequest(BaseModel):
    """7-step GitHub 파이프라인 입력."""
    workspace_path: str
    repo_name: str
    private: bool = True
    description: str = ""
    secrets: dict[str, str] = Field(default_factory=dict)
    include_dockerfile: bool = True
    include_compose: bool = True
    include_actions: bool = True
    include_dockerignore: bool = True


# ── Q4 Must-Wedge Request Models ──────────────────────────────────────────

class GitOpsShipRequest(BaseModel):
    """GitOps ArgoCD 배포 요청."""
    app_name:        str
    repo_url:        str
    ecr_image_uri:   str
    image_tag:       str = "latest"
    namespace:       str = "default"
    container_port:  int = 8000
    replica_count:   int = 2
    cpu:             str = "256"
    memory:          str = "512"
    environment:     str = "staging"
    helm_chart_path: str = "helm"
    target_revision: str = "main"
    env_vars:        dict = Field(default_factory=dict)
    argocd_url:      str = ""
    github_token:    str = ""


class RollbackPRRequest(BaseModel):
    """Rollback PR 생성 요청 (ADR-005)."""
    app_name:               str
    environment:            str = "production"
    failed_image_tag:       str
    last_healthy_image_tag: str
    helm_values_path:       str = "helm/values.yaml"
    argocd_app_name:        str = ""
    error_summary:          str = ""
    deployed_by:            str = ""
    cluster:                str = ""
    namespace:              str = "default"
    incident_severity:      int = 2
    incident_id:            str = ""
    emergency:              bool = False
    github_token:           str = ""
    github_repo:            str = ""


class PostmortemRequest(BaseModel):
    """Postmortem skeleton 생성 요청."""
    incident_id:            str
    app_name:               str
    environment:            str = "production"
    severity:               int = 2
    title:                  str = ""
    failed_image_tag:       str = ""
    last_healthy_image_tag: str = ""
    argocd_app_name:        str = ""
    rollback_pr_url:        str = ""
    failed_at:              str = ""
    resolved_at:            str = ""
    deployed_by:            str = ""
    cluster:                str = ""
    namespace:              str = "default"
    error_summary:          str = ""
    affected_users:         str = ""
    revenue_impact:         str = ""
    otel_trace_id:          str = ""
    otel_endpoint:          str = ""
    extra_refs:             list[str] = Field(default_factory=list)


# ── Ship pipeline 진행 상태 (인메모리) ────────────────────────────────

_SHIP_STATE: dict = {
    "running": False,
    "steps": [],
    "current": "",
    "error": "",
    "repo_url": "",
    "started_at": "",
    "finished_at": "",
}
_SHIP_STEP_LABELS = [
    ("init", "Git 초기화"),
    ("files", "파일 생성"),
    ("commit", "커밋"),
    ("repo", "Repo 생성"),
    ("push", "Push"),
    ("secrets", "Secrets 등록"),
    ("actions", "Actions 확인"),
]


def _ship_reset() -> None:
    _SHIP_STATE["running"] = True
    _SHIP_STATE["error"] = ""
    _SHIP_STATE["repo_url"] = ""
    _SHIP_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _SHIP_STATE["finished_at"] = ""
    _SHIP_STATE["current"] = ""
    _SHIP_STATE["steps"] = [
        {"id": sid, "label": lbl, "status": "pending", "message": ""}
        for sid, lbl in _SHIP_STEP_LABELS
    ]


def _ship_set(step_id: str, status: str, message: str = "") -> None:
    _SHIP_STATE["current"] = step_id
    for s in _SHIP_STATE["steps"]:
        if s["id"] == step_id:
            s["status"] = status
            if message:
                s["message"] = message[:200]
            break


def _ship_finish(error: str = "", repo_url: str = "") -> None:
    _SHIP_STATE["running"] = False
    _SHIP_STATE["error"] = error
    if repo_url:
        _SHIP_STATE["repo_url"] = repo_url
    _SHIP_STATE["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Health & Status ───────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """헬스 체크 (Polling 진입점, 토큰 미요구)."""
    return {
        "status": "ok",
        "version": "6.4",
        "state": _orchestrator_state.value,
        "port": PORT,
    }


@app.get("/api/status")
async def get_status(_=Depends(_verify_token)):
    """현재 Orchestrator 상태 + 활성 제안 (Polling 용)."""
    update = OrchestratorUpdate(
        state=_orchestrator_state,
        patch_proposal=_current_patch,
        infra_proposal=_current_infra,
        plan=_current_plan,
        message="",
    )
    return update.to_dict()


@app.get("/api/ready")
async def get_ready(_=Depends(_verify_token)):
    """First Run 진단 결과 (Sidebar Ready 카드용)."""
    try:
        from first_run import load_diagnostics
        diag = load_diagnostics()
        if diag is None:
            # 아직 진단 결과가 없으면 즉시 한 번 실행
            from first_run import run_diagnostics, save_diagnostics
            diag = await run_diagnostics()
            save_diagnostics(diag)
        return diag.to_dict()
    except Exception as e:
        logger.exception("[server] /api/ready failed")
        return {"core_ready": "fail", "ai_ready": "fail", "docker_ready": "fail",
                "issues": [str(e)]}


# ── Project ───────────────────────────────────────────────────────────

@app.get("/api/project")
async def get_project(_=Depends(_verify_token)):
    if _current_project:
        return _current_project.to_dict()
    return None


@app.post("/api/project/scan")
async def scan_project(body: ScanWorkspaceRequest, _=Depends(_verify_token)):
    """워크스페이스 스캔 → ProjectProfile 반환 + 메모리 캐시."""
    global _current_project

    if not body.workspace_path.strip():
        raise HTTPException(status_code=400, detail="VS Code에서 프로젝트 폴더를 먼저 열어주세요.")
    workspace_path = Path(body.workspace_path).expanduser().resolve()
    if not workspace_path.exists():
        raise HTTPException(status_code=404, detail="워크스페이스 경로가 없습니다.")

    try:
        from project_scanner import get_project_scanner
        profile = get_project_scanner().scan(str(workspace_path))
    except Exception as e:
        logger.exception("[server] project scan failed")
        raise HTTPException(status_code=500, detail=f"스캔 실패: {e}") from e

    _current_project = profile
    return profile.to_dict()


# ── Stage 1: Analyze & Patch ──────────────────────────────────────────

@app.post("/api/analyze")
async def analyze(body: AnalyzeRequestBody, _=Depends(_verify_token)):
    """
    Extension 의 분석 요청 → analyzer.analyze + code_agent.generate_patch 체인.

    응답: PatchProposal.to_dict() (Webview 가 직접 사용)
    """
    global _orchestrator_state, _current_patch

    # 1) AnalyzeRequest dataclass 로 변환
    error_text = body.error_text or _extract_error_text(body.terminal_output)
    request = AnalyzeRequest(
        workspace_path=body.workspace_path or (
            _current_project.workspace_path if _current_project else ""
        ),
        terminal_output=body.terminal_output,
        project_id=body.project_id or (
            _current_project.project_id if _current_project else ""
        ),
        active_file_path=body.active_file_path,
        selected_text=body.selected_text,
        command=body.command,
        project_files_summary=body.project_files_summary,
        error_text=error_text,
        file_context=body.file_context,
        related_files=list(body.related_files),
    )

    if not error_text.strip() and not request.terminal_output.strip():
        raise HTTPException(status_code=400, detail="분석할 에러 텍스트가 없습니다.")

    _orchestrator_state = OrchestratorState.ANALYZING
    session_id = _ensure_session()

    # 2) analyzer.analyze (LLM 1차 분석, AgentEvent)
    try:
        from analyzer import analyze as analyzer_analyze
        await analyzer_analyze(request, session_id=session_id)
    except Exception as e:
        # analyzer 가 실패해도 code_agent 단독으로 시도 가능 → 경고만
        logger.warning(f"[server] analyzer 단계 실패 (계속 진행): {e}")

    # 3) code_agent.generate_patch (실제 PatchProposal)
    try:
        # workspace_path 가 명시적으로 들어왔으면 code_agent 가 그 루트를 보도록 환경변수 설정
        if request.workspace_path:
            os.environ["RECODER_PROJECT_ROOT"] = request.workspace_path

        from code_agent import generate_patch
        proposal = generate_patch(request, session_id=session_id)
    except Exception as e:
        _orchestrator_state = OrchestratorState.IDLE
        logger.exception("[server] generate_patch failed")
        raise HTTPException(
            status_code=500,
            detail=f"PatchProposal 생성 실패: {e}",
        ) from e

    _current_patch = proposal
    _orchestrator_state = OrchestratorState.CODE_PATCH_PROPOSED
    return proposal.to_dict()


@app.post("/api/patch/approve")
async def approve_patch(body: PatchApproveRequest, _=Depends(_verify_token)):
    """PatchProposal 승인 → code_agent.apply_patch 실행."""
    global _orchestrator_state, _current_patch

    if _current_patch is None or _current_patch.proposal_id != body.proposal_id:
        raise HTTPException(status_code=400, detail="유효하지 않은 proposal_id 입니다.")

    _orchestrator_state = OrchestratorState.APPLYING_PATCH
    try:
        from code_agent import apply_patch
        result = apply_patch(_current_patch)
    except Exception as e:
        _orchestrator_state = OrchestratorState.IDLE
        logger.exception("[server] apply_patch failed")
        raise HTTPException(status_code=500, detail=f"패치 적용 실패: {e}") from e

    if result.get("success"):
        _orchestrator_state = OrchestratorState.CODE_READY
    else:
        _orchestrator_state = OrchestratorState.IDLE

    return {
        "status": "ok" if result.get("success") else "error",
        "proposal_id": _current_patch.proposal_id,
        "applied_files": result.get("applied_files", []),
        "error": result.get("error", ""),
        "message": "패치가 적용되었습니다." if result.get("success") else result.get("error", ""),
    }


@app.post("/api/patch/reject")
async def reject_patch(body: PatchRejectRequest, _=Depends(_verify_token)):
    global _orchestrator_state, _current_patch
    _current_patch = None
    _orchestrator_state = OrchestratorState.IDLE
    return {"status": "ok", "message": "패치가 거절되었습니다."}


@app.post("/api/patch/rollback")
async def rollback_patch_endpoint(body: PatchApproveRequest, _=Depends(_verify_token)):
    """code_agent.rollback_patch 호출."""
    try:
        from code_agent import rollback_patch
        result = rollback_patch(body.proposal_id)
    except Exception as e:
        logger.exception("[server] rollback_patch failed")
        raise HTTPException(status_code=500, detail=f"롤백 실패: {e}") from e
    return result


# ── Stage 2: Infra & Deploy ───────────────────────────────────────────

@app.post("/api/infra/generate")
async def generate_infra(body: InfraGenerateRequest, _=Depends(_verify_token)):
    """InfraFileProposal 생성 (Dockerfile / docker-compose / github-actions / dockerignore)."""
    global _orchestrator_state, _current_infra

    file_type = body.file_type.lower()
    _raw_workspace = body.workspace_path or (
        _current_project.workspace_path if _current_project else "."
    )
    # 원격 배포 시 로컬 경로가 없을 수 있으므로 현재 디렉터리로 폴백
    workspace = _raw_workspace if Path(_raw_workspace).exists() else str(Path.cwd())

    try:
        from infra_agent import (
            generate_dockerfile, generate_docker_compose, generate_github_actions,
            generate_dockerignore,
        )
        if file_type == "dockerignore":
            proposal = generate_dockerignore(
                project_profile=_current_project,
                workspace_path=workspace,
            )
        elif file_type == "dockerfile":
            proposal = generate_dockerfile(
                request=None,
                project_profile=_current_project,
                workspace_path=workspace,
            )
        elif file_type == "docker-compose":
            proposal = generate_docker_compose(
                project_profile=_current_project,
                workspace_path=workspace,
            )
        elif file_type == "github-actions":
            proposal = generate_github_actions(
                project_profile=_current_project,
                workspace_path=workspace,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="file_type 은 dockerfile / docker-compose / github-actions / dockerignore 중 하나여야 합니다.",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[server] infra generate failed")
        raise HTTPException(status_code=500, detail=f"인프라 파일 생성 실패: {e}") from e

    _current_infra = proposal
    _orchestrator_state = OrchestratorState.INFRA_PROPOSED
    return proposal.to_dict()


@app.post("/api/infra/approve")
async def approve_infra(body: InfraApproveRequest, _=Depends(_verify_token)):
    """InfraFileProposal 승인 → 워크스페이스에 파일 저장 + DeploymentPlan 자동 준비."""
    global _orchestrator_state, _current_infra, _current_plan

    if _current_infra is None or _current_infra.proposal_id != body.proposal_id:
        raise HTTPException(status_code=400, detail="유효하지 않은 proposal_id 입니다.")

    workspace = Path(
        _current_project.workspace_path if _current_project else "."
    ).resolve()
    target = workspace / _current_infra.target_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_current_infra.content, encoding="utf-8")
    except Exception as e:
        # 원격 배포 시 로컬 경로가 없을 수 있음 → 파일 쓰기 스킵하고 계속 진행
        logger.warning(f"[server] infra file write skipped (remote mode?): {e}")

    _orchestrator_state = OrchestratorState.INFRA_READY

    # Dockerfile 승인 직후에는 자동으로 DeploymentPlan 도 준비해 둔다 (UX: Ship 탭 ▶ 즉시 가능)
    plan_dict = None
    if _current_infra.file_type == "Dockerfile":
        port = _current_project.default_port if _current_project else 8000
        plan = DeploymentPlan(
            plan_id=uuid.uuid4().hex,
            method=DeployMethod.LOCAL_DOCKER,
            action="build_and_run",
            image="recoder-app:latest",
            container_name="recoder-app",
            command_template_id="docker_build",
            risk_level=RiskLevel.LOW,
            approval_level=2,
            ports=[{"host": port, "container": port}],
            health_check_path=(
                _current_project.health_check_path if _current_project else "/health"
            ),
        )
        _current_plan = plan
        plan_dict = plan.to_dict()

    return {
        "status": "ok",
        "saved_path": str(target),
        "proposal_id": _current_infra.proposal_id,
        "plan": plan_dict,
        "message": "인프라 파일이 저장되었습니다.",
    }


def _reset_progress(stage: str = "idle") -> None:
    _deploy_progress.update(
        stage=stage, log_tail=[], health=None, finished=False, error="",
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_at="",
    )


def _append_log(line: str) -> None:
    if not line:
        return
    _deploy_progress["log_tail"].append(line)
    if len(_deploy_progress["log_tail"]) > 200:
        _deploy_progress["log_tail"] = _deploy_progress["log_tail"][-200:]


async def _run_deploy_in_background(plan: DeploymentPlan, workspace: str, project_id: str) -> None:
    """백그라운드 task: docker build → run → health check."""
    global _orchestrator_state
    try:
        from local_deploy_agent import get_local_deploy_agent
        agent = get_local_deploy_agent()

        _deploy_progress["stage"] = "building"
        _orchestrator_state = OrchestratorState.DOCKER_BUILDING
        _append_log(f"[BUILD] image={plan.image}")

        # blocking 작업이므로 thread 로 실행
        result = await asyncio.to_thread(
            agent.deploy, plan, workspace, project_id,
        )

        for ln in result.logs or []:
            _append_log(ln)

        _deploy_progress["health"] = bool(result.success)
        _deploy_progress["error"] = result.error or ""

        if result.success:
            global _current_deploy_record
            _current_deploy_record = result.deployment_record
            _deploy_progress["stage"] = "done"
            _orchestrator_state = OrchestratorState.DEPLOYED
        else:
            # 헬스체크 실패 → 자동 롤백 시도 (§S-9)
            _deploy_progress["stage"] = "rollback"
            _append_log("[ROLLBACK] Health check failed — attempting auto-rollback...")
            try:
                rollback_result = await asyncio.to_thread(
                    agent.rollback_latest, project_id, workspace,
                )
                for ln in rollback_result.logs or []:
                    _append_log(ln)
                if rollback_result.success:
                    _deploy_progress["stage"] = "rolled_back"
                    _orchestrator_state = OrchestratorState.DEPLOY_FAILED
                    _append_log("[ROLLBACK] Auto-rollback succeeded.")
                else:
                    _deploy_progress["stage"] = "failed"
                    _deploy_progress["error"] = rollback_result.error or result.error
                    _orchestrator_state = OrchestratorState.DEPLOY_FAILED
                    _append_log(f"[ROLLBACK] Auto-rollback failed: {rollback_result.error}")
            except Exception as rb_exc:
                logger.error(f"[server] auto-rollback exception: {rb_exc}")
                _deploy_progress["stage"] = "failed"
                _deploy_progress["error"] = result.error or str(rb_exc)
                _orchestrator_state = OrchestratorState.DEPLOY_FAILED

    except Exception as e:
        logger.exception("[server] background deploy crashed")
        _deploy_progress["stage"] = "failed"
        _deploy_progress["error"] = str(e)
        _orchestrator_state = OrchestratorState.DEPLOY_FAILED
    finally:
        _deploy_progress["finished"] = True
        _deploy_progress["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


@app.post("/api/deploy/local")
async def deploy_local(body: DeployLocalRequest, _=Depends(_verify_token)):
    """로컬 Docker 배포 시작 (background). UI 는 /api/deploy/status 로 polling."""
    global _orchestrator_state, _current_plan, _deploy_task

    if _deploy_task and not _deploy_task.done():
        raise HTTPException(status_code=409, detail="이미 진행 중인 배포가 있습니다.")

    workspace = body.workspace_path or (
        _current_project.workspace_path if _current_project else "."
    )
    project_id = body.project_id or (
        _current_project.project_id if _current_project else "default"
    )

    # plan_id 재사용 또는 신규 생성
    plan = _current_plan
    if plan is None or (body.plan_id and body.plan_id != plan.plan_id):
        plan = DeploymentPlan(
            plan_id=body.plan_id or uuid.uuid4().hex,
            method=DeployMethod.LOCAL_DOCKER,
            action="build_and_run",
            image=body.image,
            container_name=body.container_name,
            command_template_id="docker_build",
            risk_level=RiskLevel.LOW,
            approval_level=2,
            ports=[{"host": body.host_port, "container": body.container_port}],
            health_check_path=body.health_check_path,
        )
        _current_plan = plan

    _reset_progress("building")
    _orchestrator_state = OrchestratorState.DEPLOYING
    _deploy_task = asyncio.create_task(
        _run_deploy_in_background(plan, workspace, project_id)
    )

    return {
        "status": "ok",
        "plan": plan.to_dict(),
        "message": "배포가 시작되었습니다. /api/deploy/status 로 진행 상황을 확인하세요.",
    }


@app.get("/api/deploy/status")
async def get_deploy_status(_=Depends(_verify_token)):
    """배포 진행 상태 (Sidebar 진행률/로그/Health 결과 polling 대상)."""
    return {
        "stage": _deploy_progress["stage"],
        "log_tail": list(_deploy_progress["log_tail"]),
        "health": _deploy_progress["health"],
        "finished": _deploy_progress["finished"],
        "error": _deploy_progress["error"],
        "started_at": _deploy_progress["started_at"],
        "finished_at": _deploy_progress["finished_at"],
        "state": _orchestrator_state.value,
    }


# ── Security Scan ─────────────────────────────────────────────────────

@app.post("/api/security/scan")
async def security_scan(body: SecurityScanRequest, _=Depends(_verify_token)):
    """Trivy + Hadolint 일회성 컨테이너 스캔. 결과는 ScanResult 2개로 묶어 반환."""
    try:
        from quality_runner import get_quality_runner
        runner = get_quality_runner()
    except Exception as e:
        logger.exception("[server] quality_runner import failed")
        raise HTTPException(status_code=500, detail=f"스캔 모듈 로드 실패: {e}") from e

    results: dict[str, dict] = {}
    if body.image:
        try:
            r = await asyncio.to_thread(runner.run_trivy, body.image)
            results["trivy"] = _scan_to_dict(r)
        except Exception as e:
            results["trivy"] = {"tool": "trivy", "passed": False, "error": str(e)}

    dockerfile_path = body.dockerfile_path or (
        str(Path(_current_project.workspace_path) / "Dockerfile")
        if _current_project else ""
    )
    if dockerfile_path and Path(dockerfile_path).exists():
        try:
            r = await asyncio.to_thread(runner.run_hadolint, dockerfile_path)
            results["hadolint"] = _scan_to_dict(r)
        except Exception as e:
            results["hadolint"] = {"tool": "hadolint", "passed": False, "error": str(e)}

    # Gitleaks 시크릿 스캔
    workspace_path = body.workspace_path or (
        _current_project.workspace_path if _current_project else ""
    )
    if workspace_path and Path(workspace_path).exists():
        try:
            r = await asyncio.to_thread(runner.run_gitleaks, workspace_path)
            results["gitleaks"] = _scan_to_dict(r)
        except Exception as e:
            results["gitleaks"] = {"tool": "gitleaks", "passed": False, "error": str(e)}

    overall_passed = all(r.get("passed", False) for r in results.values()) if results else True
    return {"passed": overall_passed, "results": results}


def _scan_to_dict(scan_result) -> dict:
    """ScanResult dataclass → dict 직렬화."""
    try:
        return {
            "tool": scan_result.tool,
            "passed": scan_result.passed,
            "critical_count": getattr(scan_result, "critical_count", 0),
            "high_count": getattr(scan_result, "high_count", 0),
            "findings": getattr(scan_result, "findings", []),
            "summary": getattr(scan_result, "summary", ""),
            "error": getattr(scan_result, "error", ""),
        }
    except Exception:
        return {"tool": "unknown", "passed": False}


# ── Deploy Rollback ───────────────────────────────────────────────────

@app.post("/api/deploy/rollback")
async def deploy_rollback(body: DeployRollbackRequest, _=Depends(_verify_token)):
    """
    수동 롤백: 마지막 성공 배포 기록으로 되돌리기. (§S-9)

    Request : { plan_id: str }
    Response: { status, message, logs }

    우선순위: 캐시된 _current_deploy_record 가 있으면 그것으로,
    없으면 project_id 기반 rollback_latest 로 폴백.
    """
    workspace = (
        _current_project.workspace_path if _current_project else "."
    )

    try:
        from local_deploy_agent import get_local_deploy_agent
        agent = get_local_deploy_agent()
        if _current_deploy_record:
            result = await asyncio.to_thread(
                agent.rollback,
                _current_deploy_record,
                workspace,
            )
        else:
            project_id = (
                _current_project.project_id if _current_project else "default"
            )
            result = await asyncio.to_thread(
                agent.rollback_latest,
                project_id,
                workspace,
            )
    except Exception as e:
        logger.exception("[server] manual rollback failed")
        raise HTTPException(status_code=500, detail=f"롤백 실패: {e}") from e

    return {
        "status": "ok" if result.success else "error",
        "message": result.error or "롤백 완료",
        "logs": result.logs or [],
    }


# ── EC2 배포 ──────────────────────────────────────────────────────────

async def _run_ec2_deploy(body: "EC2DeployRequest") -> None:
    """EC2 배포 파이프라인 백그라운드 실행."""
    import time as _time
    _ec2_deploy_state.update(
        running=True, stage="building", log_tail=[],
        image_uri="", error="",
        started_at=_time.strftime("%Y-%m-%dT%H:%M:%S"),
        finished_at="",
    )

    def _log(msg: str) -> None:
        _ec2_deploy_state["log_tail"].append(msg)
        if len(_ec2_deploy_state["log_tail"]) > 200:
            _ec2_deploy_state["log_tail"] = _ec2_deploy_state["log_tail"][-200:]
        logger.info(msg)

    try:
        from deploy_agent import EC2DeployAgent, EC2DeployConfig

        # EC2DeployConfig 구성 (요청값 우선, 없으면 환경변수)
        import os as _os
        config = EC2DeployConfig(
            ecr_registry=body.ecr_registry or _os.getenv("ECR_REGISTRY", ""),
            ec2_host=body.ec2_host or _os.getenv("EC2_HOST", ""),
            ec2_ssh_key=body.ec2_ssh_key or _os.getenv("EC2_SSH_KEY", ""),
            aws_region=body.aws_region or (
                _os.getenv("AWS_DEFAULT_REGION")
                or _os.getenv("AWS_REGION")
                or _os.getenv("BEDROCK_REGION")
                or "ap-northeast-2"
            ),
            ec2_user=body.ec2_user,
            container_name=body.container_name,
            host_port=body.host_port,
            container_port=body.container_port,
            health_check_path=body.health_check_path,
            env_vars=body.env_vars,
        )

        if not config.ecr_registry:
            raise ValueError("ECR_REGISTRY 미설정. 환경변수 또는 요청 body에 ecr_registry를 입력하세요.")
        if not config.ec2_host:
            raise ValueError("EC2_HOST 미설정. 환경변수 또는 요청 body에 ec2_host를 입력하세요.")
        if not config.ec2_ssh_key:
            raise ValueError("EC2_SSH_KEY 미설정. 환경변수 또는 요청 body에 ec2_ssh_key를 입력하세요.")

        workspace = body.workspace_path or (
            _current_project.workspace_path if _current_project else ""
        )
        if not workspace:
            raise ValueError("workspace_path 미설정. VS Code에서 프로젝트 폴더를 열어주세요.")

        agent = EC2DeployAgent()
        _log(f"[EC2] 배포 시작: {body.image_name}:{body.tag} → {config.ec2_host}")

        result = await asyncio.to_thread(
            agent.deploy,
            workspace,
            body.image_name,
            body.repo_name,
            config,
            body.tag,
        )

        for ln in result.logs or []:
            _log(ln)

        if result.success:
            _ec2_deploy_state["stage"] = "done"
            _ec2_deploy_state["image_uri"] = result.image_uri
        else:
            _ec2_deploy_state["stage"] = "failed"
            _ec2_deploy_state["error"] = result.error

    except Exception as e:
        logger.exception("[server] EC2 deploy failed")
        _ec2_deploy_state["stage"] = "failed"
        _ec2_deploy_state["error"] = str(e)
        _ec2_deploy_state["log_tail"].append(f"[ERROR] {e}")
    finally:
        import time as _t
        _ec2_deploy_state["running"] = False
        _ec2_deploy_state["finished_at"] = _t.strftime("%Y-%m-%dT%H:%M:%S")


@app.post("/api/deploy/ec2")
async def deploy_ec2(body: EC2DeployRequest, _=Depends(_verify_token)):
    """
    EC2 배포 시작. 백그라운드로 실행되며 /api/deploy/ec2/status 로 폴링.

    Request:  EC2DeployRequest
    Response: { status, message }
    """
    if _ec2_deploy_state.get("running"):
        return {"status": "in_progress", "message": "EC2 배포가 이미 진행 중입니다."}

    asyncio.create_task(_run_ec2_deploy(body))
    return {"status": "ok", "message": "EC2 배포 시작됨. /api/deploy/ec2/status 로 진행상황 확인."}


@app.get("/api/deploy/ec2/status")
async def deploy_ec2_status(_=Depends(_verify_token)):
    """EC2 배포 진행상황 폴링."""
    return _ec2_deploy_state


@app.get("/api/deploy/ec2/ready")
async def deploy_ec2_ready(_=Depends(_verify_token)):
    """
    EC2 배포 가능 여부 확인.
    AWS 자격증명, ECR_REGISTRY, EC2_HOST, EC2_SSH_KEY, aws/docker CLI 존재 여부 체크.
    """
    import os as _os, shutil
    issues: list[str] = []

    if not _os.getenv("ECR_REGISTRY"):
        issues.append("ECR_REGISTRY 환경변수 미설정")
    if not _os.getenv("EC2_HOST"):
        issues.append("EC2_HOST 환경변수 미설정")
    if not _os.getenv("EC2_SSH_KEY"):
        issues.append("EC2_SSH_KEY 환경변수 미설정")
    if not (_os.getenv("AWS_ACCESS_KEY_ID") or _os.path.exists(_os.path.expanduser("~/.aws/credentials"))):
        issues.append("AWS 자격증명 미설정 (AWS_ACCESS_KEY_ID 또는 ~/.aws/credentials)")
    if not shutil.which("aws"):
        issues.append("AWS CLI 미설치 (brew install awscli)")
    if not shutil.which("docker"):
        issues.append("Docker 미설치")
    if not shutil.which("ssh"):
        issues.append("ssh 미설치")

    return {
        "ready": len(issues) == 0,
        "issues": issues,
    }


# ── ECS Fargate 배포 (Q3-A Rolling Update) ────────────────────────────

async def _run_ecs_deploy(body: "ECSDeployRequest") -> None:
    """ECS Fargate 배포 파이프라인 백그라운드 실행.

    파이프라인: Docker build → ECR push → Task Def 등록 → Service update
              → CloudWatch 폴링 (Circuit Breaker) → 실패 시 rollback proposal
    """
    import time as _time
    import os as _os

    _ecs_deploy_state.update(
        running=True, stage="building", log_tail=[],
        image_uri="", task_def_arn="", error="",
        rollback_proposal=None,
        started_at=_time.strftime("%Y-%m-%dT%H:%M:%S"),
        finished_at="",
    )

    def _log(msg: str) -> None:
        _ecs_deploy_state["log_tail"].append(msg)
        if len(_ecs_deploy_state["log_tail"]) > 200:
            _ecs_deploy_state["log_tail"] = _ecs_deploy_state["log_tail"][-200:]
        logger.info(msg)

    try:
        from ecs_deploy_agent import ECSDeployAgent, ECSDeployConfig

        # ── ECSDeployConfig 구성 (요청값 우선, 없으면 환경변수) ──────────
        config = ECSDeployConfig(
            ecr_registry=body.ecr_registry or _os.getenv("ECR_REGISTRY", ""),
            ecs_cluster=body.ecs_cluster or _os.getenv("ECS_CLUSTER", ""),
            ecs_service=body.ecs_service or _os.getenv("ECS_SERVICE", ""),
            aws_region=body.aws_region or (
                _os.getenv("AWS_DEFAULT_REGION")
                or _os.getenv("AWS_REGION")
                or "ap-northeast-2"
            ),
            container_name=body.container_name,
            container_port=body.container_port,
            cpu=body.cpu,
            memory=body.memory,
            env_vars=body.env_vars,
        )

        # ── 필수값 검증 ──────────────────────────────────────────────────
        if not config.ecr_registry:
            raise ValueError("ECR_REGISTRY 미설정. 환경변수 또는 요청 body에 ecr_registry를 입력하세요.")
        if not config.ecs_cluster:
            raise ValueError("ECS_CLUSTER 미설정. 환경변수 또는 요청 body에 ecs_cluster를 입력하세요.")
        if not config.ecs_service:
            raise ValueError("ECS_SERVICE 미설정. 환경변수 또는 요청 body에 ecs_service를 입력하세요.")

        workspace = body.workspace_path or (
            _current_project.workspace_path if _current_project else ""
        )
        if not workspace:
            raise ValueError("workspace_path 미설정. VS Code에서 프로젝트 폴더를 열어주세요.")

        agent = ECSDeployAgent()
        _log(f"[ECS] 배포 시작: {body.image_name}:{body.tag} → {config.ecs_cluster}/{config.ecs_service}")

        # ── 배포 실행 (blocking → thread) ───────────────────────────────
        result = await asyncio.to_thread(
            agent.deploy,
            workspace,
            body.image_name,
            body.repo_name,
            config,
            body.tag,
            body.task_family,
            _log,
            body.environment,
            body.branch,
            body.skip_sbom,
            body.skip_opa,
        )

        for ln in result.logs or []:
            _log(ln)

        if result.success:
            _ecs_deploy_state["stage"] = "done"
            _ecs_deploy_state["image_uri"] = result.image_uri
            _ecs_deploy_state["task_def_arn"] = result.task_definition_arn
        else:
            _ecs_deploy_state["stage"] = "failed"
            _ecs_deploy_state["error"] = result.error
            # Circuit Breaker 발동 → rollback proposal 저장 (Approval Level 3)
            if result.rollback_required:
                proposal = agent.make_rollback_proposal(
                    config,
                    result.task_definition_arn,
                    result.prev_task_def_arn,
                    result.error,
                )
                _ecs_deploy_state["rollback_proposal"] = proposal
                _log(f"[ECS] Rollback proposal 생성됨: {proposal.get('proposal_id','')}")

    except Exception as e:
        logger.exception("[server] ECS deploy failed")
        _ecs_deploy_state["stage"] = "failed"
        _ecs_deploy_state["error"] = str(e)
        _ecs_deploy_state["log_tail"].append(f"[ERROR] {e}")
    finally:
        import time as _t
        _ecs_deploy_state["running"] = False
        _ecs_deploy_state["finished_at"] = _t.strftime("%Y-%m-%dT%H:%M:%S")


@app.post("/api/deploy/ecs")
async def deploy_ecs(body: ECSDeployRequest, _=Depends(_verify_token)):
    """
    ECS Fargate Rolling Update 배포 시작 (백그라운드).
    진행상황은 /api/deploy/ecs/status 로 폴링.

    Request:  ECSDeployRequest
    Response: { status, message }
    """
    if _ecs_deploy_state.get("running"):
        return {"status": "in_progress", "message": "ECS 배포가 이미 진행 중입니다."}

    asyncio.create_task(_run_ecs_deploy(body))
    return {
        "status": "ok",
        "message": "ECS Fargate 배포 시작됨. /api/deploy/ecs/status 로 진행상황 확인.",
    }


@app.get("/api/deploy/ecs/status")
async def deploy_ecs_status(_=Depends(_verify_token)):
    """ECS 배포 진행상황 폴링."""
    return _ecs_deploy_state


@app.post("/api/sbom/generate")
async def sbom_generate(body: SBOMGenerateRequest, _=Depends(_verify_token)):
    """
    이미지에 대한 SBOM 생성 (Syft CycloneDX). 일회성 스캔용.

    Request:  { image_uri, tag }
    Response: SBOMResult 요약 (sbom_path, package_count, sbom_hash, ...)
    """
    try:
        from sbom_agent import get_sbom_agent
        result = await asyncio.to_thread(
            get_sbom_agent().generate, body.image_uri, body.tag
        )
        return {
            "success":       result.success,
            "sbom_path":     result.sbom_path,
            "sbom_version":  result.sbom_version,
            "sbom_hash":     result.sbom_hash,
            "image_digest":  result.image_digest,
            "package_count": result.package_count,
            "error":         result.error,
            "logs":          result.logs[-20:],  # 최근 20줄
        }
    except Exception as e:
        logger.exception("[server] SBOM generate failed")
        raise HTTPException(status_code=500, detail=f"SBOM 생성 실패: {e}") from e


@app.get("/api/sbom/list")
async def sbom_list(_=Depends(_verify_token)):
    """최근 생성된 SBOM 파일 목록."""
    try:
        from sbom_agent import get_sbom_agent
        return {"sboms": get_sbom_agent().list_recent()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/opa/evaluate")
async def opa_evaluate(body: OPAEvaluateRequest, _=Depends(_verify_token)):
    """
    범용 OPA 정책 평가.

    Request:  { policy_path, input_data, approval_level }
    Response: OPAResult.to_dict()
    """
    try:
        from opa_gate import get_opa_gate
        result = await asyncio.to_thread(
            get_opa_gate().evaluate,
            body.policy_path,
            body.input_data,
            body.approval_level,
        )
        return result.to_dict()
    except Exception as e:
        logger.exception("[server] OPA evaluate failed")
        raise HTTPException(status_code=500, detail=f"OPA 평가 실패: {e}") from e


@app.get("/api/opa/ready")
async def opa_ready(_=Depends(_verify_token)):
    """OPA 서버 연결 상태 확인."""
    try:
        from opa_gate import get_opa_gate
        available = await asyncio.to_thread(get_opa_gate().is_available)
        url = __import__("os").getenv("OPA_URL", "http://localhost:8181")
        return {
            "available": available,
            "url":       url,
            "note":      "OPA 미연결 시 로컬 폴백 규칙 적용 (Level 3+ fail-closed)" if not available else "",
        }
    except Exception as e:
        return {"available": False, "url": "", "note": str(e)}


@app.get("/api/deploy/ecs/ready")
async def deploy_ecs_ready(_=Depends(_verify_token)):
    """
    ECS Fargate 배포 가능 여부 사전 확인.
    ECR_REGISTRY / ECS_CLUSTER / ECS_SERVICE 환경변수 + AWS 자격증명 + docker CLI 체크.
    """
    import os as _os, shutil

    issues: list[str] = []

    if not _os.getenv("ECR_REGISTRY"):
        issues.append("ECR_REGISTRY 환경변수 미설정")
    if not _os.getenv("ECS_CLUSTER"):
        issues.append("ECS_CLUSTER 환경변수 미설정")
    if not _os.getenv("ECS_SERVICE"):
        issues.append("ECS_SERVICE 환경변수 미설정")
    if not (_os.getenv("AWS_ACCESS_KEY_ID") or _os.path.exists(_os.path.expanduser("~/.aws/credentials"))):
        issues.append("AWS 자격증명 미설정 (AWS_ACCESS_KEY_ID 또는 ~/.aws/credentials)")
    if not shutil.which("docker"):
        issues.append("Docker 미설치")

    # boto3 preflight — read-only IAM 권한 확인 (선택적)
    if not issues:
        try:
            from ecs_deploy_agent import check_ecs_preflight, ECSDeployConfig
            import os as _os2
            cfg = ECSDeployConfig(
                ecr_registry=_os2.getenv("ECR_REGISTRY", ""),
                ecs_cluster=_os2.getenv("ECS_CLUSTER", ""),
                ecs_service=_os2.getenv("ECS_SERVICE", ""),
                aws_region=_os2.getenv("AWS_DEFAULT_REGION") or _os2.getenv("AWS_REGION") or "ap-northeast-2",
            )
            preflight = await asyncio.to_thread(check_ecs_preflight, cfg)
            if not preflight.get("ok"):
                for item in preflight.get("issues", []):
                    issues.append(f"[IAM] {item}")
        except Exception as e:
            logger.warning(f"[server] ECS preflight 확인 실패 (무시): {e}")

    return {
        "ready": len(issues) == 0,
        "issues": issues,
    }


# ── Q4 Must-Wedge: GitOps / Rollback PR / Postmortem ─────────────────

# GitOps 진행 상태 (인메모리)
_gitops_state: dict = {
    "running":      False,
    "stage":        "idle",
    "log_tail":     [],
    "pr_url":       "",
    "pr_number":    0,
    "sync_status":  "",
    "health_status": "",
    "error":        "",
    "started_at":   "",
    "finished_at":  "",
}


async def _run_gitops_ship(body: GitOpsShipRequest) -> None:
    """GitOps ship 백그라운드 태스크."""
    from gitops_agent import (
        GitOpsAgent, GitOpsConfig, GitOpsShipPayload,
    )

    def _log(msg: str) -> None:
        tail: list = _gitops_state["log_tail"]
        tail.append(msg)
        if len(tail) > 200:
            _gitops_state["log_tail"] = tail[-200:]
        logger.info(f"[gitops] {msg}")

    _gitops_state.update({
        "running": True, "stage": "starting",
        "log_tail": [], "pr_url": "", "pr_number": 0,
        "sync_status": "", "health_status": "",
        "error": "", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": "",
    })
    try:
        cfg = GitOpsConfig(
            app_name=body.app_name,
            repo_url=body.repo_url,
            helm_chart_path=body.helm_chart_path,
            namespace=body.namespace,
            target_revision=body.target_revision,
            argocd_url=body.argocd_url or os.environ.get("ARGOCD_URL", ""),
            github_token=body.github_token or os.environ.get("GITHUB_TOKEN", ""),
        )
        payload_cls = __import__("gitops_agent", fromlist=["GitOpsShipPayload"]).GitOpsShipPayload
        payload = payload_cls(
            config=cfg,
            ecr_image_uri=body.ecr_image_uri,
            image_tag=body.image_tag,
            container_port=body.container_port,
            replica_count=body.replica_count,
            cpu=body.cpu,
            memory=body.memory,
            env_vars=body.env_vars,
            environment=body.environment,
        )
        agent = GitOpsAgent()
        result = await asyncio.to_thread(agent.ship, payload, _log)
        _gitops_state.update({
            "running":       False,
            "stage":         "done" if result.success else "error",
            "pr_url":        result.pr_url,
            "pr_number":     result.pr_number,
            "sync_status":   result.sync_status,
            "health_status": result.health_status,
            "error":         result.error,
            "finished_at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    except Exception as exc:
        _gitops_state.update({
            "running": False, "stage": "error",
            "error": str(exc),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })


@app.post("/api/gitops/ship")
async def gitops_ship(body: GitOpsShipRequest, request: Request, _=Depends(_verify_token)):
    """
    GitOps ArgoCD 배포: helm/values.yaml 생성 → Git PR → ArgoCD sync.

    Request : GitOpsShipRequest
    Response: { started: bool, message: str }
    """
    if _gitops_state.get("running"):
        raise HTTPException(status_code=409, detail="GitOps ship 이미 실행 중입니다.")
    asyncio.get_event_loop().create_task(_run_gitops_ship(body))
    return {"started": True, "message": "GitOps ship 시작됨"}


@app.get("/api/gitops/ship/status")
async def gitops_ship_status(_=Depends(_verify_token)):
    """GitOps ship 진행 상태 폴링."""
    return {
        "running":       _gitops_state["running"],
        "stage":         _gitops_state["stage"],
        "log_tail":      _gitops_state["log_tail"][-50:],
        "pr_url":        _gitops_state["pr_url"],
        "pr_number":     _gitops_state["pr_number"],
        "sync_status":   _gitops_state["sync_status"],
        "health_status": _gitops_state["health_status"],
        "error":         _gitops_state["error"],
        "started_at":    _gitops_state["started_at"],
        "finished_at":   _gitops_state["finished_at"],
    }


@app.post("/api/rollback-pr/create")
async def rollback_pr_create(body: RollbackPRRequest, _=Depends(_verify_token)):
    """
    ADR-005 production rollback PR 자동 생성.

    Request : RollbackPRRequest
    Response: { success, pr_url, pr_number, branch, incident_id, error }
    """
    from rollback_pr_agent import (
        RollbackPRAgent, RollbackPRConfig, DeploymentRecord,
    )
    import time as _time

    rec = DeploymentRecord(
        app_name=body.app_name,
        environment=body.environment,
        failed_at=_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        deployed_by=body.deployed_by,
        error_summary=body.error_summary,
        cluster=body.cluster,
        namespace=body.namespace,
        incident_severity=body.incident_severity,
    )
    cfg = RollbackPRConfig(
        failed_image_tag=body.failed_image_tag,
        last_healthy_image_tag=body.last_healthy_image_tag,
        helm_values_path=body.helm_values_path,
        argocd_app_name=body.argocd_app_name or body.app_name,
        deployment_record=rec,
        incident_id=body.incident_id or f"INC-{uuid.uuid4().hex[:8].upper()}",
        github_token=body.github_token or os.environ.get("GITHUB_TOKEN", ""),
        github_repo=body.github_repo or os.environ.get("GITHUB_REPO", ""),
        emergency=body.emergency,
    )

    logs: list[str] = []
    agent = RollbackPRAgent()
    result = await asyncio.to_thread(
        agent.create_rollback_pr, cfg, lambda m: logs.append(m)
    )
    resp = result.to_summary()
    resp["logs"] = logs[-30:]
    return resp


@app.post("/api/postmortem/generate")
async def postmortem_generate(body: PostmortemRequest, _=Depends(_verify_token)):
    """
    Postmortem skeleton 자동 생성.

    Request : PostmortemRequest
    Response: { success, file_path, incident_id, markdown_preview, error }
    """
    from postmortem_agent import (
        PostmortemAgent, PostmortemInput,
    )

    inp = PostmortemInput(
        incident_id=body.incident_id,
        app_name=body.app_name,
        environment=body.environment,
        severity=body.severity,
        title=body.title,
        failed_image_tag=body.failed_image_tag,
        last_healthy_image_tag=body.last_healthy_image_tag,
        argocd_app_name=body.argocd_app_name,
        rollback_pr_url=body.rollback_pr_url,
        failed_at=body.failed_at,
        resolved_at=body.resolved_at,
        deployed_by=body.deployed_by,
        cluster=body.cluster,
        namespace=body.namespace,
        error_summary=body.error_summary,
        affected_users=body.affected_users,
        revenue_impact=body.revenue_impact,
        otel_trace_id=body.otel_trace_id,
        otel_endpoint=body.otel_endpoint or os.environ.get("OTEL_ENDPOINT", ""),
        extra_refs=body.extra_refs,
    )

    logs: list[str] = []
    agent = PostmortemAgent()
    result = await asyncio.to_thread(
        agent.generate, inp, lambda m: logs.append(m)
    )
    resp = result.to_summary()
    resp["markdown_preview"] = result.markdown_preview
    resp["logs"] = logs[-20:]
    return resp


@app.get("/api/postmortem/list")
async def postmortem_list(_=Depends(_verify_token)):
    """생성된 Postmortem 파일 목록 반환."""
    pm_dir = Path.home() / ".recoder" / "postmortems"
    if not pm_dir.exists():
        return {"items": []}
    items = []
    for f in sorted(pm_dir.glob("*.md"), reverse=True)[:20]:
        items.append({
            "incident_id": f.stem,
            "file_path":   str(f),
            "size_bytes":  f.stat().st_size,
            "modified_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(f.stat().st_mtime)
            ),
        })
    return {"items": items}


# ── Git ──────────────────────────────────────────────────────────────

@app.post("/api/git/commit")
async def git_commit(body: GitCommitRequest, _=Depends(_verify_token)):
    """
    git add -A + git commit -m {message} 실행.

    Request : { workspace_path, message, session_id }
    Response: { status, commit_hash, message }
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="커밋 메시지가 비어있습니다.")

    try:
        from git_agent import get_git_agent
        result = await asyncio.to_thread(
            get_git_agent().commit,
            body.workspace_path,
            body.message,
            body.session_id or _ensure_session(),
        )
    except Exception as e:
        logger.exception("[server] git commit failed")
        raise HTTPException(status_code=500, detail=f"Git 커밋 실패: {e}") from e

    return result


@app.get("/api/git/info")
async def git_info(workspace_path: str = "", force_refresh: bool = False, _=Depends(_verify_token)):
    """
    현재 저장소 상태 조회 (브랜치, remote URL, 변경 파일 수, ahead/behind, gh 사용자).
    force_refresh=true 이면 원격 접근 가능 여부 캐시를 무시하고 즉시 재확인.
    """
    try:
        from git_agent import get_git_agent
        return await asyncio.to_thread(get_git_agent().info, workspace_path, force_refresh)
    except Exception as e:
        logger.exception("[server] /api/git/info failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


class GitSetRemoteRequest(BaseModel):
    workspace_path: str = ""
    repo_full_name: str  # "owner/repo"


@app.post("/api/git/set-remote")
async def git_set_remote(body: GitSetRemoteRequest, _=Depends(_verify_token)):
    """원격 저장소(origin) URL 변경."""
    try:
        from git_agent import get_git_agent
        return await asyncio.to_thread(
            get_git_agent().set_remote, body.workspace_path, body.repo_full_name
        )
    except Exception as e:
        logger.exception("[server] /api/git/set-remote failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/git/branches")
async def git_branches(workspace_path: str = "", _=Depends(_verify_token)):
    """로컬/원격 브랜치 목록 반환."""
    try:
        from git_agent import get_git_agent
        return await asyncio.to_thread(get_git_agent().branches, workspace_path)
    except Exception as e:
        logger.exception("[server] /api/git/branches failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/git/checkout")
async def git_checkout(body: GitCheckoutRequest, _=Depends(_verify_token)):
    """브랜치 전환."""
    try:
        from git_agent import get_git_agent
        return await asyncio.to_thread(get_git_agent().checkout, body.workspace_path, body.branch)
    except Exception as e:
        logger.exception("[server] /api/git/checkout failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/git/branch/create")
async def git_branch_create(body: GitBranchCreateRequest, _=Depends(_verify_token)):
    """새 브랜치 생성."""
    try:
        from git_agent import get_git_agent
        return await asyncio.to_thread(
            get_git_agent().branch_create,
            body.workspace_path, body.branch_name, body.checkout,
        )
    except Exception as e:
        logger.exception("[server] /api/git/branch/create failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/git/push")
async def git_push(body: GitPushRequest, _=Depends(_verify_token)):
    """원격 push (upstream 자동 설정 포함).

    GitHub 토큰이 있으면 github_agent.push() (OAuth 인증 URL 방식),
    없으면 git_agent.push() (시스템 자격증명 — SSH 키·macOS 키체인 등) 로 폴백.
    """
    try:
        from github_agent import get_github_agent
        gh = get_github_agent()
        if gh._token:
            return await asyncio.to_thread(
                gh.push, body.workspace_path, body.branch, body.force,
            )
        # GitHub 토큰 미연결 → 시스템 git 자격증명으로 push
        from git_agent import get_git_agent
        return await asyncio.to_thread(
            get_git_agent().push, body.workspace_path, body.branch, body.force,
        )
    except Exception as e:
        logger.exception("[server] /api/git/push failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── GitHub ───────────────────────────────────────────────────────────

@app.get("/api/github/status")
async def github_status(force: bool = False, _=Depends(_verify_token)):
    """GitHub 인증 상태 — 사이드바 첫 진입에서 호출.
    force=true 로 호출하면 캐시를 건너뛰고 새로 조회한다 (수동 새로고침용).
    status() 는 dict 를 직접 반환 (to_dict 없음).
    """
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().status, force)
    except Exception as e:
        logger.exception("[server] /api/github/status failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/github/token")
async def github_set_token(body: GhTokenRequest, _=Depends(_verify_token)):
    """
    Extension 이 VS Code OAuth 로 획득한 GitHub 토큰을 Core 에 저장.
    저장 즉시 /user API 로 유효성 검증 → { status, user } 반환.
    """
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().set_token, body.token)
    except Exception as e:
        logger.exception("[server] /api/github/token failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/github/repos")
async def github_repos(_=Depends(_verify_token)):
    """인증된 사용자의 GitHub 레포지토리 목록."""
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().list_repos)
    except Exception as e:
        logger.exception("[server] /api/github/repos failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/github/login")
async def github_login_begin(_body: GhLoginRequest = None, _=Depends(_verify_token)):
    """
    [Deprecated] gh CLI device-code 방식 — VS Code OAuth 로 대체됨.
    Extension 은 vscode.authentication.getSession() + POST /api/github/token 을 사용할 것.
    """
    return {
        "status": "deprecated",
        "message": "이 엔드포인트는 더 이상 사용되지 않습니다. "
                   "Extension 에서 vscode.authentication.getSession()을 사용하고 "
                   "POST /api/github/token 으로 토큰을 전달하세요.",
    }


@app.get("/api/github/login/poll")
async def github_login_poll(_=Depends(_verify_token)):
    """[Deprecated] device-code 폴링 — VS Code OAuth 로 대체됨."""
    return {
        "stage": "deprecated",
        "code": "",
        "verify_url": "",
        "user": "",
        "error": "이 엔드포인트는 더 이상 사용되지 않습니다.",
    }


@app.post("/api/github/login/cancel")
async def github_login_cancel(_=Depends(_verify_token)):
    """[Deprecated] device-code 취소 — VS Code OAuth 로 대체됨."""
    return {"status": "ok", "message": "deprecated"}


@app.post("/api/github/repo")
async def github_repo_create(body: GhRepoCreateRequest, _=Depends(_verify_token)):
    """gh repo create + push (init/remote 자동)."""
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(
            get_github_agent().repo_create_and_push,
            body.workspace_path, body.name, body.private, body.description,
        )
    except Exception as e:
        logger.exception("[server] /api/github/repo failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/github/secret")
async def github_secret_set(body: GhSetSecretRequest, _=Depends(_verify_token)):
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(
            get_github_agent().set_secret, body.repo, body.name, body.value,
        )
    except Exception as e:
        logger.exception("[server] /api/github/secret failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/github/runs")
async def github_runs(repo: str, _=Depends(_verify_token)):
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().list_runs, repo, 5)
    except Exception as e:
        logger.exception("[server] /api/github/runs failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/github/branches")
async def github_branches(workspace_path: str = "", _=Depends(_verify_token)):
    """현재 워크스페이스의 git 브랜치 목록 반환."""
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().list_branches, workspace_path)
    except Exception as e:
        logger.exception("[server] /api/github/branches failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/github/logout")
async def github_logout(_=Depends(_verify_token)):
    """gh auth logout."""
    try:
        from github_agent import get_github_agent
        return await asyncio.to_thread(get_github_agent().logout)
    except Exception as e:
        logger.exception("[server] /api/github/logout failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 7-step Ship Pipeline ─────────────────────────────────────────────

async def _ship_pipeline(body) -> None:
    from github_agent import get_github_agent
    gh = get_github_agent()
    ws = Path(body.workspace_path).expanduser().resolve()
    t0 = time.time()

    def _log(msg: str) -> None:
        elapsed = time.time() - t0
        logger.info(f"[ship {elapsed:.1f}s] {msg}")

    try:
        # ── Step 1: Git 초기화 (.git 폴더, .gitignore, user 설정) ──────
        _ship_set("init", "running")
        _log("git init 시작")
        await asyncio.to_thread(gh._ensure_git_init, ws)
        _log("git init 완료")
        _ship_set("init", "done")

        # ── Step 2: 인프라 파일 생성 (AI 호출 — 프로젝트 스캔 후에만) ──
        _ship_set("files", "running")
        files_generated = 0
        if _current_project and any([
            body.include_dockerfile, body.include_compose,
            body.include_actions, body.include_dockerignore,
        ]):
            from infra_agent import (
                generate_dockerfile, generate_docker_compose,
                generate_dockerignore, generate_github_actions,
            )
            file_tasks = []
            if body.include_dockerignore:
                file_tasks.append(("dockerignore", generate_dockerignore, (_current_project, str(ws))))
            if body.include_dockerfile:
                file_tasks.append(("dockerfile", generate_dockerfile, (None, _current_project, str(ws))))
            if body.include_compose:
                file_tasks.append(("compose", generate_docker_compose, (_current_project, str(ws))))
            if body.include_actions:
                file_tasks.append(("actions", generate_github_actions, (_current_project, str(ws))))

            for name, fn, args in file_tasks:
                try:
                    _ship_set("files", "running", f"{name} 생성 중...")
                    p = await asyncio.wait_for(
                        asyncio.to_thread(fn, *args), timeout=60
                    )
                    target = ws / p.target_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(p.content, encoding="utf-8")
                    files_generated += 1
                except asyncio.TimeoutError:
                    logger.warning(f"[ship] {name} 생성 타임아웃 — 건너뜀")
                except Exception as fe:
                    logger.warning(f"[ship] {name} 생성 실패: {fe} — 건너뜀")
        _ship_set("files", "done", f"{files_generated}개 파일" if files_generated else "스킵")

        # ── Step 3: add + commit ────────────────────────────────────────
        _ship_set("commit", "running")
        _log("git add -A 시작")
        rc_add, _, err_add = await asyncio.to_thread(
            gh._git, ws, ["add", "-A"], 120
        )
        _log(f"git add 완료 (rc={rc_add})")
        if rc_add != 0:
            logger.warning(f"[ship] git add 경고: {err_add}")
        rc, out2, err2 = await asyncio.to_thread(
            gh._git, ws, ["commit", "-m", "ReCoder: ship to GitHub"]
        )
        _log(f"git commit 완료 (rc={rc})")
        if rc != 0 and "nothing to commit" not in (out2 + err2):
            raise RuntimeError(f"commit 실패: {err2 or out2}")
        _ship_set("commit", "done")

        # ── Step 4+5: repo 생성 & push ─────────────────────────────────
        _ship_set("repo", "running")
        _log(f"gh repo create '{body.repo_name}' 시작")
        repo_res = await asyncio.wait_for(
            asyncio.to_thread(
                gh.repo_create_and_push, str(ws), body.repo_name, body.private, body.description
            ),
            timeout=180,
        )
        _log(f"gh repo create 완료: {repo_res.get('status')} / {repo_res.get('message','')}")
        if repo_res.get("status") != "ok":
            raise RuntimeError(repo_res.get("message", "repo 생성 실패"))
        repo_url = repo_res.get("repo_url", "")
        repo_full = repo_res.get("repo_name", body.repo_name)
        _SHIP_STATE["repo_url"] = repo_url
        _ship_set("repo", "done", repo_url)
        _ship_set("push", "done")
        _log(f"push 완료 → {repo_url}")

        # ── Step 6: Secrets ────────────────────────────────────────────
        if body.secrets:
            _ship_set("secrets", "running")
            failures = []
            for sname, value in body.secrets.items():
                if not value:
                    continue
                r = await asyncio.to_thread(gh.set_secret, repo_full, sname, value)
                if r.get("status") != "ok":
                    failures.append(f"{sname}: {r.get('message', '')}")
            _ship_set(
                "secrets",
                "warn" if failures else "done",
                "; ".join(failures)[:200] if failures else f"{len(body.secrets)}개 등록",
            )
        else:
            _ship_set("secrets", "skipped", "입력값 없음")

        # ── Step 7: Actions 확인 ───────────────────────────────────────
        _ship_set("actions", "running")
        try:
            runs = await asyncio.wait_for(
                asyncio.to_thread(gh.list_runs, repo_full, 3), timeout=20
            )
            if runs.get("status") == "ok":
                _ship_set("actions", "done", f"{len(runs.get('runs', []))}건")
            else:
                _ship_set("actions", "warn", runs.get("message", ""))
        except asyncio.TimeoutError:
            _ship_set("actions", "warn", "확인 타임아웃")

        _ship_finish(error="", repo_url=repo_url)

    except asyncio.TimeoutError:
        step = _SHIP_STATE.get("current") or "repo"
        _ship_set(step, "failed", "타임아웃 — 네트워크 또는 GitHub 응답 지연")
        _ship_finish(error="타임아웃")
    except Exception as e:
        logger.exception("[server] ship pipeline failed")
        _ship_set(_SHIP_STATE.get("current") or "repo", "failed", str(e)[:200])
        _ship_finish(error=str(e)[:300])


@app.post("/api/ship/github")
async def ship_github(body: ShipGitHubRequest, _=Depends(_verify_token)):
    global _current_project
    if _SHIP_STATE.get("running"):
        return {"status": "in_progress", "message": "이미 진행 중입니다."}
    # workspace_path 미전달 시 마지막 스캔 경로로 자동 폴백
    if not body.workspace_path and _current_project:
        body = body.model_copy(update={"workspace_path": _current_project.workspace_path})
    if not body.repo_name:
        raise HTTPException(
            status_code=400,
            detail="repo 이름을 입력해주세요."
        )
    if not body.workspace_path:
        raise HTTPException(
            status_code=400,
            detail="VS Code에서 배포할 프로젝트 폴더를 먼저 열어주세요."
        )
    workspace_path = Path(body.workspace_path).expanduser().resolve()
    if not workspace_path.exists():
        raise HTTPException(status_code=404, detail="워크스페이스 경로가 없습니다.")

    include_infra = any([
        body.include_dockerfile, body.include_compose,
        body.include_actions, body.include_dockerignore,
    ])
    current_workspace = ""
    if _current_project:
        current_workspace = str(Path(_current_project.workspace_path).expanduser().resolve())
    if include_infra and current_workspace != str(workspace_path):
        try:
            from project_scanner import get_project_scanner
            _current_project = get_project_scanner().scan(str(workspace_path))
        except Exception as e:
            logger.exception("[server] ship project scan failed")
            raise HTTPException(status_code=500, detail=f"프로젝트 스캔 실패: {e}") from e

    body = body.model_copy(update={"workspace_path": str(workspace_path)})
    _ship_reset()
    asyncio.create_task(_ship_pipeline(body))
    return {"status": "ok", "message": "GitHub 파이프라인 시작."}


@app.get("/api/ship/github/status")
async def ship_github_status(_=Depends(_verify_token)):
    return _SHIP_STATE


# ── Cost ─────────────────────────────────────────────────────────────

@app.get("/api/cost")
async def get_cost(_=Depends(_verify_token)):
    """SessionLogger SQLite 누적치 기반 일/월 비용."""
    try:
        from session_logger import get_session_logger
        logger_inst = get_session_logger()
        daily = float(logger_inst.get_daily_cost() or 0.0)
        monthly = float(logger_inst.get_monthly_cost() or 0.0)
    except Exception as e:
        logger.warning(f"[server] cost lookup failed: {e}")
        daily, monthly = 0.0, 0.0
    return {"daily": daily, "monthly": monthly, "calls": 0}


# ── Control ───────────────────────────────────────────────────────────

@app.post("/api/pause")
async def pause_monitoring(_=Depends(_verify_token)):
    return {"status": "ok", "paused": True, "message": "모니터링이 일시정지되었습니다."}


# ── Token ─────────────────────────────────────────────────────────────

@app.get("/api/token")
async def get_token(request: Request):
    """대시보드 초기화용 — 127.0.0.1 만 허용."""
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")
    return {"token": SESSION_TOKEN, "port": PORT}


@app.get("/dashboard")
async def dashboard(request: Request):
    from fastapi.responses import HTMLResponse
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")
    qtoken = request.query_params.get("token", "")
    embed_token = qtoken if qtoken == SESSION_TOKEN else ""
    try:
        from dashboard_html import render as render_dashboard
    except Exception as e:
        logger.exception("[server] dashboard_html import failed")
        return HTMLResponse(
            content=f"<h2>Dashboard load error</h2><pre>{e}</pre>",
            status_code=500,
        )
    return HTMLResponse(content=render_dashboard(embed_token, PORT), status_code=200)


# ── 유틸리티 ──────────────────────────────────────────────────────────

def get_dashboard_url() -> str:
    return f"http://127.0.0.1:{PORT}/dashboard?token={SESSION_TOKEN}"


async def start_server(session_index: dict) -> None:
    """Uvicorn 서버 비동기 실행 (main.py 에서 호출)."""
    global _session_ref
    _session_ref = session_index

    _save_runtime_config()

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    _server_ready.set()

    print(f"[server] ReCoder v6.4 Local Server started on {get_dashboard_url()}")
    await server.serve()


def wait_until_ready(timeout: float = 15.0) -> bool:
    try:
        return _server_ready.wait() if hasattr(_server_ready, "wait") else asyncio.run(
            asyncio.wait_for(_server_ready.wait(), timeout=timeout)
        )
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# v5.0 Q4 — Incident / Observability / RCA / MCP endpoints
#
# 설계서 §Q4 Must-Wedge: Incident Timeline → Correlation → RCA → rollback PR →
# Approval → ArgoCD → Postmortem 흐름이 모두 하나의 incident_id 로 연결된다.
# ──────────────────────────────────────────────────────────────────────


class IncidentTimelineRequest(BaseModel):
    incident_id: str
    detected_at: str  # ISO 8601
    project_id: Optional[str] = None
    service_name: Optional[str] = None
    container_name: Optional[str] = None
    severity: str = "sev3"
    deployments: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    watchdog_jsonl_path: Optional[str] = None
    window_before_minutes: int = 120
    window_after_minutes: int = 30


class IncidentCorrelateRequest(BaseModel):
    incident_id: str
    detected_at: str
    candidate_deployment: Optional[dict[str, Any]] = None
    error_rate_before: Optional[float] = None
    error_rate_after: Optional[float] = None
    latency_before_ms: Optional[float] = None
    latency_after_ms: Optional[float] = None
    changed_files: list[str] = []
    affected_path_prefixes: list[str] = []
    container_restart_count: Optional[float] = None
    health_check_failed: Optional[bool] = None
    log_keyword_delta: dict[str, float] = {}
    traffic_rps_before: Optional[float] = None
    traffic_rps_after: Optional[float] = None
    dependency_errors: list[str] = []


class IncidentRCARequest(BaseModel):
    incident_id: str
    detected_at: str
    project_id: Optional[str] = None
    service_name: Optional[str] = None
    container_name: Optional[str] = None
    use_llm: bool = True

    # 직접 주입 (없으면 빈 값으로 deterministic 분석)
    timeline_events: list[dict[str, Any]] = []
    correlation: Optional[dict[str, Any]] = None
    suspected_deployment: Optional[dict[str, Any]] = None
    changed_files: list[str] = []
    log_excerpts: list[str] = []
    metric_snapshot: dict[str, Any] = {}


class ObservabilityQueryRequest(BaseModel):
    kind: str  # "metric" | "log"
    query: str
    container_name: Optional[str] = None
    minutes: int = 15
    limit: int = 100


@app.post("/api/incident/timeline")
async def incident_timeline(body: IncidentTimelineRequest, _=Depends(_verify_token)):
    """Incident Timeline MVP — 시간순 통합 이벤트 리스트 반환."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    try:
        from incident_timeline import build_timeline, TimelineBuildInput
        from schemas import DeploymentRecord, IncidentSeverity
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="incident_timeline module missing")

    try:
        otel_service = None
        try:
            from observability.otel_query_service import OTelQueryService
            otel_service = OTelQueryService()
        except Exception:  # noqa: BLE001
            otel_service = None

        detected = _dt.fromisoformat(body.detected_at.replace("Z", "+00:00"))
        try:
            sev = IncidentSeverity(body.severity)
        except ValueError:
            sev = IncidentSeverity.SEV3

        deployments = [DeploymentRecord(**d) for d in body.deployments]

        inp = TimelineBuildInput(
            incident_id=body.incident_id,
            detected_at=detected,
            project_id=body.project_id,
            severity=sev,
            service_name=body.service_name,
            container_name=body.container_name,
            deployments=deployments,
            audit_rows=body.audit_rows,
            watchdog_jsonl_path=Path(body.watchdog_jsonl_path) if body.watchdog_jsonl_path else None,
            window_before=_td(minutes=body.window_before_minutes),
            window_after=_td(minutes=body.window_after_minutes),
            otel_service=otel_service,
        )
        timeline = build_timeline(inp)
        return timeline.model_dump(mode="json")
    except Exception as exc:
        logger.exception("incident_timeline failed")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/incident/correlate")
async def incident_correlate(body: IncidentCorrelateRequest, _=Depends(_verify_token)):
    """Incident ↔ DeploymentRecord 상관관계 계산 (8개 신호 가중 평균)."""
    from datetime import datetime as _dt

    try:
        from incident_correlator import correlate, CorrelationInput
        from schemas import DeploymentRecord
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="incident_correlator module missing")

    detected = _dt.fromisoformat(body.detected_at.replace("Z", "+00:00"))
    candidate = None
    if body.candidate_deployment:
        try:
            candidate = DeploymentRecord(**body.candidate_deployment)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid candidate_deployment: {exc}")

    inp = CorrelationInput(
        incident_id=body.incident_id,
        detected_at=detected,
        candidate_deployment=candidate,
        error_rate_before=body.error_rate_before,
        error_rate_after=body.error_rate_after,
        latency_before_ms=body.latency_before_ms,
        latency_after_ms=body.latency_after_ms,
        changed_files=body.changed_files,
        affected_path_prefixes=body.affected_path_prefixes,
        container_restart_count=body.container_restart_count,
        health_check_failed=body.health_check_failed,
        log_keyword_delta=body.log_keyword_delta,
        traffic_rps_before=body.traffic_rps_before,
        traffic_rps_after=body.traffic_rps_after,
        dependency_errors=body.dependency_errors,
    )
    result = correlate(inp)
    return result.model_dump(mode="json")


@app.post("/api/incident/rca")
async def incident_rca(body: IncidentRCARequest, _=Depends(_verify_token)):
    """RCA MVP — '확정 원인' 금지, '가능성 높은 원인 후보' 만 반환."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    try:
        from rca_agent import RCAAgent, RCAInput
        from schemas import (
            CorrelationResult,
            DeploymentRecord,
            IncidentEvent,
            IncidentTimeline,
            IncidentSeverity,
        )
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="rca_agent module missing")

    detected = _dt.fromisoformat(body.detected_at.replace("Z", "+00:00"))

    # 1) timeline 재구성 (호출 측이 events 만 보냄)
    events = []
    for raw in body.timeline_events:
        try:
            events.append(IncidentEvent(**raw))
        except Exception:
            continue
    timeline = IncidentTimeline(
        incident_id=body.incident_id,
        project_id=body.project_id,
        severity=IncidentSeverity.SEV3,
        detected_at=detected,
        events=events,
    )

    correlation = None
    if body.correlation:
        try:
            correlation = CorrelationResult(**body.correlation)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid correlation: {exc}")

    suspected = None
    if body.suspected_deployment:
        try:
            suspected = DeploymentRecord(**body.suspected_deployment)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid suspected_deployment: {exc}")

    # OTel snapshot 보강 (옵션)
    metric_snapshot = dict(body.metric_snapshot or {})
    if not metric_snapshot and body.service_name:
        try:
            from observability.otel_query_service import OTelQueryService
            svc = OTelQueryService()
            if svc.available():
                snap = svc.snapshot_for_service(body.service_name, body.container_name)
                metric_snapshot = {
                    "error_rate_now": snap.error_rate_now,
                    "error_rate_baseline": snap.error_rate_baseline,
                    "latency_p95_now": snap.latency_p95_now,
                    "latency_p95_baseline": snap.latency_p95_baseline,
                    "restart_count_recent": snap.restart_count_recent,
                    "memory_bytes": snap.memory_bytes,
                }
        except Exception:  # noqa: BLE001
            pass

    router = None
    if body.use_llm:
        try:
            from llm.provider_router import LLMProviderRouter
            router = LLMProviderRouter()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RCA LLM router unavailable: %s", exc)

    agent = RCAAgent(llm_router=router)
    rca_input = RCAInput(
        incident_id=body.incident_id,
        timeline=timeline,
        correlation=correlation,
        suspected_deployment=suspected,
        metric_snapshot=metric_snapshot,
        changed_files=body.changed_files,
        log_excerpts=body.log_excerpts,
    )
    report = await agent.analyze_async(rca_input)
    return report.model_dump(mode="json")


@app.post("/api/observability/query")
async def observability_query(body: ObservabilityQueryRequest, _=Depends(_verify_token)):
    """Prometheus / Loki 통합 쿼리 entry point."""
    try:
        from observability.otel_query_service import OTelQueryService
        from observability.prometheus_adapter import PrometheusAdapter
        from observability.loki_adapter import LokiAdapter
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="observability package missing")

    if body.kind == "metric":
        prom = PrometheusAdapter(
            base_url=os.environ.get("PROMETHEUS_URL"),
            bearer_token=os.environ.get("PROMETHEUS_TOKEN"),
        )
        return prom.query(body.query).model_dump(mode="json")
    if body.kind == "log":
        loki = LokiAdapter(
            base_url=os.environ.get("LOKI_URL"),
            bearer_token=os.environ.get("LOKI_TOKEN"),
        )
        return loki.query_range(body.query, limit=body.limit).model_dump(mode="json")
    raise HTTPException(status_code=400, detail=f"unsupported kind: {body.kind}")


@app.get("/api/observability/ready")
async def observability_ready(_=Depends(_verify_token)):
    """OTel 백엔드 (Prometheus / Loki) 연결 가능 여부."""
    try:
        from observability.otel_query_service import OTelQueryService
        svc = OTelQueryService()
        return {
            "available": svc.available(),
            "prometheus_configured": bool(svc.prometheus.base_url),
            "loki_configured": bool(svc.loki.base_url),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}


@app.get("/api/mcp/health")
async def mcp_health(_=Depends(_verify_token)):
    """MCP 서버 (local stdio PoC) 가 export 하는 도구 목록."""
    try:
        from mcp_server import list_tools
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="mcp_server module missing")
    return {"tools": list_tools(), "transport": "stdio"}
