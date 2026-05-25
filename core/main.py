"""
ReCoder v6.4 Local Core — FastAPI Entry Point (설계서 §4.2, §6)

설계서 매핑:
- §4.2 Local Core: PyInstaller 단일 실행파일, FastAPI 서버 127.0.0.1 바인딩
- §6.1 Lazy Spawn: Extension이 호출할 때만 실행
- §6.2 Singleton: core.lock으로 단일 인스턴스 보장 + 포트 17894 (fallback 17895~17910)
- §6.3 좀비 프로세스 방지: stale lock 감지 + 강제 종료, SIGTERM → SIGKILL
- §6.4 다중 VSCode 창: lock file에 PID 목록 관리
- §21 데이터 저장: ~/.recoder/ 하위 통일, runtime.json/core.lock 권한 0600

특징:
- session-token 인증 (X-Session-Token 헤더)
- CORS는 127.0.0.1 / localhost로만 제한
- 모든 핵심 라우터 포함 (health, analyze, deploy, ops, session, policy, ecs, gitops, incident)
- graceful shutdown (SIGTERM/SIGINT)
- 멀티 VSCode 창 환경에서 attached_pids 관리
"""

from __future__ import annotations

import atexit
import multiprocessing
import os
import secrets
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv가 없어도 코어는 동작해야 함
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

# core 모듈을 패키지 외부에서도 import 가능하도록 sys.path 보정
_CORE_DIR = Path(__file__).parent
_ROOT_DIR = _CORE_DIR.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from singleton import CoreSingleton  # noqa: E402
from api.middleware.auth import SessionTokenMiddleware  # noqa: E402
from api.routes import (  # noqa: E402
    health,
    analyze,
    deploy,
    ops,
    session,
    policy,
    ecs,
    gitops,
    incident,
    workbench,
)

_bound_port: int = 0
VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan — 설계서 §6.2 / §6.3 / §6.4
    - singleton 락 획득 시도
    - 락 실패 시 기존 runtime.json 재사용 또는 강제 권리 취득
    - graceful shutdown 시 attached PID 정리 + 락 해제
    """
    pid = os.getpid()

    lock_acquired = CoreSingleton.acquire_lock(pid)
    if not lock_acquired:
        existing = CoreSingleton.read_runtime()
        if existing:
            # 다른 ReCoder Core 인스턴스가 살아있음 → 같은 runtime 재사용
            app.state.port = existing.port
            app.state.session_token = existing.session_token
            app.state.started_at = datetime.now(timezone.utc)
        else:
            # Orphan state — singleton lock exists but runtime.json was deleted.
            # 강제로 권리를 취득해서 인증 엔드포인트가 영구 503으로 빠지지 않도록 한다.
            port = _bound_port if _bound_port else CoreSingleton.find_available_port()
            token = secrets.token_urlsafe(32)
            app.state.port = port
            app.state.session_token = token
            CoreSingleton.write_runtime(port=port, token=token, pid=pid)
            CoreSingleton.set_file_permissions(CoreSingleton.RUNTIME_FILE)
            app.state.started_at = datetime.now(timezone.utc)

        try:
            yield
        finally:
            CoreSingleton.remove_window(pid)
        return

    # 정상 경로 — 락 획득 성공
    port = _bound_port if _bound_port else CoreSingleton.find_available_port()
    app.state.port = port

    token = secrets.token_urlsafe(32)
    app.state.session_token = token

    CoreSingleton.write_runtime(port=port, token=token, pid=pid)
    CoreSingleton.set_file_permissions(CoreSingleton.RUNTIME_FILE)
    CoreSingleton.set_file_permissions(CoreSingleton.LOCK_FILE)

    app.state.started_at = datetime.now(timezone.utc)

    # OTel 초기화 (Q4) — 선택적, 실패해도 무시
    try:
        from observability import observability  # type: ignore
        observability.initialize()
    except Exception:
        pass

    try:
        yield
    finally:
        is_last = CoreSingleton.remove_window(pid)
        if is_last:
            CoreSingleton.release_lock(pid)


def create_app() -> FastAPI:
    """FastAPI 앱 팩토리 — 모든 라우터와 미들웨어 부착."""
    app = FastAPI(
        title="ReCoder Local Core",
        version=VERSION,
        description="Local AI-assisted DevOps backend for the ReCoder VSCode extension.",
        lifespan=lifespan,
    )

    # CORS는 로컬 루프백만 허용 (§7 보안 정책)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # X-Session-Token 인증 (§4.2 / §15)
    app.add_middleware(SessionTokenMiddleware)

    # Q1~Q3 라우터
    app.include_router(health.router)
    app.include_router(analyze.router)
    app.include_router(deploy.router)
    app.include_router(ops.router)
    app.include_router(session.router)
    app.include_router(policy.router)
    app.include_router(ecs.router)

    # Q4 라우터
    app.include_router(gitops.router)
    app.include_router(incident.router)

    # Workbench 통합 라우터 (Discord ↔ Core ↔ VSCode 양방향 sync)
    app.include_router(workbench.router)

    return app


app = create_app()


def _handle_shutdown(signum, _frame) -> None:
    """SIGTERM/SIGINT 처리 — graceful shutdown (§6.3)."""
    print(f"[ReCoder Core] Received signal {signum}, initiating graceful shutdown...", flush=True)
    try:
        CoreSingleton.release_lock(os.getpid())
    except Exception:
        pass
    sys.exit(0)


def main() -> None:
    """메인 엔트리포인트 — uvicorn 직접 실행."""
    multiprocessing.freeze_support()  # PyInstaller 호환 (§4.2)
    load_dotenv()

    # ~/.recoder/ 디렉토리 초기화 (§21)
    try:
        from first_run import setup_recoder_home  # noqa: WPS433
        setup_recoder_home()
    except ImportError:
        # first_run 모듈이 없어도 코어는 동작해야 함
        Path.home().joinpath(".recoder").mkdir(parents=True, exist_ok=True)

    global _bound_port
    try:
        _bound_port = CoreSingleton.find_available_port()
    except RuntimeError as exc:
        print(f"[ReCoder Core] FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    # graceful shutdown 핸들러 등록
    atexit.register(lambda: CoreSingleton.release_lock(os.getpid()))
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
    except (ValueError, OSError):
        # Windows 메인 스레드 외부에서는 signal 등록 불가
        pass

    print(f"[ReCoder Core] Starting v{VERSION} on http://127.0.0.1:{_bound_port}", flush=True)
    print(f"[ReCoder Core] RECODER_HOME: {CoreSingleton.RECODER_HOME}", flush=True)

    try:
        uvicorn.run(
            "main:app",
            host="127.0.0.1",
            port=_bound_port,
            log_level="info",
            timeout_graceful_shutdown=10,
        )
    except KeyboardInterrupt:
        print("[ReCoder Core] Interrupted by user.", flush=True)
    except Exception as exc:
        print(f"[ReCoder Core] ERROR: {exc}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            CoreSingleton.release_lock(os.getpid())
        except Exception:
            pass
        print("[ReCoder Core] Stopped.", flush=True)


if __name__ == "__main__":
    main()
