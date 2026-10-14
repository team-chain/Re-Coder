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


@pytest.mark.parametrize('scan,expected',[
    ({'status':'not_run','summary':'확인하지 못했습니다 — 스캐너 이미지 또는 취약점 DB 를 내려받지 못했습니다.'},'unverified'),
    ({'status':'ok','critical_count':0,'high_count':2},'passed'),
])
def test_result_reports_post_build_scan_state(monkeypatch,scan,expected):
    from schemas import ApprovalLevel
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='fixture:v2',container_name='fixture',ports={'18118':'3000'},enable_continuous_verification=False,approval_level=ApprovalLevel.DOUBLE_CONFIRM)
    monkeypatch.setattr(d,'_deployment_plans',{plan.plan_id:plan})
    monkeypatch.setattr(d,'_deployment_records',{})
    monkeypatch.setattr(d,'_plans_pending_image_scan',{plan.plan_id:plan.image})
    monkeypatch.setattr(d,'_plan_workspaces',{})
    monkeypatch.setattr(d,'_container_locks',{})
    monkeypatch.setattr(d,'_refresh_rollback_target',lambda _:(None,'none'))
    monkeypatch.setattr(d,'_rollback_source_for',lambda *args:None)
    monkeypatch.setattr(d,'_save_records',lambda:None)
    async def nothing(*args):return None
    async def healthy(*args):return True
    async def image_id(image):return image
    async def run_scan(*args):return scan
    async def prune(*args):return []
    for name in ['_build_local_image','_stop_prior_verifications_for_container','_remove_existing_local_container','_pin_rollback_image','_running_image_id','_local_startup_failure']:
        monkeypatch.setattr(d,name,nothing)
    monkeypatch.setattr(d,'_verify_rollback_candidate_health',healthy)
    monkeypatch.setattr(d,'_local_image_id',image_id)
    monkeypatch.setattr(d,'_execute_scan',run_scan)
    monkeypatch.setattr(d,'_prune_old_rollback_pins',prune)
    monkeypatch.setattr(d.subprocess,'run',lambda cmd,**kwargs:subprocess.CompletedProcess(cmd,0,'container-id',''))
    result=asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert result['security_scan']['status']==expected
    if expected=='unverified':assert '내려받지' in result['security_scan']['reason']
    else:assert result['security_scan']['high_count']==2


@pytest.mark.parametrize('screen,status,restored',[
    ({'ok':False,'problems':['첫 화면이 HTTP 404 입니다: ENOENT client/dist/index.html'],'diagnosis':{'code':'SCREEN_NOT_HTML','title':'배포 주소는 열리지만 화면이 표시되지 않습니다'}},'failed',True),
    ({'ok':True,'problems':[]},'success',False),
    (None,'success',False),
])
def test_health_ok_but_blank_screen_is_not_success(monkeypatch,screen,status,restored):
    """실기기: /health 는 200 인데 화면이 없는 배포가 성공으로 끝났다. 화면 확인이 실패하면 failed 다."""
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='fixture:v2',container_name='fixture',ports={'18119':'3000'},enable_continuous_verification=False)
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
    async def healthy(*args):return True
    async def restore(*args):return True,'restored',''
    async def resumed(*args):return True
    async def prune(*args):return []
    async def screen_check(*args):return screen
    for name in ['_build_local_image','_stop_prior_verifications_for_container','_remove_existing_local_container','_pin_rollback_image','_running_image_id','_local_startup_failure']:
        monkeypatch.setattr(d,name,nothing)
    monkeypatch.setattr(d,'_verify_rollback_candidate_health',healthy)
    monkeypatch.setattr(d,'_verify_local_screen',screen_check)
    monkeypatch.setattr(d,'_restore_prior_local_container',restore)
    monkeypatch.setattr(d,'_resume_verification_for',resumed)
    monkeypatch.setattr(d,'_prune_old_rollback_pins',prune)
    monkeypatch.setattr(d.subprocess,'run',lambda cmd,**kwargs:subprocess.CompletedProcess(cmd,0,'container-id',''))
    result=asyncio.run(d.execute_deployment(d.ExecuteRequest(plan_id=plan.plan_id,approved=True)))
    assert result['status']==status and result['restored_previous'] is restored
    assert d._deployment_records[result['deployment_id']].status.value==status
    if screen and not screen['ok']:
        assert result['diagnosis']['code']=='SCREEN_NOT_HTML' and result['health_ok'] is False and 'ENOENT' in result['stderr']
    if screen is None:
        assert 'screen' not in result


def test_api_only_server_is_not_judged_by_screen(monkeypatch,tmp_path):
    """화면이 없는 API 서버는 '/' 가 JSON·404 여도 정상 — 화면 확인이 배포를 막지 않는다."""
    import screen_check
    (tmp_path/'package.json').write_text('{"name":"api","scripts":{"start":"node s.js"}}',encoding='utf-8')
    (tmp_path/'s.js').write_text("require('express')().listen(3000)",encoding='utf-8')
    monkeypatch.setenv('RECODER_SCREEN_CHECK','1')
    monkeypatch.setattr(screen_check,'check_screen',lambda url,**kw:screen_check.ScreenResult(ok=False,url=url,code='SCREEN_NOT_HTML',problems=['404']))
    plan=DeploymentPlan(method=DeployMethod.LOCAL_DOCKER,action=ActionType.DOCKER_RUN,image='x:1',container_name='x',ports={'18120':'3000'})
    assert asyncio.run(d._verify_local_screen(plan,str(tmp_path))) is None


def test_crash_loop_that_looks_running_is_a_failure(monkeypatch):
    """시작하자마자 죽고 재시작 정책으로 다시 뜨는 서버는 확인하는 순간 running 으로 보인다(실측).
    재시작 횟수와 잠깐 뒤 상태로 가른다 — 느린 시작으로 두면 이전 버전이 복구되지 않는다."""
    seen = {'n': 0}
    def run(cmd, **kwargs):
        if cmd[1] == 'inspect':
            seen['n'] += 1
            count = 0 if seen['n'] == 1 else 2
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'State': {'Status': 'running', 'Running': True, 'Restarting': False, 'ExitCode': 0}, 'RestartCount': count}), '')
        return subprocess.CompletedProcess(cmd, 0, '', 'Error: boom at start\n    at Object.<anonymous> (/app/index.js:6:7)')
    monkeypatch.setattr(d.subprocess, 'run', run)
    import time
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    error = asyncio.run(d._local_startup_failure('fixture'))
    assert error and 'boom at start' in error and '2번 다시 시작됨' in error


def test_steady_slow_start_stays_pending(monkeypatch):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'State': {'Status': 'running', 'Running': True, 'Restarting': False}, 'RestartCount': 0}), '')
    monkeypatch.setattr(d.subprocess, 'run', run)
    import time
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    assert asyncio.run(d._local_startup_failure('fixture')) is None
