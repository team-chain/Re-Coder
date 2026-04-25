"""tkinter 기반 GUI 윈도우 모듈."""

from __future__ import annotations

import sys
import threading
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import scrolledtext

    _HAS_TKINTER = True
except Exception:
    _HAS_TKINTER = False

# ── 플랫폼별 폰트 ──
if sys.platform == 'darwin':
    _FONT_FAMILY = 'Apple SD Gothic Neo'
else:
    _FONT_FAMILY = '맑은 고딕'

FONT_TITLE = (_FONT_FAMILY, 14, 'bold')
FONT_BODY = (_FONT_FAMILY, 10)
FONT_SMALL = (_FONT_FAMILY, 9)
FONT_MONO = ('Courier', 10)

# ── 다크 테마 색상 ──
BG = '#1a1a2e'
BG_CARD = '#16213e'
BG_INPUT = '#0f3460'
FG = '#e0e0e0'
FG_DIM = '#8888aa'
ERR_RED = '#ff4444'
OK_GREEN = '#00cc66'
ACCENT_BLUE = '#4a90d9'
BORDER = '#2a2a4a'


def _configure_dark(widget: tk.Misc) -> None:
    """위젯에 다크 테마 색상을 적용합니다."""
    try:
        widget.configure(bg=BG, fg=FG)
    except tk.TclError:
        try:
            widget.configure(bg=BG)
        except tk.TclError:
            pass


# =====================================================================
#  StatusWindow — 메인 대시보드
# =====================================================================
class StatusWindow:
    """세션 상태, AI 요약, 최근 이벤트를 표시하는 대시보드 윈도우."""

    def __init__(self, session_index_ref: dict) -> None:
        if not _HAS_TKINTER:
            return
        self._session = session_index_ref
        self._win: tk.Toplevel | None = None
        self._prompt_btn: tk.Button | None = None
        self._last_error: dict | None = None

    # ── 윈도우 표시 ──
    def show(self) -> None:
        if not _HAS_TKINTER:
            return
        if self._win is not None:
            try:
                self._win.lift()
                return
            except tk.TclError:
                self._win = None

        self._win = tk.Toplevel()
        self._win.title('AI 업무 어시스턴트 — 대시보드')
        self._win.geometry('700x500')
        self._win.resizable(False, False)
        self._win.configure(bg=BG)
        self._win.protocol('WM_DELETE_WINDOW', self._on_close)

        # ── 상단: 정보 카드 ──
        card = tk.Frame(self._win, bg=BG_CARD, padx=14, pady=10)
        card.pack(fill='x', padx=12, pady=(12, 6))

        self._lbl_session = tk.Label(card, text='세션: —', font=FONT_SMALL, bg=BG_CARD, fg=FG_DIM, anchor='w')
        self._lbl_session.pack(fill='x')

        row = tk.Frame(card, bg=BG_CARD)
        row.pack(fill='x', pady=(4, 0))
        self._lbl_task = tk.Label(row, text='작업: —', font=FONT_BODY, bg=BG_CARD, fg=FG, anchor='w')
        self._lbl_task.pack(side='left', fill='x', expand=True)
        self._lbl_score = tk.Label(row, text='중요도: 0', font=FONT_BODY, bg=BG_CARD, fg=ACCENT_BLUE, anchor='e')
        self._lbl_score.pack(side='right')

        row2 = tk.Frame(card, bg=BG_CARD)
        row2.pack(fill='x', pady=(4, 0))
        self._lbl_start = tk.Label(row2, text='시작: —', font=FONT_SMALL, bg=BG_CARD, fg=FG_DIM, anchor='w')
        self._lbl_start.pack(side='left', fill='x', expand=True)
        self._lbl_server = tk.Label(row2, text='● 서버 연결 안됨', font=FONT_SMALL, bg=BG_CARD, fg=ERR_RED, anchor='e')
        self._lbl_server.pack(side='right')

        # ── 중단: AI 요약 ──
        tk.Label(self._win, text='AI 요약', font=FONT_TITLE, bg=BG, fg=ACCENT_BLUE, anchor='w').pack(
            fill='x', padx=14, pady=(10, 2)
        )
        self._txt_summary = scrolledtext.ScrolledText(
            self._win, height=8, wrap='word', font=FONT_BODY,
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief='flat',
            state='disabled', borderwidth=0, highlightthickness=1, highlightbackground=BORDER,
        )
        self._txt_summary.pack(fill='x', padx=14, pady=(0, 6))

        # ── 하단: 최근 이벤트 ──
        tk.Label(self._win, text='최근 이벤트', font=FONT_TITLE, bg=BG, fg=ACCENT_BLUE, anchor='w').pack(
            fill='x', padx=14, pady=(4, 2)
        )
        self._txt_events = scrolledtext.ScrolledText(
            self._win, height=6, wrap='word', font=FONT_SMALL,
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief='flat',
            state='disabled', borderwidth=0, highlightthickness=1, highlightbackground=BORDER,
        )
        self._txt_events.pack(fill='x', padx=14, pady=(0, 6))

        # ── 프롬프트 복사 버튼 (에러 감지 시 활성화) ──
        btn_frame = tk.Frame(self._win, bg=BG)
        btn_frame.pack(fill='x', padx=14, pady=(0, 10))
        self._prompt_btn = tk.Button(
            btn_frame, text='🔧 프롬프트 생성', font=FONT_BODY,
            bg=ACCENT_BLUE, fg='white', activebackground='#3a7cc9', activeforeground='white',
            relief='flat', state='disabled', command=self._open_prompt_window,
        )
        self._prompt_btn.pack(side='right')

        self._refresh()

    # ── 자동 새로고침 (5초) ──
    def _refresh(self) -> None:
        if self._win is None:
            return
        try:
            self._update_labels()
            self._win.after(5000, self._refresh)
        except tk.TclError:
            self._win = None

    def _update_labels(self) -> None:
        s = self._session
        sid = s.get('session_id', '—')
        self._lbl_session.config(text=f"세션: {sid[:16]}...")
        self._lbl_task.config(text=f"작업: {s.get('current_task') or '—'}")
        self._lbl_score.config(text=f"중요도: {s.get('importance_score', 0)}")
        self._lbl_start.config(text=f"시작: {s.get('start_time', '—')}")

        import os
        if os.getenv('USER_TOKEN', '').strip():
            self._lbl_server.config(text='● 서버 연결됨', fg=OK_GREEN)
        else:
            self._lbl_server.config(text='● 로컬 전용', fg=FG_DIM)

        # AI 요약
        summary = s.get('ai_summary') or '아직 분석 결과가 없습니다.'
        self._txt_summary.config(state='normal')
        self._txt_summary.delete('1.0', 'end')
        self._txt_summary.insert('1.0', summary)
        self._txt_summary.config(state='disabled')

        # 최근 이벤트 5개
        events = s.get('events', [])[-5:]
        lines: list[str] = []
        for ev in reversed(events):
            t = ev.get('time', '')
            etype = ev.get('type', '')
            detail = ''
            if etype in {'error_detected', 'terminal_error'}:
                detail = ', '.join(ev.get('errors', []))
            elif etype == 'terminal_commands':
                cmds = ev.get('commands', [])
                detail = '; '.join(c[:60] for c in cmds[:3])
            elif etype == 'resolved':
                detail = '에러 해결됨'
            lines.append(f"[{t}] {etype}: {detail}")

        self._txt_events.config(state='normal')
        self._txt_events.delete('1.0', 'end')
        self._txt_events.insert('1.0', '\n'.join(lines) if lines else '이벤트가 없습니다.')
        self._txt_events.config(state='disabled')

        # 에러 감지 시 프롬프트 버튼 활성화
        last_error = s.get('last_error')
        if last_error and self._prompt_btn:
            self._last_error = last_error
            self._prompt_btn.config(state='normal')

    def _open_prompt_window(self) -> None:
        if self._last_error:
            PromptWindow(self._session, self._last_error)

    def _on_close(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None

    # ── 에러 콜백으로 자동 팝업 ──
    def on_error_detected(self, result: dict) -> None:
        """monitor.py의 에러 콜백에서 호출됩니다."""
        try:
            self.show()
            if self._win:
                self._win.lift()
        except Exception:
            pass


# =====================================================================
#  ErrorHistoryWindow — 에러 이력
# =====================================================================
class ErrorHistoryWindow:
    """에러 이벤트 이력을 표시하고, 클릭 시 상세 정보와 프롬프트 생성을 제공합니다."""

    def __init__(self, session_index_ref: dict) -> None:
        if not _HAS_TKINTER:
            return
        self._session = session_index_ref
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if not _HAS_TKINTER:
            return
        if self._win is not None:
            try:
                self._win.lift()
                return
            except tk.TclError:
                self._win = None

        self._win = tk.Toplevel()
        self._win.title('AI 업무 어시스턴트 — 에러 이력')
        self._win.geometry('600x450')
        self._win.resizable(False, False)
        self._win.configure(bg=BG)
        self._win.protocol('WM_DELETE_WINDOW', self._on_close)

        # ── 에러 목록 (좌측) ──
        pane = tk.PanedWindow(self._win, orient='horizontal', bg=BG, sashwidth=4, sashrelief='flat')
        pane.pack(fill='both', expand=True, padx=10, pady=10)

        left = tk.Frame(pane, bg=BG)
        pane.add(left, width=240)

        tk.Label(left, text='에러 목록', font=FONT_TITLE, bg=BG, fg=ACCENT_BLUE, anchor='w').pack(
            fill='x', pady=(0, 6)
        )

        list_frame = tk.Frame(left, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        list_frame.pack(fill='both', expand=True)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side='right', fill='y')

        self._listbox = tk.Listbox(
            list_frame, font=FONT_SMALL, bg=BG_INPUT, fg=FG,
            selectbackground=ACCENT_BLUE, selectforeground='white',
            relief='flat', borderwidth=0, yscrollcommand=scrollbar.set,
            activestyle='none',
        )
        self._listbox.pack(fill='both', expand=True)
        scrollbar.config(command=self._listbox.yview)
        self._listbox.bind('<<ListboxSelect>>', self._on_select)

        # ── 상세 패널 (우측) ──
        right = tk.Frame(pane, bg=BG)
        pane.add(right)

        tk.Label(right, text='상세 정보', font=FONT_TITLE, bg=BG, fg=ACCENT_BLUE, anchor='w').pack(
            fill='x', pady=(0, 6)
        )
        self._txt_detail = scrolledtext.ScrolledText(
            right, wrap='word', font=FONT_BODY,
            bg=BG_INPUT, fg=FG, relief='flat', state='disabled',
            borderwidth=0, highlightthickness=1, highlightbackground=BORDER,
        )
        self._txt_detail.pack(fill='both', expand=True, pady=(0, 6))

        self._btn_prompt = tk.Button(
            right, text='🔧 해결 프롬프트 생성', font=FONT_BODY,
            bg=ACCENT_BLUE, fg='white', activebackground='#3a7cc9', activeforeground='white',
            relief='flat', state='disabled', command=self._open_prompt,
        )
        self._btn_prompt.pack(anchor='e')

        self._error_events: list[dict] = []
        self._populate()

    def _populate(self) -> None:
        events = self._session.get('events', [])
        self._error_events = [
            e for e in events if e.get('type') in {'error_detected', 'terminal_error'}
        ]
        resolved_times = {
            e.get('time') for e in events if e.get('type') == 'resolved'
        }
        self._listbox.delete(0, 'end')
        for i, ev in enumerate(self._error_events):
            t = ev.get('time', '')
            keywords = ', '.join(ev.get('errors', [])[:3])
            # 해결 여부 간단 판단 (resolved 이벤트가 이 에러 이후 있으면 해결)
            is_resolved = any(rt > t for rt in resolved_times if rt)
            marker = '✅' if is_resolved else '❌'
            self._listbox.insert('end', f'{marker} [{t}] {keywords}')

    def _on_select(self, event: object) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._error_events):
            return
        ev = self._error_events[idx]

        # error_history에서 시간이 가장 가까운 항목 매칭
        error_history = self._session.get('error_history', [])
        ev_time = ev.get('time', '')
        matched = None
        for entry in error_history:
            if entry.get('time', '')[:19] == ev_time[:19]:  # 초 단위까지 비교
                matched = entry
                break
        if matched is None:
            matched = self._session.get('last_error', {})

        error_desc = matched.get('error_description', ', '.join(ev.get('errors', [])))
        solution = matched.get('solution', '')

        text = (
            f"시간: {ev.get('time', '')}\n"
            f"유형: {ev.get('type', '')}\n"
            f"감지 키워드: {', '.join(ev.get('errors', []))}\n"
            f"프레임: {ev.get('frame', '—')}\n\n"
            f"터미널 출력:\n{ev.get('output', '—')}\n\n"
            f"에러 설명:\n{error_desc}\n\n"
            f"제안 해결법:\n{solution or '(정보 없음)'}"
        )
        self._txt_detail.config(state='normal')
        self._txt_detail.delete('1.0', 'end')
        self._txt_detail.insert('1.0', text)
        self._txt_detail.config(state='disabled')
        self._btn_prompt.config(state='normal')
        self._selected_error = matched  # 프롬프트 생성 시 사용

    def _open_prompt(self) -> None:
        error = getattr(self, '_selected_error', None) or self._session.get('last_error', {})
        if error:
            PromptWindow(self._session, error)

    def _on_close(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None



# =====================================================================
#  PromptWindow — 프롬프트 생성/복사
# =====================================================================
class PromptWindow:
    """에러 정보를 기반으로 해결 프롬프트를 생성하고 클립보드에 복사합니다."""

    def __init__(self, session_index_ref: dict, error_info: dict) -> None:
        if not _HAS_TKINTER:
            return
        self._session = session_index_ref
        self._error = error_info
        self._win: tk.Toplevel | None = None
        self._show()

    def _show(self) -> None:
        self._win = tk.Toplevel()
        self._win.title('AI 업무 어시스턴트 — 프롬프트 생성')
        self._win.geometry('650x500')
        self._win.resizable(False, False)
        self._win.configure(bg=BG)
        self._win.protocol('WM_DELETE_WINDOW', self._on_close)

        # ── 에러 요약 ──
        card = tk.Frame(self._win, bg=BG_CARD, padx=12, pady=8)
        card.pack(fill='x', padx=12, pady=(12, 6))

        tk.Label(card, text='에러 정보', font=FONT_TITLE, bg=BG_CARD, fg=ERR_RED, anchor='w').pack(fill='x')
        err_desc = self._error.get('error_description', '—')
        tk.Label(card, text=err_desc, font=FONT_BODY, bg=BG_CARD, fg=FG, wraplength=600, anchor='w', justify='left').pack(
            fill='x', pady=(4, 0)
        )

        # ── 생성된 프롬프트 텍스트 ──
        tk.Label(self._win, text='생성된 프롬프트', font=FONT_TITLE, bg=BG, fg=ACCENT_BLUE, anchor='w').pack(
            fill='x', padx=14, pady=(10, 4)
        )
        self._txt_prompt = scrolledtext.ScrolledText(
            self._win, wrap='word', font=FONT_MONO,
            bg=BG_INPUT, fg=FG, insertbackground=FG, relief='flat',
            borderwidth=0, highlightthickness=1, highlightbackground=BORDER,
        )
        self._txt_prompt.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        self._txt_prompt.insert('1.0', '프롬프트를 생성하고 있습니다...')
        self._txt_prompt.config(state='disabled')

        # ── 상태 ──
        self._status_var = tk.StringVar()
        tk.Label(self._win, textvariable=self._status_var, font=FONT_SMALL, bg=BG, fg=FG_DIM, anchor='w').pack(
            fill='x', padx=14
        )

        # ── 버튼 ──
        btn_frame = tk.Frame(self._win, bg=BG)
        btn_frame.pack(fill='x', padx=14, pady=(4, 12))

        tk.Button(
            btn_frame, text='🔄 다시 생성', font=FONT_BODY,
            bg=BG_CARD, fg=FG, activebackground=BG_INPUT, activeforeground=FG,
            relief='flat', command=self._generate,
        ).pack(side='left')

        tk.Button(
            btn_frame, text='📋 클립보드에 복사', font=FONT_BODY,
            bg=OK_GREEN, fg='white', activebackground='#00aa55', activeforeground='white',
            relief='flat', command=self._copy,
        ).pack(side='right')

        # 자동 생성 시작
        self._generate()

    def _generate(self) -> None:
        self._txt_prompt.config(state='normal')
        self._txt_prompt.delete('1.0', 'end')
        self._txt_prompt.insert('1.0', '프롬프트를 생성하고 있습니다...')
        self._txt_prompt.config(state='disabled')
        self._status_var.set('Gemini에 요청 중...')

        # 백그라운드 스레드에서 생성
        t = threading.Thread(target=self._generate_in_thread, daemon=True)
        t.start()

    def _generate_in_thread(self) -> None:
        try:
            from prompt_generator import generate_fix_prompt

            recent_commands: list[str] = []
            for ev in self._session.get('events', [])[-10:]:
                if ev.get('type') == 'terminal_commands':
                    recent_commands.extend(ev.get('commands', []))
            recent_commands = recent_commands[-5:]

            prompt = generate_fix_prompt(
                error_description=self._error.get('error_description', ''),
                solution=self._error.get('solution', ''),
                current_task=self._error.get('current_task', ''),
                recent_commands=recent_commands,
                session_index=self._session,
            )
            self._update_prompt(prompt, '프롬프트 생성 완료!')
        except Exception as e:
            # 실패 시 fallback 프롬프트라도 표시
            fallback = (
                f"다음 에러를 수정해주세요:\n\n"
                f"에러: {self._error.get('error_description', '알 수 없음')}\n"
                f"현재 작업: {self._error.get('current_task', '알 수 없음')}\n"
                f"제안된 해결법: {self._error.get('solution', '없음')}"
            )
            self._update_prompt(fallback, f'Gemini 호출 실패 — 기본 템플릿 사용: {e}')


    def _update_prompt(self, text: str, status: str) -> None:
        if self._win is None:
            return
        try:
            self._win.after(0, self._set_prompt_text, text, status)
        except tk.TclError:
            pass

    def _set_prompt_text(self, text: str, status: str) -> None:
        self._txt_prompt.config(state='normal')
        self._txt_prompt.delete('1.0', 'end')
        self._txt_prompt.insert('1.0', text)
        self._txt_prompt.config(state='disabled')
        self._status_var.set(status)

    def _copy(self) -> None:
        text = self._txt_prompt.get('1.0', 'end').strip()
        if not text:
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            self._status_var.set('✅ 클립보드에 복사되었습니다!')
        except Exception:
            # pyperclip 실패 시 tkinter 클립보드 사용
            try:
                self._win.clipboard_clear()
                self._win.clipboard_append(text)
                self._status_var.set('✅ 클립보드에 복사되었습니다!')
            except Exception as e:
                self._status_var.set(f'복사 실패: {e}')

    def _on_close(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None

