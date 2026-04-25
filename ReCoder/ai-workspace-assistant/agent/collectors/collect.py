"""OS 레벨 데이터 수집 모듈 (Windows / macOS 크로스플랫폼)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

try:
    import psutil
except Exception:  # pragma: no cover - 환경 의존성 대응
    psutil = None

try:
    import pyperclip
except Exception:  # pragma: no cover - 환경 의존성 대응
    pyperclip = None

if sys.platform == 'win32':
    try:
        import win32gui
        import win32process
    except Exception:  # pragma: no cover - 비윈도우 환경 대응
        win32gui = None
        win32process = None
else:
    win32gui = None
    win32process = None


SYSTEM_PROCS = {
    name.lower()
    for name in (
        'svchost.exe',
        'dwm.exe',
        'csrss.exe',
        'wininit.exe',
        'services.exe',
        'lsass.exe',
        'explorer.exe',
        'RuntimeBroker.exe',
        'SearchIndexer.exe',
        'WmiPrvSE.exe',
        'conhost.exe',
        'taskhostw.exe',
        'ShellExperienceHost.exe',
        'StartMenuExperienceHost.exe',
        'ctfmon.exe',
        'fontdrvhost.exe',
        'sihost.exe',
        'TextInputHost.exe',
    )
}

MACOS_SYSTEM_APPS = {
    name.lower()
    for name in (
        'Finder',
        'Dock',
        'SystemUIServer',
        'Spotlight',
        'Window Manager',
        'Control Center',
        'Notification Center',
        'loginwindow',
        'universalAccessAuthWarn',
    )
}

last_window_title = ''
last_clipboard_content = ''
last_cmd_count = 0

_accessibility_warning_shown = False

_ZSH_TIMESTAMP_RE = re.compile(r'^: \d+:\d+;(.*)$')


def _warn_accessibility_once() -> None:
    """macOS 접근성 권한 안내를 최초 1회만 출력합니다."""
    global _accessibility_warning_shown
    if _accessibility_warning_shown:
        return
    _accessibility_warning_shown = True
    print(
        '[collect] macOS 접근성 권한이 필요합니다: '
        '시스템 설정 > 개인정보 보호 및 보안 > 접근성에서 '
        '터미널/IDE를 허용해주세요.'
    )


# ──────────────────────────────────────────────
# 창 변경 감지
# ──────────────────────────────────────────────

def _mark_window_changed(current_title: str) -> bool:
    """현재 활성 창/앱 제목을 기준으로 변경 여부를 갱신합니다."""
    global last_window_title
    if current_title != last_window_title:
        last_window_title = current_title
        return True
    return False


def _window_changed_win32() -> bool:
    """Windows: 현재 활성 창 제목이 이전과 달라졌는지 반환합니다."""
    try:
        if win32gui is None:
            return False
        hwnd = win32gui.GetForegroundWindow()
        current_title = win32gui.GetWindowText(hwnd) or ''
        return _mark_window_changed(current_title)
    except Exception:
        return False


def _window_changed_darwin() -> bool:
    try:
        result = subprocess.run(
            ['osascript'],
            input='tell application "System Events" to get name of first process whose frontmost is true',
            capture_output=True, text=True, timeout=3,
        )
        current_title = result.stdout.strip()
        if not current_title:
            if result.returncode != 0:
                print(f'[collect] window_changed osascript 실패 (code={result.returncode}): {result.stderr.strip()}')
                _warn_accessibility_once()
            return False
        return _mark_window_changed(current_title)
    except Exception as e:
        print(f'[collect] osascript 예외: {e}')
        _warn_accessibility_once()
        return False


def window_changed() -> bool:
    """현재 활성 창/앱이 이전과 달라졌는지 반환합니다."""
    # deprecated: collect_os_snapshot()의 window_changed 필드를 사용하세요
    if sys.platform == 'darwin':
        return _window_changed_darwin()
    return _window_changed_win32()


# ──────────────────────────────────────────────
# 포그라운드 프로세스 수집
# ──────────────────────────────────────────────

def _get_foreground_processes_win32() -> list[dict]:
    """Windows: 표시 중인 포그라운드 창의 프로세스/제목 목록을 반환합니다."""
    try:
        if win32gui is None or win32process is None:
            return []
        if psutil is None:
            return []

        windows: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def callback(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True

                title = (win32gui.GetWindowText(hwnd) or '').strip()
                if not title:
                    return True

                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                proc_name = psutil.Process(pid).name()
                if proc_name.lower() in SYSTEM_PROCS:
                    return True

                key = (proc_name, title)
                if key in seen:
                    return True

                seen.add(key)
                windows.append({'name': proc_name, 'title': title})
                return len(windows) < 10
            except Exception:
                return True

        win32gui.EnumWindows(callback, None)
        return windows[:10]
    except Exception:
        return []


def _get_foreground_processes_darwin_payload() -> tuple[list[dict], str]:
    """macOS: 표시 중인 앱 목록과 front_app을 osascript로 가져옵니다."""
    try:
        script = '''
tell application "System Events"
    set visible_names to name of every process whose visible is true
    set front_app to ""
    set front_title to ""
    try
        set front_proc to first process whose frontmost is true
        set front_app to name of front_proc
        try
            set front_title to name of first window of front_proc
        on error
            set front_title to ""
        end try
    end try
end tell

set the text item delimiters to "|||"
set names_str to visible_names as text
set the text item delimiters to ""

return names_str & linefeed & front_app & linefeed & front_title
'''
        result = subprocess.run(
            ['osascript'],
            input=script,
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print(f'[collect] osascript 실패 (code={result.returncode}): {result.stderr.strip()}')
            _warn_accessibility_once()
            return [], ''

        lines = result.stdout.strip().split('\n')
        names_str = lines[0] if len(lines) > 0 else ''
        front_app = lines[1].strip() if len(lines) > 1 else ''
        front_title = lines[2].strip() if len(lines) > 2 else ''

        app_names = [n.strip() for n in names_str.split('|||') if n.strip()]

        windows: list[dict] = []
        seen: set[str] = set()
        for name in app_names:
            if not name or name.lower() in MACOS_SYSTEM_APPS:
                continue
            if name in seen:
                continue
            seen.add(name)
            title = front_title if name == front_app else ''
            windows.append({'name': name, 'title': title})
            if len(windows) >= 10:
                break
        return windows, front_app
    except Exception:
        return [], ''


def _get_foreground_processes_darwin() -> list[dict]:
    """macOS: 표시 중인 앱 목록을 osascript로 가져옵니다."""
    windows, _front_app = _get_foreground_processes_darwin_payload()
    return windows


def get_foreground_processes() -> list[dict]:
    """표시 중인 포그라운드 창/앱의 프로세스 목록을 반환합니다."""
    if sys.platform == 'darwin':
        return _get_foreground_processes_darwin()
    return _get_foreground_processes_win32()


# ──────────────────────────────────────────────
# 터미널 히스토리 수집
# ──────────────────────────────────────────────

def _get_terminal_history_win32() -> dict:
    """Windows: PowerShell 히스토리에서 최근/신규 명령어를 반환합니다."""
    global last_cmd_count
    try:
        history_path = os.path.expanduser(
            '~/AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt'
        )
        if not os.path.exists(history_path):
            return {'recent': [], 'new_commands': []}

        with open(history_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = [line.rstrip('\n') for line in f]

        start_index = min(last_cmd_count, len(lines))
        delta_commands = lines[start_index:]
        new_commands = delta_commands[-10:]
        recent = lines[-10:]
        last_cmd_count = len(lines)
        return {'recent': recent, 'new_commands': new_commands}
    except Exception:
        return {'recent': [], 'new_commands': []}


def _parse_zsh_line(line: str) -> str:
    """zsh 히스토리 라인에서 타임스탬프 접두사를 제거하고 명령어만 반환합니다."""
    match = _ZSH_TIMESTAMP_RE.match(line)
    if match:
        return match.group(1)
    return line


def _get_terminal_history_darwin() -> dict:
    """macOS: zsh/bash 히스토리에서 최근/신규 명령어를 반환합니다."""
    global last_cmd_count
    try:
        history_path = os.path.expanduser('~/.zsh_history')
        if not os.path.exists(history_path):
            history_path = os.path.expanduser('~/.bash_history')
        if not os.path.exists(history_path):
            return {'recent': [], 'new_commands': []}

        with open(history_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw_lines = [line.rstrip('\n') for line in f]

        lines = [_parse_zsh_line(line) for line in raw_lines if line.strip()]

        start_index = min(last_cmd_count, len(lines))
        delta_commands = lines[start_index:]
        new_commands = delta_commands[-10:]
        recent = lines[-10:]
        last_cmd_count = len(lines)
        return {'recent': recent, 'new_commands': new_commands}
    except Exception:
        return {'recent': [], 'new_commands': []}


def get_terminal_history() -> dict:
    """플랫폼에 맞는 터미널 히스토리에서 최근/신규 명령어를 반환합니다."""
    if sys.platform == 'darwin':
        return _get_terminal_history_darwin()
    return _get_terminal_history_win32()


# ──────────────────────────────────────────────
# 클립보드 변경 감지
# ──────────────────────────────────────────────

def _get_clipboard_pbpaste() -> str:
    """macOS: pbpaste를 사용하여 클립보드 텍스트를 가져옵니다."""
    try:
        result = subprocess.run(
            ['pbpaste'], capture_output=True, text=True, timeout=3,
        )
        return result.stdout or ''
    except Exception:
        return ''


def get_clipboard_change() -> dict:
    """클립보드 변경 여부와 텍스트를 반환합니다."""
    global last_clipboard_content
    try:
        if pyperclip is not None:
            current_content = str(pyperclip.paste() or '')
        elif sys.platform == 'darwin':
            current_content = _get_clipboard_pbpaste()
        else:
            return {'changed': False, 'content': ''}

        changed = current_content != last_clipboard_content
        if changed:
            last_clipboard_content = current_content
        return {'changed': changed, 'content': current_content[:200]}
    except Exception:
        return {'changed': False, 'content': ''}


# ──────────────────────────────────────────────
# 초기화 및 스냅샷 수집
# ──────────────────────────────────────────────

def _init_cmd_count_win32() -> None:
    """Windows: PowerShell 히스토리 줄 수로 초기화합니다."""
    global last_cmd_count
    try:
        history_path = os.path.expanduser(
            '~/AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt'
        )
        if not os.path.exists(history_path):
            last_cmd_count = 0
            return
        with open(history_path, 'r', encoding='utf-8', errors='ignore') as f:
            last_cmd_count = sum(1 for _ in f)
    except Exception:
        last_cmd_count = 0


def _init_cmd_count_darwin() -> None:
    """macOS: zsh/bash 히스토리 줄 수로 초기화합니다."""
    global last_cmd_count
    try:
        history_path = os.path.expanduser('~/.zsh_history')
        if not os.path.exists(history_path):
            history_path = os.path.expanduser('~/.bash_history')
        if not os.path.exists(history_path):
            last_cmd_count = 0
            return
        with open(history_path, 'r', encoding='utf-8', errors='ignore') as f:
            last_cmd_count = sum(1 for _ in f)
    except Exception:
        last_cmd_count = 0


def init_cmd_count() -> None:
    """프로그램 시작 시 히스토리 줄 수로 last_cmd_count를 초기화합니다."""
    if sys.platform == 'darwin':
        _init_cmd_count_darwin()
    else:
        _init_cmd_count_win32()


def collect_os_snapshot() -> dict:
    """OS 스냅샷을 단일 딕셔너리로 수집합니다."""
    try:
        if sys.platform == 'darwin':
            foreground_processes, front_app = _get_foreground_processes_darwin_payload()
            changed = _mark_window_changed(front_app) if front_app else False
        else:
            foreground_processes = get_foreground_processes()
            changed = False
            if sys.platform == 'win32' and foreground_processes:
                current_title = str(foreground_processes[0].get('title') or '')
                changed = _mark_window_changed(current_title)

        return {
            'foreground_processes': foreground_processes,
            'terminal': get_terminal_history(),
            'clipboard': get_clipboard_change(),
            'window_changed': changed,
        }
    except Exception:
        return {
            'foreground_processes': [],
            'terminal': {'recent': [], 'new_commands': []},
            'clipboard': {'changed': False, 'content': ''},
            'window_changed': False,
        }
