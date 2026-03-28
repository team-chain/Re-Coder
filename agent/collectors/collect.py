# collectors/collect.py
# OS 레벨 수집 모듈: 포그라운드 프로세스, 터미널 히스토리(delta), 클립보드, 창 전환 감지
#
# ⚠ 주의: 프로그램 시작 시 반드시 init_cmd_count()를 호출해야 합니다.
# 호출하지 않으면 PowerShell 히스토리 전체가 "새 명령어"로 잡혀 Gemini 프롬프트가 폭발합니다.

from pathlib import Path

import psutil
import pyperclip
import win32gui
import win32process

# 시스템 프로세스 (필터 제외 대상)
SYSTEM_PROCS = {
    'svchost.exe', 'dwm.exe', 'csrss.exe', 'wininit.exe',
    'services.exe', 'lsass.exe', 'explorer.exe', 'RuntimeBroker.exe',
    'SearchIndexer.exe', 'WmiPrvSE.exe', 'conhost.exe', 'taskhostw.exe',
}

# 전역 상태
last_window_title: str = ""
last_cmd_count: int = 0
last_clipboard: str = ""

# PowerShell 히스토리 경로
_PS_HISTORY_PATH = (
    Path.home()
    / 'AppData/Roaming/Microsoft/Windows'
    / 'PowerShell/PSReadLine/ConsoleHost_history.txt'
)


def window_changed() -> bool:
    """활성 창 변화 감지 — win32gui 사용."""
    global last_window_title
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        if title and title != last_window_title:
            last_window_title = title
            return True
    except Exception:
        pass
    return False


def get_foreground_processes() -> list[dict]:
    """현재 표시 중인 창 기반 포그라운드 프로세스 목록 (시스템 프로세스 제외, 최대 10개)."""
    result = []

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                name = psutil.Process(pid).name()
                if name not in SYSTEM_PROCS:
                    result.append({
                        'name': name,
                        'title': win32gui.GetWindowText(hwnd),
                        'pid': pid,
                    })
            except Exception:
                pass

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass

    # 중복 제거 (프로세스명 기준)
    seen, unique = set(), []
    for p in result:
        if p['name'] not in seen:
            seen.add(p['name'])
            unique.append(p)

    return unique[:10]


def get_terminal_history() -> dict:
    """PowerShell 히스토리 delta 방식 — 새 명령어만 추출 (최대 10개).
    
    ▶ 설계 근거: 전체 히스토리를 매번 보내면 Gemini 프롬프트가 폭발합니다.
    last_cmd_count 기준으로 새로 추가된 줄만 추출합니다.
    """
    global last_cmd_count

    if not _PS_HISTORY_PATH.exists():
        return {'recent': [], 'new_commands': []}

    try:
        lines = [
            l.strip()
            for l in _PS_HISTORY_PATH.read_text('utf-8', errors='ignore').split('\n')
            if l.strip()
        ]
        new = lines[last_cmd_count:][-10:]   # 새 명령어, 최대 10개
        last_cmd_count = len(lines)
        return {'recent': lines[-10:], 'new_commands': new}
    except Exception:
        return {'recent': [], 'new_commands': []}


def get_clipboard_change() -> dict:
    """클립보드 변화 감지 — 변경된 경우에만 내용 반환 (최대 200자)."""
    global last_clipboard
    try:
        current = pyperclip.paste()
        if current and current != last_clipboard:
            last_clipboard = current
            return {'changed': True, 'content': current[:200]}
    except Exception:
        pass
    return {'changed': False, 'content': ''}


def init_cmd_count():
    """프로그램 시작 시 현재 PowerShell 히스토리 줄 수로 초기화.
    
    ⚠ 반드시 monitor.py의 run() 함수 맨 위에서 호출해야 합니다.
    호출하지 않으면 기존 히스토리 전체가 "새 명령어"로 잡힙니다.
    """
    global last_cmd_count
    if _PS_HISTORY_PATH.exists():
        try:
            lines = [
                l.strip()
                for l in _PS_HISTORY_PATH.read_text('utf-8', errors='ignore').split('\n')
                if l.strip()
            ]
            last_cmd_count = len(lines)
        except Exception:
            pass


def collect_os_snapshot() -> dict:
    """OS 레벨 전체 스냅샷 수집 — monitor.py의 capture_loop()에서 호출."""
    return {
        'foreground_processes': get_foreground_processes(),
        'terminal': get_terminal_history(),
        'clipboard': get_clipboard_change(),
    }
