# ws_client.py
# WebSocket 클라이언트 + httpx RAG 검색
#
# ▶ 설계 근거: 일반 PC는 NAT 뒤에 있어 서버가 직접 HTTP 요청을 보낼 수 없습니다.
# 에이전트가 WebSocket을 먼저 연결하고 대기하면 서버가 이 통로로 메시지를 푸시할 수 있습니다.
# async def 안에서 동기 requests를 쓰면 이벤트 루프 전체가 블로킹되므로 httpx.AsyncClient를 사용합니다.
#
# ping_interval=20: NAT 타임아웃 방지 (20초마다 Ping/Pong)
# 연결 실패 시 5초 후 자동 재연결

import asyncio
import json
import os

import httpx
import websockets
from dotenv import load_dotenv

import analyzer
from collectors.collect import collect_os_snapshot

load_dotenv()

API_BASE_URL = os.getenv('API_BASE_URL', 'http://your-ec2-ip')
WS_URL = os.getenv('API_WS_URL', 'ws://your-ec2-ip')
user_id = os.getenv('USER_ID', '')
token = os.getenv('USER_TOKEN', '')


def _capture_all_monitors():
    """현재 화면 전체 캡처 (mss 사용, 모든 모니터 통합)."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])  # monitors[0] = 전체 모니터 통합
        return Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')


async def ws_listen(session_index: dict):
    """WebSocket 연결 유지 + 채팅 질문 수신 처리.

    서버에서 chat_question 메시지를 수신하면:
    1. RAG로 과거 유사 세션 검색
    2. 현재 화면 캡처
    3. Gemini Vision으로 질문 답변 생성
    4. chat_answer로 서버에 전송
    """
    while True:
        try:
            async with websockets.connect(
                f'{WS_URL}/ws/{user_id}',
                ping_interval=20,   # NAT 타임아웃 방지
                ping_timeout=10,
                extra_headers={'Authorization': f'Bearer {token}'},
            ) as ws:
                print(f'✅ WebSocket 연결됨: {WS_URL}/ws/{user_id}')

                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    if data.get('type') == 'chat_question':
                        question = data.get('question', '')
                        request_id = data.get('request_id', '')

                        # 1. RAG: 과거 유사 세션 검색
                        past_sessions = []
                        try:
                            async with httpx.AsyncClient(timeout=10) as client:
                                resp = await client.get(
                                    f'{API_BASE_URL}/rag',
                                    params={'q': question, 'user_id': user_id},
                                    headers={'Authorization': f'Bearer {token}'},
                                )
                                if resp.status_code == 200:
                                    past_sessions = resp.json()
                        except Exception as e:
                            print(f'RAG 검색 실패: {e}')

                        # 2. 현재 화면 캡처 + OS 스냅샷
                        try:
                            screenshot = _capture_all_monitors()
                            os_snapshot = collect_os_snapshot()
                        except Exception as e:
                            print(f'캡처 실패: {e}')
                            screenshot = None
                            os_snapshot = {
                                'foreground_processes': [],
                                'terminal': {'new_commands': []},
                                'clipboard': {'content': ''},
                            }

                        # 3. Gemini Vision 분석
                        if screenshot:
                            result = await asyncio.to_thread(
                                analyzer.analyze_context,
                                screenshot,
                                os_snapshot,
                                session_index,
                                user_question=question,
                                past_sessions=past_sessions,
                            )
                        else:
                            result = {'answer': '화면 캡처에 실패했습니다.'}

                        # 4. 답변 전송
                        answer = result.get('answer', '답변을 생성하지 못했습니다.')
                        await ws.send(json.dumps({
                            'type': 'chat_answer',
                            'answer': answer,
                            'request_id': request_id,
                        }))

        except websockets.exceptions.ConnectionClosed as e:
            print(f'WebSocket 연결 종료 (code={e.code}), 5초 후 재연결...')
        except Exception as e:
            print(f'WebSocket 오류: {e}, 5초 후 재연결...')

        await asyncio.sleep(5)
