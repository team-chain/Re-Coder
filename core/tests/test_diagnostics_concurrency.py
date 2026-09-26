"""Slow provider probes must not block health or multiply across webviews."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import first_run
from api.routes import health as routes
from schemas import DiagnosticsResult, ReadyStatus


def test_slow_ai_probe_leaves_health_responsive(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def probe():
        started.set()
        assert release.wait(2), 'health was blocked by synchronous AI I/O'
        return ReadyStatus.OK, 'fixture', '', 'fixture', False
    monkeypatch.setattr(first_run, '_check_ai_ready_sync', probe)
    async def scenario():
        task = asyncio.create_task(first_run.check_ai_ready())
        try:
            while not started.is_set():
                await asyncio.sleep(.001)
            result = await asyncio.wait_for(routes.health(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(port=0)))), .2)
            assert result['status'] == 'ok'
        finally:
            release.set()
        assert (await task)[0] == ReadyStatus.OK
    asyncio.run(scenario())


def test_concurrent_probes_share_work_and_client_cancellation_is_isolated(monkeypatch):
    calls = []
    async def scenario():
        release = asyncio.Event()
        async def run_all(self):
            calls.append(1)
            await release.wait()
            return DiagnosticsResult()
        monkeypatch.setattr(routes.FirstRunDiagnostics, 'run_all', run_all)
        monkeypatch.setattr(routes, '_diagnostics_task', None)
        clients = [asyncio.create_task(routes.run_diagnostics()) for _ in range(3)]
        await asyncio.sleep(.02)
        assert len(calls) == 1
        clients[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await clients[0]
        release.set()
        results = await asyncio.gather(*clients[1:])
        assert results[0] is results[1]
        await routes.run_diagnostics()
        assert len(calls) == 2, 'explicit retry must perform a fresh probe'
    asyncio.run(scenario())


def test_failed_probe_can_retry(monkeypatch):
    async def fail(self):
        raise RuntimeError('fixture failure')
    async def scenario():
        monkeypatch.setattr(routes, '_diagnostics_task', None)
        monkeypatch.setattr(routes.FirstRunDiagnostics, 'run_all', fail)
        with pytest.raises(RuntimeError, match='fixture failure'):
            await routes.run_diagnostics()
        async def succeed(self):
            return DiagnosticsResult()
        monkeypatch.setattr(routes.FirstRunDiagnostics, 'run_all', succeed)
        assert isinstance(await routes.run_diagnostics(), DiagnosticsResult)
    asyncio.run(scenario())


def test_gateway_readiness_uses_gateway_without_aws(monkeypatch):
    from llm import gateway_provider
    monkeypatch.setattr(gateway_provider, 'gateway_enabled', lambda: True)
    monkeypatch.setattr(gateway_provider.GatewayProvider, '__init__', lambda self: setattr(self, '_timeout', 60))
    monkeypatch.setattr(gateway_provider.GatewayProvider, 'call', lambda self, request: SimpleNamespace(model_used='gateway-model'))
    result = asyncio.run(first_run.check_ai_ready())
    assert result == (ReadyStatus.OK, 'gateway-model', '', 'gateway', False)


def test_chat_passes_bounded_references_to_model(monkeypatch):
    from api.routes import analyze
    from llm import router
    requests = []
    def call(request, *args):
        requests.append(request)
        return SimpleNamespace(text='{"reply":"확인했습니다","action":null}', model_used='fixture')
    monkeypatch.setattr(router, 'get_router', lambda: SimpleNamespace(call=call))
    body = analyze.ChatRequest(message='이 파일 설명해줘', context_files=[{'path': 'app.ts', 'content': 'REFERENCE' + 'x' * 30000}])
    asyncio.run(analyze.chat_route(body))
    assert '파일: app.ts\nREFERENCE' in requests[0].prompt
    assert len(requests[0].prompt) < 23000
