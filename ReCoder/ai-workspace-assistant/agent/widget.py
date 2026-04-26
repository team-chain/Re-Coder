"""
ReCoder Widget — PyQt6 기반 항상 위에 떠 있는 AI 에러 감지 채팅 위젯
ws://127.0.0.1:18080/ws/updates 에 연결하여 에러/분석 결과를 실시간 수신

실행: python widget.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QObject, QPoint, QSize, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPalette,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizeGrip, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget,
)

_HAS_WS = False  # HTTP 폴링 사용 (PyQt6-WebSockets 불필요)

try:
    import pyperclip
    _HAS_CLIPBOARD = True
except ImportError:
    _HAS_CLIPBOARD = False


# ── 이모지 폰트 헬퍼 ────────────────────────────────────────────────────

def _emoji_label(text: str, style: str = "", parent=None) -> "QLabel":
    """이모지가 깨지지 않도록 Apple Color Emoji / Noto Emoji 폰트를 지정한 QLabel."""
    lbl = QLabel(text, parent)
    # macOS: Apple Color Emoji / Linux·Win: Noto Emoji → Segoe UI Emoji
    emoji_font = (
        "font-family: 'Apple Color Emoji', 'Noto Emoji', 'Segoe UI Emoji', sans-serif;"
        "font-size: 14px;"
    )
    lbl.setStyleSheet(f"{emoji_font} background: transparent; border: none; {style}")
    return lbl


# ── 상수 ───────────────────────────────────────────────────────────────

SERVER_PORT     = int(os.getenv("LOCAL_PORT", "17894"))
BASE_URL        = f"http://127.0.0.1:{SERVER_PORT}"
SSE_URL         = f"{BASE_URL}/api/updates/stream"
POS_FILE        = Path.home() / ".ai_assistant" / "widget_pos.json"
WIN_W, WIN_H    = 380, 620
WIN_MIN_W       = 320
WIN_MIN_H       = 420
RECONNECT_MS    = 3000

# セッショントークン (server起動後に取得)
_session_token: str = ""

# ── VSCode 다크 팔레트 ────────────────────────────────────────────────

C_BG            = "#1e1e1e"   # VSCode 기본 배경
C_BG_CARD       = "#252526"   # 사이드바
C_BG_INPUT      = "#3c3c3c"   # 입력창
C_BG_CODE       = "#0d0d0d"   # 코드 블록
C_BG_TITLE      = "#323233"   # 타이틀바
C_FG            = "#d4d4d4"   # 기본 텍스트
C_FG_DIM        = "#6a6a6a"   # 흐린 텍스트
C_ACCENT        = "#0e639c"   # VSCode 파란색
C_ACCENT_LIGHT  = "#1177bb"
C_ERROR         = "#f14c4c"   # 에러 빨강
C_ERROR_BG      = "#3d1a1a"   # 에러 배경
C_SUCCESS       = "#4ec994"   # 성공 초록
C_WARN          = "#d7ba7d"   # 경고 노랑
C_CODE_TEXT     = "#569cd6"   # 코드 텍스트 (VSCode 파란)
C_BORDER        = "#3e3e3e"


def _qss_base() -> str:
    return f"""
    QWidget {{
        background: {C_BG};
        color: {C_FG};
        font-family: 'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', 'Segoe UI Emoji', sans-serif;
        font-size: 13px;
    }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{
        background: {C_BG};
        width: 6px;
        margin: 0;
        border-radius: 3px;
    }}
    QScrollBar::handle:vertical {{
        background: {C_BORDER};
        border-radius: 3px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {C_FG_DIM}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QLineEdit {{
        background: {C_BG_INPUT};
        color: {C_FG};
        border: 1px solid {C_BORDER};
        border-radius: 6px;
        padding: 8px 12px;
        font-size: 13px;
    }}
    QLineEdit:focus {{ border: 1px solid {C_ACCENT}; }}
    QPushButton {{
        background: {C_ACCENT};
        color: #ffffff;
        border: none;
        border-radius: 6px;
        padding: 8px 14px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {C_ACCENT_LIGHT}; }}
    QPushButton:pressed {{ background: #0a4f7e; }}
    """


# ── HTTP 폴링 워커 ────────────────────────────────────────────────────

class SseWorker(QObject):
    """
    server.py SSE(/api/updates/stream)를 백그라운드 스레드에서 수신.
    연결 전 /api/token으로 세션 토큰을 먼저 취득한다.
    """
    message_received = pyqtSignal(str)
    connected        = pyqtSignal()
    disconnected     = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _fetch_token(self) -> str:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{BASE_URL}/api/token", timeout=3) as r:
                data = json.loads(r.read().decode())
                return data.get("token", "")
        except Exception:
            return ""

    def _run(self) -> None:
        import urllib.request, time, threading

        global _session_token
        while self._running:
            # 토큰 취득
            if not _session_token:
                _session_token = self._fetch_token()
                if not _session_token:
                    time.sleep(2)
                    continue

            try:
                req = urllib.request.Request(
                    SSE_URL,
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.connected.emit()
                    # 바이트 버퍼에 누적 → SSE 메시지 경계(\n\n)에서만 UTF-8 디코딩.
                    # 1바이트씩 즉시 디코딩하면 한글(3바이트) 중간에서 잘려 �로 깨진다.
                    buf_bytes = b""
                    while self._running:
                        chunk = resp.read(1024)
                        if not chunk:
                            break
                        buf_bytes += chunk
                        while b"\n\n" in buf_bytes:
                            raw, _, buf_bytes = buf_bytes.partition(b"\n\n")
                            try:
                                text = raw.decode("utf-8")
                            except UnicodeDecodeError:
                                text = raw.decode("utf-8", errors="replace")
                            for line in text.splitlines():
                                if line.startswith("data:"):
                                    self.message_received.emit(line[5:].strip())
            except Exception:
                self.disconnected.emit()
                time.sleep(RECONNECT_MS / 1000)

    def stop(self) -> None:
        self._running = False


# ── 메시지 버블 위젯들 ────────────────────────────────────────────────

class _BubbleBase(QFrame):
    """말풍선 베이스."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)


class AiBubble(_BubbleBase):
    """AI 분석 메시지 — 좌측 파란 보더."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CARD};
                border-left: 3px solid {C_ACCENT};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(0)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {C_FG}; font-size: 13px; background: transparent; border: none;")
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(lbl)


class ErrorBubble(_BubbleBase):
    """에러 감지 메시지 — 빨간 배경."""

    def __init__(self, error_desc: str, solution: str, command: str, error_count: int, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_ERROR_BG};
                border-left: 3px solid {C_ERROR};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        # 헤더
        header = QHBoxLayout()
        icon_lbl = _emoji_label("🔴")
        title_lbl = QLabel(f"에러 감지 #{error_count}")
        title_lbl.setStyleSheet(f"color: {C_ERROR}; font-weight: 700; font-size: 13px; background: transparent; border: none;")
        header.addWidget(icon_lbl)
        header.addWidget(title_lbl)
        header.addStretch()
        lay.addLayout(header)

        # 에러 설명
        if error_desc:
            desc_lbl = QLabel(error_desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(f"color: {C_FG}; font-size: 13px; background: transparent; border: none;")
            desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(desc_lbl)

        # 해결책
        if solution:
            sol_lbl = QLabel(solution)
            sol_lbl.setWordWrap(True)
            sol_lbl.setStyleSheet(f"color: {C_WARN}; font-size: 12px; background: transparent; border: none;")
            sol_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # 💡 아이콘 따로 분리해서 깨짐 방지
            sol_row = QHBoxLayout()
            sol_row.setSpacing(4)
            sol_row.addWidget(_emoji_label("💡"))
            sol_row.addWidget(sol_lbl, stretch=1)
            lay.addLayout(sol_row)

        # 명령어 블록
        if command:
            lay.addWidget(CommandBlock(command))


class CommandBlock(QFrame):
    """명령어 코드 블록 + 📋 복사 버튼."""

    def __init__(self, command: str, parent=None) -> None:
        super().__init__(parent)
        self._command = command
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CODE};
                border: 1px solid {C_BORDER};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 상단 바 (bash 라벨 + 복사 버튼)
        bar = QFrame()
        bar.setStyleSheet(f"background: #1a1a1a; border-radius: 6px 6px 0 0; border-bottom: 1px solid {C_BORDER};")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(10, 4, 6, 4)
        lang_lbl = QLabel("bash")
        lang_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; font-family: 'Consolas', 'D2Coding', monospace; background: transparent; border: none;")
        copy_btn = QPushButton("📋 복사")
        copy_btn.setFixedHeight(24)
        copy_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_BG_INPUT};
                color: {C_FG};
                border: 1px solid {C_BORDER};
                border-radius: 4px;
                padding: 0 8px;
                font-size: 11px;
            }}
            QPushButton:hover {{ background: {C_ACCENT}; color: white; border-color: {C_ACCENT}; }}
            QPushButton:pressed {{ background: #0a4f7e; }}
        """)
        copy_btn.clicked.connect(self._copy)
        bar_lay.addWidget(lang_lbl)
        bar_lay.addStretch()
        bar_lay.addWidget(copy_btn)
        lay.addWidget(bar)

        # 명령어 텍스트
        code_lbl = QLabel(command)
        code_lbl.setWordWrap(True)
        code_lbl.setStyleSheet(f"""
            color: {C_CODE_TEXT};
            font-family: 'D2Coding', 'Consolas', 'Courier New', monospace;
            font-size: 13px;
            padding: 10px 12px;
            background: transparent;
            border: none;
        """)
        code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(code_lbl)
        self._copy_btn = copy_btn

    def _copy(self) -> None:
        if _HAS_CLIPBOARD:
            try:
                pyperclip.copy(self._command)
            except Exception:
                QApplication.clipboard().setText(self._command)
        else:
            QApplication.clipboard().setText(self._command)

        self._copy_btn.setText("✅ 복사됨")
        QTimer.singleShot(1500, lambda: self._copy_btn.setText("📋 복사"))


class ActionBubble(_BubbleBase):
    """
    Orchestrator WAITING_USER_ACTION 상태에서 표시되는 선택지 버블.
    버튼 클릭 → server.py /api/patch/propose 또는 /api/orchestrator/action 호출.
    """
    action_clicked = pyqtSignal(str)   # action value 전달

    def __init__(self, event: dict, parent=None) -> None:
        super().__init__(parent)
        self._event   = event
        self._buttons: list[QPushButton] = []
        self._done    = False

        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CARD};
                border-left: 3px solid {C_ACCENT};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # 헤더
        header = QHBoxLayout()
        icon_lbl  = QLabel("⚡")
        icon_lbl.setStyleSheet("background: transparent; border: none; font-size: 14px;")
        title_lbl = QLabel("어떻게 할까요?")
        title_lbl.setStyleSheet(
            f"color: {C_ACCENT}; font-weight: 700; font-size: 13px;"
            " background: transparent; border: none;"
        )
        header.addWidget(icon_lbl)
        header.addWidget(title_lbl)
        header.addStretch()
        lay.addLayout(header)

        # 버튼 행
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        def _btn(label: str, action: str, primary: bool = False) -> QPushButton:
            b = QPushButton(label)
            b.setFixedHeight(30)
            accent = C_ACCENT if primary else C_BG_INPUT
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {accent};
                    color: {'white' if primary else C_FG};
                    border: 1px solid {C_BORDER};
                    border-radius: 5px;
                    font-size: 12px;
                    padding: 0 10px;
                }}
                QPushButton:hover {{ background: {C_ACCENT_LIGHT if primary else '#4a4a4a'}; }}
                QPushButton:disabled {{ color: {C_FG_DIM}; background: {C_BG}; }}
            """)
            b.clicked.connect(lambda _, a=action: self._on_action(a))
            self._buttons.append(b)
            return b

        btn_row.addWidget(_btn("🔧 코드 수정", "fix_code", primary=True))
        btn_row.addWidget(_btn("💡 원인 설명", "explain_error"))
        btn_row.addWidget(_btn("✕ 무시",       "ignore"))
        lay.addLayout(btn_row)

    def _on_action(self, action: str) -> None:
        if self._done:
            return
        self._done = True
        for b in self._buttons:
            b.setEnabled(False)
        self.action_clicked.emit(action)

    def mark_done(self, label: str = "처리됨") -> None:
        self._done = True
        for b in self._buttons:
            b.setEnabled(False)


class CodeReadyBubble(_BubbleBase):
    """CODE_READY 상태 — Dockerfile 생성 / 무시 선택지."""
    action_clicked = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._done = False
        self._buttons: list[QPushButton] = []

        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CARD};
                border-left: 3px solid {C_SUCCESS};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        title_lbl = QLabel("✅ 코드 수정 완료 — 다음 단계를 선택하세요")
        title_lbl.setStyleSheet(
            f"color: {C_SUCCESS}; font-weight: 700; font-size: 13px;"
            " background: transparent; border: none;"
        )
        title_lbl.setWordWrap(True)
        lay.addWidget(title_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        def _btn(label: str, action: str, primary: bool = False) -> QPushButton:
            b = QPushButton(label)
            b.setFixedHeight(30)
            accent = C_SUCCESS if primary else C_BG_INPUT
            fg_color = "#1a1a1a" if primary else C_FG
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {accent};
                    color: {fg_color};
                    border: 1px solid {C_BORDER};
                    border-radius: 5px;
                    font-size: 12px;
                    padding: 0 10px;
                }}
                QPushButton:hover {{ opacity: 0.85; }}
                QPushButton:disabled {{ color: {C_FG_DIM}; background: {C_BG}; }}
            """)
            b.clicked.connect(lambda _, a=action: self._on_action(a))
            self._buttons.append(b)
            return b

        btn_row.addWidget(_btn("🐳 Dockerfile", "generate_dockerfile", primary=True))
        btn_row.addWidget(_btn("Compose", "generate_docker_compose"))
        btn_row.addWidget(_btn("CI", "generate_github_actions"))
        btn_row.addWidget(_btn("✕ 무시", "ignore"))
        lay.addLayout(btn_row)

    def _on_action(self, action: str) -> None:
        if self._done:
            return
        self._done = True
        for b in self._buttons:
            b.setEnabled(False)
        self.action_clicked.emit(action)


class InfraProposalBubble(_BubbleBase):
    """Generated infra file preview and actions in the widget."""
    action_clicked = pyqtSignal(str)

    def __init__(self, proposal: dict, parent=None) -> None:
        super().__init__(parent)
        self._done = False
        self._buttons: list[QPushButton] = []
        target = proposal.get("target_path") or "Dockerfile"
        template = proposal.get("base_template") or ""
        content = proposal.get("content") or ""
        lines = content.splitlines()
        preview = "\n".join(lines[:12])
        if len(lines) > 12:
            preview += "\n..."

        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CARD};
                border-left: 3px solid {C_ACCENT};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        title_lbl = QLabel(f"인프라 파일 생성됨: {target}")
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(
            f"color: {C_ACCENT_LIGHT}; font-weight: 700; font-size: 13px;"
            " background: transparent; border: none;"
        )
        lay.addWidget(title_lbl)

        if template:
            meta_lbl = QLabel(f"template: {template}")
            meta_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")
            lay.addWidget(meta_lbl)

        preview_lbl = QLabel(preview)
        preview_lbl.setTextFormat(Qt.TextFormat.PlainText)
        preview_lbl.setWordWrap(True)
        preview_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_lbl.setStyleSheet(f"""
            QLabel {{
                background: {C_BG_CODE};
                color: {C_FG};
                border: 1px solid {C_BORDER};
                border-radius: 5px;
                padding: 8px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }}
        """)
        lay.addWidget(preview_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        def _btn(label: str, action: str, primary: bool = False) -> QPushButton:
            b = QPushButton(label)
            b.setFixedHeight(30)
            accent = C_ACCENT if primary else C_BG_INPUT
            fg_color = "white" if primary else C_FG
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {accent};
                    color: {fg_color};
                    border: 1px solid {C_BORDER};
                    border-radius: 5px;
                    font-size: 12px;
                    padding: 0 10px;
                }}
                QPushButton:hover {{ opacity: 0.85; }}
                QPushButton:disabled {{ color: {C_FG_DIM}; background: {C_BG}; }}
            """)
            b.clicked.connect(lambda _, a=action: self._on_action(a))
            self._buttons.append(b)
            return b

        btn_row.addWidget(_btn("저장", "save_infra"))
        btn_row.addWidget(_btn("저장 후 실행", "run_infra", primary=True))
        btn_row.addWidget(_btn("대시보드", "open_dashboard"))
        btn_row.addWidget(_btn("취소", "cancel_infra"))
        lay.addLayout(btn_row)

    def _on_action(self, action: str) -> None:
        if self._done:
            return
        if action in {"save_infra", "run_infra", "cancel_infra"}:
            self._done = True
            for b in self._buttons:
                b.setEnabled(False)
        self.action_clicked.emit(action)


class UserBubble(_BubbleBase):
    """사용자 입력 말풍선 — 우측 정렬."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_ACCENT};
                border-radius: 6px;
                margin: 2px 4px 2px 40px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: white; font-size: 13px; background: transparent; border: none;")
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(lbl)


class SystemBubble(_BubbleBase):
    """시스템 알림 — 가운데 작은 글씨."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addStretch()
        lay.addWidget(lbl)
        lay.addStretch()


class RepeatWarnBubble(_BubbleBase):
    """에러 반복 경고 — 노란 배경."""

    def __init__(self, count: int, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: #2d2500;
                border-left: 3px solid {C_WARN};
                border-radius: 6px;
                margin: 2px 8px 2px 4px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel(f"⚠️  에러가 {count}번 반복됐어. 근본 원인이 있을 수 있어.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {C_WARN}; font-size: 13px; background: transparent; border: none;")
        lay.addWidget(lbl)


# ── 상태바 ────────────────────────────────────────────────────────────

class StatusBar(QFrame):
    """현재 에러 상태 + 오늘 요약을 한 줄에 표시."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_CARD};
                border-bottom: 1px solid {C_BORDER};
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {C_SUCCESS}; font-size: 10px; background: transparent; border: none;")
        self._status_lbl = QLabel("대기 중")
        self._status_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")
        self._summary_lbl = QLabel("")
        self._summary_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")
        self._summary_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(self._dot)
        lay.addWidget(self._status_lbl)
        lay.addStretch()
        lay.addWidget(self._summary_lbl)

    def set_ok(self, task: str = "") -> None:
        self._dot.setStyleSheet(f"color: {C_SUCCESS}; font-size: 10px; background: transparent; border: none;")
        self._status_lbl.setText(task[:40] if task else "정상")
        self._status_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")

    def set_error(self, msg: str = "") -> None:
        self._dot.setStyleSheet(f"color: {C_ERROR}; font-size: 10px; background: transparent; border: none;")
        self._status_lbl.setText(msg[:40] if msg else "에러 감지됨")
        self._status_lbl.setStyleSheet(f"color: {C_ERROR}; font-size: 11px; background: transparent; border: none;")

    def set_connecting(self) -> None:
        self._dot.setStyleSheet(f"color: {C_FG_DIM}; font-size: 10px; background: transparent; border: none;")
        self._status_lbl.setText("연결 중...")
        self._status_lbl.setStyleSheet(f"color: {C_FG_DIM}; font-size: 11px; background: transparent; border: none;")

    def set_summary(self, total: int, breakdown: str) -> None:
        if total == 0:
            self._summary_lbl.setText("")
        else:
            self._summary_lbl.setText(f"오늘 에러 {total}건 | {breakdown}")


# ── 타이틀바 ─────────────────────────────────────────────────────────

class TitleBar(QFrame):
    """드래그 이동 가능한 커스텀 타이틀바."""

    close_clicked    = pyqtSignal()
    minimize_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setStyleSheet(f"""
            QFrame {{
                background: {C_BG_TITLE};
                border-radius: 8px 8px 0 0;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(6)

        # 로고 + 이름
        dot = QLabel("◆")
        dot.setStyleSheet(f"color: {C_ACCENT}; font-size: 12px; background: transparent; border: none;")
        name = QLabel("ReCoder")
        name.setStyleSheet(f"color: {C_FG}; font-weight: 700; font-size: 13px; background: transparent; border: none;")

        lay.addWidget(dot)
        lay.addWidget(name)
        lay.addStretch()

        # 버튼들
        min_btn = QPushButton("─")
        min_btn.setFixedSize(24, 24)
        min_btn.setStyleSheet(self._btn_style(C_FG_DIM))
        min_btn.clicked.connect(self.minimize_clicked.emit)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(self._btn_style(C_ERROR))
        close_btn.clicked.connect(self.close_clicked.emit)

        lay.addWidget(min_btn)
        lay.addWidget(close_btn)

        self._drag_pos: QPoint | None = None

    def _btn_style(self, hover_color: str) -> str:
        return f"""
            QPushButton {{
                background: transparent;
                color: {C_FG_DIM};
                border: none;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {hover_color}33;
                color: {hover_color};
            }}
        """

    # 드래그 이동
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_pos = None


# ── 메인 위젯 ─────────────────────────────────────────────────────────

class ReCoderWidget(QWidget):
    ai_message_received = pyqtSignal(str)
    system_message_received = pyqtSignal(str)
    """ReCoder 메인 위젯."""

    def __init__(self) -> None:
        super().__init__()

        # 에러 추적용
        self._error_count       = 0
        self._error_types: list[str] = []
        self._last_warn_count   = 0
        self._current_event_id: str = ""      # 현재 처리 중인 AgentEvent
        self._last_error_text:  str = ""      # 채팅 컨텍스트용 최근 에러 텍스트

        self._ignore_sse_chat_count = 0

        self.ai_message_received.connect(self._add_ai_slot)
        self.system_message_received.connect(self._add_system)
        self._setup_window()
        self._build_ui()
        self._restore_pos()
        self._setup_sse()

        self._add_system("ReCoder 시작됨 — 화면을 감시하고 있어.")

    # ── 창 설정 ────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(WIN_MIN_W, WIN_MIN_H)
        self.resize(WIN_W, WIN_H)
        self.setStyleSheet(_qss_base())
        # AX가 이 창을 "ReCoder Widget"으로 식별할 수 있도록 타이틀 지정
        # → monitor.py의 _SELF_MARKERS 에서 걸러냄
        self.setWindowTitle("ReCoder Widget")

    # ── UI 구성 ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # 반투명 컨테이너
        container = QFrame(self)
        container.setObjectName("container")
        container.setStyleSheet(f"""
            QFrame#container {{
                background: {C_BG};
                border-radius: 8px;
                border: 1px solid {C_BORDER};
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        main = QVBoxLayout(container)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # 타이틀바
        self._title_bar = TitleBar()
        self._title_bar.close_clicked.connect(self._save_pos_and_close)
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        main.addWidget(self._title_bar)

        # 상태바
        self._status_bar = StatusBar()
        main.addWidget(self._status_bar)

        # 채팅 영역 (스크롤)
        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chat_scroll.setStyleSheet(f"background: {C_BG}; border: none;")

        self._chat_container = QWidget()
        self._chat_container.setStyleSheet(f"background: {C_BG};")
        self._chat_layout = QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(4, 8, 4, 8)
        self._chat_layout.setSpacing(6)
        self._chat_layout.addStretch()

        self._chat_scroll.setWidget(self._chat_container)
        main.addWidget(self._chat_scroll, stretch=1)

        # 구분선
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {C_BORDER};")
        main.addWidget(sep)

        # 입력창
        input_frame = QFrame()
        input_frame.setStyleSheet(f"background: {C_BG_CARD}; border-radius: 0 0 8px 8px;")
        input_lay = QHBoxLayout(input_frame)
        input_lay.setContentsMargins(10, 8, 10, 10)
        input_lay.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("AI에게 질문하거나 명령하세요...")
        self._input.returnPressed.connect(self._send_user_message)

        send_btn = QPushButton("▶")
        send_btn.setFixedSize(36, 36)
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_ACCENT};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }}
            QPushButton:hover {{ background: {C_ACCENT_LIGHT}; }}
        """)
        send_btn.clicked.connect(self._send_user_message)

        input_lay.addWidget(self._input)
        input_lay.addWidget(send_btn)
        main.addWidget(input_frame)

        # 리사이즈 그립
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        grip_lay = QHBoxLayout()
        grip_lay.addStretch()
        grip_lay.addWidget(grip)
        grip_lay.setContentsMargins(0, 0, 2, 2)
        main.addLayout(grip_lay)

    # ── 채팅 추가 헬퍼 ─────────────────────────────────────────────────

    def _insert_bubble(self, widget: QWidget) -> None:
        """stretch 바로 앞에 버블 삽입."""
        count = self._chat_layout.count()
        self._chat_layout.insertWidget(count - 1, widget)
        QTimer.singleShot(50, self._scroll_bottom)

    def _scroll_bottom(self) -> None:
        sb = self._chat_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _add_system(self, text: str) -> None:
        self._insert_bubble(SystemBubble(text))

    def _add_ai(self, text: str) -> None:
        self._insert_bubble(AiBubble(text))

    def _add_error(self, error_desc: str, solution: str, command: str) -> None:
        self._error_count += 1
        # 에러 유형 추적
        if error_desc:
            short = error_desc.split(":")[0].strip()[:30]
            self._error_types.append(short)
        self._insert_bubble(ErrorBubble(error_desc, solution, command, self._error_count))
        self._status_bar.set_error(error_desc[:40] if error_desc else "에러 감지됨")
        self._update_summary()

        # 반복 에러 경고 (3번 이상, 직전 경고 이후 추가 발생 시)
        if self._error_count >= 3 and self._error_count != self._last_warn_count:
            self._last_warn_count = self._error_count
            self._insert_bubble(RepeatWarnBubble(self._error_count))

    def _add_user(self, text: str) -> None:
        self._insert_bubble(UserBubble(text))

    def _update_summary(self) -> None:
        if not self._error_types:
            self._status_bar.set_summary(0, "")
            return
        counter = Counter(self._error_types)
        parts = [f"{t} {n}번" for t, n in counter.most_common(2)]
        self._status_bar.set_summary(self._error_count, " / ".join(parts))

    # ── 입력창 전송 ────────────────────────────────────────────────────

    def _send_user_message(self) -> None:
        print(f"[widget][chat] 입력 받음: '{self._input.text()}'")
        text = self._input.text().strip()
        if not text:
            print("[widget][chat] 빈 텍스트 — 중단")
            return
        print(f"[widget][chat] 버블 추가 시작: '{text}'")
        self._add_user(text)
        print("[widget][chat] 버블 추가 완료")
        self._input.clear()
        self._ignore_sse_chat_count += 1
        print("[widget][chat] 스레드 시작 직전")
        threading.Thread(
            target=self._post_chat,
            args=(text,),
            daemon=True,
        ).start()
        print("[widget][chat] 스레드 시작 완료")

    def _post_chat(self, message: str) -> None:
        print(f"[widget][chat] _post_chat 진입: '{message}'")
        """백그라운드에서 /api/chat 호출 → 응답을 SSE로 수신하거나 직접 표시."""
        import urllib.request
        import urllib.error

        url     = f"{BASE_URL}/api/chat"
        print(f"[widget][chat] URL: {url}")
        print(f"[widget][chat] 토큰: {_session_token!r}")
        payload = json.dumps({
            "message": message,
            "context": self._current_context(),
        }).encode("utf-8")
        print(f"[widget][chat] payload 크기: {len(payload)} bytes")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "X-ReCoder-Token": _session_token,
            },
            method="POST",
        )
        def _show(text: str) -> None:
            print(f"[widget][chat] _show: {text[:60]}")
            self.ai_message_received.emit(text)

        print("[widget][chat] urlopen 호출 직전")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                print(f"[widget][chat] 응답 수신, status={resp.status}")
                body = json.loads(resp.read().decode("utf-8"))
                print(f"[widget][chat] body 파싱 완료: keys={list(body.keys())}")
                answer = body.get("answer", "")
                print(f"[widget][chat] answer 길이: {len(answer)}")
                if answer:
                    _show(answer)
                else:
                    _show("⚠️ 빈 응답")
        except urllib.error.HTTPError as e:
            print(f"[widget][chat] HTTPError: {e.code}")
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(err_body).get("detail", err_body)
            except Exception:
                msg = err_body
            _show(f"⚠️ 오류: {msg}")
        except Exception as e:
            print(f"[widget][chat] 일반 예외: {type(e).__name__}: {e}")
            _show(f"⚠️ 연결 실패: {e}")
        print("[widget][chat] _post_chat 종료")

    def _current_context(self) -> str:
        """현재 에러 이벤트가 있으면 컨텍스트로 반환."""
        return getattr(self, "_last_error_text", "")

    def _add_ai_slot(self, text: str) -> None:
        """백그라운드 스레드에서 QueuedConnection으로 호출되는 슬롯."""
        self._add_ai(text)

    # ── WebSocket → SSE 교체 ──────────────────────────────────────────

    def _setup_sse(self) -> None:
        self._sse_worker = SseWorker()
        self._sse_worker.message_received.connect(self._on_sse_message)
        self._sse_worker.connected.connect(self._on_connected)
        self._sse_worker.disconnected.connect(self._on_disconnected)
        self._sse_worker.start()
        self._status_bar.set_connecting()

    def _setup_websocket(self) -> None:
        self._setup_sse()   # 하위호환

    def _setup_polling_fallback(self) -> None:
        pass

    def _on_connected(self) -> None:
        self._add_system("✅ Monitor Agent 연결됨")
        self._status_bar.set_ok()

    def _on_disconnected(self) -> None:
        self._status_bar.set_connecting()

    def _on_ws_message(self, raw: str) -> None:
        self._on_sse_message(raw)

    def _on_sse_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type", "")

        # ── 채팅 응답 ──
        if msg_type == "chat_response":
            answer = data.get("message", "")
            if self._ignore_sse_chat_count > 0:
                self._ignore_sse_chat_count -= 1
                return
            if answer:
                self._add_ai(answer)
            return

        # ── Orchestrator 상태 업데이트 ──
        if msg_type == "orchestrator_update":
            state = data.get("state", "")

            if state == "WAITING_USER_ACTION":
                event = data.get("event") or {}
                self._show_action_choices(event)

            elif state == "CODE_PATCH_PROPOSED":
                proposal = data.get("patch_proposal") or {}
                self._add_ai(f"🔧 수정안 생성됨: {proposal.get('summary', '')}")
                self._open_dashboard()

            elif state == "CODE_READY":
                self._add_system("✅ 코드 수정 완료")
                self._show_code_ready_choices()

            elif state == "INFRA_PROPOSED":
                proposal = data.get("infra_proposal") or {}
                self._show_infra_proposal(proposal)

            elif state == "INFRA_READY":
                msg = data.get("message", "인프라 파일 저장 완료")
                self._add_system(f"✅ {msg}")

            elif state == "IDLE":
                msg = data.get("message", "")
                if msg:
                    self._add_system(msg)
            return

        # ── 기존 session_update 호환 ──
        if msg_type == "infra_run_result":
            self._show_infra_run_result(data.get("result") or {})
            return

        reason = data.get("reason", "")
        if reason == "connected":
            self._status_bar.set_ok(data.get("status", {}).get("current_task", ""))
        elif reason == "session_started":
            self._add_system("🔄 새 세션 시작됨")
            self._error_count = 0
            self._error_types.clear()
            self._update_summary()

    def _show_action_choices(self, event: dict) -> None:
        """에러 감지 시 선택지 버블 표시."""
        error_text = event.get("summary") or event.get("error_text", "에러 감지됨")
        self._add_error(error_text, "", "")
        self._current_event_id = event.get("event_id", "")
        # 채팅 컨텍스트로 활용
        self._last_error_text = event.get("error_text", "") or error_text

        bubble = ActionBubble(event)
        bubble.action_clicked.connect(self._on_action_selected)
        self._insert_bubble(bubble)
        self._status_bar.set_error(error_text[:40])
        self._update_summary()

    def _show_code_ready_choices(self) -> None:
        """CODE_READY 상태에서 Dockerfile 생성 선택지 표시."""
        bubble = CodeReadyBubble()
        bubble.action_clicked.connect(self._on_code_ready_action)
        self._insert_bubble(bubble)

    def _show_infra_proposal(self, proposal: dict) -> None:
        bubble = InfraProposalBubble(proposal)
        bubble.action_clicked.connect(self._on_infra_action)
        self._insert_bubble(bubble)

    def _show_infra_run_result(self, result: dict) -> None:
        mode = result.get("mode", "")
        container = result.get("container", "")
        url = result.get("url", "")
        parts = ["Docker 컨테이너 실행 완료"]
        if mode:
            parts.append(f"mode={mode}")
        if container:
            parts.append(f"container={container}")
        if url:
            parts.append(url)
        self._add_system(" | ".join(parts))

    def _on_infra_action(self, action: str) -> None:
        if action == "save_infra":
            self._add_system("인프라 파일 저장 중...")
            self._call_api_async("POST", "/api/infra/save", {})
        elif action == "run_infra":
            self._add_system("Docker 컨테이너 실행 중...")
            self._call_api_async("POST", "/api/infra/run", {"prefer_compose": True})
        elif action == "open_dashboard":
            self._open_dashboard()
        elif action == "cancel_infra":
            self._call_api_async("POST", "/api/orchestrator/action", {"action": "cancel"})
            self._add_system("인프라 생성 취소")

    def _on_action_selected(self, action: str) -> None:
        """선택지 버튼 클릭 처리."""
        import urllib.request, urllib.error

        if action == "fix_code":
            self._add_system("🔧 코드 수정안 생성 중...")
            # server.py /api/patch/propose 호출
            # _last_error_text 는 _show_action_choices 에서 이벤트 수신 시 저장됨
            self._call_api_async(
                method  = "POST",
                path    = "/api/patch/propose",
                body    = {
                    "event_id":      self._current_event_id,
                    "error_text":    getattr(self, "_last_error_text", "") or "",
                    "related_files": [],
                },
            )
        elif action == "explain_error":
            self._add_ai("에러 원인을 분석합니다. 대시보드에서 확인하세요.")
            self._open_dashboard()
        elif action == "ignore":
            self._call_api_async("POST", "/api/orchestrator/action", {"action": "ignore"})
            self._add_system("무시됨")

    def _on_code_ready_action(self, action: str) -> None:
        if action == "generate_dockerfile":
            self._add_system("🐳 Dockerfile 생성 중...")
            self._call_api_async("GET", "/api/infra/dockerfile", {})
        elif action == "generate_docker_compose":
            self._add_system("docker-compose.yml 생성 중...")
            self._call_api_async("GET", "/api/infra/docker-compose", {})
        elif action == "generate_github_actions":
            self._add_system("GitHub Actions CI 생성 중...")
            self._call_api_async("GET", "/api/infra/github-actions", {})
        elif action == "ignore":
            self._add_system("무시됨")

    def _call_api_async(self, method: str, path: str, body: dict) -> None:
        """API 호출을 백그라운드 스레드에서 실행."""
        import threading, urllib.request, urllib.error

        def _run():
            try:
                import json as _json
                url     = f"{BASE_URL}{path}"
                headers = {
                    "Content-Type":    "application/json",
                    "X-ReCoder-Token": _session_token,
                }
                if method == "GET":
                    # GET 파라미터는 무시 (간단 구현)
                    req = urllib.request.Request(url, headers=headers)
                else:
                    data_bytes = _json.dumps(body).encode()
                    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=900):
                    pass
            except Exception as e:
                print(f"[widget] API 호출 실패 {path}: {e}")
                self.system_message_received.emit(f"API 호출 실패: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def _open_dashboard(self) -> None:
        import webbrowser
        webbrowser.open(f"{BASE_URL}/dashboard?token={_session_token}")



    # ── 위치 저장/복원 ─────────────────────────────────────────────────

    def _restore_pos(self) -> None:
        try:
            if POS_FILE.exists():
                d = json.loads(POS_FILE.read_text())
                self.move(d.get("x", 100), d.get("y", 100))
                return
        except Exception:
            pass
        # 기본: 우하단
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - WIN_W - 20, screen.bottom() - WIN_H - 40)

    def _save_pos(self) -> None:
        try:
            POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            POS_FILE.write_text(json.dumps({"x": self.x(), "y": self.y()}))
        except Exception:
            pass

    def _save_pos_and_close(self) -> None:
        self._save_pos()
        self.close()

    def closeEvent(self, e) -> None:
        self._save_pos()
        if hasattr(self, "_sse_worker"):
            self._sse_worker.stop()
        super().closeEvent(e)


# ── 진입점 ────────────────────────────────────────────────────────────

def main() -> None:
    print("[1] QApplication 생성 중...")
    app = QApplication(sys.argv)
    app.setApplicationName("ReCoder")
    print("[2] ReCoderWidget 생성 중...")
    widget = ReCoderWidget()
    print("[3] widget.show() 호출...")
    widget.show()
    widget.raise_()
    widget.activateWindow()
    print(f"[4] 위젯 위치: x={widget.x()}, y={widget.y()}, 크기: {widget.width()}x{widget.height()}")
    print("[5] 이벤트 루프 시작")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
