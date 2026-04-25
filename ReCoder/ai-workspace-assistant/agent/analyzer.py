"""Gemini 기반 컨텍스트 분석 모듈."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from PIL import Image

from collectors.source_context import (
    extract_file_path_from_window_title,
    find_project_root,
    read_source_context,
    resolve_source_file_path,
)

RETRY_DELAY_SECONDS = 30

_gemini_client = None
_gemini_client_lock = threading.Lock()


def _get_gemini_client():
    """Gemini Client를 lazy singleton으로 반환합니다."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    with _gemini_client_lock:
        if _gemini_client is not None:
            return _gemini_client

        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise RuntimeError('GEMINI_API_KEY가 설정되지 않았습니다.')

        from google import genai
        _gemini_client = genai.Client(api_key=api_key)
        return _gemini_client

MAX_ALERT_ERROR_DESC_LENGTH = 50
MAX_ALERT_SOLUTION_LENGTH = 80

_SYSTEM_INSTRUCTION = """당신은 개발자의 화면을 실시간으로 모니터링하는 AI 업무 어시스턴트입니다.
UI 메뉴, 아이콘, 툴바는 무시하고 실제 업무 내용만 파악합니다.
에러가 있으면 구체적인 해결 명령어를 solution에 포함합니다.
command 필드는 반드시 채워야 합니다. 에러가 있으면 해결 명령어를, 에러가 없으면 현재 작업과 관련된 유용한 명령어를 넣으세요.
command 필드에는 터미널에 바로 붙여넣을 수 있는 단일 명령어 또는 명령어 체인만 작성합니다.
설명 문구 없이 순수한 실행 가능한 명령어만 넣어야 합니다. 예: 'pip install flask' 또는 'git status && git add . && git commit -m \"fix\"'
command가 없는 상황이라도 반드시 빈 문자열이 아닌 관련 명령어를 제시하세요."""

_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'current_task': {'type': 'string', 'description': '현재 작업'},
        'summary': {'type': 'string', 'description': '업무 요약'},
        'has_error': {'type': 'boolean', 'description': '에러 존재 여부'},
        'error_description': {'type': 'string', 'description': '에러 내용'},
        'solution': {'type': 'string', 'description': '해결 방법 (자연어 설명)'},
        'command': {'type': 'string', 'description': '터미널에 바로 붙여넣을 실행 가능한 명령어만. 설명 없이 순수 명령어.'},
        'next_action': {'type': 'string', 'description': '다음 할 일'},
        'importance_score': {'type': 'integer', 'description': '중요도 0~100'},
        'voice_briefing': {'type': 'string', 'description': '음성 브리핑 한국어 2문장'},
        'answer': {'type': 'string', 'description': '질문 답변 또는 빈 문자열'},
    },
    'required': ['current_task', 'summary', 'has_error', 'importance_score', 'voice_briefing', 'command', 'error_description'],
}


def _copy_to_clipboard(text: str) -> bool:
    """텍스트를 클립보드에 복사합니다. 성공 시 True 반환."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


# ── 싱글톤 위젯 관리 ──────────────────────────────────────────────────
_widget_instance = None
_widget_lock = threading.Lock()


class _ErrorWidget:
    """
    싱글톤 에러 위젯.
    - 화면 우하단 고정, 드래그로 위치 이동 가능
    - 마지막 위치 ~/.ai_assistant/widget_pos.json 에 저장
    - 에러 누적 & AI와 추가 질문 가능 (채팅 UI)
    """

    _POS_FILE = os.path.expanduser('~/.ai_assistant/widget_pos.json')

    # 색상 팔레트
    BG        = '#0d1117'
    BG_CARD   = '#161b22'
    BG_INPUT  = '#21262d'
    BG_MSG_AI = '#1c2128'
    FG        = '#e6edf3'
    FG_DIM    = '#8b949e'
    ACCENT    = '#58a6ff'
    RED       = '#f85149'
    GREEN     = '#3fb950'
    YELLOW    = '#d29922'
    BORDER    = '#30363d'
    CMD_FG    = '#79c0ff'

    W, H = 400, 560

    def __init__(self, tk_root):
        import tkinter as tk
        from tkinter import scrolledtext

        self._tk = tk
        self._st = scrolledtext
        self._tk_root = tk_root   # 메인 스레드 루트 (Toplevel의 부모)
        self._root = None
        self._chat_log = None
        self._entry = None
        self._status_var = None
        self._current_error: dict = {}
        self._error_count = 0
        self._drag_x = 0
        self._drag_y = 0
        self._build()

    def _load_pos(self):
        try:
            with open(self._POS_FILE, 'r') as f:
                d = json.load(f)
                return d.get('x'), d.get('y')
        except Exception:
            return None, None

    def _save_pos(self):
        try:
            os.makedirs(os.path.dirname(self._POS_FILE), exist_ok=True)
            x = self._root.winfo_x()
            y = self._root.winfo_y()
            with open(self._POS_FILE, 'w') as f:
                json.dump({'x': x, 'y': y}, f)
        except Exception:
            pass

    def _default_pos(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        return sw - self.W - 24, sh - self.H - 60

    def _build(self):
        tk = self._tk
        st = self._st

        root = tk.Toplevel(self._tk_root)   # 메인 스레드 루트 하위에 생성 (스레드 안전)
        self._root = root
        root.title('ReCoder — AI 어시스턴트')
        root.geometry(f'{self.W}x{self.H}')
        root.resizable(False, False)
        root.configure(bg=self.BG)
        root.overrideredirect(True)   # 네이티브 타이틀바 제거
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.97)

        # 위치 복원
        px, py = self._load_pos()
        if px is None:
            px, py = self._default_pos()
        root.geometry(f'{self.W}x{self.H}+{px}+{py}')

        self._root = root

        # ── 커스텀 타이틀바 ──────────────────────────────
        title_bar = tk.Frame(root, bg='#010409', height=36, cursor='fleur')
        title_bar.pack(fill='x')
        title_bar.pack_propagate(False)

        # 좌: 상태 표시등 + 제목
        left_f = tk.Frame(title_bar, bg='#010409')
        left_f.pack(side='left', padx=10, pady=8)
        self._dot = tk.Label(left_f, text='●', font=('맑은 고딕', 9),
                             bg='#010409', fg=self.RED)
        self._dot.pack(side='left', padx=(0, 6))
        tk.Label(left_f, text='ReCoder', font=('맑은 고딕', 10, 'bold'),
                 bg='#010409', fg=self.FG).pack(side='left')

        # 우: 버튼 (최소화 / 닫기)
        right_f = tk.Frame(title_bar, bg='#010409')
        right_f.pack(side='right', padx=8)

        def _minimize():
            root.withdraw()

        tk.Button(right_f, text='─', font=('맑은 고딕', 9),
                  bg='#010409', fg=self.FG_DIM, relief='flat',
                  bd=0, padx=6, cursor='hand2',
                  command=_minimize).pack(side='left')
        tk.Button(right_f, text='✕', font=('맑은 고딕', 9),
                  bg='#010409', fg=self.FG_DIM, relief='flat',
                  bd=0, padx=6, cursor='hand2',
                  command=self._on_close).pack(side='left')

        # 드래그 이동
        title_bar.bind('<ButtonPress-1>',   self._on_drag_start)
        title_bar.bind('<B1-Motion>',       self._on_drag_motion)
        title_bar.bind('<ButtonRelease-1>', lambda e: self._save_pos())

        # ── 에러 요약 헤더 ────────────────────────────────
        self._header_frame = tk.Frame(root, bg=self.BG_CARD,
                                      highlightbackground=self.BORDER,
                                      highlightthickness=1)
        self._header_frame.pack(fill='x', padx=10, pady=(6, 0))

        hrow = tk.Frame(self._header_frame, bg=self.BG_CARD)
        hrow.pack(fill='x', padx=12, pady=(10, 4))

        self._badge = tk.Label(hrow, text='  에러 감지  ',
                               font=('맑은 고딕', 8, 'bold'),
                               bg=self.RED, fg='white')
        self._badge.pack(side='left')

        self._err_count_lbl = tk.Label(hrow, text='',
                                       font=('맑은 고딕', 8),
                                       bg=self.BG_CARD, fg=self.FG_DIM)
        self._err_count_lbl.pack(side='right')

        self._err_title = tk.Label(self._header_frame, text='',
                                   font=('맑은 고딕', 9, 'bold'),
                                   bg=self.BG_CARD, fg=self.FG,
                                   anchor='w', wraplength=360, justify='left')
        self._err_title.pack(fill='x', padx=12, pady=(0, 4))

        # 명령어 박스
        cmd_outer = tk.Frame(self._header_frame, bg=self.BG_INPUT,
                             highlightbackground=self.BORDER,
                             highlightthickness=1)
        cmd_outer.pack(fill='x', padx=12, pady=(0, 4))

        cmd_inner = tk.Frame(cmd_outer, bg=self.BG_INPUT)
        cmd_inner.pack(fill='x', padx=8, pady=6)

        self._cmd_lbl = tk.Label(cmd_inner, text='',
                                 font=('Consolas', 9),
                                 bg=self.BG_INPUT, fg=self.CMD_FG,
                                 anchor='w', wraplength=300, justify='left')
        self._cmd_lbl.pack(side='left', fill='x', expand=True)

        self._status_var = tk.StringVar()
        self._copy_btn = tk.Button(
            cmd_inner, text='복사',
            font=('맑은 고딕', 8, 'bold'),
            bg=self.ACCENT, fg='white', relief='flat',
            padx=8, pady=2, cursor='hand2',
            command=self._on_copy,
        )
        self._copy_btn.pack(side='right', padx=(6, 0))

        self._copy_status = tk.Label(self._header_frame,
                                     textvariable=self._status_var,
                                     font=('맑은 고딕', 8),
                                     bg=self.BG_CARD, fg=self.GREEN,
                                     anchor='w')
        self._copy_status.pack(fill='x', padx=12, pady=(0, 8))

        # ── 채팅 영역 ─────────────────────────────────────
        tk.Frame(root, bg=self.BORDER, height=1).pack(fill='x', padx=10)

        chat_container = tk.Frame(root, bg=self.BG)
        chat_container.pack(fill='both', expand=True, padx=10, pady=(6, 0))

        self._chat_log = st.ScrolledText(
            chat_container,
            font=('맑은 고딕', 9),
            bg=self.BG, fg=self.FG,
            relief='flat', bd=0,
            state='disabled',
            wrap='word',
            insertbackground=self.FG,
        )
        self._chat_log.pack(fill='both', expand=True)
        self._chat_log.tag_config('ai',   foreground=self.ACCENT)
        self._chat_log.tag_config('user', foreground=self.FG_DIM)
        self._chat_log.tag_config('sys',  foreground=self.YELLOW)
        self._chat_log.tag_config('mono', font=('Consolas', 9),
                                  foreground=self.CMD_FG)

        # ── 입력창 ────────────────────────────────────────
        tk.Frame(root, bg=self.BORDER, height=1).pack(fill='x', padx=10)

        input_frame = tk.Frame(root, bg=self.BG_CARD,
                               highlightbackground=self.BORDER,
                               highlightthickness=1)
        input_frame.pack(fill='x', padx=10, pady=8)

        self._entry = tk.Entry(
            input_frame,
            font=('맑은 고딕', 9),
            bg=self.BG_CARD, fg=self.FG,
            relief='flat', bd=0,
            insertbackground=self.FG,
        )
        self._entry.pack(side='left', fill='x', expand=True, padx=(10, 0), ipady=7)
        self._entry.bind('<Return>', self._on_send)
        self._entry.insert(0, '추가 질문을 입력하세요...')
        self._entry.config(fg=self.FG_DIM)
        self._entry.bind('<FocusIn>',  self._on_entry_focus_in)
        self._entry.bind('<FocusOut>', self._on_entry_focus_out)

        send_btn = tk.Button(
            input_frame, text='전송',
            font=('맑은 고딕', 9, 'bold'),
            bg=self.ACCENT, fg='white', relief='flat',
            padx=12, pady=6, cursor='hand2',
            command=self._on_send,
        )
        send_btn.pack(side='right', padx=6, pady=4)

        # 초기 안내 메시지
        self._append_chat('sys', 'ReCoder가 에러를 감지했습니다. 추가 질문을 입력하세요.\n')

        root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── 드래그 ──────────────────────────────────────────
    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_motion(self, event):
        x = self._root.winfo_x() + event.x - self._drag_x
        y = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f'+{x}+{y}')

    # ── 채팅 ────────────────────────────────────────────
    def _append_chat(self, role: str, text: str):
        log = self._chat_log
        log.config(state='normal')
        if role == 'ai':
            log.insert('end', 'AI  ', 'ai')
        elif role == 'user':
            log.insert('end', '나  ', 'user')
        elif role == 'sys':
            log.insert('end', '', 'sys')
        log.insert('end', text + '\n')
        log.config(state='disabled')
        log.see('end')

    def _on_entry_focus_in(self, event):
        if self._entry.get() == '추가 질문을 입력하세요...':
            self._entry.delete(0, 'end')
            self._entry.config(fg=self.FG)

    def _on_entry_focus_out(self, event):
        if not self._entry.get().strip():
            self._entry.insert(0, '추가 질문을 입력하세요...')
            self._entry.config(fg=self.FG_DIM)

    def _on_send(self, event=None):
        question = self._entry.get().strip()
        if not question or question == '추가 질문을 입력하세요...':
            return
        self._entry.delete(0, 'end')
        self._append_chat('user', question)
        self._append_chat('sys', '분석 중...\n')

        def _ask():
            try:
                from collectors.collect import collect_os_snapshot
                os_snap = collect_os_snapshot()
                # 현재 에러 컨텍스트를 포함해서 질문
                ctx_snap = dict(os_snap)
                ctx_snap['detected_errors'] = [self._current_error.get('error_description', '')]
                result = _analyze_with_optional_image(
                    None, ctx_snap,
                    {'events': [], 'error_history': [self._current_error]},
                    user_question=question,
                )
                answer = result.get('answer') or result.get('summary') or '답변을 생성하지 못했습니다.'
                cmd = result.get('command', '')
            except Exception as e:
                answer = f'분석 오류: {e}'
                cmd = ''
            self._root.after(0, lambda: self._on_answer(answer, cmd))

        threading.Thread(target=_ask, daemon=True).start()

    def _on_answer(self, answer: str, cmd: str):
        # 마지막 '분석 중...' 제거
        log = self._chat_log
        log.config(state='normal')
        content = log.get('1.0', 'end')
        if '분석 중...' in content:
            idx = content.rfind('분석 중...')
            line_num = content[:idx].count('\n') + 1
            log.delete(f'{line_num}.0', f'{line_num}.end+1c')
        log.config(state='disabled')

        self._append_chat('ai', answer)
        if cmd:
            self._append_chat('sys', f'명령어: ')
            log.config(state='normal')
            log.insert('end', cmd + '\n', 'mono')
            log.config(state='disabled')
            log.see('end')
            _copy_to_clipboard(cmd)
            if self._status_var:
                self._status_var.set('✅ 새 명령어가 클립보드에 복사되었습니다!')
                self._root.after(3000, lambda: self._status_var.set(''))
            # 헤더 명령어도 업데이트
            self._cmd_lbl.config(text=cmd)
            self._current_error['command'] = cmd

    # ── 에러 업데이트 (새 에러 수신 시 호출) ────────────────
    def update_error(self, error_desc: str, solution: str, command: str):
        self._error_count += 1
        self._current_error = {
            'error_description': error_desc,
            'solution': solution,
            'command': command,
        }

        # 헤더 갱신
        self._err_title.config(text=error_desc[:100] if error_desc else '')
        self._err_count_lbl.config(text=f'#{self._error_count}')
        self._cmd_lbl.config(text=command or solution[:80])

        # 채팅에 새 에러 알림 추가
        self._append_chat('sys', f'\n── 에러 #{self._error_count} 감지 ──\n')
        self._append_chat('ai', f'{solution}\n')
        if command:
            log = self._chat_log
            log.config(state='normal')
            log.insert('end', command + '\n', 'mono')
            log.config(state='disabled')
            log.see('end')

        # 자동 클립보드 복사
        target = command if command else solution
        if target:
            _copy_to_clipboard(target)
            if self._status_var:
                self._status_var.set('✅ 명령어가 클립보드에 자동 복사되었습니다!')
                self._root.after(3000, lambda: self._status_var.set(''))

        # 상태 표시등
        self._dot.config(fg=self.RED)

        # 창 표시 & 최상단
        self._root.deiconify()
        self._root.lift()
        self._root.attributes('-topmost', True)

    def _on_copy(self):
        cmd = self._current_error.get('command') or self._current_error.get('solution', '')
        if _copy_to_clipboard(cmd):
            if self._status_var:
                self._status_var.set('✅ 클립보드에 복사되었습니다!')
                self._root.after(2000, lambda: self._status_var.set(''))

    def _on_close(self):
        self._save_pos()
        self._root.destroy()
        global _widget_instance
        with _widget_lock:
            _widget_instance = None

    def run(self):
        pass  # Toplevel은 부모 루트의 mainloop을 공유하므로 별도 실행 불필요


def _get_or_create_widget(error_desc: str, solution: str, command: str) -> None:
    """싱글톤 위젯을 메인 스레드(tray_app._tk_root)의 after()로 생성/업데이트합니다.
    Windows에서 tkinter는 메인 스레드에서만 Tk()를 생성할 수 있습니다.
    """
    global _widget_instance

    # tray_app의 메인 스레드 tk_root를 가져옴
    try:
        import tray_app
        tk_root = tray_app._tk_root
    except Exception:
        tk_root = None

    if tk_root is None:
        print('[send_alert] tk_root가 없어 위젯을 생성할 수 없습니다.')
        return

    def _create_or_update():
        global _widget_instance
        with _widget_lock:
            if _widget_instance is None:
                try:
                    widget = _ErrorWidget(tk_root)
                    _widget_instance = widget
                except Exception as e:
                    print(f'[send_alert] 위젯 생성 실패: {e}')
                    return
            _widget_instance.update_error(error_desc, solution, command)

    # 메인 스레드의 이벤트 루프에서 실행
    try:
        tk_root.after(0, _create_or_update)
    except Exception as e:
        print(f'[send_alert] after() 호출 실패: {e}')


def send_alert(error_desc: str, solution: str, command: str = '') -> None:
    """싱글톤 에러 위젯에 에러를 전달합니다.
    - 위젯이 없으면 우하단에 새로 생성 (메인 스레드에서)
    - 이미 있으면 기존 위젯에 에러 추가 (창 중복 없음)
    - command(순수 명령어)를 클립보드에 자동 복사
    """
    print(f'[send_alert] 에러 감지: {error_desc[:60]}')

    if sys.platform == 'darwin':
        target = command if command else solution
        if target:
            _copy_to_clipboard(target)
        print(f'[에러 감지] {error_desc}\n해결: {solution}\n명령어: {command}')
        return

    # PyQt6 위젯이 local_server 폴링으로 에러를 표시하므로 별도 위젯 생성 불필요
    try:
        from plyer import notification
        notification.notify(
            title='ReCoder — 에러 감지',
            message=(error_desc or '')[:80],
            app_name='ReCoder',
            timeout=5,
        )
    except Exception:
        pass



def speak(text: str) -> None:
    """gTTS로 한국어 브리핑을 음성 재생합니다 (pygame 불필요)."""
    if not text:
        return
    tmp_path = None
    try:
        import tempfile
        from gtts import gTTS

        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as fp:
            tts = gTTS(text=text, lang='ko')
            tts.save(fp.name)
            tmp_path = fp.name

        if sys.platform == 'darwin':
            subprocess.run(['afplay', tmp_path], check=False)
        elif sys.platform == 'win32':
            # PresentationCore MediaPlayer — MP3 직접 재생 (SoundPlayer는 WAV 전용이라 사용 불가)
            safe_path = tmp_path.replace(os.sep, '/')
            ps_script = (
                'Add-Type -AssemblyName PresentationCore; '
                '$mp = New-Object System.Windows.Media.MediaPlayer; '
                f'$mp.Open([Uri]"file:///{safe_path}"); '
                'Start-Sleep -Milliseconds 500; '
                '$mp.Play(); '
                'Start-Sleep -s 8; '
                '$mp.Stop(); '
                '$mp.Close()'
            )
            subprocess.run(
                ['powershell', '-NonInteractive', '-WindowStyle', 'Hidden', '-c', ps_script],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                capture_output=True,
                encoding='utf-8',
                errors='ignore',
                check=False,
            )
        else:
            # Linux: mpg123 우선, 없으면 ffplay 사용
            for command in (
                ['mpg123', '-q', tmp_path],
                ['ffplay', '-nodisp', '-autoexit', tmp_path],
            ):
                if subprocess.run(
                    ['which', command[0]],
                    capture_output=True,
                    check=False,
                ).returncode == 0:
                    subprocess.run(command, check=False)
                    break
            else:
                print('[speak] Linux 오디오 플레이어를 찾을 수 없습니다. mpg123 또는 ffplay를 설치해주세요.')
    except Exception as e:
        print(f'[speak] 음성 재생 실패: {e}')
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass



def _build_prompt(
    os_snapshot: dict,
    session_index: dict,
    user_question: str | None = None,
    past_sessions: list | None = None,
    source_context: str | None = None,
) -> str:
    foreground = os_snapshot.get('foreground_processes', [])
    new_commands = os_snapshot.get('terminal', {}).get('new_commands', [])
    clipboard = os_snapshot.get('clipboard', {})
    recent_events = session_index.get('events', [])[-5:]

    # ── 인프라 에러 (Docker / Kubernetes) 특별 처리 ──
    infra_source = os_snapshot.get('infra_source')
    infra_error = os_snapshot.get('infra_error')
    if infra_source and infra_error:
        if infra_source == 'docker':
            container = infra_error.get('container', '')
            image = infra_error.get('image', '')
            status = infra_error.get('status', '')
            logs = infra_error.get('logs', '')
            lines = [
                '아래 Docker 에러를 분석하고 JSON으로 해결책을 제시하세요.',
                '',
                '[Docker 에러 정보]',
                f'- 컨테이너: {container}',
                f'- 이미지: {image}',
                f'- 상태: {status}',
                f'- 로그:\n{logs}',
                '',
                '반드시 command 필드에 docker 명령어를 포함하세요.',
                '예: docker restart <container> 또는 docker-compose up -d',
            ]
        else:  # kubernetes
            pod = infra_error.get('pod', '')
            namespace = infra_error.get('namespace', 'default')
            reason = infra_error.get('reason', '')
            restart_count = infra_error.get('restart_count', 0)
            logs = infra_error.get('logs', '')
            lines = [
                '아래 Kubernetes 에러를 분석하고 JSON으로 해결책을 제시하세요.',
                '',
                '[Kubernetes 에러 정보]',
                f'- Pod: {pod}',
                f'- Namespace: {namespace}',
                f'- 원인: {reason}',
                f'- 재시작 횟수: {restart_count}',
                f'- 로그:\n{logs}',
                '',
                '반드시 command 필드에 kubectl 명령어를 포함하세요.',
                '예: kubectl describe pod <pod> -n <namespace>',
            ]
        if user_question:
            lines.extend(['', '[사용자 질문]', user_question])
        return '\n'.join(lines)

    # ── 기존 코드 에러 프롬프트 ──────────────────────────
    lines = [
        '아래 정보를 바탕으로 현재 업무 상황을 JSON으로 분석하세요.',
        '',
        '[OS 스냅샷 요약]',
        f'- 포그라운드 프로세스: {json.dumps(foreground, ensure_ascii=False)}',
        f'- 새 터미널 명령어: {json.dumps(new_commands, ensure_ascii=False)}',
        f'- 클립보드: {json.dumps(clipboard, ensure_ascii=False)}',
        '',
        '[최근 이벤트 5개]',
        json.dumps(recent_events, ensure_ascii=False),
    ]
    if source_context:
        lines.extend(['', '[현재 편집 중인 소스 코드]', source_context])
    if past_sessions:
        lines.extend(['', '[과거 유사 세션]', json.dumps(past_sessions, ensure_ascii=False)])
    if user_question:
        lines.extend(['', '[사용자 질문]', user_question])
    return '\n'.join(lines)


def _call_gemini(image_part: object | None, prompt_text: str, recent_image_part: object | None = None) -> dict:
    from google.genai import types

    client = _get_gemini_client()
    model_name = os.getenv('GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    contents = []
    if image_part is not None:
        contents.append(image_part)
    if recent_image_part is not None:
        contents.append(recent_image_part)
    contents.append(prompt_text)
    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            response_mime_type='application/json',
            response_json_schema=_RESPONSE_SCHEMA,
        ),
    )
    try:
        return json.loads(response.text)
    except Exception as e:
        print(f'[analyze_context] JSON 파싱 실패: {e}')
        return {}


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, 'status_code', None)
    code = getattr(exc, 'code', None)
    if status_code == 429 or code == 429:
        return True
    response = getattr(exc, 'response', None)
    if response is not None and getattr(response, 'status_code', None) == 429:
        return True
    name = type(exc).__name__.lower()
    return 'toomanyrequests' in name or 'ratelimit' in name


def _finalize_analysis_result(result: dict, session_index: dict) -> dict:
    if not result:
        return {}

    session_index['ai_summary'] = result.get('summary')
    session_index['importance_score'] = result.get('importance_score', 0)
    session_index['current_task'] = result.get('current_task')
    session_index['ai_updated_at'] = datetime.now().isoformat()

    if result.get('has_error') and result.get('solution'):
        command = result.get('command', '')
        send_alert(result.get('error_description', ''), result.get('solution', ''), command)
        error_entry = {
            'time': datetime.now().isoformat(),
            'error_description': result.get('error_description', ''),
            'solution': result.get('solution', ''),
            'command': command,
            'current_task': result.get('current_task', ''),
        }
        session_index['last_error'] = error_entry
        # 에러 이력 누적
        if 'error_history' not in session_index:
            session_index['error_history'] = []
        session_index['error_history'].append(error_entry)
        # 최대 50개 유지
        if len(session_index['error_history']) > 50:
            session_index['error_history'] = session_index['error_history'][-50:]

    # 음성 브리핑
    voice_text = str(result.get('voice_briefing') or '').strip()
    if voice_text:
        threading.Thread(target=speak, args=(voice_text,), daemon=True).start()

    return result


def _get_source_context_from_snapshot(
    os_snapshot: dict,
    user_question: str | None = None,
) -> str | None:
    detected_errors = os_snapshot.get('detected_errors') or []
    if not detected_errors and not user_question:
        return None

    foreground = os_snapshot.get('foreground_processes', [])
    for window in foreground:
        title = str(window.get('title') or '').strip()
        if not title:
            continue

        file_hint = extract_file_path_from_window_title(title)
        if not file_hint:
            continue

        try:
            project_root = find_project_root(file_hint, os.getcwd())
            resolved_path = resolve_source_file_path(file_hint, project_root or os.getcwd())
            if not resolved_path:
                continue

            source_context = read_source_context(resolved_path)
            if source_context:
                return f'파일 경로: {resolved_path}\n{source_context}'
        except Exception as e:
            print(f'[analyze_context] 소스 컨텍스트 읽기 실패: {e}')
            continue

    return None


def _analyze_with_optional_image(
    image_part: object | None,
    os_snapshot: dict,
    session_index: dict,
    user_question: str | None = None,
    past_sessions: list | None = None,
    recent_image_part: object | None = None,
) -> dict:
    source_context = _get_source_context_from_snapshot(os_snapshot, user_question)
    prompt_text = _build_prompt(
        os_snapshot,
        session_index,
        user_question,
        past_sessions,
        source_context=source_context,
    )

    try:
        result = _call_gemini(image_part, prompt_text, recent_image_part=recent_image_part)
    except Exception as e:
        if not _is_rate_limit_error(e):
            raise
        print(f'[analyze_context] 429 rate limit 발생, {RETRY_DELAY_SECONDS}초 후 재시도합니다.')
        time.sleep(RETRY_DELAY_SECONDS)
        result = _call_gemini(image_part, prompt_text)

    return _finalize_analysis_result(result, session_index)


def analyze_text_context(
    os_snapshot: dict,
    session_index: dict,
    user_question: str | None = None,
    past_sessions: list | None = None,
) -> dict:
    """이미지 없이 텍스트 컨텍스트만으로 세션 인덱스를 갱신합니다."""
    try:
        return _analyze_with_optional_image(
            None,
            os_snapshot,
            session_index,
            user_question,
            past_sessions,
        )
    except Exception as e:
        print(f'[analyze_context] {e}')
        return {}


def analyze_context(
    screenshot: Image.Image | None,
    os_snapshot: dict,
    session_index: dict,
    user_question: str | None = None,
    past_sessions: list | None = None,
    recent_screenshot: Image.Image | None = None,
) -> dict:
    """스크린샷/OS 컨텍스트를 분석해 세션 인덱스를 갱신합니다.
    recent_screenshot: 최근 전환된 업무 창 이미지 (멀티윈도우 컨텍스트 보완)
    """
    try:
        from google.genai import types

        def _to_image_part(img: Image.Image) -> object:
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return types.Part.from_bytes(data=buf.getvalue(), mime_type='image/png')

        image_part = _to_image_part(screenshot) if screenshot is not None else None
        recent_image_part = _to_image_part(recent_screenshot) if recent_screenshot is not None else None

        return _analyze_with_optional_image(
            image_part,
            os_snapshot,
            session_index,
            user_question,
            past_sessions,
            recent_image_part=recent_image_part,
        )
    except Exception as e:
        print(f'[analyze_context] {e}')
        return {}