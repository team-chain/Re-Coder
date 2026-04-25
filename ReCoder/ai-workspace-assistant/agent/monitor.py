"""
Monitor Agent — 재작성 버전
EasyOCR 완전 제거. UIA 텍스트 추출 + 터미널 패턴 감지 → Trigger Detector → Context Gate → AgentEvent.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import shutil
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from collectors.collect import collect_os_snapshot, init_cmd_count
from collectors.terminal_output import (
    ERROR_PATTERNS,
    RESOLVE_PATTERNS,
    match_patterns,
    watch_terminal_output,
)
from context_gate import run_gate
from trigger_detector import should_trigger
from schemas import (
    AgentEvent, ContextSource, ContextWeight, EventType,
    ExtractedContext, OrchestratorState, OrchestratorUpdate, UserAction,
)

BASE_DIR    = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / 'output' / 'sessions'

executor = ThreadPoolExecutor(max_workers=1)

session_index: dict = {}
_last_active_windows: list[dict] = []
_prev_task: str = ""
_prev_uia_text: str = ""

_server_event_queue: asyncio.Queue | None = None   # server.py가 등록


def set_server_queue(q: asyncio.Queue) -> None:
    global _server_event_queue
    _server_event_queue = q


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _create_session_index() -> dict:
    return {
        'session_id':      uuid.uuid4().hex,
        'start_time':      _now_str(),
        'end_time':        None,
        'active_windows':  [],
        'events':          [],
        'error_count':     0,
        'resolved':        False,
        'current_task':    None,
        'task_timeline':   [],
        'orchestrator_state': OrchestratorState.IDLE.value,
    }


def _empty_os_snapshot() -> dict:
    return {
        'foreground_processes': [],
        'terminal': {'new_commands': [], 'recent': []},
        'clipboard': {'changed': False, 'content': ''},
        'window_changed': False,
    }


def _get_session_paths() -> tuple[Path, Path]:
    session_dir  = SESSIONS_DIR / f"session_{session_index['session_id']}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir, session_dir


def _trim_session_index() -> None:
    max_events = 200
    if len(session_index['events']) > max_events:
        session_index['events'] = session_index['events'][-max_events:]


def _write_session_index(session_dir: Path | None = None) -> None:
    if session_dir is None:
        session_dir, _ = _get_session_paths()
    _trim_session_index()
    with open(session_dir / 'index.json', 'w', encoding='utf-8') as f:
        json.dump(session_index, f, ensure_ascii=False, indent=2)


# ── UIA 텍스트 추출 (2단계) ───────────────────────────────────────────

def extract_uia_text() -> tuple[str, bool]:
    """
    UI Automation으로 현재 포그라운드 창 텍스트를 추출한다.
    Returns: (text, failure_flag)
    """
    if sys.platform != 'win32':
        return "", True

    try:
        import comtypes.client
        import comtypes.gen.UIAutomationClient as UIA

        clsid_uia = comtypes.GUID('{FF48DBA4-60EF-4201-AA87-54103EEF594E}')
        iface_uia = comtypes.GUID('{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}')
        automation = comtypes.client.CreateObject(clsid_uia, interface=iface_uia)

        root = automation.GetRootElement()
        condition = automation.CreateTrueCondition()
        elements = root.FindAll(UIA.TreeScope_Descendants, condition)

        texts: list[str] = []
        for i in range(elements.Length):
            try:
                el = elements.GetElement(i)
                ct = el.CurrentControlType
                # Text / Edit / Document 컨트롤만
                if ct in (50020, 50004, 50030):
                    val = el.CurrentName or ""
                    if val.strip():
                        texts.append(val.strip())
            except Exception:
                continue

        combined = "\n".join(texts)
        if len(combined) < 20:
            return combined, True
        return combined, False

    except Exception as e:
        return "", True


# ── Window Tracker (1단계) ────────────────────────────────────────────

def get_active_window_info() -> dict:
    """현재 포그라운드 창 정보 반환."""
    if sys.platform == 'win32':
        try:
            import win32gui
            hwnd  = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            return {"title": title, "hwnd": hwnd}
        except Exception:
            pass
    elif sys.platform == 'darwin':
        try:
            import subprocess
            result = subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            return {"title": result.stdout.strip()}
        except Exception:
            pass
    return {"title": ""}


# ── AgentEvent 생성 및 전달 ───────────────────────────────────────────

def _build_agent_event(
    error_text:  str,
    raw_errors:  list[str],
    context_id:  str,
    score:       int,
) -> AgentEvent:
    return AgentEvent(
        event_id          = uuid.uuid4().hex,
        event_type        = EventType.ERROR_DETECTED,
        summary           = error_text[:120],
        contexts          = [context_id],
        importance_score  = min(score, 100),
        suggested_actions = [UserAction.FIX_CODE, UserAction.EXPLAIN, UserAction.IGNORE],
        created_at        = _now_iso(),
        raw_errors        = raw_errors,
        error_text        = error_text,
    )


async def _post_agent_event(event: AgentEvent) -> None:
    """server.py의 큐로 AgentEvent 전달."""
    if _server_event_queue is not None:
        try:
            _server_event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    # session_index에도 기록
    session_index['events'].append({
        'type':       event.event_type.value,
        'time':       event.created_at,
        'event_id':   event.event_id,
        'summary':    event.summary,
        'raw_errors': event.raw_errors,
        'score':      event.importance_score,
    })
    session_index['error_count'] = session_index.get('error_count', 0) + 1
    session_index['orchestrator_state'] = OrchestratorState.ERROR_DETECTED.value
    await asyncio.to_thread(_write_session_index)


# ── 터미널 에러 핸들러 ────────────────────────────────────────────────

async def _on_terminal_error_detected(output: str, raw_errors: list[str]) -> None:
    gate = run_gate(output)
    masked_text = gate.text if gate.text else output

    context = ExtractedContext(
        context_id    = uuid.uuid4().hex,
        source        = ContextSource.TERMINAL,
        app_name      = "Terminal",
        window_title  = "Terminal",
        text          = masked_text,
        weight        = ContextWeight.HIGH,
        quality_score = gate.quality_score,
        failure_flag  = False,
        captured_at   = _now_iso(),
    )

    trigger, score, _ = should_trigger(
        errors          = raw_errors,
        new_commands    = [],
        text_changed    = True,
        window_switched = False,
        uia_failure     = False,
    )

    if not trigger:
        return

    event = _build_agent_event(masked_text, raw_errors, context.context_id, score)
    await _post_agent_event(event)


def _on_new_terminal_output(_text: str) -> None:
    pass


# ── 메인 모니터 루프 ──────────────────────────────────────────────────

async def monitor_loop() -> None:
    """
    UIA 텍스트 추출 → Trigger Detector → Context Gate → AgentEvent 생성.
    EasyOCR / mss 캡처 없음. UIA 실패 시 터미널 패턴으로 대체.
    """
    global _prev_uia_text, _last_active_windows

    INTERVAL = int(os.getenv('MONITOR_INTERVAL', '5'))

    while True:
        try:
            # 1단계: Window Tracker
            win_info = await asyncio.to_thread(get_active_window_info)
            window_title = win_info.get("title", "")

            # OS 스냅샷 (터미널 명령 감지)
            try:
                os_snapshot = await asyncio.to_thread(collect_os_snapshot)
            except Exception:
                os_snapshot = _empty_os_snapshot()

            new_commands    = os_snapshot.get('terminal', {}).get('new_commands', [])
            window_switched = os_snapshot.get('window_changed', False)

            # 2단계: UIA 텍스트 추출
            uia_text, uia_failure = await asyncio.to_thread(extract_uia_text)

            # 텍스트 변화 감지
            text_changed = (uia_text != _prev_uia_text) and bool(uia_text)
            _prev_uia_text = uia_text

            # 에러 패턴 매칭
            raw_errors: list[str] = []
            if uia_text:
                raw_errors = match_patterns([uia_text], ERROR_PATTERNS)

            # 3단계: Trigger Detector
            trigger, score, need_capture = should_trigger(
                errors          = raw_errors,
                new_commands    = new_commands,
                text_changed    = text_changed,
                window_switched = window_switched,
                uia_failure     = uia_failure,
            )

            if not trigger:
                await asyncio.sleep(INTERVAL)
                continue

            # 6단계: Context Gate
            source_text = uia_text or "\n".join(new_commands)
            gate = run_gate(source_text)
            if not gate.passed:
                await asyncio.sleep(INTERVAL)
                continue

            context = ExtractedContext(
                context_id    = uuid.uuid4().hex,
                source        = ContextSource.TERMINAL if uia_failure else ContextSource.UIA,
                app_name      = window_title.split(" - ")[-1] if " - " in window_title else "Unknown",
                window_title  = window_title,
                text          = gate.text,
                weight        = ContextWeight.HIGH,
                quality_score = gate.quality_score,
                failure_flag  = uia_failure,
                captured_at   = _now_iso(),
            )

            event = _build_agent_event(gate.text, raw_errors, context.context_id, score)
            await _post_agent_event(event)
            gc.collect()

        except Exception as e:
            print(f'[monitor_loop] {e}')
            traceback.print_exc()

        await asyncio.sleep(INTERVAL)


def cleanup_old_sessions() -> None:
    if not SESSIONS_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=7)
    for session_dir in SESSIONS_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        if datetime.fromtimestamp(session_dir.stat().st_mtime) < cutoff:
            shutil.rmtree(session_dir, ignore_errors=True)


async def run() -> None:
    global session_index, _last_active_windows

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    init_cmd_count()
    cleanup_old_sessions()

    session_index.clear()
    session_index.update(_create_session_index())

    await asyncio.gather(
        monitor_loop(),
        watch_terminal_output(_on_new_terminal_output, _on_terminal_error_detected),
    )
