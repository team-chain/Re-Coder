"""Regressions found while checking the actual local deployment path."""
import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from deployment_inputs import dockerfile_runtime_port


@pytest.mark.parametrize('dockerfile,expected', [
    ('FROM node:22 AS build\nEXPOSE 3000\nFROM nginx:alpine\nEXPOSE 8080/tcp\n',8080),
    ('FROM node:22 AS base\nEXPOSE 3000\nFROM base AS runtime\n',3000),
    ('FROM node:22 AS builder\nEXPOSE 3000\nFROM scratch\n',None),
    ('FROM node:22\nEXPOSE 0 65536\n',None),
    ('FROM node:22\nEXPOSE 3000 9000\n',None),
    ('FROM node:22\nEXPOSE 53/udp 8080/tcp\n',8080),
])
def test_port_detection_uses_unambiguous_final_stage(tmp_path, dockerfile, expected):
    path=tmp_path/'Dockerfile'
    path.write_text(dockerfile,encoding='utf-8')
    assert dockerfile_runtime_port(path)==expected


def test_generated_static_ecs_port_mismatch_stops_before_aws(tmp_path, monkeypatch):
    from registry import FileTemplateRegistry
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRequest, ECSDeployStatus
    content=FileTemplateRegistry().render('Dockerfile.node-static',{'PORT':'3000','OUTPUT_DIR':'build'})
    (tmp_path/'Dockerfile').write_text(content,encoding='utf-8')
    agent=ECSAgent()
    def forbidden(*args):raise AssertionError('AWS must not be called for mismatched port')
    monkeypatch.setattr(agent,'_clients',forbidden)
    req=ECSDeployRequest(project_id='p',cluster='c',service='s',region='us-east-1',workspace_path=str(tmp_path),container_port=8000)
    result=asyncio.run(agent.deploy(req))
    assert result.status==ECSDeployStatus.FAILED
    assert '3000' in result.error_message and '3000' in result.error_remedy

from api.routes import deploy as d
from agents.deploy_agent import DeployAgent
from deployment_inputs import workspace_container_name
from schemas import ActionType, ApprovalLevel, DeploymentPlan, DeployMethod


@pytest.mark.parametrize('fields,ports', [({'host_port': 4567}, {'4567':'3000'}), ({'container_port': 5678}, {'3000':'5678'})])
def test_partial_port_override_is_preserved(tmp_path, fields, ports):
    (tmp_path/'Dockerfile').write_text('FROM node:22\nEXPOSE 3000\n')
    req=d.DeployPlanRequest(workspace_path=str(tmp_path),project_id='board',env={'MODE':'test'},health_check_path='/ready',enable_continuous_verification=False,**fields)
    plan=asyncio.run(DeployAgent().create_plan(req))
    assert plan.ports==ports
    assert plan.env=={'MODE':'test'} and plan.health_check_path=='/ready'
    assert plan.project_id=='board' and plan.enable_continuous_verification is False


@pytest.mark.parametrize('field', ['host_port','container_port'])
@pytest.mark.parametrize('port', [0,-1,65536])
def test_invalid_port_rejected_before_deployment(field, port):
    with pytest.raises(ValidationError):d.DeployPlanRequest(workspace_path='/sample',**{field:port})


def test_unicode_project_names_work_and_do_not_collide():
    first=workspace_container_name('/tmp/게시판')
    assert first.isascii() and first!=workspace_container_name('/tmp/테스트')
    assert workspace_container_name('/tmp/My App')=='my-app'
    assert d._default_image_name('/tmp/게시판')==first+':latest'


def test_local_plan_detects_real_next_health_route_and_preserves_explicit_path(tmp_path):
    (tmp_path/'Dockerfile').write_text('FROM node:22\nEXPOSE 3000\n')
    source=tmp_path/'app/api/health/route.ts'
    source.parent.mkdir(parents=True)
    source.write_text('export function GET() { return Response.json({ok: true}); }')
    for options,expected in [({},'/api/health'),({'health_check_path':'/health'},'/health'),({'health_check_path':'/ready'},'/ready')]:
        request=d.DeployPlanRequest(workspace_path=str(tmp_path),**options)
        plan=asyncio.run(DeployAgent().create_plan(request))
        assert plan.health_check_path==expected


def test_invalid_environment_rejected_before_build(monkeypatch):
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='app:v1',container_name='app',env={'BAD NAME':'value'})
    monkeypatch.setattr(d,'_deployment_plans',{plan.plan_id:plan})
    async def forbidden(*args):raise AssertionError('must not build invalid plan')
    monkeypatch.setattr(d,'_build_local_image',forbidden)
    with pytest.raises(HTTPException) as error:asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert error.value.status_code==400


def test_approved_run_uses_scanned_image_all_ports_and_environment(monkeypatch):
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='localhost:5000/app:v1',container_name='a.b',ports={'4567':'3000','4568':'3001'},env={'MODE':'test'},command_template_id='docker_run',enable_continuous_verification=False)
    monkeypatch.setattr(d,'_deployment_plans',{plan.plan_id:plan})
    monkeypatch.setattr(d,'_plans_pending_image_scan',{plan.plan_id:plan.image})
    monkeypatch.setattr(d,'_plan_workspaces',{plan.plan_id:'/fixture'})
    monkeypatch.setattr(d,'_deployment_records',{})
    monkeypatch.setattr(d,'_container_locks',{})
    image_id='sha256:'+'b'*64
    events=[]
    async def nothing(*args):return None
    async def healthy(*args):return True
    async def fixed_id(*args):return image_id
    async def scan(*args):
        events.append(('scan',args[2]))
        return {'status':'ok','critical_count':0}
    for name in ['_build_local_image','_capture_running_local_container','_stop_prior_verifications_for_container','_remove_existing_local_container','_pin_rollback_image']:
        monkeypatch.setattr(d,name,nothing)
    monkeypatch.setattr(d,'_verify_rollback_candidate_health',healthy)
    monkeypatch.setattr(d,'_local_image_id',fixed_id)
    monkeypatch.setattr(d,'_running_image_id',fixed_id)
    monkeypatch.setattr(d,'_execute_scan',scan)
    monkeypatch.setattr(d,'_refresh_rollback_target',lambda p:(None,''))
    monkeypatch.setattr(d,'_rollback_source_for',lambda *args:None)
    monkeypatch.setattr(d,'_save_records',lambda:None)
    async def prune(*args):return []
    monkeypatch.setattr(d,'_prune_old_rollback_pins',prune)
    def run(cmd,**kwargs):
        events.append(('run',cmd))
        return subprocess.CompletedProcess(cmd,0,'container-id','')
    monkeypatch.setattr(d.subprocess,'run',run)
    result=asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert result['status']=='success' and result['health_ok'] is True
    assert events[0]==('scan',image_id)
    command=events[1][1]
    assert command[-1]==image_id and 'MODE=test' in command
    assert '4567:3000' in command and '4568:3001' in command
    assert not d._plans_pending_image_scan and not d._plan_workspaces
    assert result['continuous_verification']=={'enabled':False,'started':False}


def test_new_scan_failure_needs_new_approval_before_touching_container(monkeypatch):
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='app:v1',container_name='app',approval_level=ApprovalLevel.CONFIRM)
    monkeypatch.setattr(d,'_deployment_plans',{plan.plan_id:plan})
    monkeypatch.setattr(d,'_plans_pending_image_scan',{plan.plan_id:plan.image})
    async def built(*args):return None
    async def image(*args):return 'sha256:'+'a'*64
    async def unavailable(*args):return {'status':'error','message':'scanner unavailable'}
    monkeypatch.setattr(d,'_build_local_image',built)
    monkeypatch.setattr(d,'_local_image_id',image)
    monkeypatch.setattr(d,'_execute_scan',unavailable)
    with pytest.raises(HTTPException) as error:asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert error.value.status_code==409
    assert plan.plan_id in d._plans_pending_image_scan
