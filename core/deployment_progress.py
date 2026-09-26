"""Request-local deployment progress; disconnecting a viewer never cancels work."""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from typing import Callable

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

_reporter: ContextVar[Callable[[dict], None] | None] = ContextVar('deployment_reporter', default=None)
_running: set[asyncio.Task] = set()


def report(step: str, message: str) -> None:
    callback = _reporter.get()
    if callback:
        callback({'step': step, 'message': message})


def stream(operation, plan_id: str) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def run():
        token = _reporter.set(queue.put_nowait)
        try:
            report('queued', '승인된 배포 작업을 준비합니다')
            result = await operation()
            queue.put_nowait({'step': 'done', 'result': result})
        except HTTPException as exc:
            queue.put_nowait({'step': 'error', 'message': str(exc.detail), 'status': exc.status_code})
        except Exception:
            logging.getLogger(__name__).exception('Deployment stream execution failed')
            queue.put_nowait({'step': 'error', 'message': '배포 실행 중 오류가 발생했습니다. Core 로그와 배포 상태를 확인하세요.'})
        finally:
            _reporter.reset(token)
            queue.put_nowait(sentinel)

    async def events():
        task = asyncio.create_task(run())
        _running.add(task)
        task.add_done_callback(_running.discard)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield b': heartbeat\n\n'
                continue
            if event is sentinel:
                return
            yield ('data: ' + json.dumps({**event, 'plan_id': plan_id}, ensure_ascii=False) + '\n\n').encode('utf-8')

    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
