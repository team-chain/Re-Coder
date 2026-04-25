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
from trigger_detector import should_trigger, notify_resolved
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

# UIA에서 감지한 최근 에러를 추적 — 터미널과 중복 트리거 방지
_uia_recent_error_fps: set[str] = set()

_server_event_queue: asyncio.Queue | None = None   # server.py가 등록

# ── 개발 도구 앱 허용 목록 ────────────────────────────────────────────
# 환경변수 RECODER_ALLOWED_APPS 에 쉼표로 추가하면 목록 확장 가능
# 예: export RECODER_ALLOWED_APPS="Slack,Notion"
_DEFAULT_DEV_APPS: frozenset[str] = frozenset({
    # 에디터 / IDE
    'code', 'visual studio code', 'vscodium',
    'pycharm', 'intellij idea', 'webstorm', 'goland', 'clion', 'rubymine',
    'xcode', 'android studio',
    'sublime text', 'atom', 'zed',
    'vim', 'nvim', 'neovim', 'emacs',
    'cursor',
    # 터미널
    'terminal', 'iterm', 'iterm2', 'alacritty', 'kitty', 'warp',
    'hyper', 'tabby', 'wezterm',
    # 브라우저 (개발자 도구가 열려 있는 경우 대응)
    'chrome', 'google chrome', 'chromium',
    'firefox', 'safari',
    'arc',
    # 기타 개발 도구
    'docker desktop', 'postman', 'insomnia',
    'tableplus', 'dbeaver', 'sequel pro',
    'github desktop', 'sourcetree', 'fork',
    'python', 'node', 'npm',
})

def _get_allowed_apps() -> frozenset[str]:
    """기본 목록 + 환경변수 RECODER_ALLOWED_APPS 병합."""
    extra_raw = os.getenv('RECODER_ALLOWED_APPS', '')
    extra = frozenset(a.strip().lower() for a in extra_raw.split(',') if a.strip())
    return _DEFAULT_DEV_APPS | extra


def is_dev_app(app_name: str) -> bool:
    """앱 이름이 개발 도구 허용 목록에 포함되는지 확인 (부분 일치)."""
    if not app_name:
        return False
    lower = app_name.lower()
    return any(allowed in lower for allowed in _get_allowed_apps())


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

def _extract_uia_text_windows() -> tuple[str, bool]:
    """Windows UI Automation으로 포그라운드 창 텍스트 추출."""
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

    except Exception:
        return "", True


# macOS AX API 권한 요청 — 최초 1회만 프롬프트
_ax_permission_prompted = False

def _request_ax_permission_macos() -> bool:
    """
    접근성 권한이 없으면 macOS 시스템 설정 창을 자동으로 열어 권한을 요청한다.
    Returns True if trusted after prompt.
    """
    try:
        import ApplicationServices as AX
        # kAXTrustedCheckOptionPrompt=True → 권한 없으면 시스템 설정을 자동으로 열어줌
        trusted = AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})
        return bool(trusted)
    except Exception:
        return False


def _extract_ax_text_macos() -> tuple[str, bool]:
    """
    macOS Accessibility API (AXUIElement)로 포그라운드 앱 텍스트 추출.
    Windows UIA의 macOS 대응 구현.

    권한이 없으면 macOS 시스템 설정 창을 자동으로 열어 요청합니다.
    """
    global _ax_permission_prompted

    try:
        import ApplicationServices as AX
        from AppKit import NSWorkspace
    except ImportError:
        return "", True  # 패키지 미설치

    try:
        # 권한 확인 — 없으면 최초 1회 시스템 설정 자동 오픈
        if not AX.AXIsProcessTrusted():
            if not _ax_permission_prompted:
                _ax_permission_prompted = True
                print('[ReCoder] 접근성 권한을 요청합니다. 시스템 설정 창이 열립니다.')
                _request_ax_permission_macos()
            return "", True

        # 포그라운드 앱 PID
        ws  = NSWorkspace.sharedWorkspace()
        pid = ws.frontmostApplication().processIdentifier()
        app_el = AX.AXUIElementCreateApplication(pid)

        texts: list[str] = []
        seen:  set[str]  = set()
        _MAX_DEPTH    = 8
        _MAX_CHILDREN = 40
        _MAX_TEXTS    = 300

        def _walk(el, depth: int) -> None:
            if depth > _MAX_DEPTH or len(texts) >= _MAX_TEXTS:
                return

            for attr in (AX.kAXValueAttribute, AX.kAXTitleAttribute):
                err, val = AX.AXUIElementCopyAttributeValue(el, attr, None)
                if err == 0 and isinstance(val, str):
                    v = val.strip()
                    if v and v not in seen:
                        seen.add(v)
                        texts.append(v)

            err, children = AX.AXUIElementCopyAttributeValue(
                el, AX.kAXChildrenAttribute, None
            )
            if err == 0 and children:
                for child in list(children)[:_MAX_CHILDREN]:
                    _walk(child, depth + 1)

        _walk(app_el, 0)
        combined = "\n".join(texts)
        if len(combined) < 20:
            return combined, True
        return combined, False

    except Exception as e:
        print(f'[extract_ax_text] {e}')
        return "", True


def extract_uia_text() -> tuple[str, bool]:
    """
    플랫폼별 UI 텍스트 추출.
      - Windows : UI Automation (comtypes)
      - macOS   : Accessibility API (pyobjc-framework-ApplicationServices)
      - 기타    : ("", True) 반환
    Returns: (text, failure_flag)
    """
    if sys.platform == 'win32':
        return _extract_uia_text_windows()
    if sys.platform == 'darwin':
        return _extract_ax_text_macos()
    return "", True


# ── Window Tracker (1단계) ────────────────────────────────────────────

def get_active_window_info() -> dict:
    """현재 포그라운드 창 정보 반환.

    반환 형식: {"title": "파일명 - 앱 이름", "app": "앱 이름"}
    - Windows: win32gui.GetForegroundWindow() + GetWindowText()
    - macOS:   AXUIElement kAXTitleAttribute → osascript 폴백
    """
    if sys.platform == 'win32':
        try:
            import win32gui
            hwnd  = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            return {"title": title, "hwnd": hwnd}
        except Exception:
            pass

    elif sys.platform == 'darwin':
        # ── 1차 시도: AXUIElement (접근성 허용 시 전체 창 제목 반환) ──────
        try:
            from ApplicationServices import (
                AXUIElementCreateSystemWide,
                AXUIElementCopyAttributeValue,
                AXUIElementCreateApplication,
                kAXFocusedApplicationAttribute,
                kAXFocusedWindowAttribute,
                kAXTitleAttribute,
            )
            import AppKit

            # 포그라운드 앱 PID 가져오기
            ws  = AppKit.NSWorkspace.sharedWorkspace()
            app = ws.frontmostApplication()
            if app is not None:
                pid        = app.processIdentifier()
                app_name   = app.localizedName() or ""
                ax_app     = AXUIElementCreateApplication(pid)

                # 포커스된 창의 kAXTitle 읽기
                err, win = AXUIElementCopyAttributeValue(
                    ax_app, kAXFocusedWindowAttribute, None
                )
                if err == 0 and win is not None:
                    err2, title_val = AXUIElementCopyAttributeValue(
                        win, kAXTitleAttribute, None
                    )
                    if err2 == 0 and title_val:
                        title = str(title_val)
                        # 앱 이름이 빠진 경우 "파일명 — 앱이름" 형태로 보완
                        if app_name and app_name.lower() not in title.lower():
                            title = f"{title} — {app_name}"
                        return {"title": title, "app": app_name}

                # 창 제목을 못 읽었으면 앱 이름만 반환
                if app_name:
                    return {"title": app_name, "app": app_name}
        except Exception:
            pass

        # ── 2차 시도: osascript 폴백 (접근성 없어도 앱 이름은 가져옴) ───
        try:
            import subprocess
            # 앱 이름 + 현재 문서 이름을 동시에 가져오는 AppleScript
            script = (
                'tell application "System Events"\n'
                '  set frontApp to first process whose frontmost is true\n'
                '  set appName to name of frontApp\n'
                '  set winTitle to ""\n'
                '  try\n'
                '    set winTitle to name of front window of frontApp\n'
                '  end try\n'
                '  if winTitle is "" then\n'
                '    return appName\n'
                '  else\n'
                '    return winTitle & " — " & appName\n'
                '  end if\n'
                'end tell'
            )
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=3,
            )
            title = result.stdout.strip()
            if title:
                # "파일명 — 앱이름" 에서 앱이름 분리
                app_name = title.split(' — ')[-1] if ' — ' in title else title
                return {"title": title, "app": app_name}
        except Exception:
            pass

    return {"title": "", "app": ""}


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


async def _post_resolved_event(raw_errors: list[str]) -> None:
    """에러 해결 이벤트를 session_index에 기록하고 큐에 전달."""
    event_entry = {
        'type':       EventType.RESOLVED.value if hasattr(EventType, 'RESOLVED') else 'resolved',
        'time':       _now_iso(),
        'event_id':   uuid.uuid4().hex,
        'summary':    '에러가 해결되었습니다.',
        'raw_errors': raw_errors,
        'score':      0,
    }
    session_index['events'].append(event_entry)
    session_index['resolved'] = True
    session_index['orchestrator_state'] = OrchestratorState.IDLE.value
    await asyncio.to_thread(_write_session_index)


# ── 터미널 에러 핸들러 ────────────────────────────────────────────────

async def _on_terminal_error_detected(output: str, raw_errors: list[str]) -> None:
    gate = run_gate(output)
    masked_text = gate.text if gate.text else output

    # UIA 루프에서 이미 동일 에러를 처리한 경우 중복 트리거 방지
    from trigger_detector import _error_fingerprint as _fp
    fp = _fp(raw_errors)
    if fp in _uia_recent_error_fps:
        return

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


async def _on_terminal_resolved(output: str, prev_errors: list[str]) -> None:
    """터미널에서 해결 패턴 감지 시 쿨다운을 리셋하고 resolved 이벤트를 기록."""
    print(f'[monitor] 터미널 에러 해결 감지: {prev_errors[:3]}')
    notify_resolved(prev_errors)
    await _post_resolved_event(prev_errors)


def _on_new_terminal_output(text: str) -> None:
    """새 터미널 출력 — RESOLVE_PATTERNS 확인은 watch_terminal_output에서 처리."""
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
            # app 키 우선: AXUIElement/osascript에서 분리된 순수 앱 이름
            # 없으면 window_title 전체로 폴백 (Windows 동작과 동일)
            app_name = win_info.get("app", "") or window_title

            # OS 스냅샷 (터미널 명령 감지)
            try:
                os_snapshot = await asyncio.to_thread(collect_os_snapshot)
            except Exception:
                os_snapshot = _empty_os_snapshot()

            new_commands    = os_snapshot.get('terminal', {}).get('new_commands', [])
            window_switched = os_snapshot.get('window_changed', False)

            # 개발 도구 앱 여부 확인 — 카톡·메일 등 무관한 앱은 건너뜀
            # app_name(순수 앱 이름)으로 먼저 확인, 폴백으로 window_title 전체 사용
            if not is_dev_app(app_name) and not is_dev_app(window_title):
                await asyncio.sleep(INTERVAL)
                continue

            # 자기 자신(ReCoder 대시보드) 모니터링 방지 — 무한 루프 차단
            # 대시보드가 Safari/Chrome에 열려 있을 때 AX가 대시보드 텍스트를 읽어
            # "에러 감지됨"을 다시 트리거하는 재귀 현상을 방지한다.
            _SELF_MARKERS = (
                '127.0.0.1:17894',   # 브라우저 대시보드
                'recoder dashboard',  # Safari 탭 제목
                'recoder - dashboard',
                'recoder widget',     # Qt 트레이 위젯 (FramelessWindowHint여도 AX가 읽음)
            )
            if any(m in window_title.lower() for m in _SELF_MARKERS):
                await asyncio.sleep(INTERVAL)
                continue

            # 2단계: UIA 텍스트 추출
            uia_text, uia_failure = await asyncio.to_thread(extract_uia_text)

            # 텍스트 변화 감지
            text_changed = (uia_text != _prev_uia_text) and bool(uia_text)
            _prev_uia_text = uia_text

            # 에러 패턴 매칭
            raw_errors: list[str] = []
            if uia_text:
                raw_errors = match_patterns([uia_text], ERROR_PATTERNS)

            # UIA 에러 fingerprint 갱신 (터미널 중복 감지 방지용)
            from trigger_detector import _error_fingerprint as _fp
            if raw_errors:
                _uia_recent_error_fps.add(_fp(raw_errors))
                # 세트가 너무 커지지 않도록 오래된 항목 제거 (최근 20개만 유지)
                if len(_uia_recent_error_fps) > 20:
                    oldest = next(iter(_uia_recent_error_fps))
                    _uia_recent_error_fps.discard(oldest)
            else:
                # 에러가 사라졌으면 UIA 해결 처리
                if _uia_recent_error_fps:
                    notify_resolved(list(_uia_recent_error_fps))
                    await _post_resolved_event(list(_uia_recent_error_fps))
                    _uia_recent_error_fps.clear()

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
        watch_terminal_output(
            _on_new_terminal_output,
            _on_terminal_error_detected,
            on_resolved=_on_terminal_resolved,
        ),
    )
