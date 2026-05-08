"""
ReCoder v6.4 Local Core 엔트리포인트 (설계서 §6)
- §6.1 Lazy Spawn: Extension이 호출할 때만 실행
- §6.2 Singleton: core.lock으로 단일 인스턴스 보장
- §6.3 좀비 프로세스 방지: stale lock 감지 + 강제 종료
- §6.4 다중 VSCode 창: lock file에 PID 목록 관리
"""

import asyncio
import atexit
import json
import multiprocessing
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

RECODER_HOME = Path(os.getenv("RECODER_HOME", str(Path.home() / ".recoder")))
LOCK_FILE = RECODER_HOME / "core.lock"
RUNTIME_FILE = RECODER_HOME / "runtime.json"


def acquire_lock() -> bool:
    """
    core.lock 확인 + stale 처리 + 새 lock 생성.

    동작 순서 (설계서 §6.2 / §6.3):
      1) lock 이 없으면 새로 생성하고 True
      2) lock 의 PID 가 살아있으면 같은 ReCoder Core 프로세스인지 확인
         - "recoder-core" 또는 main.py 를 띄운 python 이면 → 다른 인스턴스 살아있음 → False
         - 다른 프로세스(좀비 PID 재사용 등) 면 stale 로 간주 → 강제 종료 후 새 lock
      3) PID 가 죽어 있으면 stale → 새 lock 생성 후 True
      4) PID 목록 (다중 VSCode 창) 은 attached_pids 로 관리

    lock 내용: {
        "pid": int,
        "attached_pids": [int, ...],
        "started_at": str
    }
    """
    RECODER_HOME.mkdir(parents=True, exist_ok=True)

    import psutil
    from datetime import datetime, timezone

    current_pid = os.getpid()

    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                lock_data = json.load(f)

            existing_pid = lock_data.get("pid")

            if existing_pid and psutil.pid_exists(existing_pid):
                # 같은 ReCoder Core 프로세스인지 확인
                try:
                    proc = psutil.Process(existing_pid)
                    cmd = " ".join(proc.cmdline()).lower()
                    name = proc.name().lower()
                    is_recoder = (
                        "recoder-core" in name
                        or "recoder-core" in cmd
                        or "core/main.py" in cmd
                        or "core\\main.py" in cmd
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    is_recoder = False

                if is_recoder:
                    # 정상 동작 중인 다른 인스턴스 → 양보
                    return False

                # Stale: 다른 프로세스가 같은 PID 를 물고 있음 → 강제 종료
                try:
                    proc = psutil.Process(existing_pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # 죽은 PID 또는 강제 종료한 stale → 새 lock 생성
        except Exception:
            # lock 파일 파싱 실패 → 새로 생성
            pass

    # 새 lock 파일 생성
    lock_data = {
        "pid": current_pid,
        "attached_pids": [],
        "started_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
        # 가능하면 0600 권한 설정 (Windows 는 ACL 별도, 실패 시 무시)
        try:
            os.chmod(LOCK_FILE, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def attach_pid(pid: int) -> None:
    """다중 VSCode 창 지원: lock 의 attached_pids 에 PID 추가."""
    if not LOCK_FILE.exists():
        return
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        attached = list(data.get("attached_pids", []))
        if pid not in attached:
            attached.append(pid)
            data["attached_pids"] = attached
            with open(LOCK_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass


def detach_pid(pid: int) -> int:
    """attached_pids 에서 PID 제거 후 남은 개수 반환."""
    if not LOCK_FILE.exists():
        return 0
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        attached = [p for p in data.get("attached_pids", []) if p != pid]
        data["attached_pids"] = attached
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return len(attached)
    except Exception:
        return 0


def release_lock() -> None:
    """프로세스 종료 시 lock file 제거"""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def _handle_shutdown(signum, frame) -> None:
    """SIGTERM/SIGINT 처리. graceful shutdown."""
    print("[Core] Graceful shutdown initiated...")
    release_lock()
    sys.exit(0)


async def _main_async() -> None:
    """FastAPI 서버 비동기 실행"""
    # server 모듈 import
    try:
        from server import start_server
    except ImportError:
        print("[Core] ERROR: server module not found. Ensure FastAPI is installed.")
        sys.exit(1)

    session_index: dict = {}
    await start_server(session_index)


def main() -> None:
    """메인 엔트리포인트"""
    multiprocessing.freeze_support()
    load_dotenv()

    # 디렉터리 초기화
    try:
        from first_run import setup_recoder_home, run_diagnostics
    except ImportError:
        print("[Core] ERROR: first_run module not found.")
        sys.exit(1)

    setup_recoder_home()

    # Singleton 체크
    if not acquire_lock():
        print("[Core] An ReCoder instance is already running. Check runtime.json.")
        sys.exit(0)

    # 종료 핸들러 등록
    atexit.register(release_lock)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    print("[Core] ReCoder v6.4 Local Core starting...")
    print(f"[Core] RECODER_HOME: {RECODER_HOME}")

    # 비동기 서버 실행
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("[Core] Interrupted by user.")
    except Exception as e:
        print(f"[Core] ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        release_lock()
        print("[Core] ReCoder Local Core stopped.")


if __name__ == "__main__":
    main()
