from __future__ import annotations

import asyncio
import json
import os

import httpx
import websockets

import analyzer
from collectors.collect import collect_os_snapshot


def _normalize_ws_url(api_ws_url: str, api_base_url: str) -> str:
    ws_url = api_ws_url.strip().rstrip('/')
    if ws_url:
        return ws_url

    base = api_base_url.strip().rstrip('/')
    if base.startswith('https://'):
        return f"wss://{base[len('https://'):]}"
    if base.startswith('http://'):
        return f"ws://{base[len('http://'):]}"
    return ''


async def _fetch_rag_context(api_base_url: str, user_token: str, question: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f'{api_base_url}/rag',
                params={'q': question},
                headers={'Authorization': f'Bearer {user_token}'},
            )
            response.raise_for_status()
            return response.json().get('results', [])
    except Exception as e:
        print(f'[ws_client] RAG 조회 실패: {e}')
        return []


def _empty_os_snapshot() -> dict:
    return {
        'foreground_processes': [],
        'terminal': {'new_commands': [], 'recent': []},
        'clipboard': {'changed': False, 'content': ''},
    }


async def listen_ws(capture_func, session_index_ref: dict) -> None:
    user_token = os.getenv('USER_TOKEN', '').strip()
    user_id = os.getenv('USER_ID', '').strip()
    api_base_url = os.getenv('API_BASE_URL', '').strip().rstrip('/')
    api_ws_url = _normalize_ws_url(os.getenv('API_WS_URL', ''), api_base_url)

    if not user_token:
        print('[ws_client] USER_TOKEN이 없어 WebSocket 연결을 건너뜁니다.')
        return

    if not user_id or not api_ws_url or not api_base_url:
        print('[ws_client] USER_ID/API_WS_URL/API_BASE_URL 설정이 필요합니다.')
        return

    ws_url = f'{api_ws_url}/ws/{user_id}'

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                additional_headers={'Authorization': f'Bearer {user_token}'},
            ) as websocket:
                print(f'[ws_client] 연결됨: {ws_url}')
                async for message in websocket:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    if data.get('type') != 'chat_question':
                        continue

                    request_id = data.get('request_id')
                    question = str(data.get('question', '')).strip()

                    rag_results = await _fetch_rag_context(api_base_url, user_token, question)
                    try:
                        screenshot = await asyncio.to_thread(capture_func)
                    except Exception as e:
                        print(f'[ws_client] 스크린샷 캡처 실패, 텍스트 전용 분석으로 폴백합니다: {e}')
                        screenshot = None
                    try:
                        os_snapshot = await asyncio.to_thread(collect_os_snapshot)
                    except Exception as e:
                        print(f'[ws_client] OS 스냅샷 수집 실패: {e}')
                        os_snapshot = _empty_os_snapshot()

                    if screenshot is None:
                        result = await asyncio.to_thread(
                            analyzer.analyze_text_context,
                            os_snapshot,
                            session_index_ref,
                            question,
                            rag_results,
                        )
                    else:
                        result = await asyncio.to_thread(
                            analyzer.analyze_context,
                            screenshot,
                            os_snapshot,
                            session_index_ref,
                            question,
                            rag_results,
                        )
                    answer = result.get('answer') or result.get('summary') or '답변을 생성하지 못했습니다.'
                    await websocket.send(
                        json.dumps(
                            {
                                'type': 'chat_answer',
                                'request_id': request_id,
                                'answer': answer,
                            },
                            ensure_ascii=False,
                        )
                    )
        except Exception as e:
            print(f'[ws_client] 연결 끊김: {e}. 5초 후 재연결합니다.')
            await asyncio.sleep(5)
