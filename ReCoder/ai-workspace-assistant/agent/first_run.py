"""첫 실행 설정 창."""

from __future__ import annotations

import getpass
import os
import sys

import requests

try:
    import tkinter as tk
    import tkinter.ttk as ttk
    from tkinter import messagebox

    _HAS_TKINTER = True
except Exception:  # pragma: no cover - macOS 등 tkinter 미설치 환경 대응
    _HAS_TKINTER = False


DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'
DEFAULT_API_WS_URL = 'ws://127.0.0.1:8000'
DEFAULT_TERMINAL_LOG_PATH = '~/.ai_assistant/terminal.log'


def _derive_ws_url(api_base_url: str, existing_ws_url: str) -> str:
    ws_url = existing_ws_url.strip().rstrip('/')
    if ws_url:
        return ws_url

    base = api_base_url.strip().rstrip('/')
    if base.startswith('https://'):
        return f"wss://{base[len('https://'):]}"
    if base.startswith('http://'):
        return f"ws://{base[len('http://'):]}"
    return DEFAULT_API_WS_URL


def _extract_login_credentials(data: dict) -> tuple[str, str]:
    token = str(data.get('access_token') or data.get('token') or '').strip()
    user = data.get('user')
    user_id = ''
    if isinstance(user, dict):
        user_id = str(user.get('user_id') or '').strip()
    if not user_id:
        user_id = str(data.get('user_id') or '').strip()
    return token, user_id


def _terminal_capture_guide() -> str:
    if sys.platform == 'win32':
        return (
            '터미널 출력 캡처를 위해 PowerShell에서 다음 명령어를 실행하세요:\n'
            'Start-Transcript -Path "$env:USERPROFILE\\.ai_assistant\\terminal.log" -Append'
        )
    return (
        '터미널 출력 캡처(권장: 수동 시작)\n'
        '1) 쉘 설정 파일(~/.zshrc 또는 ~/.bashrc)에 아래 함수를 추가\n'
        '   logterm() { mkdir -p "$HOME/.ai_assistant"; script -q -a "$HOME/.ai_assistant/terminal.log"; }\n'
        '2) 필요할 때 logterm 실행\n\n'
        '프로그램 시작 시 자동 캡처를 원하면 .env에 AUTO_START_TERMINAL_CAPTURE=1을 설정하세요.\n'
        '(재귀 실행 방지 가드가 적용됩니다.)'
    )


_SHELL_FUNC_MARKER = '# >>> recoder-logterm >>>'
_SHELL_FUNC_BLOCK = """\
# >>> recoder-logterm >>>
# ReCoder 터미널 로그 캡처 함수 (자동 삽입됨)
logterm() {
  mkdir -p "$HOME/.ai_assistant"
  if [ "$(uname)" = "Darwin" ]; then
    script -q -a "$HOME/.ai_assistant/terminal.log" /bin/zsh
  else
    script -q -a "$HOME/.ai_assistant/terminal.log"
  fi
}
# ReCoder 시작 시 자동 캡처 (원하지 않으면 이 줄을 삭제하세요)
if [ -z "$RECODER_LOGTERM_ACTIVE" ]; then
  export RECODER_LOGTERM_ACTIVE=1
  logterm
fi
# <<< recoder-logterm <<<
"""


def setup_terminal_logging() -> str:
    """
    macOS / Linux에서 터미널 출력을 ~/.ai_assistant/terminal.log로 파이핑하는
    shell 함수(logterm)를 ~/.zshrc 또는 ~/.bashrc에 자동으로 삽입합니다.

    Returns:
        'already_set'  — 이미 설정되어 있음
        'inserted'     — 새로 삽입됨
        'skipped'      — Windows이거나 오류 발생
    """
    if sys.platform == 'win32':
        return 'skipped'

    # 삽입 대상 파일 결정 (zsh 우선)
    shell = os.environ.get('SHELL', '')
    if 'zsh' in shell or os.path.exists(os.path.expanduser('~/.zshrc')):
        rc_path = os.path.expanduser('~/.zshrc')
    else:
        rc_path = os.path.expanduser('~/.bashrc')

    try:
        content = ''
        if os.path.exists(rc_path):
            with open(rc_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

        if _SHELL_FUNC_MARKER in content:
            return 'already_set'

        with open(rc_path, 'a', encoding='utf-8') as f:
            f.write('\n' + _SHELL_FUNC_BLOCK)

        print(f'[ReCoder] 터미널 로그 캡처 함수를 {rc_path}에 추가했습니다.')
        print(f'[ReCoder] 새 터미널 창을 열면 자동으로 로그가 시작됩니다.')
        return 'inserted'
    except Exception as e:
        print(f'[ReCoder] 셸 설정 자동 삽입 실패: {e}')
        return 'skipped'


def _read_existing_env() -> tuple[list[str], dict[str, str]]:
    """기존 .env 파일을 읽어 라인 목록과 키-값 딕셔너리를 반환합니다."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f]

    existing_env: dict[str, str] = {}
    for line in lines:
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        existing_env[k] = v
    return lines, existing_env


def _register(api_base_url: str, email: str, password: str, name: str) -> tuple[bool, str]:
    """회원가입 API 호출. (성공여부, 메시지) 반환."""
    try:
        body: dict[str, str] = {'email': email, 'password': password}
        if name:
            body['name'] = name
        response = requests.post(
            f'{api_base_url.rstrip("/")}/auth/register',
            json=body,
            timeout=10,
        )
        if response.ok:
            return True, '회원가입 성공! 로그인 탭에서 로그인해주세요.'
        if response.status_code == 400:
            return False, '이미 등록된 이메일입니다.'
        return False, f'회원가입 실패 (상태코드 {response.status_code})'
    except requests.RequestException:
        return False, '서버에 연결할 수 없습니다. 서버 주소를 확인해주세요.'


def _login(api_base_url: str, email: str, password: str) -> tuple[bool, str, str, str]:
    """로그인 API 호출. (성공여부, 메시지, token, user_id) 반환."""
    try:
        response = requests.post(
            f'{api_base_url.rstrip("/")}/auth/login',
            json={'email': email, 'password': password},
            timeout=10,
        )
        if response.ok:
            try:
                data = response.json()
            except ValueError:
                return False, '로그인 실패: 서버 응답을 해석하는 데 실패했습니다.', '', ''
            token, user_id = _extract_login_credentials(data)
            if token and user_id:
                return True, '로그인 성공! 서버 연동 모드로 시작합니다.', token, user_id
            return False, '로그인 실패: 서버 응답 형식이 올바르지 않습니다.', '', ''
        return False, f'로그인 실패: 이메일/비밀번호를 확인해주세요. (상태코드 {response.status_code})', '', ''
    except requests.RequestException as e:
        return False, f'서버에 연결할 수 없습니다. 서버 주소를 확인해주세요. ({e})', '', ''


def _save_env_file(lines: list[str], existing_env: dict[str, str], updates: dict[str, str]) -> None:
    """.env 파일에 updates dict의 키-값을 반영하여 저장합니다."""
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        if '=' not in line:
            new_lines.append(line)
            continue
        k, _v = line.split('=', 1)
        if k in updates:
            new_lines.append(f'{k}={updates[k]}')
            seen.add(k)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f'{k}={v}')

    env_path = os.path.join(os.path.dirname(__file__), '.env')
    with open(env_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(new_lines).rstrip() + '\n')


def show_setup_window_cli() -> None:
    """CLI 기반 초기 설정 (tkinter 미설치 환경용)."""
    print('=' * 50)
    print('  AI 업무 어시스턴트 초기 설정 (CLI 모드)')
    print('=' * 50)
    print()

    lines, existing_env = _read_existing_env()
    api_base_url = existing_env.get('API_BASE_URL', DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
    terminal_log_path = (
        existing_env.get('TERMINAL_LOG_PATH', DEFAULT_TERMINAL_LOG_PATH).strip()
        or DEFAULT_TERMINAL_LOG_PATH
    )
    auto_start_capture = (
        existing_env.get('AUTO_START_TERMINAL_CAPTURE', '0').strip() or '0'
    )

    # 서버 주소
    server_input = input(f'서버 주소 (기본값: {api_base_url}): ').strip()
    if server_input:
        api_base_url = server_input
    print()

    print(_terminal_capture_guide())
    terminal_log_input = input(f'터미널 로그 경로 (기본값: {terminal_log_path}): ').strip()
    if terminal_log_input:
        terminal_log_path = terminal_log_input
    auto_capture_default = 'y' if auto_start_capture in {'1', 'true', 'yes', 'on', 'y'} else 'n'
    auto_capture_input = input(
        f'프로그램 시작 시 터미널 로그 자동 캡처 사용? (y/n, 기본값: {auto_capture_default}): '
    ).strip().lower()
    if auto_capture_input in {'y', 'yes', '1', 'on', 'true'}:
        auto_start_capture = '1'
    elif auto_capture_input in {'n', 'no', '0', 'off', 'false'}:
        auto_start_capture = '0'
    print()

    # 1) Gemini API 키
    key = input('Gemini API 키를 입력하세요 (https://aistudio.google.com/app/apikey): ').strip()
    if not key:
        print('오류: GEMINI_API_KEY를 입력해주세요.')
        return
    print()

    token = ''
    user_id = ''

    # 2) 회원가입
    reg_choice = input('회원가입하시겠습니까? (y/n): ').strip().lower()
    if reg_choice == 'y':
        reg_email = input('이메일: ').strip()
        reg_password = getpass.getpass('비밀번호: ').strip()
        reg_name = input('이름 (선택, Enter로 건너뛰기): ').strip()
        success, msg = _register(api_base_url, reg_email, reg_password, reg_name)
        print(msg)
        print()

    # 3) 로그인
    login_choice = input('로그인하시겠습니까? (y/n): ').strip().lower()
    if login_choice == 'y':
        login_email = input('이메일: ').strip()
        login_password = getpass.getpass('비밀번호: ').strip()
        success, msg, token, user_id = _login(api_base_url, login_email, login_password)
        print(msg)
        print()

    # 4) .env 저장
    api_ws_url = _derive_ws_url(api_base_url, existing_env.get('API_WS_URL', ''))
    updates: dict[str, str] = {
        'GEMINI_API_KEY': key,
        'GEMINI_MODEL': 'gemini-3.1-flash-lite-preview',
        'EMBEDDING_MODEL': 'gemini-embedding-001',
        'API_BASE_URL': api_base_url,
        'API_WS_URL': api_ws_url,
        'TERMINAL_LOG_PATH': terminal_log_path,
        'AUTO_START_TERMINAL_CAPTURE': auto_start_capture,
    }
    if token and user_id:
        updates['USER_TOKEN'] = token
        updates['USER_ID'] = user_id

    _save_env_file(lines, existing_env, updates)

    if token and user_id:
        print('.env 설정 저장이 완료되었습니다. (서버 연동 모드)')
    else:
        print('.env 설정 저장이 완료되었습니다. (로컬 전용 모드)')
    print()


def show_setup_window() -> None:
    """초기 설정(회원가입 + 로그인 + GEMINI_API_KEY) 정보를 받아 .env를 생성/갱신합니다."""
    if not _HAS_TKINTER:
        show_setup_window_cli()
        return

    lines, existing_env = _read_existing_env()

    root = tk.Tk()
    root.title('AI 업무 어시스턴트 설정')
    root.geometry('780x780')
    root.resizable(True, True)
    root.minsize(780, 680)

    frame = tk.Frame(root, padx=16, pady=16)
    frame.pack(fill='both', expand=True)

    # ── 상단: 서버 주소 입력 필드 (탭 밖) ──
    server_frame = tk.Frame(frame)
    server_frame.pack(fill='x', pady=(0, 10))
    tk.Label(server_frame, text='서버 주소').pack(side='left')
    server_url_var = tk.StringVar(
        value=existing_env.get('API_BASE_URL', DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
    )
    tk.Entry(server_frame, textvariable=server_url_var, width=58).pack(side='left', padx=(8, 0))

    terminal_log_path_var = tk.StringVar(
        value=existing_env.get('TERMINAL_LOG_PATH', DEFAULT_TERMINAL_LOG_PATH).strip()
        or DEFAULT_TERMINAL_LOG_PATH
    )
    auto_capture_value = existing_env.get('AUTO_START_TERMINAL_CAPTURE', '0').strip().lower()
    auto_start_capture_var = tk.BooleanVar(
        value=auto_capture_value in {'1', 'true', 'yes', 'on', 'y'}
    )
    terminal_frame = tk.LabelFrame(frame, text='터미널 출력 캡처', padx=10, pady=10)
    terminal_frame.pack(fill='x', pady=(0, 12))
    tk.Label(
        terminal_frame,
        text=_terminal_capture_guide(),
        justify='left',
        fg='gray',
        wraplength=560,
    ).pack(anchor='w')

    terminal_path_frame = tk.Frame(terminal_frame)
    terminal_path_frame.pack(fill='x', pady=(8, 0))
    tk.Label(terminal_path_frame, text='TERMINAL_ LOG_PATH').pack(side='left')
    tk.Entry(terminal_path_frame, textvariable=terminal_log_path_var, width=42).pack(
        side='left', padx=(8, 0), fill='x', expand=True
    )
    tk.Checkbutton(
        terminal_frame,
        text='프로그램 시작 시 자동으로 터미널 로그 캡처 시작 (macOS/Linux)',
        variable=auto_start_capture_var,
    ).pack(anchor='w', pady=(8, 0))

    # ── 탭 ──
    notebook = ttk.Notebook(frame)
    notebook.pack(fill='both', expand=True)

    # ── 탭 1: Gemini API 키 ──
    gemini_tab = tk.Frame(notebook, padx=12, pady=12)
    notebook.add(gemini_tab, text='Gemini API 키')

    tk.Label(gemini_tab, text='Gemini API 키').pack(anchor='w')
    key_var = tk.StringVar()
    key_entry = tk.Entry(gemini_tab, textvariable=key_var, width=68, show='*')
    key_entry.pack(fill='x', pady=(6, 8))
    tk.Label(gemini_tab, text='키 발급: https://aistudio.google.com/app/apikey', fg='gray').pack(
        anchor='w'
    )
    key_entry.focus_set()

    # ── 탭 2: 회원가입 ──
    register_tab = tk.Frame(notebook, padx=12, pady=12)
    notebook.add(register_tab, text='회원가입')

    tk.Label(register_tab, text='이메일').pack(anchor='w')
    reg_email_var = tk.StringVar()
    tk.Entry(register_tab, textvariable=reg_email_var, width=68).pack(fill='x', pady=(4, 8))

    tk.Label(register_tab, text='비밀번호').pack(anchor='w')
    reg_password_var = tk.StringVar()
    tk.Entry(register_tab, textvariable=reg_password_var, width=68, show='*').pack(
        fill='x', pady=(4, 8)
    )

    tk.Label(register_tab, text='비밀번호 확인').pack(anchor='w')
    reg_password_confirm_var = tk.StringVar()
    tk.Entry(register_tab, textvariable=reg_password_confirm_var, width=68, show='*').pack(
        fill='x', pady=(4, 8)
    )

    tk.Label(register_tab, text='이름 (선택)').pack(anchor='w')
    reg_name_var = tk.StringVar()
    tk.Entry(register_tab, textvariable=reg_name_var, width=68).pack(fill='x', pady=(4, 8))

    reg_status_var = tk.StringVar()
    reg_status_label = tk.Label(register_tab, textvariable=reg_status_var, fg='gray')
    reg_status_label.pack(anchor='w', pady=(4, 0))

    # ── 탭 3: 로그인 ──
    login_tab = tk.Frame(notebook, padx=12, pady=12)
    notebook.add(login_tab, text='로그인')

    tk.Label(login_tab, text='이메일').pack(anchor='w')
    login_email_var = tk.StringVar()
    tk.Entry(login_tab, textvariable=login_email_var, width=68).pack(fill='x', pady=(4, 8))

    tk.Label(login_tab, text='비밀번호').pack(anchor='w')
    login_password_var = tk.StringVar()
    tk.Entry(login_tab, textvariable=login_password_var, width=68, show='*').pack(
        fill='x', pady=(4, 8)
    )

    login_status_var = tk.StringVar(value='서버 연동은 선택 사항입니다. (미로그인 시 로컬 전용 모드)')
    tk.Label(login_tab, textvariable=login_status_var, fg='gray').pack(anchor='w', pady=(4, 0))

    # 로그인 성공 시 토큰/유저 ID 임시 저장용
    session_data: dict[str, str] = {}

    # ── 회원가입 버튼 핸들러 ──
    def on_register() -> None:
        email = reg_email_var.get().strip()
        password = reg_password_var.get().strip()
        password_confirm = reg_password_confirm_var.get().strip()
        name = reg_name_var.get().strip()
        api_base_url = server_url_var.get().strip()

        if not email or not password:
            reg_status_var.set('이메일과 비밀번호를 입력해주세요.')
            reg_status_label.config(fg='red')
            return

        if password != password_confirm:
            reg_status_var.set('비밀번호가 일치하지 않습니다.')
            reg_status_label.config(fg='red')
            return

        success, msg = _register(api_base_url, email, password, name)
        reg_status_var.set(msg)
        reg_status_label.config(fg='green' if success else 'red')

        if success:
            # 로그인 탭으로 전환하고 이메일 자동 채우기
            login_email_var.set(email)
            notebook.select(login_tab)

    tk.Button(register_tab, text='회원가입', command=on_register).pack(anchor='e', pady=(8, 0))

    # ── 로그인 버튼 핸들러 ──
    def on_login() -> None:
        email = login_email_var.get().strip()
        password = login_password_var.get().strip()
        api_base_url = server_url_var.get().strip()

        if not email or not password:
            login_status_var.set('이메일과 비밀번호를 모두 입력해주세요.')
            return

        success, msg, token, user_id = _login(api_base_url, email, password)
        login_status_var.set(msg)

        if success:
            session_data['USER_TOKEN'] = token
            session_data['USER_ID'] = user_id

    tk.Button(login_tab, text='로그인', command=on_login).pack(anchor='e', pady=(8, 0))

    # ── 하단 저장 버튼 ──
    def on_save() -> None:
        key = key_var.get().strip()
        if not key:
            messagebox.showwarning('입력 필요', 'GEMINI_API_KEY를 입력해주세요.')
            return

        api_base_url = server_url_var.get().strip() or DEFAULT_API_BASE_URL
        api_ws_url = _derive_ws_url(api_base_url, existing_env.get('API_WS_URL', ''))

        updates: dict[str, str] = {
            'GEMINI_API_KEY': key,
            'GEMINI_MODEL': 'gemini-3.1-flash-lite-preview',
            'EMBEDDING_MODEL': 'gemini-embedding-001',
            'API_BASE_URL': api_base_url,
            'API_WS_URL': api_ws_url,
            'TERMINAL_LOG_PATH': terminal_log_path_var.get().strip() or DEFAULT_TERMINAL_LOG_PATH,
            'AUTO_START_TERMINAL_CAPTURE': '1' if auto_start_capture_var.get() else '0',
        }

        # 로그인 탭에서 로그인 성공한 경우 토큰 저장
        if session_data.get('USER_TOKEN') and session_data.get('USER_ID'):
            updates['USER_TOKEN'] = session_data['USER_TOKEN']
            updates['USER_ID'] = session_data['USER_ID']
        elif existing_env.get('USER_TOKEN', '').strip() and existing_env.get('USER_ID', '').strip():
            # 기존 .env에 토큰이 있으면 유지
            updates['USER_TOKEN'] = existing_env['USER_TOKEN']
            updates['USER_ID'] = existing_env['USER_ID']

        _save_env_file(lines, existing_env, updates)

        has_token = bool(updates.get('USER_TOKEN', '').strip())
        if has_token:
            messagebox.showinfo('저장 완료', '.env 설정 저장이 완료되었습니다. (서버 연동 모드)')
        else:
            messagebox.showinfo('저장 완료', 'Gemini 키를 저장했습니다. 로컬 전용 모드로 저장합니다.')
        root.destroy()

    tk.Button(frame, text='저장', command=on_save).pack(anchor='e', pady=(10, 0))
    root.mainloop()
