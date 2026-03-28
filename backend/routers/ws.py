# routers/ws.py
# WebSocket 서버 + /chat 엔드포인트
#
# ⚠ --workers 1 필수: connected_agents 딕셔너리가 프로세스마다 분리되면 채팅 불가
# ⚠ 에이전트가 먼저 WebSocket 연결해야 /chat 동작함

import asyncio
import json
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from routers.auth import get_current_user

router = APIRouter()

# 연결된 에이전트 {user_id: WebSocket}
connected_agents: Dict[str, WebSocket] = {}

# 대기 중인 요청 {request_id: Future}
pending_requests: Dict[str, asyncio.Future] = {}


# ── WebSocket 엔드포인트 ──────────────────────────────────────
@router.websocket('/ws/{user_id}')
async def ws_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    connected_agents[user_id] = websocket
    print(f'✅ 에이전트 연결: user_id={user_id}')

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            if msg.get('type') == 'chat_answer':
                req_id = msg.get('request_id')
                if req_id and req_id in pending_requests:
                    pending_requests[req_id].set_result(msg.get('answer', ''))

    except WebSocketDisconnect:
        connected_agents.pop(user_id, None)
        print(f'❌ 에이전트 연결 종료: user_id={user_id}')


# ── 채팅 엔드포인트 ───────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None


@router.post('/chat')
async def chat(req: ChatRequest, user=Depends(get_current_user)):
    user_id = user['user_id']

    if user_id not in connected_agents:
        raise HTTPException(503, '에이전트가 연결되어 있지 않습니다. agent를 먼저 실행해주세요.')

    request_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    pending_requests[request_id] = future

    try:
        await connected_agents[user_id].send_text(json.dumps({
            'type': 'chat_question',
            'question': req.question,
            'request_id': request_id,
        }))
    except Exception:
        connected_agents.pop(user_id, None)
        pending_requests.pop(request_id, None)
        raise HTTPException(503, '에이전트 연결이 끊어졌습니다. 재연결 중...')

    try:
        answer = await asyncio.wait_for(future, timeout=30.0)
        return {'answer': answer}
    except asyncio.TimeoutError:
        raise HTTPException(504, '에이전트 응답 시간 초과 (30초)')
    finally:
        pending_requests.pop(request_id, None)


# ── 에이전트 연결 상태 확인 ───────────────────────────────────
@router.get('/agent/status')
def agent_status(user=Depends(get_current_user)):
    user_id = user['user_id']
    return {'connected': user_id in connected_agents}
