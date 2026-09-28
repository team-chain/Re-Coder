import asyncio
import json

import pytest
from fastapi import HTTPException
from api.routes import deploy as d
from deployment_progress import report, stream, _running
from schemas import ActionType, DeployMethod, DeploymentPlan


def decode(frame):
    return json.loads(frame.decode().split('data: ', 1)[1])


def test_actual_deploy_emits_build_before_completion_and_keeps_build_failure(monkeypatch):
    plan = DeploymentPlan(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN,
                          image='fixture:v1', container_name='fixture')
    monkeypatch.setattr(d, '_deployment_plans', {plan.plan_id: plan})
    monkeypatch.setattr(d, '_container_locks', {})
    monkeypatch.setattr(d, '_refresh_rollback_target', lambda _: (None, ''))

    async def scenario():
        release = asyncio.Event()
        async def build(*args):
            await release.wait()
            return {'status': 'failed', 'stage': 'build', 'stderr': 'npm install failed'}
        monkeypatch.setattr(d, '_build_local_image', build)
        response = await d.execute_deployment_stream(d.ExecuteRequest(plan_id=plan.plan_id, approved=True))
        iterator = response.body_iterator
        assert decode(await anext(iterator))['step'] == 'queued'
        assert decode(await anext(iterator))['step'] == 'build'
        assert not release.is_set()  # Already delivered while Docker is still busy.
        release.set()
        event = decode(await anext(iterator))
        assert event['plan_id'] == plan.plan_id
        assert event['result']['status'] == 'failed'
        assert event['result']['stderr'] == 'npm install failed'
        await iterator.aclose()
    asyncio.run(scenario())


def test_viewer_disconnect_does_not_cancel_approved_work():
    async def scenario():
        release, finished = asyncio.Event(), asyncio.Event()
        async def operation():
            report('start', 'starting')
            await release.wait()
            finished.set()
            return {'status': 'success'}
        response = stream(operation, 'one')
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(*list(_running))
    asyncio.run(scenario())


def test_concurrent_streams_are_isolated_and_policy_denial_stays_error():
    async def scenario():
        async def run(name):
            async def operation():
                report('scan', name)
                await asyncio.sleep(0)
                raise HTTPException(409, detail=name+' blocked')
            response = stream(operation, name)
            return [decode(frame) async for frame in response.body_iterator]
        for name, events in zip(['a','b'], await asyncio.gather(run('a'),run('b'))):
            assert all(e['plan_id']==name for e in events)
            assert events[1]['message']==name
            assert events[-1]['step']=='error' and events[-1]['status']==409
    asyncio.run(scenario())


def test_unknown_plan_never_opens_execution_stream():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(d.execute_deployment_stream(d.ExecuteRequest(plan_id='missing', approved=True)))
    assert exc.value.status_code==404
