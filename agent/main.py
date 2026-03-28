# main.py
# ▶ 설계 근거: Windows + PyInstaller 환경에서 multiprocessing을 사용할 때
# freeze_support()가 없으면 .exe 실행 시 자식 프로세스가 무한 생성됩니다.
# GEMINI_API_KEY 또는 USER_TOKEN 둘 중 하나라도 없으면 first_run.py 팝업을 띄웁니다.

import multiprocessing
import os

from dotenv import load_dotenv

if __name__ == '__main__':
    multiprocessing.freeze_support()  # [필수] PyInstaller Fork Bomb 방지

    load_dotenv()

    if not os.getenv('GEMINI_API_KEY') or not os.getenv('USER_TOKEN'):
        from first_run import show_setup_window
        show_setup_window()
        load_dotenv()  # 설정 후 .env 재로드

    import asyncio
    from monitor import run

    asyncio.run(run())
