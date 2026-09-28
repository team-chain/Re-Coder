"""Canvas is a read-only view over real deployments; no fabricated health or scan success."""
from types import SimpleNamespace
import asyncio

from core.schemas import ECSDeployRecord, ECSDeployRequest, SecurityScanResult, SecurityFinding, SecurityScanTool, SecurityScanSeverity
from api.routes.canvas import same_workspace, scan_projection, read_topology, canvas_snapshot


def record(workspace="/project"):
    return ECSDeployRecord(cluster="actual-cluster",service="actual-service",region="us-east-1",
        request=ECSDeployRequest(project_id="p",workspace_path=workspace,cluster="actual-cluster",service="actual-service",region="us-east-1",image="app:v1"))


def test_workspace_filter_never_reuses_another_projects_resources(tmp_path):
    a=tmp_path/"a"
    b=tmp_path/"b"
    assert same_workspace(record(str(a)),str(a))
    assert not same_workspace(record(str(a)),str(b))
    assert not same_workspace(ECSDeployRecord(),str(a))


def test_unrun_or_missing_tools_never_render_as_passed():
    r=record()
    assert [t["state"] for t in scan_projection(r)["tools"][:3]] == ["pending"]*3
    r.scan_result=SecurityScanResult(repo_path="/project",image="app:v1",tool_errors=["trivy_not_installed"])
    tools={t["tool"]:t for t in scan_projection(r)["tools"]}
    assert tools["trivy"]["state"]=="unverified"
    assert tools["hadolint"]["state"]=="pending"
    assert tools["gitleaks"]["state"]=="passed"


def test_secret_values_are_never_projected_into_canvas_metadata():
    r=record()
    r.scan_result=SecurityScanResult(repo_path="/project",findings=[SecurityFinding(tool=SecurityScanTool.GITLEAKS,severity=SecurityScanSeverity.CRITICAL,title="secret_leak: secret-value",description="secret-value",location="app.py:4",fix_suggestion="secret-value")])
    r.scan_result.compute_pass()
    projection=scan_projection(r)
    assert "secret-value" not in str(projection)
    assert projection["blocked"]
    assert projection["findings"][0]["location"]=="app.py:4"


def test_rejected_or_not_started_policy_is_not_shown_as_accepted():
    r=record()
    assert scan_projection(r)["tools"][-1]["state"]=="pending"
    r.error_message="정책 통과 실패(deny) — requires review"
    result=scan_projection(r)
    assert result["tools"][-1]["state"]=="findings" and result["blocked"]


def test_real_task_identity_and_count_are_kept_when_desired_differs():
    class Client:
        def describe_services(self,**kwargs):
            assert kwargs=={"cluster":"actual-cluster","services":["actual-service"]}
            return {"services":[{"serviceName":"actual-service","desiredCount":3,"runningCount":1,"taskDefinition":"td:9"}]}
        def list_tasks(self,**kwargs): return {"taskArns":["arn/task/real-id"]}
        def describe_tasks(self,**kwargs): return {"tasks":[{"taskArn":"arn/task/real-id","lastStatus":"RUNNING","healthStatus":"HEALTHY","containers":[{"image":"app:v1","imageDigest":"sha256:real"}]}]}
    result=read_topology(SimpleNamespace(client=lambda *a,**k:Client()),record())
    assert result["desired"]==3 and result["running"]==1
    assert len(result["tasks"])==1 and result["tasks"][0]["id"]=="real-id"
    assert result["tasks"][0]["images"][0]["digest"]=="sha256:real"


def test_disconnected_snapshot_does_not_query_aws_or_leak_other_workspace(monkeypatch,tmp_path):
    from api.routes import aws,ecs
    async def status(): return SimpleNamespace(ready=False,identity=None,region="us-east-1")
    monkeypatch.setattr(aws,"get_aws_status",status)
    monkeypatch.setattr(aws,"_build_boto3_session",lambda **kw:(_ for _ in ()).throw(AssertionError("AWS access")))
    monkeypatch.setattr(ecs,"_deploy_records",{"other":record(str(tmp_path/"other"))})
    result=asyncio.run(canvas_snapshot(str(tmp_path/"current"),"project"))
    assert result["resource"] is None and result["topology"] is None
    assert result["aws"]=={"ready":False,"region":"","account":""}


def test_topology_failure_keeps_record_and_explicit_unknown(monkeypatch,tmp_path):
    from api.routes import aws,ecs
    r=record(str(tmp_path))
    async def status(): return SimpleNamespace(ready=True,identity=SimpleNamespace(account="123"),region="us-east-1")
    monkeypatch.setattr(aws,"get_aws_status",status)
    monkeypatch.setattr(aws,"_build_boto3_session",lambda **kw:SimpleNamespace(client=lambda *a,**k:(_ for _ in ()).throw(RuntimeError("denied"))))
    monkeypatch.setattr(ecs,"_deploy_records",{"this":r})
    result=asyncio.run(canvas_snapshot(str(tmp_path),""))
    assert result["resource"]["cluster"]=="actual-cluster"
    assert result["topology"] is None
    assert result["warnings"]


def test_approval_target_uses_current_revision_and_actual_image_digest():
    from api.routes.canvas import read_target
    class Client:
        def describe_services(self,**kw): return {"services":[{"taskDefinition":"td:current"}]}
        def describe_task_definition(self,**kw):
            raise AssertionError("Running service must work with the previously granted runtime-task permissions")
        def list_tasks(self,**kw): return {"taskArns":["current-task","old-task"]}
        def describe_tasks(self,**kw): return {"tasks":[{"taskDefinitionArn":"td:current","containers":[{"image":"app:v3","imageDigest":"sha256:current"}]},{"taskDefinitionArn":"td:old","containers":[{"image":"app:v2","imageDigest":"sha256:old"}]}]}
    data=read_target(SimpleNamespace(client=lambda *a,**kw:Client()),"us-east-1","cluster","service")
    assert data["task_definition"]=="td:current"
    assert data["images"]==[{"image":"app:v3","digest":"sha256:current"}]


def test_new_service_has_no_invented_rollback_revision():
    from api.routes.canvas import read_target
    client=SimpleNamespace(describe_services=lambda **kw:{"services":[],"failures":[{"reason":"MISSING"}]})
    data=read_target(SimpleNamespace(client=lambda *a,**kw:client),"us-east-1","cluster","new")
    assert data=={"exists":False,"task_definition":"","images":[]}


def test_github_canvas_opt_out_keeps_legacy_auto_commit_default(monkeypatch):
    import sys
    from api.routes import github
    calls=[]
    class Agent:
        _token="fixture"
        def push(self,*args,**kwargs):
            calls.append((args,kwargs))
            return {"status":"ok"}
    monkeypatch.setitem(sys.modules,"github_agent",SimpleNamespace(get_github_agent=lambda:Agent()))
    asyncio.run(github.git_push_route({"workspace_path":"/project","branch":"main"}))
    asyncio.run(github.git_push_route({"workspace_path":"/project","branch":"main","auto_commit":False}))
    assert calls[0][1]["auto_commit"] is True
    assert calls[1][1]["auto_commit"] is False


def test_discord_event_is_explicit_deduplicated_and_never_mentions_users(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    spec=importlib.util.spec_from_file_location("canvas_events_test",Path(__file__).parents[2]/"discord-bot"/"canvas_events.py")
    module=importlib.util.module_from_spec(spec)
    # The core environment does not install the bot's separate aiohttp dependency.
    monkeypatch.setitem(sys.modules,"aiohttp",SimpleNamespace(web=SimpleNamespace(json_response=lambda body,status=200:SimpleNamespace(status=status,body=body))))
    spec.loader.exec_module(module)
    mentions=object()
    monkeypatch.setitem(sys.modules,"discord",SimpleNamespace(Embed=lambda **kw:kw,AllowedMentions=SimpleNamespace(none=lambda:mentions)))
    calls=[]
    class Channel:
        async def send(self,**kwargs): calls.append(kwargs)
    bot=SimpleNamespace(get_channel=lambda channel_id:Channel())
    class Request:
        async def json(self): return {"event_id":"deployment:1:done","title":"완료","detail":"service"}
    async def run():
        denied=await module.send_canvas_event(Request(),authorized=False,bot=bot,channel_id=1)
        assert denied.status==401 and not calls
        missing=await module.send_canvas_event(Request(),authorized=True,bot=bot,channel_id=0)
        assert missing.status==409 and not calls
        for _ in range(2):
            response=await module.send_canvas_event(Request(),authorized=True,bot=bot,channel_id=1)
            assert response.status==200
    asyncio.run(run())
    assert len(calls)==1 and calls[0]["allowed_mentions"] is mentions
