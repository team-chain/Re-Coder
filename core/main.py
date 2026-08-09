"""
ReCoder v6.4 Local Core — FastAPI Entry Point (designed in section 4.2, 6)

Includes Hybrid Cloud Relay (section 6.4.2 flow 1) with DynamoDB queue + RelayPoller.
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
except ImportError:
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

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
    deploy_ecs,
    ops,
    session,
    policy,
    ecs,
    gitops,
    incident,
    workbench,
    relay,
    aws,
    github,
)

_bound_port: int = 0
VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: singleton lock + optional Hybrid Cloud Relay poller."""
    pid = os.getpid()

    lock_acquired = CoreSingleton.acquire_lock(pid)
    if not lock_acquired:
        existing = CoreSingleton.read_runtime()
        if existing:
            app.state.port = existing.port
            app.state.session_token = existing.session_token
            app.state.started_at = datetime.now(timezone.utc)
        else:
            port = _bound_port if _bound_port else CoreSingleton.find_available_port()
            token = os.environ.get("SESSION_TOKEN") or secrets.token_urlsafe(32)
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

    port = _bound_port if _bound_port else CoreSingleton.find_available_port()
    app.state.port = port

    # 토큰 안정화: 재시작해도 같은 토큰을 유지한다 (GitHub Secret 재등록 불필요).
    # runtime.json 은 정상 종료 시 삭제되므로, 종료에도 살아남는 별도 파일에 보관한다.
    # 우선순위: SESSION_TOKEN env > 영속 파일 > 기존 runtime.json > 새로 생성.
    from pathlib import Path as _TokPath
    _tok_file = _TokPath.home() / ".recoder" / ".session_token"
    _persisted_token = None
    try:
        if _tok_file.is_file():
            _persisted_token = (_tok_file.read_text(encoding="utf-8").strip() or None)
    except Exception:
        _persisted_token = None
    _existing_rt = CoreSingleton.read_runtime()
    _existing_token = getattr(_existing_rt, "session_token", None) if _existing_rt else None
    token = (
        os.environ.get("SESSION_TOKEN")
        or _persisted_token
        or _existing_token
        or secrets.token_urlsafe(32)
    )
    app.state.session_token = token
    try:
        _tok_file.parent.mkdir(parents=True, exist_ok=True)
        _tok_file.write_text(token, encoding="utf-8")
    except Exception:
        pass

    CoreSingleton.write_runtime(port=port, token=token, pid=pid)
    CoreSingleton.set_file_permissions(CoreSingleton.RUNTIME_FILE)
    CoreSingleton.set_file_permissions(CoreSingleton.LOCK_FILE)

    app.state.started_at = datetime.now(timezone.utc)

    try:
        from observability import observability  # type: ignore
        observability.initialize()
    except Exception:
        pass

    # Hybrid Cloud Relay poller (section 6.4.2 flow 1) - opt-in
    relay_poller = None
    if os.environ.get("RECODER_RELAY_ENABLED", "false").strip().lower() == "true":
        try:
            from relay.poller import RelayPoller  # type: ignore
            relay_poller = RelayPoller()
            start_result = relay_poller.start()
            try:
                from api.routes import relay as _relay_module  # type: ignore
                _relay_module._active_poller = relay_poller
            except Exception:
                pass
            print(f"[ReCoder Core] Relay poller: {start_result}", flush=True)
        except Exception as exc:
            print(f"[ReCoder Core] Relay poller init failed: {exc}", flush=True)
            relay_poller = None

    try:
        yield
    finally:
        if relay_poller is not None:
            try:
                await relay_poller.stop()
            except Exception:
                pass
        is_last = CoreSingleton.remove_window(pid)
        if is_last:
            CoreSingleton.release_lock(pid)


def create_app() -> FastAPI:
    app = FastAPI(
        title="ReCoder Local Core",
        version=VERSION,
        description="Local AI-assisted DevOps backend for the ReCoder VSCode extension.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # Strict whitelist — vscode webview + localhost(임의 포트) 만.
        # 정규식 패턴으로 임의 포트를 허용. allow_credentials=False 로 쿠키 차단.
        allow_origin_regex=r"^(vscode-webview://[^/]+|http://127\.0\.0\.1(:\d+)?|http://localhost(:\d+)?)$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── CSRF 보호 — Origin / Sec-Fetch-Site 검증 ─────────────────────
    # localhost 면제와 결합 시 브라우저 CSRF 가능성을 차단한다.
    # Extension(Node fetch) 은 Origin 헤더 없음 → 허용 (localhost 전제).
    # 브라우저 mutation 요청은 Origin 헤더 강제 → 화이트리스트만 허용.
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    import re as _re

    _ALLOWED_ORIGIN_RE = _re.compile(
        r"^(vscode-webview://[^/]+|http://127\.0\.0\.1(:\d+)?|http://localhost(:\d+)?)$"
    )

    class _CSRFOriginMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            method = (request.method or "").upper()
            # GET/HEAD/OPTIONS 는 mutation 아님 → 통과.
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                return await call_next(request)
            origin = request.headers.get("origin", "")
            sec_fetch_site = request.headers.get("sec-fetch-site", "")
            # 1) Origin 헤더 있고 화이트리스트 불일치 → 거부.
            if origin and not _ALLOWED_ORIGIN_RE.match(origin):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Origin '{origin}' not allowed for mutation."},
                )
            # 2) Sec-Fetch-Site: cross-site/same-site(다른 포트) 거부.
            #    'none' = 주소창 직접 입력 (CSRF 아님), 'same-origin' = 같은 origin, 둘 다 허용.
            if sec_fetch_site in ("cross-site", "same-site"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Cross-site mutation blocked (sec-fetch-site={sec_fetch_site})."},
                )
            return await call_next(request)

    app.add_middleware(_CSRFOriginMiddleware)
    app.add_middleware(SessionTokenMiddleware)

    app.include_router(health.router)
    app.include_router(analyze.router)
    app.include_router(deploy.router)
    app.include_router(ops.router)
    app.include_router(session.router)
    app.include_router(policy.router)
    app.include_router(ecs.router)
    # 확장이 부르는 /api/deploy/ecs* 호환 계층 (FR-05-04)
    app.include_router(deploy_ecs.router)
    app.include_router(gitops.router)
    app.include_router(incident.router)
    app.include_router(workbench.router)
    app.include_router(relay.router)
    app.include_router(aws.router)
    app.include_router(github.router)

    return app


app = create_app()


def _handle_shutdown(signum, _frame) -> None:
    print(f"[ReCoder Core] Received signal {signum}, initiating graceful shutdown...", flush=True)
    try:
        CoreSingleton.release_lock(os.getpid())
    except Exception:
        pass
    sys.exit(0)


def main() -> None:
    multiprocessing.freeze_support()
    load_dotenv()

    try:
        from first_run import setup_recoder_home  # noqa: WPS433
        setup_recoder_home()
    except ImportError:
        Path.home().joinpath(".recoder").mkdir(parents=True, exist_ok=True)

    # ~/.recoder/aws_credentials.json 가 있으면 프로세스 환경변수로 주입
    try:
        from api.routes.aws import _load_into_process_if_needed  # type: ignore
        _load_into_process_if_needed()
    except Exception as _aws_load_exc:
        print(f"[ReCoder Core] aws credentials preload skipped: {_aws_load_exc}", flush=True)

    global _bound_port
    try:
        _bound_port = CoreSingleton.find_available_port()
    except RuntimeError as exc:
        print(f"[ReCoder Core] FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    atexit.register(lambda: CoreSingleton.release_lock(os.getpid()))
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
    except (ValueError, OSError):
        pass

    print(f"[ReCoder Core] Starting v{VERSION} on http://127.0.0.1:{_bound_port}", flush=True)
    print(f"[ReCoder Core] RECODER_HOME: {CoreSingleton.RECODER_HOME}", flush=True)

    try:
        # PyInstaller 번들(frozen)에서는 "main:app" 모듈 재import가 안 되므로
        # app 객체를 직접 넘긴다. 개발 모드에서는 import 문자열을 그대로 사용.
        _target = app if getattr(sys, "frozen", False) else "main:app"
        uvicorn.run(
            _target,
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
