"""
server.py — ReCoder 로컬 FastAPI 대시보드 서버.
기존 local_server.py(aiohttp) 대체.
- 127.0.0.1 only 바인딩
- 세션 토큰 UUID (Write API 보호)
- AgentEvent 수신 큐 → SSE 브로드캐스트
- PatchProposal / InfraFileProposal API
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from schemas import (
    AgentEvent, EventType, InfraFileProposal, OrchestratorState,
    OrchestratorUpdate, PatchProposal, RiskLevel, UserAction,
)

# ── 경로 ──────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).resolve().parent
DASHBOARD_PATH = BASE_DIR / 'dashboard' / 'index.html'
BACKUP_DIR     = Path.home() / '.recoder' / 'backups'

# ── 앱 시작 시 세션 토큰 발급 ─────────────────────────────────────────
SESSION_TOKEN: str = uuid.uuid4().hex
PORT: int = int(os.getenv('LOCAL_PORT', '17894'))

app = FastAPI(title="ReCoder Local Server", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://127.0.0.1:{PORT}"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 상태 저장소 ───────────────────────────────────────────────────────
_session_ref: dict           = {}
_event_queue: asyncio.Queue  = asyncio.Queue(maxsize=64)
_sse_clients: list[asyncio.Queue] = []

# Orchestrator 상태
_orchestrator_state: OrchestratorState = OrchestratorState.IDLE
_current_event:   AgentEvent | None       = None
_current_patch:   PatchProposal | None    = None
_current_infra:   InfraFileProposal | None = None

_server_ready = threading.Event()


# ── 토큰 검증 ─────────────────────────────────────────────────────────

def _verify_token(request: Request) -> None:
    token = request.headers.get("X-ReCoder-Token", "")
    if token != SESSION_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def _project_root() -> Path:
    try:
        from code_agent import _project_root as _detect_project_root
        return _detect_project_root()
    except Exception:
        return Path.cwd()


def _safe_project_path(relative_path: str) -> Path:
    root = _project_root().resolve()
    target = (root / relative_path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(status_code=400, detail="Target path is outside the project root.")
    return target


def _append_session_event(event_type: str, summary: str, **extra: Any) -> None:
    events = _session_ref.setdefault("events", [])
    if not isinstance(events, list):
        return
    events.append({
        "type": event_type,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event_id": uuid.uuid4().hex,
        "summary": summary,
        **extra,
    })


def _docker_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-")
    return slug or "project"


def _run_docker_command(args: list[str], cwd: Path, timeout: int = 300) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="Docker CLI를 찾지 못했습니다. Docker Desktop 설치 후 PATH에 docker가 잡혀 있는지 확인하세요.",
        )
    except subprocess.TimeoutExpired as e:
        output = "\n".join(part for part in (e.stdout or "", e.stderr or "") if part)
        raise HTTPException(
            status_code=500,
            detail={"message": "Docker 명령 시간이 초과되었습니다.", "command": " ".join(args), "output": output[-8000:]},
        )

    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    return {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "output": output[-8000:],
    }


def _raise_failed_docker_step(step: dict[str, Any]) -> None:
    raise HTTPException(
        status_code=500,
        detail={
            "message": "Docker 실행에 실패했습니다. Docker Desktop이 켜져 있는지와 Dockerfile 내용을 확인하세요.",
            **step,
        },
    )


def _check_docker_ready(root: Path) -> dict[str, Any]:
    info = _run_docker_command(["docker", "info", "--format", "{{.ServerVersion}}"], root, timeout=30)
    if info["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Docker Desktop 엔진에 연결할 수 없습니다. Docker Desktop을 켠 뒤 `docker ps`가 되는지 확인하세요.",
                **info,
            },
        )
    return info


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _choose_host_port(container_port: str) -> tuple[str, bool]:
    try:
        base = int(container_port)
    except ValueError:
        return container_port, False
    if _port_available(base):
        return str(base), False
    for candidate in range(base + 1, base + 101):
        if _port_available(candidate):
            return str(candidate), True
    return str(base), True


def _ensure_current_infra_saved_if_docker() -> str | None:
    if _current_infra is None:
        return None
    if _current_infra.target_path not in {"Dockerfile", "docker-compose.yml"}:
        return None

    target = _safe_project_path(_current_infra.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_current_infra.content, encoding="utf-8")
    _ensure_dockerignore(_project_root().resolve())
    return str(target)


def _ensure_dockerignore(root: Path) -> str | None:
    dockerignore = root / ".dockerignore"
    recommended = [
        ".git",
        ".recoder",
        ".env",
        ".env.*",
        "__pycache__",
        "*.pyc",
        "venv",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "output",
    ]
    if dockerignore.exists():
        content = dockerignore.read_text(encoding="utf-8", errors="replace")
        missing = [line for line in recommended if line not in content.splitlines()]
        if not missing:
            return None
        dockerignore.write_text(content.rstrip() + "\n" + "\n".join(missing) + "\n", encoding="utf-8")
    else:
        dockerignore.write_text("\n".join(recommended) + "\n", encoding="utf-8")
    return str(dockerignore)


def _terminal_log_path() -> Path:
    raw = os.getenv("TERMINAL_LOG_PATH", "~/.ai_assistant/terminal-live.log").strip()
    return Path(raw or "~/.ai_assistant/terminal-live.log").expanduser()


def _powershell_exe() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _open_monitored_terminal() -> dict[str, Any]:
    root = _project_root().resolve()
    log_path = _terminal_log_path().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    script = f"""
$ErrorActionPreference = 'Continue'
$recoderLogPath = '{str(log_path).replace("'", "''")}'
$recoderLogDir = Split-Path -Parent $recoderLogPath
New-Item -ItemType Directory -Path $recoderLogDir -Force | Out-Null
$recoderTranscriptPath = Join-Path $recoderLogDir 'terminal-transcript.log'
try {{
  Start-Transcript -Path $recoderTranscriptPath -Append | Out-Null
  Write-Host '[ReCoder] Monitored terminal is active.'
}} catch {{
  Write-Host '[ReCoder] Failed to start transcript:' $_
}}
try {{
  Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:{PORT}/api/token' -TimeoutSec 2 | Out-Null
  Write-Host '[ReCoder] Local server is running. Errors printed here can be detected.'
}} catch {{
  Write-Host '[ReCoder] WARNING: Local server is not reachable. Start python main.py first.'
}}
Set-Location -LiteralPath '{str(root).replace("'", "''")}'
function Invoke-ReCoderLoggedApplication {{
  param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(ValueFromRemainingArguments=$true)][object[]]$CommandArgs
  )
  $cmd = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $cmd) {{
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  }}
  if (-not $cmd) {{
    Write-Host "[ReCoder] Command not found: $Name"
    return
  }}
  & $cmd.Source @CommandArgs 2>&1 | ForEach-Object {{
    $line = $_
    Write-Output $line
    Add-Content -Path $recoderLogPath -Value $line -Encoding UTF8
  }}
  $global:LASTEXITCODE = $LASTEXITCODE
}}
function python {{ Invoke-ReCoderLoggedApplication 'python' @args }}
function py {{ Invoke-ReCoderLoggedApplication 'py' @args }}
function node {{ Invoke-ReCoderLoggedApplication 'node' @args }}
function npm {{ Invoke-ReCoderLoggedApplication 'npm' @args }}
function pytest {{ Invoke-ReCoderLoggedApplication 'pytest' @args }}
function uvicorn {{ Invoke-ReCoderLoggedApplication 'uvicorn' @args }}
Write-Host '[ReCoder] Live command capture enabled for python, py, node, npm, pytest, uvicorn.'
"""
    script_dir = root / ".recoder"
    script_dir.mkdir(parents=True, exist_ok=True)
    try:
        from code_agent import _ensure_gitignore_recoder
        _ensure_gitignore_recoder(root)
    except Exception:
        pass
    script_path = script_dir / "start_monitored_terminal.ps1"
    script_path.write_text(script, encoding="utf-8-sig")
    args = [
        _powershell_exe(),
        "-NoProfile",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(args, cwd=str(root), creationflags=creationflags)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"모니터링 터미널 실행 실패: {e}")
    return {"status": "ok", "cwd": str(root), "log_path": str(log_path)}


def _monitor_paused() -> bool:
    try:
        import monitor
        return monitor.is_paused()
    except Exception:
        return False


# ── AgentEvent 소비 태스크 ────────────────────────────────────────────

async def _consume_events() -> None:
    """monitor.py가 큐에 넣은 AgentEvent를 소비해 Orchestrator 상태를 갱신."""
    global _orchestrator_state, _current_event

    while True:
        event: AgentEvent = await _event_queue.get()
        _current_event = event
        _orchestrator_state = OrchestratorState.WAITING_USER_ACTION

        update = OrchestratorUpdate(
            state   = _orchestrator_state,
            event   = event,
            message = f"에러 감지됨: {event.summary[:80]}",
        )
        await _broadcast(update.to_dict())


async def _broadcast(payload: dict) -> None:
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)


# ── SSE 엔드포인트 ────────────────────────────────────────────────────

async def _activate_event(event: AgentEvent, message: str = "이벤트가 등록되었습니다.") -> None:
    global _current_event, _orchestrator_state
    _current_event = event
    _orchestrator_state = OrchestratorState.WAITING_USER_ACTION
    _append_session_event(event.event_type.value, event.summary, raw_errors=event.raw_errors)
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        event=event,
        message=message,
    ).to_dict())


@app.get("/api/updates/stream")
async def sse_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _sse_clients.append(q)

    async def generator():
        try:
            # 연결 즉시 현재 상태 전송
            init = OrchestratorUpdate(
                state=_orchestrator_state,
                event=_current_event,
                patch_proposal=_current_patch,
                infra_proposal=_current_infra,
                message="connected",
            )
            yield f"data: {json.dumps(init.to_dict(), ensure_ascii=False)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return StreamingResponse(generator(), media_type="text/event-stream")


# ── 상태 조회 API ─────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "state":          _orchestrator_state.value,
        "event":          _current_event.to_dict() if _current_event else None,
        "patch_proposal": _current_patch.to_dict() if _current_patch else None,
        "infra_proposal": _current_infra.to_dict() if _current_infra else None,
        "session":        _session_ref,
    }


@app.get("/api/session/history")
async def get_session_history():
    events = _session_ref.get("events", [])
    return {
        "session_id": _session_ref.get("session_id"),
        "start_time": _session_ref.get("start_time"),
        "end_time": _session_ref.get("end_time"),
        "events": events if isinstance(events, list) else [],
        "error_count": _session_ref.get("error_count", 0),
    }


@app.get("/api/logs/stream")
async def logs_stream(request: Request):
    return await sse_stream(request)


@app.get("/api/monitor/status")
async def monitor_status():
    log_path = _terminal_log_path()
    exists = log_path.exists()
    stat = log_path.stat() if exists else None
    return {
        "terminal_log_path": str(log_path),
        "terminal_log_exists": exists,
        "terminal_log_size": stat.st_size if stat else 0,
        "terminal_log_last_write": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)) if stat else None,
        "orchestrator_state": _orchestrator_state.value,
        "event_count": len(_session_ref.get("events", [])) if isinstance(_session_ref.get("events", []), list) else 0,
        "monitor_paused": _monitor_paused(),
    }


@app.post("/api/terminal/open")
async def open_monitored_terminal(_=Depends(_verify_token)):
    return _open_monitored_terminal()


# ── Pydantic 모델 ─────────────────────────────────────────────────────

class ProposeRequest(BaseModel):
    event_id:      str
    error_text:    str
    related_files: list[str] = []


class SuggestFilesRequest(BaseModel):
    error_text: str = ""
    related_files: list[str] = []


class FilePatchRequest(BaseModel):
    file_path: str
    issue_text: str = ""


class ApplyRequest(BaseModel):
    proposal_id: str


class ActionRequest(BaseModel):
    action: str   # UserAction.value


class InfraRunRequest(BaseModel):
    prefer_compose: bool = True


class ManualEventRequest(BaseModel):
    event_type: str = "error_detected"
    summary: str
    importance_score: int = 70
    error_text: str = ""
    raw_errors: list[str] = []


# ── Code Agent 연동 ───────────────────────────────────────────────────

class VisionAnalyzeRequest(BaseModel):
    include_image: bool = False
    create_event: bool = True
    user_question: str = ""


class AwsDeployRequest(BaseModel):
    host: str = ""
    user: str = ""
    ssh_key_path: str = ""
    container_port: str = ""
    host_port: str = ""
    container_name: str = ""
    image_name: str = ""


class GitHubRepoCreateRequest(BaseModel):
    name: str
    visibility: str = "private"
    description: str = ""


class GitHubBranchRequest(BaseModel):
    branch: str


class GitHubAddRequest(BaseModel):
    files: list[str] = []


class GitHubDiffRequest(BaseModel):
    files: list[str] = []


class GitHubCommitRequest(BaseModel):
    message: str


class GitHubPushRequest(BaseModel):
    branch: str = ""


class GitHubCommitPushRequest(BaseModel):
    files: list[str] = []
    message: str
    branch: str = ""


@app.post("/api/vision/analyze")
async def analyze_current_screen(body: VisionAnalyzeRequest, _=Depends(_verify_token)):
    """Explicit user-approved OCR/Vision analysis of the current screen."""
    from capture_agent import capture_foreground_window, extract_text_with_ocr
    from collectors.collect import collect_os_snapshot
    from collectors.terminal_output import ERROR_PATTERNS, match_patterns
    from context_gate import run_gate

    capture = await asyncio.to_thread(capture_foreground_window)
    if capture.blocked or capture.image is None:
        return {
            "status": "blocked",
            "blocked": True,
            "reason": capture.reason,
            "app_name": capture.app_name,
            "window_title": capture.window_title,
        }

    raw_text = await asyncio.to_thread(extract_text_with_ocr, capture.image)
    gate = run_gate(raw_text)
    matches = match_patterns([gate.text or raw_text], ERROR_PATTERNS)
    result: dict[str, Any] = {
        "status": "ok",
        "blocked": False,
        "app_name": capture.app_name,
        "window_title": capture.window_title,
        "ocr_text": gate.text,
        "quality_score": gate.quality_score,
        "passed": gate.passed,
        "raw_errors": matches,
        "vision": None,
        "event": None,
    }

    vision_result: dict[str, Any] = {}
    if body.include_image:
        try:
            from analyzer import analyze_context
            try:
                os_snapshot = await asyncio.to_thread(collect_os_snapshot)
            except Exception:
                os_snapshot = {
                    "foreground_processes": [{"name": capture.app_name, "title": capture.window_title}],
                    "terminal": {"new_commands": [], "recent": []},
                    "detected_errors": matches,
                }
            os_snapshot["detected_errors"] = matches
            vision_result = await asyncio.to_thread(
                analyze_context,
                capture.image,
                os_snapshot,
                _session_ref,
                body.user_question or None,
            )
            result["vision"] = vision_result
        except Exception as e:
            result["vision_error"] = str(e)

    summary = ""
    error_text = gate.text or raw_text
    importance = 70 if matches else 55
    if vision_result:
        summary = vision_result.get("error_description") or vision_result.get("summary") or ""
        error_text = summary or error_text
        importance = int(vision_result.get("importance_score") or importance)
    elif matches:
        summary = (gate.text or raw_text)[:160]

    if body.create_event and (matches or vision_result.get("has_error")):
        event = AgentEvent(
            event_id=uuid.uuid4().hex,
            event_type=EventType.ERROR_DETECTED,
            summary=(summary or "화면에서 에러가 감지되었습니다.")[:200],
            contexts=[],
            importance_score=max(0, min(importance, 100)),
            suggested_actions=[UserAction.FIX_CODE, UserAction.EXPLAIN, UserAction.IGNORE],
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw_errors=matches,
            error_text=error_text,
        )
        await _activate_event(event, "화면 OCR/Vision 분석으로 에러를 감지했습니다.")
        result["event"] = event.to_dict()

    return result


@app.get("/api/deploy/aws/status")
async def aws_deploy_status(_=Depends(_verify_token)):
    from deploy_agent import aws_deploy_status
    return aws_deploy_status(_project_root().resolve())


@app.post("/api/deploy/aws")
async def deploy_aws(body: AwsDeployRequest, _=Depends(_verify_token)):
    from deploy_agent import deploy_to_ec2

    overrides = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    result = await asyncio.to_thread(deploy_to_ec2, _project_root().resolve(), overrides)
    _append_session_event(
        "aws_deploy",
        "AWS EC2 deploy completed." if result.get("status") == "ok" else "AWS EC2 deploy failed.",
        result=result,
    )
    await _broadcast({
        "type": "deploy_result",
        "state": _orchestrator_state.value,
        "result": result,
    })
    if result.get("status") != "ok":
        raise HTTPException(status_code=500, detail=result)
    return result


# ── GitHub / Git Agent 연동 ─────────────────────────────────────────────

def _raise_git_agent_error(error: Exception) -> None:
    try:
        from git_agent import GitAgentError
    except Exception:
        GitAgentError = RuntimeError  # type: ignore
    if isinstance(error, GitAgentError):
        detail: dict[str, Any] = {"message": getattr(error, "message", str(error))}
        step = getattr(error, "step", None)
        if step:
            detail["step"] = step
        raise HTTPException(status_code=400, detail=detail)
    raise HTTPException(status_code=500, detail=str(error))


async def _broadcast_github_result(action: str, result: dict[str, Any]) -> None:
    _append_session_event(f"github_{action}", f"GitHub action completed: {action}", result=result)
    await _broadcast({
        "type": "github_result",
        "action": action,
        "result": result,
    })


@app.get("/api/github/status")
async def api_github_status(_=Depends(_verify_token)):
    from git_agent import github_status
    return await asyncio.to_thread(github_status, _project_root().resolve())


@app.post("/api/github/init")
async def api_github_init(_=Depends(_verify_token)):
    from git_agent import init_repository
    try:
        result = await asyncio.to_thread(init_repository, _project_root().resolve())
        await _broadcast_github_result("init", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/login")
async def api_github_login(_=Depends(_verify_token)):
    from git_agent import open_github_login
    try:
        result = await asyncio.to_thread(open_github_login, _project_root().resolve())
        await _broadcast_github_result("login", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/repo")
async def api_github_create_repo(body: GitHubRepoCreateRequest, _=Depends(_verify_token)):
    from git_agent import create_repository
    try:
        result = await asyncio.to_thread(
            create_repository,
            _project_root().resolve(),
            body.name,
            body.visibility,
            body.description,
        )
        await _broadcast_github_result("repo_create", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/branch")
async def api_github_branch(body: GitHubBranchRequest, _=Depends(_verify_token)):
    from git_agent import create_or_switch_branch
    try:
        result = await asyncio.to_thread(create_or_switch_branch, _project_root().resolve(), body.branch)
        await _broadcast_github_result("branch", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/add")
async def api_github_add(body: GitHubAddRequest, _=Depends(_verify_token)):
    from git_agent import add_files
    try:
        result = await asyncio.to_thread(add_files, _project_root().resolve(), body.files)
        await _broadcast_github_result("add", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/diff")
async def api_github_diff(body: GitHubDiffRequest, _=Depends(_verify_token)):
    from git_agent import diff_files
    try:
        result = await asyncio.to_thread(diff_files, _project_root().resolve(), body.files)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/commit")
async def api_github_commit(body: GitHubCommitRequest, _=Depends(_verify_token)):
    from git_agent import commit
    try:
        result = await asyncio.to_thread(commit, _project_root().resolve(), body.message)
        await _broadcast_github_result("commit", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/push")
async def api_github_push(body: GitHubPushRequest, _=Depends(_verify_token)):
    from git_agent import push
    try:
        result = await asyncio.to_thread(push, _project_root().resolve(), body.branch)
        await _broadcast_github_result("push", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


@app.post("/api/github/commit-push")
async def api_github_commit_push(body: GitHubCommitPushRequest, _=Depends(_verify_token)):
    from git_agent import commit_and_push
    try:
        result = await asyncio.to_thread(
            commit_and_push,
            _project_root().resolve(),
            body.files,
            body.message,
            body.branch,
        )
        await _broadcast_github_result("commit_push", result)
        return result
    except Exception as e:
        _raise_git_agent_error(e)


def _event_error_text(body_text: str = "") -> str:
    error_text = (body_text or "").strip()
    if not error_text and _current_event is not None:
        error_text = (
            _current_event.error_text
            or "\n".join(_current_event.raw_errors or [])
            or _current_event.summary
            or ""
        ).strip()
    return error_text


@app.post("/api/patch/suggest-files")
async def suggest_patch_files(body: SuggestFilesRequest, _=Depends(_verify_token)):
    from code_agent import suggest_related_file_paths

    error_text = _event_error_text(body.error_text)
    if not error_text:
        return {"files": [], "error_text": ""}
    files = await asyncio.to_thread(
        suggest_related_file_paths,
        error_text,
        body.related_files,
    )
    return {"files": files, "error_text": error_text}


@app.post("/api/patch/propose")
async def propose_patch(body: ProposeRequest, _=Depends(_verify_token)):
    """Gemini Flash 호출 → PatchProposal 생성."""
    global _current_patch, _orchestrator_state

    # 클라이언트가 error_text 를 비워 보내면 마지막으로 감지된 AgentEvent 에서 보충.
    # widget.py 에서 빈 문자열로 호출하던 옛 동작을 안전하게 흡수한다.
    error_text = _event_error_text(body.error_text)

    if not error_text:
        raise HTTPException(
            status_code=400,
            detail="에러 텍스트가 없습니다. 에러 감지 직후에 다시 시도해주세요.",
        )

    from code_agent import generate_patch_proposal, suggest_related_file_paths
    try:
        related_files = body.related_files or await asyncio.to_thread(
            suggest_related_file_paths,
            error_text,
            [],
        )
        proposal = await asyncio.to_thread(
            generate_patch_proposal,
            error_text,
            related_files,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _current_patch = proposal
    _orchestrator_state = OrchestratorState.CODE_PATCH_PROPOSED

    update = OrchestratorUpdate(
        state=_orchestrator_state,
        patch_proposal=proposal,
        message="코드 수정안이 생성되었습니다. 검토 후 승인해주세요.",
    )
    await _broadcast(update.to_dict())
    return proposal.to_dict()


@app.post("/api/patch/file-propose")
async def propose_file_patch(body: FilePatchRequest, _=Depends(_verify_token)):
    """Create a PatchProposal for a user-specified file without requiring terminal output."""
    global _current_event, _current_patch, _orchestrator_state

    file_path = body.file_path.strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="파일 경로가 필요합니다.")

    target = _safe_project_path(file_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"파일을 찾지 못했습니다: {file_path}")

    try:
        relative = str(target.resolve().relative_to(_project_root().resolve()))
    except ValueError:
        relative = file_path

    issue_text = body.issue_text.strip() or "이 파일에 포함된 문법/런타임 가능 오류를 찾아 최소 수정하세요."
    error_text = (
        "사용자가 터미널 실행 없이 파일 직접 수정을 요청했습니다.\n"
        f"대상 파일: {relative}\n"
        f"요청 내용: {issue_text}\n"
        "파일 전체를 덮어쓰지 말고, 문제 해결에 필요한 최소 unified diff만 생성하세요."
    )

    event = AgentEvent(
        event_id=uuid.uuid4().hex,
        event_type=EventType.USER_QUESTION,
        summary=f"파일 직접 수정 요청: {relative}",
        contexts=[],
        importance_score=70,
        suggested_actions=[UserAction.FIX_CODE, UserAction.IGNORE],
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        raw_errors=[],
        error_text=error_text,
    )
    _current_event = event

    from code_agent import generate_patch_proposal
    try:
        proposal = await asyncio.to_thread(generate_patch_proposal, error_text, [relative])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _current_patch = proposal
    _orchestrator_state = OrchestratorState.CODE_PATCH_PROPOSED
    _append_session_event("file_patch_requested", event.summary, target_path=relative)
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        event=event,
        patch_proposal=proposal,
        message="파일 직접 수정안이 생성되었습니다. 승인하면 실제 파일에 적용됩니다.",
    ).to_dict())
    return proposal.to_dict()


@app.post("/api/patch/apply")
async def apply_patch(_body: ApplyRequest, _=Depends(_verify_token)):
    """base_sha256 검증 + patch 적용 + 백업."""
    global _orchestrator_state

    if _current_patch is None:
        raise HTTPException(status_code=400, detail="적용할 PatchProposal이 없습니다.")

    from code_agent import apply_patch_proposal
    try:
        results = await asyncio.to_thread(apply_patch_proposal, _current_patch)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    ok_statuses = {"ok", "empty_diff"}
    all_ok = bool(results) and all(r.get("status") in ok_statuses for r in results)
    _orchestrator_state = OrchestratorState.CODE_READY if all_ok else OrchestratorState.CODE_PATCH_PROPOSED
    _append_session_event(
        "patch_applied" if all_ok else "patch_apply_failed",
        "Patch applied." if all_ok else "Patch apply failed.",
        results=results,
    )

    update = OrchestratorUpdate(
        state=_orchestrator_state,
        message="✅ 코드 수정이 적용되었습니다." if all_ok else "패치 적용에 실패했습니다. 결과를 확인해주세요.",
    )
    await _broadcast(update.to_dict())
    return {"status": "ok" if all_ok else "failed", "results": results}


@app.post("/api/patch/rollback")
async def rollback_patch(_body: ApplyRequest, _=Depends(_verify_token)):
    global _orchestrator_state

    if _current_patch is None:
        raise HTTPException(status_code=400, detail="롤백할 PatchProposal이 없습니다.")

    from code_agent import rollback_patch_proposal
    try:
        results = await asyncio.to_thread(rollback_patch_proposal, _current_patch)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    did_rollback = any(r.get("status") == "ok" for r in results)
    _orchestrator_state = OrchestratorState.WAITING_USER_ACTION if did_rollback else _orchestrator_state
    _append_session_event("rollback", "Patch rollback requested.", results=results)
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        message="롤백이 완료되었습니다." if did_rollback else "롤백할 백업을 찾지 못했습니다.",
    ).to_dict())
    return {"status": "ok" if did_rollback else "no_backup", "results": results}


@app.post("/api/patch/reject")
async def reject_patch(_=Depends(_verify_token)):
    global _orchestrator_state, _current_patch
    _current_patch = None
    _orchestrator_state = OrchestratorState.IDLE
    await _broadcast(OrchestratorUpdate(state=OrchestratorState.IDLE, message="수정안이 거절되었습니다.").to_dict())
    return {"status": "ok"}


# ── Infra Agent 연동 ──────────────────────────────────────────────────

@app.get("/api/infra/dockerfile")
async def get_dockerfile(project_path: str = ".", _: None = Depends(_verify_token)):
    global _current_infra, _orchestrator_state

    from infra_agent import generate_dockerfile
    try:
        proposal = await asyncio.to_thread(generate_dockerfile, project_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _current_infra = proposal
    _orchestrator_state = OrchestratorState.INFRA_PROPOSED

    update = OrchestratorUpdate(
        state=_orchestrator_state,
        infra_proposal=proposal,
        message="Dockerfile 미리보기입니다. 저장하려면 승인하세요.",
    )
    await _broadcast(update.to_dict())
    return proposal.to_dict()


@app.get("/api/infra/docker-compose")
async def get_docker_compose(project_path: str = ".", _: None = Depends(_verify_token)):
    global _current_infra, _orchestrator_state

    from infra_agent import generate_docker_compose
    try:
        proposal = await asyncio.to_thread(generate_docker_compose, project_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _current_infra = proposal
    _orchestrator_state = OrchestratorState.INFRA_PROPOSED
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        infra_proposal=proposal,
        message="docker-compose.yml 미리보기입니다. 저장하려면 승인하세요.",
    ).to_dict())
    return proposal.to_dict()


@app.get("/api/infra/github-actions")
async def get_github_actions(project_path: str = ".", _: None = Depends(_verify_token)):
    global _current_infra, _orchestrator_state

    from infra_agent import generate_github_actions
    try:
        proposal = await asyncio.to_thread(generate_github_actions, project_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _current_infra = proposal
    _orchestrator_state = OrchestratorState.INFRA_PROPOSED
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        infra_proposal=proposal,
        message="GitHub Actions CI 미리보기입니다. 저장하려면 승인하세요.",
    ).to_dict())
    return proposal.to_dict()


@app.post("/api/infra/save")
async def save_infra(_=Depends(_verify_token)):
    global _orchestrator_state

    if _current_infra is None:
        raise HTTPException(status_code=400, detail="저장할 인프라 파일이 없습니다.")

    target = _safe_project_path(_current_infra.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_current_infra.content, encoding='utf-8')
    dockerignore_path = None
    if _current_infra.target_path in {"Dockerfile", "docker-compose.yml"}:
        dockerignore_path = _ensure_dockerignore(_project_root().resolve())
    _orchestrator_state = OrchestratorState.INFRA_READY
    _append_session_event(
        "infra_saved",
        f"{_current_infra.target_path} saved.",
        file_type=_current_infra.file_type,
        target_path=_current_infra.target_path,
        dockerignore_path=dockerignore_path,
    )

    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        message=f"✅ {_current_infra.target_path} 저장 완료",
    ).to_dict())
    return {"status": "ok", "path": str(target)}


@app.post("/api/infra/run")
async def run_infra(body: InfraRunRequest | None = None, _=Depends(_verify_token)):
    """Build and run the generated Docker assets so they appear in Docker Desktop."""
    global _orchestrator_state

    body = body or InfraRunRequest()
    if shutil.which("docker") is None:
        raise HTTPException(
            status_code=500,
            detail="Docker CLI를 찾지 못했습니다. Docker Desktop을 설치하고 다시 실행하세요.",
        )

    saved_path = await asyncio.to_thread(_ensure_current_infra_saved_if_docker)
    root = _project_root().resolve()
    docker_info = await asyncio.to_thread(_check_docker_ready, root)
    compose_path = root / "docker-compose.yml"
    dockerfile_path = root / "Dockerfile"
    steps: list[dict[str, Any]] = [docker_info]

    if body.prefer_compose and compose_path.exists():
        step = await asyncio.to_thread(
            _run_docker_command,
            ["docker", "compose", "up", "-d", "--build"],
            root,
            600,
        )
        steps.append(step)
        if step["returncode"] != 0:
            _append_session_event("infra_run_failed", "docker compose up failed.", steps=steps)
            _raise_failed_docker_step(step)

        ps = await asyncio.to_thread(_run_docker_command, ["docker", "compose", "ps"], root, 60)
        steps.append(ps)
        result = {
            "status": "ok",
            "mode": "compose",
            "project_root": str(root),
            "saved_path": saved_path,
            "steps": steps,
        }
    else:
        if not dockerfile_path.exists():
            raise HTTPException(
                status_code=400,
                detail="Dockerfile 또는 docker-compose.yml이 없습니다. 먼저 인프라 파일을 생성/저장하세요.",
            )

        try:
            from infra_agent import _detect_stack
            _stack, meta = _detect_stack(str(root))
        except Exception:
            meta = {"port": "8000"}

        port = str(meta.get("port") or "8000").strip()
        host_port, port_was_remapped = _choose_host_port(port)
        slug = _docker_slug(root.name)
        image_name = f"recoder-{slug}:latest"
        container_name = f"recoder-{slug}"

        build_step = await asyncio.to_thread(
            _run_docker_command,
            ["docker", "build", "-t", image_name, "."],
            root,
            600,
        )
        steps.append(build_step)
        if build_step["returncode"] != 0:
            _append_session_event("infra_run_failed", "docker build failed.", steps=steps)
            _raise_failed_docker_step(build_step)

        existing = await asyncio.to_thread(
            _run_docker_command,
            ["docker", "ps", "-aq", "--filter", f"name=^/{container_name}$"],
            root,
            60,
        )
        steps.append(existing)
        if existing["returncode"] != 0:
            _append_session_event("infra_run_failed", "docker ps failed.", steps=steps)
            _raise_failed_docker_step(existing)

        if existing["output"].strip():
            remove_step = await asyncio.to_thread(
                _run_docker_command,
                ["docker", "rm", "-f", container_name],
                root,
                120,
            )
            steps.append(remove_step)
            if remove_step["returncode"] != 0:
                _append_session_event("infra_run_failed", "docker rm failed.", steps=steps)
                _raise_failed_docker_step(remove_step)

        run_args = ["docker", "run", "-d", "--name", container_name]
        if port:
            run_args.extend(["-p", f"{host_port}:{port}"])
        run_args.append(image_name)
        run_step = await asyncio.to_thread(_run_docker_command, run_args, root, 120)
        steps.append(run_step)
        if run_step["returncode"] != 0:
            _append_session_event("infra_run_failed", "docker run failed.", steps=steps)
            _raise_failed_docker_step(run_step)

        result = {
            "status": "ok",
            "mode": "dockerfile",
            "project_root": str(root),
            "saved_path": saved_path,
            "image": image_name,
            "container": container_name,
            "port": port,
            "host_port": host_port,
            "port_was_remapped": port_was_remapped,
            "url": f"http://127.0.0.1:{host_port}" if host_port else "",
            "steps": steps,
        }

    _orchestrator_state = OrchestratorState.INFRA_READY
    _append_session_event("infra_run", "Docker container started.", result=result)
    await _broadcast({
        "type": "infra_run_result",
        "state": _orchestrator_state.value,
        "result": result,
    })
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        message="Docker 컨테이너 실행이 완료되었습니다.",
    ).to_dict())
    return result


# ── 사용자 액션 (위젯 버튼 → 상태 전환) ──────────────────────────────

@app.post("/api/orchestrator/action")
async def user_action(body: ActionRequest, _=Depends(_verify_token)):
    global _orchestrator_state, _current_patch, _current_infra

    action = body.action
    if action in {UserAction.IGNORE.value, "cancel"}:
        _orchestrator_state = OrchestratorState.IDLE
        if action == "cancel":
            _current_patch = None
            _current_infra = None
        _append_session_event(action, f"User action: {action}")
        await _broadcast(OrchestratorUpdate(state=OrchestratorState.IDLE, message="무시됨").to_dict())
    elif action == "retry":
        _orchestrator_state = OrchestratorState.WAITING_USER_ACTION
        await _broadcast(OrchestratorUpdate(
            state=_orchestrator_state,
            event=_current_event,
            message="다시 시도할 수 있습니다.",
        ).to_dict())
    elif action == "pause":
        try:
            import monitor
            monitor.set_paused(True)
        except Exception:
            pass
        _append_session_event("pause", "Monitoring pause requested.")
        await _broadcast(OrchestratorUpdate(
            state=_orchestrator_state,
            message="모니터링 일시정지는 기록되었습니다.",
        ).to_dict())
    elif action == "resume":
        try:
            import monitor
            monitor.set_paused(False)
        except Exception:
            pass
        _append_session_event("resume", "Monitoring resumed.")
        await _broadcast(OrchestratorUpdate(
            state=_orchestrator_state,
            message="모니터링이 재개되었습니다.",
        ).to_dict())
    elif action == "rollback":
        if _current_patch is None:
            raise HTTPException(status_code=400, detail="롤백할 PatchProposal이 없습니다.")
        from code_agent import rollback_patch_proposal
        results = await asyncio.to_thread(rollback_patch_proposal, _current_patch)
        _append_session_event("rollback", "Patch rollback requested.", results=results)
        await _broadcast(OrchestratorUpdate(
            state=_orchestrator_state,
            message="롤백 요청이 처리되었습니다.",
        ).to_dict())

    return {"status": "ok", "state": _orchestrator_state.value}


@app.post("/api/orchestrator/event")
async def post_manual_event(body: ManualEventRequest, _=Depends(_verify_token)):
    global _current_event, _orchestrator_state

    try:
        event_type = EventType(body.event_type)
    except ValueError:
        event_type = EventType.ERROR_DETECTED

    event = AgentEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        summary=body.summary[:200],
        contexts=[],
        importance_score=max(0, min(body.importance_score, 100)),
        suggested_actions=[UserAction.FIX_CODE, UserAction.EXPLAIN, UserAction.IGNORE],
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        raw_errors=body.raw_errors,
        error_text=body.error_text or body.summary,
    )
    _current_event = event
    _orchestrator_state = OrchestratorState.WAITING_USER_ACTION
    _append_session_event(event.event_type.value, event.summary, raw_errors=event.raw_errors)
    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        event=event,
        message="수동 이벤트가 등록되었습니다.",
    ).to_dict())
    return event.to_dict()


# ── 채팅 API ─────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    context: str = ""   # 현재 에러 컨텍스트 (선택)


@app.post("/api/chat")
async def chat(body: ChatRequest, _=Depends(_verify_token)):
    """위젯 채팅창에서 보낸 질문을 Gemini에 전달하고 답변을 반환."""
    user_msg = body.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="빈 메시지입니다.")

    chat_guard = (
        "중요: 이 채팅 API는 파일 생성, 파일 수정, 파일 삭제, 명령 실행을 직접 수행하지 않습니다. "
        "사용자가 작업 실행을 요청하면 실제로 수행했다고 말하지 말고, 현재 채팅에서는 설명만 가능하다고 답하세요."
    )

    # 컨텍스트가 있으면 프롬프트에 포함
    if body.context:
        prompt = (
            f"{chat_guard}\n\n"
            f"## 현재 에러 컨텍스트\n{body.context[:800]}\n\n"
            f"## 질문\n{user_msg}\n\n"
            "한국어로 간결하게 답변하세요."
        )
    else:
        prompt = f"{chat_guard}\n\n## 질문\n{user_msg}\n\n한국어로 간결하게 답변하세요."

    try:
        from code_agent import _call_with_fallback, _get_client
        client = _get_client()
        response, _used_model = await asyncio.to_thread(_call_with_fallback, client, prompt)
        answer = (response.text or "").strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 응답 실패: {e}")

    # SSE로 채팅 응답 브로드캐스트 (위젯이 실시간 수신)
    await _broadcast({
        "type":    "chat_response",
        "message": answer,
    })
    return {"answer": answer}


# ── 대시보드 HTML ─────────────────────────────────────────────────────

@app.get("/")
@app.get("/dashboard")
async def dashboard():
    if DASHBOARD_PATH.exists():
        return FileResponse(DASHBOARD_PATH)
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


# ── 토큰 노출 엔드포인트 (첫 실행 시 위젯이 호출) ──────────────────────

@app.get("/api/token")
async def get_token(request: Request):
    """127.0.0.1에서만 접근 가능. 위젯이 토큰을 가져가는 용도."""
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")
    return {"token": SESSION_TOKEN, "port": PORT}


# ── 서버 시작 유틸 ────────────────────────────────────────────────────

def get_dashboard_url() -> str:
    return f"http://127.0.0.1:{PORT}/dashboard?token={SESSION_TOKEN}"


def get_event_queue() -> asyncio.Queue:
    return _event_queue


def notify_session_update(reason: str = "status") -> None:
    """monitor.py 호환용 — 브로드캐스트."""
    payload = {
        "type":   "session_update",
        "reason": reason,
        "state":  _orchestrator_state.value,
    }
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


async def start_server(session_index_ref: dict) -> None:
    global _session_ref
    _session_ref = session_index_ref

    # monitor.py에 큐 등록
    import monitor as _monitor
    _monitor.set_server_queue(_event_queue)

    asyncio.create_task(_consume_events())

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    _server_ready.set()
    print(f"[server] 대시보드: {get_dashboard_url()}")
    await server.serve()


def wait_until_ready(timeout: float = 15.0) -> bool:
    return _server_ready.wait(timeout)


def open_dashboard() -> None:
    webbrowser.open(get_dashboard_url())
