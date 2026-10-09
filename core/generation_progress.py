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

        def put(event: dict) -> None:
            with lock:
                loop.call_soon_threadsafe(queue.put_nowait, event)

        def work():
            token = _reporter.set(put)
            try:
                return operation()
            finally:
                _reporter.reset(token)

        try:
            result = await asyncio.to_thread(contextvars.copy_context().run, work)
            queue.put_nowait({"step": "done", "result": result})
        except Exception as exc:  # noqa: BLE001
            payload = {"step": "error", "message": str(exc) or type(exc).__name__}
            extra = getattr(exc, "progress_payload", None)
            if callable(extra):
                payload.update(extra())
            queue.put_nowait(payload)
        finally:
            queue.put_nowait(sentinel)

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
