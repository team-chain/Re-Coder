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
    AgentEvent, InfraFileProposal, OrchestratorState,
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


# ── Pydantic 모델 ─────────────────────────────────────────────────────

class ProposeRequest(BaseModel):
    event_id:      str
    error_text:    str
    related_files: list[str] = []


class ApplyRequest(BaseModel):
    proposal_id: str


class ActionRequest(BaseModel):
    action: str   # UserAction.value


# ── Code Agent 연동 ───────────────────────────────────────────────────

@app.post("/api/patch/propose")
async def propose_patch(body: ProposeRequest, _=Depends(_verify_token)):
    """Gemini Flash 호출 → PatchProposal 생성."""
    global _current_patch, _orchestrator_state

    # 클라이언트가 error_text 를 비워 보내면 마지막으로 감지된 AgentEvent 에서 보충.
    # widget.py 에서 빈 문자열로 호출하던 옛 동작을 안전하게 흡수한다.
    error_text = (body.error_text or "").strip()
    if not error_text and _current_event is not None:
        error_text = (
            _current_event.error_text
            or "\n".join(_current_event.raw_errors or [])
            or _current_event.summary
            or ""
        ).strip()

    if not error_text:
        raise HTTPException(
            status_code=400,
            detail="에러 텍스트가 없습니다. 에러 감지 직후에 다시 시도해주세요.",
        )

    from code_agent import generate_patch_proposal
    try:
        proposal = await asyncio.to_thread(
            generate_patch_proposal,
            error_text,
            body.related_files,
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

    _orchestrator_state = OrchestratorState.CODE_READY

    update = OrchestratorUpdate(
        state=_orchestrator_state,
        message="✅ 코드 수정이 적용되었습니다.",
    )
    await _broadcast(update.to_dict())
    return {"status": "ok", "results": results}


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


@app.post("/api/infra/save")
async def save_infra(_=Depends(_verify_token)):
    global _orchestrator_state

    if _current_infra is None:
        raise HTTPException(status_code=400, detail="저장할 인프라 파일이 없습니다.")

    target = Path(_current_infra.target_path)
    target.write_text(_current_infra.content, encoding='utf-8')
    _orchestrator_state = OrchestratorState.INFRA_READY

    await _broadcast(OrchestratorUpdate(
        state=_orchestrator_state,
        message=f"✅ {_current_infra.target_path} 저장 완료",
    ).to_dict())
    return {"status": "ok", "path": str(target)}


# ── 사용자 액션 (위젯 버튼 → 상태 전환) ──────────────────────────────

@app.post("/api/orchestrator/action")
async def user_action(body: ActionRequest, _=Depends(_verify_token)):
    global _orchestrator_state

    action = body.action
    if action == UserAction.IGNORE.value:
        _orchestrator_state = OrchestratorState.IDLE
        await _broadcast(OrchestratorUpdate(state=OrchestratorState.IDLE, message="무시됨").to_dict())

    return {"status": "ok", "state": _orchestrator_state.value}


# ── 채팅 API ─────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    context: str = ""   # 현재 에러 컨텍스트 (선택)


@app.post("/api/chat")
async def chat(body: ChatRequest, _=Depends(_verify_token)):
    """위젯 채팅창에서 보낸 질문을 Gemini에 전달하고 답변을 반환."""
    import os

    user_msg = body.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="빈 메시지입니다.")

    # 컨텍스트가 있으면 프롬프트에 포함
    if body.context:
        prompt = (
            f"## 현재 에러 컨텍스트\n{body.context[:800]}\n\n"
            f"## 질문\n{user_msg}\n\n"
            "한국어로 간결하게 답변하세요."
        )
    else:
        prompt = f"{user_msg}\n\n한국어로 간결하게 답변하세요."

    try:
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        # google-genai 1.x SDK 는 키워드 인자만 받는다 (model=, contents=).
        # asyncio.to_thread 는 위치 인자만 전달하므로 lambda 로 감싼다.
        response = await asyncio.to_thread(
            lambda: client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
        )
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