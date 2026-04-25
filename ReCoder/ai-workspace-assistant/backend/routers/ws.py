from __future__ import annotations

import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from jose import JWTError, jwt
from pydantic import BaseModel

from routers.auth import ALGORITHM, _get_jwt_secret, get_current_user

router = APIRouter()

connected_agents: dict[str, WebSocket] = {}
pending_requests: dict[str, asyncio.Future] = {}
pending_request_users: dict[str, str] = {}
connected_agent_tokens: dict[str, str] = {}


class ChatRequest(BaseModel):
    user_id: str
    question: str


def _chat_timeout_seconds() -> int:
    try:
        timeout = int(os.getenv('CHAT_TIMEOUT_SECONDS', '30'))
    except ValueError:
        return 30
    return max(1, timeout)


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(' ', 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    return parts[1].strip()


def _verify_ws_user(token: str | None, user_id: str) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[ALGORITHM])
    except JWTError:
        return False
    return payload.get('sub') == user_id


@router.websocket('/ws/{user_id}')
async def websocket_agent(websocket: WebSocket, user_id: str) -> None:
    token = _extract_bearer_token(websocket.headers.get('authorization'))
    if not _verify_ws_user(token, user_id):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    connection_token = str(uuid.uuid4())
    connected_agents[user_id] = websocket
    connected_agent_tokens[user_id] = connection_token
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type')
            if msg_type == 'chat_answer':
                request_id = data.get('request_id')
                future = pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(data.get('answer', ''))
            elif msg_type == 'ping':
                await websocket.send_json({'type': 'pong'})
    except WebSocketDisconnect:
        pass
    finally:
        if connected_agent_tokens.get(user_id) == connection_token:
            connected_agents.pop(user_id, None)
            connected_agent_tokens.pop(user_id, None)

        request_ids = [
            request_id
            for request_id, owner_user_id in pending_request_users.items()
            if owner_user_id == user_id
        ]
        for request_id in request_ids:
            future = pending_requests.get(request_id)
            if future and not future.done():
                future.cancel()
            pending_requests.pop(request_id, None)
            pending_request_users.pop(request_id, None)


@router.post('/chat')
async def chat_with_agent(payload: ChatRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if payload.user_id != current_user['user_id']:
        raise HTTPException(status_code=403, detail='Forbidden')

    websocket = connected_agents.get(payload.user_id)
    if websocket is None:
        raise HTTPException(status_code=404, detail='Agent is not connected')

    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending_requests[request_id] = future
    pending_request_users[request_id] = payload.user_id

    try:
        await websocket.send_json(
            {
                'type': 'chat_question',
                'request_id': request_id,
                'question': payload.question,
            }
        )
        answer = await asyncio.wait_for(future, timeout=_chat_timeout_seconds())
        return {'answer': answer}
    except asyncio.CancelledError as exc:
        raise HTTPException(status_code=503, detail='Agent disconnected') from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail='Agent response timeout') from exc
    finally:
        pending_requests.pop(request_id, None)
        pending_request_users.pop(request_id, None)


@router.get('/agent/status')
async def get_agent_status(current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user['user_id']
    websocket = connected_agents.get(user_id)
    return {'connected': websocket is not None and connected_agent_tokens.get(user_id) is not None}
