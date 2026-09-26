"""docker run returning 0 must not hide the user's MODULE_NOT_FOUND crash."""
import asyncio, json, subprocess
from types import SimpleNamespace
import pytest
from api.routes import deploy as d
from schemas import DeploymentPlan, DeploymentRecord, DeployStatus, DeployMethod, ActionType


@pytest.mark.parametrize('state',[
    {'Status':'restarting','Running':True,'Restarting':True,'ExitCode':1},
    {'Status':'exited','Running':False,'Restarting':False,'ExitCode':1},
])
def test_restart_loop_and_exited_process_include_actual_masked_error(monkeypatch,state):
    def run(cmd,**kwargs):
        if cmd[1]=='inspect':return subprocess.CompletedProcess(cmd,0,json.dumps(state),'')
        return subprocess.CompletedProcess(cmd,0,'',"Cannot find module '/app/index.js'\npassword=private-test-password")
    monkeypatch.setattr(d.subprocess,'run',run)
    error=asyncio.run(d._local_startup_failure('fixture'))
    assert 'index.js' in error and 'private-test-password' not in error


@pytest.mark.parametrize('startup_error,status,restored',[
    ("Cannot find module '/app/index.js'",'failed',True),
    (None,'pending',False),
])
def test_failed_health_never_becomes_success_and_crash_restores_previous(monkeypatch,startup_error,status,restored):
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='fixture:v2',container_name='fixture',ports={'18117':'3000'},enable_continuous_verification=False)
    previous=DeploymentRecord(method=DeployMethod.LOCAL_DOCKER,project_id='fixture',image='fixture:v1',container_name='fixture',ports=plan.ports,status=DeployStatus.SUCCESS,rollback_eligible=True)
    monkeypatch.setattr(d,'_deployment_plans',{plan.plan_id:plan})
    monkeypatch.setattr(d,'_deployment_records',{})
    monkeypatch.setattr(d,'_plans_pending_image_scan',{})
    monkeypatch.setattr(d,'_plan_workspaces',{})
    monkeypatch.setattr(d,'_container_locks',{})
    monkeypatch.setattr(d,'_refresh_rollback_target',lambda _:('fixture:v1','previous'))
    monkeypatch.setattr(d,'_rollback_source_for',lambda *args:previous)
    monkeypatch.setattr(d,'_save_records',lambda:None)
    async def nothing(*args):return None
    async def unhealthy(*args):return False
    async def inspect(*args):return startup_error
    async def restore(*args):return True,'restored',''
    async def resumed(*args):return True
    async def prune(*args):return []
    for name in ['_build_local_image','_stop_prior_verifications_for_container','_remove_existing_local_container','_pin_rollback_image','_running_image_id']:
        monkeypatch.setattr(d,name,nothing)
    monkeypatch.setattr(d,'_verify_rollback_candidate_health',unhealthy)
    monkeypatch.setattr(d,'_local_startup_failure',inspect)
    monkeypatch.setattr(d,'_restore_prior_local_container',restore)
    monkeypatch.setattr(d,'_resume_verification_for',resumed)
    monkeypatch.setattr(d,'_prune_old_rollback_pins',prune)
    monkeypatch.setattr(d.subprocess,'run',lambda cmd,**kwargs:subprocess.CompletedProcess(cmd,0,'container-id',''))
    result=asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert result['status']==status and result['health_ok'] is False
    assert result['restored_previous'] is restored
    assert d._deployment_records[result['deployment_id']].status.value==status
    if startup_error:assert startup_error in result['stderr']


@pytest.mark.parametrize('verdict,expected',[('stable',DeployStatus.SUCCESS),('unstable',DeployStatus.FAILED)])
def test_background_verification_updates_pending_record(monkeypatch,verdict,expected):
    record=DeploymentRecord(method=DeployMethod.LOCAL_DOCKER,project_id='fixture',image='fixture:v1',container_name='fixture',status=DeployStatus.PENDING)
    monkeypatch.setattr(d,'_deployment_records',{record.deployment_id:record})
    monkeypatch.setattr(d,'_save_records',lambda:None)
    asyncio.run(d._update_rollback_candidate_after_verification(SimpleNamespace(deployment_id=record.deployment_id,status=verdict)))
    assert record.status==expected and record.rollback_eligible==(verdict=='stable')
