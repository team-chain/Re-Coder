"""A3: pipeline events, persistence and the extension polling contract, no AWS."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from core.agents.ecs_agent import ECSAgent
from core.agents.ecs_progress import initialize_progress, advance_progress, finish_progress
from core.agents import ecs_build
from core.aws_infra import InfraError
from core.schemas import ECSDeployRecord, ECSDeployRequest, ECSDeployStatus
from api.routes import deploy_ecs, ecs


def request(**changes):
    return ECSDeployRequest(**dict({"project_id": "test", "region": "us-east-1", "cluster": "test", "service": "test", "image": "test:v1", "workspace_path": "/fixture", "run_preflight": True, "run_security_scan": True, "generate_sbom": True}, **changes))


def by_key(record):
    return {step.key: step for step in record.progress_steps}


@pytest.fixture
def pipeline(monkeypatch):
    """Keep the real deployment driver and executor; replace external operations."""
    import core.agents.ecs_agent as module
    seen = []
    rec = ECSDeployRecord()
    agent = ECSAgent(on_progress=lambda: seen.append(rec.model_dump(mode="json")))
    monkeypatch.setattr(agent, "_clients", lambda region: {"ecr": object()})
    monkeypatch.setattr(module, "configure_health_check", lambda req: "")
    monkeypatch.setattr(agent, "_warn_if_resources_may_be_running", lambda *args: None)
    monkeypatch.setattr(agent, "_scan_gate_message", lambda result: None)
    monkeypatch.setattr(module.aws_infra, "ensure_ecr_repository", lambda *args: "repo/test")

    async def passing(req, record, *args):
        record.preflight_passed = True
        return record
    for name in ["_step_preflight", "_step_provision", "_step_scan_sources", "_step_security_scan", "_step_sbom", "_step_ensure_service", "_stop_after_cancel"]:
        monkeypatch.setattr(agent, name, passing)
    async def register(*args): return "task:2", "task:1"
    async def stable(*args): return True, 0, False
    async def url(req, record, clients): record.service_url = "http://example.test:3456"
    monkeypatch.setattr(agent, "_step_register_task_definition", register)
    monkeypatch.setattr(agent, "_step_poll_deployment", stable)
    monkeypatch.setattr(agent, "_step_resolve_url", url)

    def build(*args, on_progress, **kwargs):
        on_progress("building")
        on_progress("ecr_push")
        return ecs_build.PushResult("repo/test:v1", "test:v1")
    monkeypatch.setattr(ecs_build, "build_and_push", build)
    return agent, rec, seen


def test_actual_pipeline_exposes_all_stages_in_order(pipeline):
    agent, rec, snapshots = pipeline
    result = asyncio.run(agent.deploy(request(), rec))
    assert result is rec
    assert result.status == ECSDeployStatus.SUCCEEDED
    stages = []
    for snapshot in snapshots:
        current = next((s["key"] for s in snapshot["progress_steps"] if s["status"] == "running"), None)
        if current and (not stages or stages[-1] != current): stages.append(current)
    assert stages == ["preflight", "provisioning", "source_scan", "building", "ecr_push", "image_scan", "sbom", "task_def", "svc_update", "stabilizing", "url_check"]
    assert all(s.status == "done" and s.started_at and s.finished_at for s in rec.progress_steps)
    response = deploy_ecs.to_status_response(rec)
    assert response.service_url == "http://example.test:3456"
    assert response.stage == "done" and response.running is False
    assert response.steps == rec.progress_steps


@pytest.mark.parametrize("method,key", [("_step_preflight", "preflight"), ("_step_provision", "provisioning"), ("_step_scan_sources", "source_scan"), ("_step_register_task_definition", "task_def"), ("_step_ensure_service", "svc_update"), ("_step_poll_deployment", "stabilizing")])
def test_failure_marks_only_reached_stage_and_keeps_reason(pipeline, monkeypatch, method, key):
    agent, rec, _ = pipeline
    async def fail(*args): raise InfraError("stage failed", detail="specific reason", remedy="repair it")
    monkeypatch.setattr(agent, method, fail)
    asyncio.run(agent.deploy(request(), rec))
    assert rec.status == ECSDeployStatus.FAILED
    assert by_key(rec)[key].status == "failed"
    assert not any(s.status == "running" for s in rec.progress_steps)
    if key != "stabilizing": assert by_key(rec)["url_check"].status == "pending"
    response = deploy_ecs.to_status_response(rec)
    assert (response.error, response.error_detail, response.remedy) == ("stage failed", "specific reason", "repair it")


def test_push_failure_from_worker_is_attributed_to_upload(pipeline, monkeypatch):
    agent, rec, _ = pipeline
    def fail(*args, on_progress, **kwargs):
        on_progress("ecr_push")
        raise InfraError("push failed")
    monkeypatch.setattr(ecs_build, "build_and_push", fail)
    asyncio.run(agent.deploy(request(), rec))
    assert by_key(rec)["building"].status == "done"
    assert by_key(rec)["ecr_push"].status == "failed"
    assert by_key(rec)["image_scan"].status == "pending"


def test_cancel_is_not_a_successful_stage(pipeline, monkeypatch):
    agent, rec, _ = pipeline
    async def provision(req, record, clients): record.cancel_requested = True
    monkeypatch.setattr(agent, "_step_provision", provision)
    asyncio.run(agent.deploy(request(), rec))
    assert rec.status == ECSDeployStatus.CANCELLED
    assert by_key(rec)["provisioning"].status == "cancelled"
    assert by_key(rec)["building"].status == "pending"


def test_disabled_work_is_skipped_instead_of_completed(pipeline):
    agent, rec, _ = pipeline
    asyncio.run(agent.deploy(request(workspace_path=None, run_security_scan=False, generate_sbom=False, desired_count=0), rec))
    for key in ["building", "ecr_push", "source_scan", "image_scan", "sbom", "stabilizing", "url_check"]:
        assert by_key(rec)[key].status == "skipped"
        assert by_key(rec)[key].started_at is None
    assert by_key(rec)["svc_update"].status == "done"


def test_url_warning_is_visible_and_not_verified_success(pipeline, monkeypatch):
    agent, rec, _ = pipeline
    async def warn(req, record, clients): record.provisioned["url_warning"] = "접속 확인 실패"
    monkeypatch.setattr(agent, "_step_resolve_url", warn)
    asyncio.run(agent.deploy(request(), rec))
    assert by_key(rec)["url_check"].status == "warning"
    assert deploy_ecs.to_status_response(rec).warnings == ["접속 확인 실패"]


def test_inflight_serialization_and_restart_preserve_last_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_ECS_STORE", str(tmp_path / "records.json"))
    rec = ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)
    initialize_progress(rec, request())
    advance_progress(rec, "preflight")
    advance_progress(rec, "building")
    monkeypatch.setattr(ecs, "_deploy_records", {rec.deployment_id: rec})
    ecs._save_records()
    recovered = ecs._load_records()[rec.deployment_id]
    assert recovered.status == ECSDeployStatus.FAILED
    assert by_key(recovered)["preflight"].status == "done"
    assert by_key(recovered)["building"].status == "failed"
    assert by_key(recovered)["ecr_push"].status == "pending"
    assert deploy_ecs.to_status_response(recovered).running is False


def test_legacy_record_does_not_invent_step_history():
    rec = ECSDeployRecord(status=ECSDeployStatus.IN_PROGRESS)
    response = deploy_ecs.to_status_response(rec)
    assert response.steps == []
    assert response.stage == "deploying" and response.stage_text == "배포 중"


def test_poll_by_id_does_not_switch_to_another_deployment(monkeypatch):
    first, second = ECSDeployRecord(), ECSDeployRecord()
    monkeypatch.setattr(ecs, "_deploy_records", {r.deployment_id: r for r in [first, second]})
    assert asyncio.run(deploy_ecs.deploy_ecs_status(first.deployment_id)).deployment_id == first.deployment_id
    with pytest.raises(HTTPException) as err:
        asyncio.run(deploy_ecs.deploy_ecs_status("missing"))
    assert err.value.status_code == 404


def test_build_helper_reports_push_only_after_build(monkeypatch):
    events = []
    monkeypatch.setattr(ecs_build, "ensure_docker_available", lambda **kwargs: None)
    monkeypatch.setattr(ecs_build, "build_image", lambda *args, **kwargs: events.append("built"))
    monkeypatch.setattr(ecs_build, "ecr_login", lambda *args, **kwargs: events.append("login"))
    monkeypatch.setattr(ecs_build, "push_image", lambda *args, **kwargs: events.append("pushed"))
    ecs_build.build_and_push(object(), workspace_path="/fixture", repository_uri="repo/app", tag="v1", on_progress=events.append)
    assert events == ["building", "built", "ecr_push", "login", "pushed"]
