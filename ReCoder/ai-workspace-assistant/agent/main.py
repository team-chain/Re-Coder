"""
ReCoder 진입점 — server.py (FastAPI) + monitor.py 통합 실행.
"""

import asyncio
import multiprocessing
import os
import threading
from pathlib import Path

from dotenv import load_dotenv


def _run_widget() -> None:
    """PyQt6 위젯 메인 스레드 실행 (별도 프로세스 불필요)."""
    try:
        from PyQt6.QtWidgets import QApplication
        from widget import ReCoderWidget
        import sys
        app    = QApplication(sys.argv)
        widget = ReCoderWidget()
        widget.show()
        app.exec()
    except Exception as e:
        print(f"[main] 위젯 실행 실패: {e}")


async def _main_async() -> None:
    from monitor import run as monitor_run, session_index
    from server  import start_server

    await asyncio.gather(
        monitor_run(),
        start_server(session_index),
    )


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

    # 위젯은 별도 스레드 (Qt 이벤트 루프)
    widget_thread = threading.Thread(target=_run_widget, daemon=True)
    widget_thread.start()

    # FastAPI + Monitor는 asyncio
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
