"""코드 생성 진행 이벤트 — 요청 하나에 묶인 보고 채널과 SSE 스트림.

배포 진행(deployment_progress)과 같은 모양이지만 이벤트가 더 풍부하다(에이전트·파일·조각).
생성은 수십 분 걸릴 수 있으므로 확장은 HTTP 응답 하나를 기다리지 않고 이 스트림을 구독한다.
보는 쪽이 끊겨도 작업은 끝까지 간다 — 결과는 체크포인트에 남고 이어 받을 수 있다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
import time
from typing import Any, Callable

_reporter: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "generation_reporter", default=None,
)
_running: set[asyncio.Task] = set()
log = logging.getLogger(__name__)


def report(event: dict) -> None:
    """현재 요청의 구독자에게 이벤트 하나를 보낸다. 구독자가 없으면 조용히 버린다."""
    callback = _reporter.get()
    if callback is None:
        return
    try:
        callback(dict(event))
    except Exception:  # noqa: BLE001 — 보고 실패가 생성을 멈추면 안 된다
        log.debug("generation progress report failed", exc_info=True)


def current() -> Callable[[dict], None]:
    """작업 스레드로 넘길 보고 함수. 컨텍스트 변수는 스레드 풀로 자동 전파되지 않는다."""
    callback = _reporter.get()

    def emit(event: dict) -> None:
        if callback is None:
            return
        try:
            callback(dict(event))
        except Exception:  # noqa: BLE001
            log.debug("generation progress emit failed", exc_info=True)

    return emit


def bind(callback: Callable[[dict], None] | None):
    """테스트·동기 호출자용: 이 컨텍스트의 보고 채널을 지정한다."""
    return _reporter.set(callback)


def stream(operation: Callable[[], Any], job_id: str):
    """operation(동기 함수)을 스레드에서 실행하며 진행 이벤트를 SSE 로 흘린다."""
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        lock = threading.Lock()

        started = time.monotonic()

        def send(item: Any) -> None:
            with lock:
                loop.call_soon_threadsafe(queue.put_nowait, item)

        def put(event: dict) -> None:
            #: 2.0.45 모든 이벤트에 같은 시계(요청 시작부터 초)를 붙인다 — 엔진 밖 단계(전체 점검·테스트)도
            #: 화면의 시간·다시 보기에 들어가고, 엔진 이벤트와 순서가 어긋나지 않는다.
            send({**event, "elapsed": round(time.monotonic() - started, 1)})

        def finish(payload: dict) -> None:
            #: 마지막 결과(done/error)와 끝 표시도 **작업 스레드에서 같은 통로로** 보낸다. 예전에는 루프 쪽에서
            #: 바로 넣어서, 작업이 빨리 끝나면 직전 진행 이벤트(전체 점검·테스트 결과)가 done 뒤로 밀려 버려졌다.
            send(payload)
            send(sentinel)

        def work() -> None:
            token = _reporter.set(put)
            try:
                result = operation()
            except Exception as exc:  # noqa: BLE001
                payload = {"step": "error", "message": str(exc) or type(exc).__name__}
                extra = getattr(exc, "progress_payload", None)
                if callable(extra):
                    try:
                        payload.update(extra())
                    except Exception:  # noqa: BLE001
                        log.debug("progress payload failed", exc_info=True)
                finish(payload)
                return
            finally:
                _reporter.reset(token)
            finish({"step": "done", "result": result})

        try:
            await asyncio.to_thread(contextvars.copy_context().run, work)
        except BaseException as exc:  # noqa: BLE001 — 스레드를 못 띄운 경우 등: 보는 쪽이 끝없이 기다리지 않게
            queue.put_nowait({"step": "error", "message": str(exc) or type(exc).__name__})
            queue.put_nowait(sentinel)
            if not isinstance(exc, Exception):
                raise

    async def events():
        task = asyncio.create_task(run())
        _running.add(task)
        task.add_done_callback(_running.discard)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if event is sentinel:
                return
            yield ("data: " + json.dumps({**event, "job_id": job_id}, ensure_ascii=False, default=str) + "\n\n").encode("utf-8")

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
