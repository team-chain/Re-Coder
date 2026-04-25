"""로컬 웹 대시보드용 HTTP/WebSocket 서버."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from prompt_generator import generate_fix_prompt


BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_PATH = BASE_DIR / 'dashboard' / 'index.html'
ERROR_EVENT_TYPES = {'error_detected', 'terminal_error', 'infra_error'}

_session_ref: dict | None = None
_clients: set[web.WebSocketResponse] = set()
_update_queue: asyncio.Queue | None = None
_server_loop: asyncio.AbstractEventLoop | None = None
_server_ready = threading.Event()


def get_local_port() -> int:
    raw = os.getenv('LOCAL_PORT', '18080').strip()
    try:
        return int(raw)
    except ValueError:
        return 18080


def get_dashboard_url() -> str:
    return f'http://127.0.0.1:{get_local_port()}'


def wait_until_server_ready(timeout: float = 15.0) -> bool:
    return _server_ready.wait(timeout)


def open_dashboard() -> None:
    webbrowser.open(get_dashboard_url())


def notify_session_update(reason: str = 'status') -> None:
    if _server_loop is None or _update_queue is None:
        return

    payload = _build_update_payload(reason)
    try:
        _server_loop.call_soon_threadsafe(_queue_update, payload)
    except RuntimeError:
        return


def _queue_update(payload: dict[str, Any]) -> None:
    if _update_queue is None:
        return
    if _update_queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            _update_queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        _update_queue.put_nowait(payload)


def _snapshot_session_state() -> dict[str, Any]:
    if _session_ref is None:
        return {}

    for _ in range(3):
        try:
            return _clone_value(_session_ref)
        except RuntimeError:
            time.sleep(0)
    return {}


def _clone_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clone_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_value(item) for item in value]
    return value


def _session_status() -> dict[str, Any]:
    status = _snapshot_session_state()
    status['server_connected'] = bool(os.getenv('USER_TOKEN', '').strip())
    status['local_url'] = get_dashboard_url()
    return status


def _recent_events(limit: int = 50) -> list[dict[str, Any]]:
    events = _session_status().get('events', [])
    if not isinstance(events, list):
        return []
    return events[-limit:]


def _find_matching_error_detail(event: dict[str, Any], session_state: dict[str, Any]) -> dict[str, Any]:
    error_history = session_state.get('error_history', [])
    event_time = str(event.get('time', ''))[:19]
    for entry in error_history:
        if str(entry.get('time', ''))[:19] == event_time:
            return entry
    last_error = session_state.get('last_error')
    return last_error if isinstance(last_error, dict) else {}


def _find_matching_infra_detail(event: dict[str, Any], session_state: dict[str, Any]) -> dict[str, Any]:
    """infra_error 이벤트에 대응하는 Gemini 분석 결과(solution/command)를 error_history에서 찾는다.
    container명 또는 pod명 + 시간 기반으로 매칭."""
    error_history = session_state.get('error_history', [])
    source = event.get('source', 'docker')
    if source == 'docker':
        target_name = event.get('container', '')
    else:
        target_name = event.get('pod', '')

    # 시간 기반 매칭 (±30초 허용)
    event_time_str = str(event.get('time', ''))[:19]
    for entry in reversed(error_history):
        entry_time_str = str(entry.get('time', ''))[:19]
        desc = str(entry.get('error_description', ''))
        # container/pod 이름이 error_description에 포함되어 있으면 매칭
        if target_name and target_name in desc:
            return entry
        # 시간이 비슷하면 매칭 (같은 분 내)
        if event_time_str and entry_time_str and event_time_str[:16] == entry_time_str[:16]:
            return entry

    # 최신 에러를 폴백으로
    last_error = session_state.get('last_error')
    return last_error if isinstance(last_error, dict) else {}


def _error_events(limit: int = 50) -> list[dict[str, Any]]:
    session_state = _session_status()
    events = session_state.get('events', [])
    if not isinstance(events, list):
        return []

    resolved_times = {
        str(event.get('time', ''))
        for event in events
        if event.get('type') == 'resolved'
    }
    error_events: list[dict[str, Any]] = []
    for event in events:
        evt_type = event.get('type')
        if evt_type not in ERROR_EVENT_TYPES:
            continue

        enriched = dict(event)
        event_time = str(event.get('time', ''))
        enriched['resolved'] = any(rt > event_time for rt in resolved_times if rt)

        if evt_type == 'infra_error':
            # infra_error 전용 enrichment
            # widget._dispatch()가 ev.get("infra_source") or ev.get("source") 로 판단
            source = event.get('source', 'docker')
            enriched['infra_source'] = source

            # widget._process_infra()가 ev.get("infra_error", ev) 로 infra 서브딕셔너리 접근
            # event 자체에 container/status(docker) 또는 pod/reason(k8s) 필드가 있으므로 그대로 사용
            # solution/command는 Gemini 분석 완료 후 error_history에서 매칭
            detail = _find_matching_infra_detail(event, session_state)
            enriched['solution'] = detail.get('solution', '')
            enriched['command'] = detail.get('command', '')
            enriched['error_description'] = detail.get(
                'error_description',
                f"{source}: {event.get('container', event.get('pod', ''))} — "
                f"{event.get('status', event.get('reason', ''))}",
            )
            enriched['current_task'] = detail.get('current_task', session_state.get('current_task', ''))
        else:
            detail = _find_matching_error_detail(event, session_state)
            enriched['error_description'] = detail.get(
                'error_description',
                ', '.join(event.get('errors', [])),
            )
            enriched['solution'] = detail.get('solution', '')
            enriched['command'] = detail.get('command', '')
            enriched['current_task'] = detail.get('current_task', session_state.get('current_task', ''))

        error_events.append(enriched)

    return error_events[-limit:]


def _recent_commands(session_state: dict[str, Any], limit: int = 10) -> list[str]:
    commands: list[str] = []
    for event in session_state.get('events', []):
        if event.get('type') != 'terminal_commands':
            continue
        for command in event.get('commands', []):
            command_text = str(command).strip()
            if command_text:
                commands.append(command_text)
    return commands[-limit:]


def _resolve_prompt_context(payload: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    session_state = _session_status()
    error_description = str(payload.get('error_description') or '').strip()
    solution = str(payload.get('solution') or '').strip()
    current_task = str(payload.get('current_task') or session_state.get('current_task') or '').strip()
    recent_commands = payload.get('recent_commands')
    if not isinstance(recent_commands, list):
        recent_commands = _recent_commands(session_state)

    if not error_description:
        event_time = str(payload.get('time') or payload.get('event_time') or '').strip()
        for event in reversed(_error_events(limit=100)):
            if event_time and str(event.get('time', '')) != event_time:
                continue
            error_description = str(
                event.get('error_description') or ', '.join(event.get('errors', []))
            ).strip()
            solution = solution or str(event.get('solution') or '').strip()
            current_task = current_task or str(event.get('current_task') or '').strip()
            break

    if not error_description:
        last_error = session_state.get('last_error', {})
        error_description = str(last_error.get('error_description') or '').strip()
        solution = solution or str(last_error.get('solution') or '').strip()
        current_task = current_task or str(last_error.get('current_task') or '').strip()

    return error_description, solution, current_task, recent_commands


def _build_update_payload(reason: str = 'status') -> dict[str, Any]:
    return {
        'type': 'session_update',
        'reason': reason,
        'status': _session_status(),
        'events': _recent_events(),
        'errors': _error_events(),
        'task_timeline': _session_status().get('task_timeline', []),
    }


async def _handle_status(_request: web.Request) -> web.Response:
    return web.json_response(_session_status())


async def _handle_events(_request: web.Request) -> web.Response:
    return web.json_response(_recent_events())


async def _handle_errors(_request: web.Request) -> web.Response:
    return web.json_response(_error_events())


async def _handle_timeline(_request: web.Request) -> web.Response:
    return web.json_response(_session_status().get('task_timeline', []))


async def _handle_prompt(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'error': '잘못된 요청 형식입니다.'}, status=400)

    error_description, solution, current_task, recent_commands = _resolve_prompt_context(payload)
    if not error_description:
        return web.json_response({'error': '에러 정보가 없습니다.'}, status=400)

    session_state = _session_status()

    source_files = []
    try:
        import sys
        sys.path.append(os.path.join(os.path.dirname(__file__), 'collectors'))
        from collectors import source_context
        
        file_paths = []
        if isinstance(payload, dict) and payload.get('related_files'):
            file_paths.extend(payload.get('related_files', []))
            
        if not file_paths:
            timeline = session_state.get('task_timeline', [])
            if timeline:
                file_paths.extend(timeline[-1].get('related_files', []))
                
        if not file_paths:
            active_windows = session_state.get('active_windows', [])
            if active_windows:
                for w in active_windows[-1].get('windows', []):
                    title = w.get('name', '')
                    if title:
                        extracted = source_context.extract_file_path_from_window_title(title)
                        if extracted:
                            file_paths.append(extracted)

        file_paths = list(dict.fromkeys(file_paths))
        
        total_chars = 0
        for fp in file_paths:
            resolved_path = source_context.resolve_source_file_path(fp)
            if not resolved_path:
                continue
                
            content = source_context.get_relevant_source(resolved_path)
            if not content:
                content = "(읽기 실패)"
            
            if total_chars + len(content) > 50000:
                if total_chars == 0:
                    content = content[:50000] + "\n... (생략됨) ..."
                    total_chars += len(content)
                    source_files.append({"path": fp, "content": content})
                else:
                    source_files.append({"path": fp, "content": "(길이 제한으로 내용 생략됨)"})
            else:
                total_chars += len(content)
                source_files.append({"path": fp, "content": content})
                
    except Exception as e:
        print(f"[local_server] 소스 파일 수집 실패: {e}")

    try:
        prompt = await asyncio.to_thread(
            generate_fix_prompt,
            error_description,
            solution,
            current_task,
            recent_commands,
            session_state,
            source_files,
        )
        return web.json_response({'prompt': prompt})
    except Exception as e:
        print(f'[local_server] 프롬프트 생성 실패: {e}')
        fallback = (
            f'다음 에러를 수정해주세요: {error_description}\n'
            f'현재 작업: {current_task}\n'
            f'제안된 해결법: {solution}'
        )
        return web.json_response({'prompt': fallback, 'fallback': True})


async def _handle_ask(request: web.Request) -> web.Response:
    """
    POST /api/ask
    Body: {"question": "..."}
    Response: {"answer": "..."}

    사용자 자유 질문을 Gemini에 직접 전달하고 자연어 답변을 반환.
    현재 세션 컨텍스트(에러 히스토리, 현재 작업)를 시스템 프롬프트에 주입해
    개발 상황을 아는 AI처럼 동작하게 한다.
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'error': '잘못된 요청 형식입니다.'}, status=400)

    question = str(payload.get('question') or '').strip()
    if not question:
        return web.json_response({'error': '질문이 비어있습니다.'}, status=400)

    session_state = _session_status()
    current_task  = session_state.get('current_task', '')
    last_error    = session_state.get('last_error', {})
    error_history = session_state.get('error_history', [])

    # 세션 컨텍스트 요약 (최근 에러 3개)
    recent_errors = error_history[-3:] if error_history else []
    context_lines = []
    if current_task:
        context_lines.append(f"현재 작업: {current_task}")
    if last_error:
        context_lines.append(
            f"최근 에러: {last_error.get('error_description', '')} "
            f"(해결책: {last_error.get('solution', '')})"
        )
    if recent_errors:
        summaries = [e.get('error_description', '') for e in recent_errors if e.get('error_description')]
        if summaries:
            context_lines.append(f"에러 이력: {' / '.join(summaries)}")

    context_block = '\n'.join(context_lines) if context_lines else '(세션 정보 없음)'

    system_prompt = (
        "당신은 개발자의 화면을 실시간으로 감시하는 AI 개발 어시스턴트 ReCoder입니다. "
        "개발자의 에러를 감지하고 명령어와 해결책을 제시하는 역할을 합니다. "
        "항상 한국어로 간결하고 실용적으로 답하세요. "
        "명령어가 필요한 경우 코드 블록 없이 명령어만 제시하세요."
    )

    user_prompt = (
        f"[현재 세션 컨텍스트]\n{context_block}\n\n"
        f"[사용자 질문]\n{question}"
    )

    try:
        import os as _os
        api_key = _os.getenv('GEMINI_API_KEY', '').strip()
        if not api_key:
            return web.json_response({'error': 'GEMINI_API_KEY가 설정되지 않았습니다.'}, status=500)

        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)
        model  = _os.getenv('GEMINI_MODEL', 'gemini-2.0-flash-lite')

        response = await asyncio.to_thread(
            lambda: client.models.generate_content(
                model=model,
                contents=[user_prompt],
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=1024,
                ),
            )
        )
        answer = (response.text or '').strip()
        if not answer:
            return web.json_response({'error': 'AI가 빈 응답을 반환했습니다.'}, status=500)

        return web.json_response({'answer': answer})

    except Exception as e:
        print(f'[local_server] /api/ask 오류: {e}')
        return web.json_response({'error': f'AI 호출 실패: {e}'}, status=500)


async def _handle_dashboard(_request: web.Request) -> web.Response:
    return web.FileResponse(DASHBOARD_PATH)


async def _handle_ws_updates(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _clients.add(ws)
    await ws.send_json(_build_update_payload('connected'))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                print(f'[local_server] websocket error: {ws.exception()}')
    finally:
        _clients.discard(ws)

    return ws


async def _broadcast_updates() -> None:
    while True:
        if _update_queue is None:
            await asyncio.sleep(0.1)
            continue

        payload = await _update_queue.get()
        stale_clients = []
        for ws in list(_clients):
            if ws.closed:
                stale_clients.append(ws)
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                stale_clients.append(ws)
        for ws in stale_clients:
            _clients.discard(ws)


async def start_local_server(session_index_ref: dict) -> None:
    global _session_ref, _update_queue, _server_loop

    _session_ref = session_index_ref
    _server_loop = asyncio.get_running_loop()
    _update_queue = asyncio.Queue(maxsize=32)

    app = web.Application()
    app.router.add_get('/api/status', _handle_status)
    app.router.add_get('/api/events', _handle_events)
    app.router.add_get('/api/errors', _handle_errors)
    app.router.add_get('/api/timeline', _handle_timeline)
    app.router.add_post('/api/prompt', _handle_prompt)
    app.router.add_post('/api/ask', _handle_ask)
    app.router.add_get('/ws/updates', _handle_ws_updates)
    app.router.add_get('/', _handle_dashboard)
    app.router.add_get('/index.html', _handle_dashboard)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='127.0.0.1', port=get_local_port())
    broadcaster = asyncio.create_task(_broadcast_updates())

    try:
        await site.start()
        _server_ready.set()
        print(f'[local_server] 대시보드 시작: {get_dashboard_url()}')
        await asyncio.Event().wait()
    finally:
        broadcaster.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await broadcaster
        for ws in list(_clients):
            with contextlib.suppress(Exception):
                await ws.close()
        _clients.clear()
        _server_ready.clear()
        await runner.cleanup()