"""
ReCoder 진입점 — server.py (FastAPI) + monitor.py 통합 실행.

macOS 제약:
  - QApplication(PyQt6)은 반드시 메인 스레드에서 실행해야 합니다.
  - 따라서 asyncio(FastAPI + Monitor)를 별도 스레드로 분리하고,
    메인 스레드에서 Qt 이벤트 루프를 실행합니다.
"""

import asyncio
import multiprocessing
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv


def _run_async_backend() -> None:
    """FastAPI + Monitor를 백그라운드 스레드에서 실행."""
    from monitor import run as monitor_run, session_index
    from server  import start_server

    async def _main_async() -> None:
        await asyncio.gather(
            monitor_run(),
            start_server(session_index),
        )

    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[main] 백엔드 실행 오류: {e}")


if __name__ == '__main__':
    multiprocessing.freeze_support()
    load_dotenv()

    if not os.getenv('GEMINI_API_KEY'):
        from first_run import show_setup_window
        show_setup_window()
        load_dotenv(override=True)
        if not os.getenv('GEMINI_API_KEY'):
            print('GEMINI_API_KEY가 설정되지 않아 종료합니다.')
            raise SystemExit(1)

    # 터미널 로그 캡처 자동 설정.
    # Windows는 PowerShell profile hook, macOS/Linux는 shell hook을 한 번만 삽입한다.
    auto_capture = os.getenv('AUTO_START_TERMINAL_CAPTURE', '1').strip().lower()
    if auto_capture in {'1', 'true', 'yes', 'on', 'y'}:
        from first_run import setup_terminal_logging
        result = setup_terminal_logging()
        if result == 'inserted':
            print('[ReCoder] ✅ 터미널 로그 설정 완료 — 새 터미널 창을 열면 에러 자동 감지가 시작됩니다.')
        elif result == 'already_set':
            # 이미 설정됨 — 현재 세션에서 로그가 흐르는지 확인
            log_path = os.path.expanduser('~/.ai_assistant/terminal.log')
            if not os.path.exists(log_path):
                print('[ReCoder] ⚠ 터미널 로그 파일이 없습니다. 새 터미널 창을 열어주세요.')

    # FastAPI + Monitor는 백그라운드 스레드에서 asyncio 실행
    backend_thread = threading.Thread(target=_run_async_backend, daemon=True)
    backend_thread.start()

    # PyQt6 위젯은 메인 스레드에서 실행 (macOS 필수)
    try:
        from PyQt6.QtWidgets import QApplication
        from widget import ReCoderWidget

        app    = QApplication(sys.argv)
        widget = ReCoderWidget()
        widget.show()
        sys.exit(app.exec())
    except Exception as e:
        print(f"[main] 위젯 실행 실패: {e}")
