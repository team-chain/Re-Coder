"""
에러 감지 테스트 스크립트.
터미널 로그 파일에 에러를 직접 주입해 대시보드 감지 여부를 확인합니다.

사용법:
  python test_error_detection.py          # 기본: Python ImportError 시뮬레이션
  python test_error_detection.py http     # HTTP 500 에러
  python test_error_detection.py ts       # TypeScript 컴파일 에러
  python test_error_detection.py db       # DB 연결 에러
  python test_error_detection.py all      # 모든 케이스 순서대로
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LOG_PATH = Path(os.getenv("TERMINAL_LOG_PATH", "~/.ai_assistant/terminal.log")).expanduser()

# ── 에러 시나리오 목록 ─────────────────────────────────────────────────
SCENARIOS: dict[str, str] = {
    "python": """\
$ python app.py
Traceback (most recent call last):
  File "app.py", line 3, in <module>
    from flask import Flask
ModuleNotFoundError: No module named 'flask'
""",
    "http": """\
$ curl -X POST http://localhost:8000/api/users
POST /api/users 500
{"detail": "Internal Server Error", "status": 500}
""",
    "ts": """\
$ npx tsc --noEmit
src/components/App.tsx:12:5 - error TS2304: Cannot find name 'useEffect'.
src/utils/api.ts:34:18 - error TS7006: Parameter 'data' implicitly has an 'any' type.
Found 2 errors.
""",
    "db": """\
$ python manage.py migrate
django.db.utils.OperationalError: could not connect to server: Connection refused
        Is the server running on host "localhost" (127.0.0.1) and accepting
        TCP/IP connections on port 5432?
""",
    "npm": """\
$ npm install
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! Found: react@18.2.0
npm ERR! Could not resolve dependency: peer react@"^17.0.0" from react-router-dom@5.3.4
""",
    "syntax": """\
$ python server.py
  File "server.py", line 27
    def handle_request(req
                          ^
SyntaxError: '(' was never closed
""",
}


def write_to_log(text: str) -> None:
    """터미널 로그 파일에 직접 기록."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(text)
    print(f"[테스트] 로그 파일에 기록됨: {LOG_PATH}")


def run_scenario(name: str) -> None:
    scenario = SCENARIOS.get(name)
    if not scenario:
        print(f"[테스트] 알 수 없는 시나리오: {name}")
        return

    labels = {
        "python": "Python ModuleNotFoundError",
        "http":   "HTTP 500 에러",
        "ts":     "TypeScript 컴파일 에러",
        "db":     "DB 연결 에러",
        "npm":    "npm 의존성 에러",
        "syntax": "SyntaxError",
    }
    print(f"\n{'─'*50}")
    print(f"▶ 시나리오: {labels.get(name, name)}")
    print(f"{'─'*50}")
    print(scenario)
    write_to_log(scenario)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "python"

    if arg == "all":
        for name in SCENARIOS:
            run_scenario(name)
            print("  → 대시보드에서 에러 감지 확인 후 5초 후 다음 에러 주입...")
            time.sleep(5)
    elif arg in SCENARIOS:
        run_scenario(arg)
    else:
        print(f"사용 가능한 시나리오: {', '.join(SCENARIOS.keys())}, all")
        sys.exit(1)

    print(f"\n✅ 에러 주입 완료.")
    print(f"   대시보드( http://127.0.0.1:17894/dashboard )에서 에러 감지 여부를 확인하세요.")
    print(f"   이벤트 이력 탭에서도 확인 가능합니다.")


if __name__ == "__main__":
    main()
