"""History reads persisted facts; approving a stale record cannot replace a service."""
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import deploy, deploy_ecs, deploy_history as history, ecs
from schemas import DeploymentRecord, DeployMethod, DeployStatus
from core.schemas import ECSDeployRecord, ECSDeployStatus
import local_deploy_store as store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_LOCAL_DEPLOY_STORE", str(tmp_path / "local_deployments.json"))
    monkeypatch.setattr(deploy, "_deployment_records", {})
    monkeypatch.setattr(ecs, "_deploy_records", {})
    monkeypatch.setattr(deploy, "_container_locks", {})
    monkeypatch.setattr(store, "load_incomplete", False)
    monkeypatch.setattr(store, "save_failed", False)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(history.router)
    return TestClient(app)


def local(id="local-2", **kwargs):
    values = dict(deployment_id=id, project_id="sample", method=DeployMethod.LOCAL_DOCKER, image="app:v2", image_id="sha256:new", pinned_image="app:recoder-rb-new", container_name="sample", ports={"3456": "3456"}, env={"PRIVATE_TOKEN": "never-in-history"}, rollback_env={"PRIVATE_TOKEN": "old-never-in-history"}, rollback_target="app:recoder-rb-old", deployed_at=datetime(2026, 9, 23), rollback_eligible=True)
    values.update(kwargs)
    record = DeploymentRecord(**values)
    deploy._deployment_records[record.deployment_id] = record
    return record


def remote(id="ecs-1", **kwargs):
    values = dict(deployment_id=id, project_id="extension", cluster="c", service="s", region="ap-northeast-2", image="registry/app:v1", status=ECSDeployStatus.FAILED, previous_task_definition_arn="task:1", task_definition_arn="task:2", rollback_proposal_id="proposal-1", rollback_proposal_status="pending", started_at=datetime(2026, 9, 22, tzinfo=timezone.utc))
    values.update(kwargs)
    record = ECSDeployRecord(**values)
    ecs._deploy_records[record.deployment_id] = record
    return record


def test_history_merges_sources_sorts_and_omits_environment(client, monkeypatch):
    local(); remote()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("History reads must not call Docker or AWS")
    monkeypatch.setattr(subprocess, "run", forbidden)
    import boto3
    monkeypatch.setattr(boto3.session, "Session", forbidden)
    data = client.get("/api/deploy/history").json()
    assert data["total"] == 2
    assert [r["source"] for r in data["entries"]] == ["local", "ecs"]
    assert "never-in-history" not in json.dumps(data)
    assert "PRIVATE_TOKEN" not in json.dumps(data)
    assert data["entries"][0]["rollback"]["available"] is True
    assert client.get("/api/deploy/history?source=ecs").json()["total"] == 1
    assert len(client.get("/api/deploy/history?limit=1").json()["entries"]) == 1
    assert client.get("/api/deploy/history?limit=0").status_code == 422


def test_empty_and_legacy_rollback_times_are_honest(client):
    assert client.get("/api/deploy/history").json() == {"entries": [], "total": 0, "warnings": []}
    local(status=DeployStatus.ROLLED_BACK)
    row = client.get("/api/deploy/history").json()["entries"][0]
    assert row["events"][-1]["at"] == ""
    assert not row["rollback"]["available"]


def test_persistence_restores_rollback_contract_and_private_permissions():
    record = local(rollback_source_deployment_id="previous", rollback_ports={"9876": "8000"}, rollback_health_check_path="/ready")
    deploy._save_records()
    loaded = store.load_records()[record.deployment_id]
    assert loaded.model_dump() == record.model_dump()
    assert store.store_path().stat().st_mode & 0o777 == 0o600
    assert not list(store.store_path().parent.glob(".local-deployments-*"))
    deploy._deployment_records = store.load_records()
    assert deploy._previous_image_for("sample", "app:v3")[0] == "app:recoder-rb-new"


def test_legacy_jsonl_bad_lines_and_snapshot_precedence(client):
    record = local()
    project_dir = store.store_path().parent / "projects"
    project_dir.mkdir()
    legacy = record.model_dump(mode="json")
    legacy["image"] = "app:old"
    legacy["status"] = "deployed"
    (project_dir / "sample_deployments.jsonl").write_text(json.dumps(legacy) + "\n{invalid\n" + json.dumps({"image": "incomplete"}))
    assert store.save_records({record.deployment_id: record})
    deploy._deployment_records = store.load_records()
    assert deploy._deployment_records[record.deployment_id].image == "app:v2"
    assert len(deploy._deployment_records) == 1
    assert client.get("/api/deploy/history").json()["warnings"]


def test_interrupted_rollback_is_not_reported_as_success():
    record = local(rollback_status="running")
    assert store.save_records({record.deployment_id: record})
    restored = store.load_records()[record.deployment_id]
    assert restored.rollback_status == "unknown"
    assert restored.rollback_completed_at is None


def test_save_failure_preserves_previous_snapshot_and_reports_warning(client, monkeypatch):
    record = local()
    assert store.save_records({record.deployment_id: record})
    old = store.store_path().read_bytes()
    def fail(*_): raise OSError("full")
    monkeypatch.setattr(Path, "replace", fail)
    record.image = "app:v3"
    assert not store.save_records({record.deployment_id: record})
    assert store.store_path().read_bytes() == old
    assert client.get("/api/deploy/history").json()["warnings"]


def test_equal_and_mixed_timezone_order_survives_restart():
    local("a", deployed_at=datetime(2026, 9, 23))
    local("b", deployed_at=datetime(2026, 9, 23, tzinfo=timezone.utc))
    deploy._save_records()
    deploy._deployment_records = store.load_records()
    assert [r.deployment_id for r in deploy._records_newest_first()] == ["b", "a"]


def test_declined_or_missing_approval_never_executes(client, monkeypatch):
    remote()
    action = AsyncMock()
    monkeypatch.setattr(deploy_ecs, "resolve_ecs_rollback", action)
    assert client.post("/api/deploy/history/ecs/ecs-1/rollback", json={"approved": False}).status_code == 400
    assert client.post("/api/deploy/history/ecs/ecs-1/rollback", json={}).status_code == 422
    action.assert_not_awaited()


def test_ecs_uses_existing_proposal_approval_and_returns_request_not_recovery(client, monkeypatch):
    remote()
    response = deploy_ecs.EcsRollbackResponse(status="completed", message="롤백 요청 완료", deployment_id="ecs-1", proposal_id="proposal-1", adr={"file": "docs/adr/test.md", "content": "test"})
    action = AsyncMock(return_value=response)
    monkeypatch.setattr(deploy_ecs, "resolve_ecs_rollback", action)
    result = client.post("/api/deploy/history/ecs/ecs-1/rollback", json={"approved": True})
    assert result.status_code == 200
    body = action.await_args.args[0]
    assert body.proposal_id == "proposal-1" and body.approved is True
    assert result.json()["message"] == "롤백 요청 완료"


@pytest.mark.parametrize("status", ["completed", "ignored", "superseded", "approving"])
def test_processed_ecs_proposal_is_not_actionable(client, status):
    remote(rollback_proposal_status=status)
    assert not client.get("/api/deploy/history").json()["entries"][0]["rollback"]["available"]
    assert client.post("/api/deploy/history/ecs/ecs-1/rollback", json={"approved": True}).status_code == 409


def test_newer_ecs_deployment_blocks_old_proposal(client):
    remote()
    remote("new", started_at=datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert client.post("/api/deploy/history/ecs/ecs-1/rollback", json={"approved": True}).status_code == 409


def test_old_local_record_is_read_only(client, monkeypatch):
    local("old", deployed_at=datetime(2026, 9, 21))
    local()
    action = AsyncMock()
    monkeypatch.setattr(deploy, "rollback", action)
    assert client.post("/api/deploy/history/local/old/rollback", json={"approved": True}).status_code == 409
    action.assert_not_awaited()


def stub_docker(monkeypatch, image="sha256:new", healthy=True):
    calls = []
    monkeypatch.setattr(deploy, "_running_image_id", AsyncMock(return_value=image))
    monkeypatch.setattr(deploy, "_rollback_image_available", AsyncMock(return_value=True))
    monkeypatch.setattr(deploy, "_probe_local_http_health", AsyncMock(return_value=healthy))
    monkeypatch.setattr(deploy, "_stop_verification_for_deployment", AsyncMock())
    monkeypatch.setattr(subprocess, "run", lambda args, **_: calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="ok", stderr=""))
    return calls


def test_changed_current_image_is_rejected_without_stopping_container(client, monkeypatch):
    local()
    calls = stub_docker(monkeypatch, image="sha256:other")
    result = client.post("/api/deploy/history/local/local-2/rollback", json={"approved": True})
    assert result.status_code == 409
    assert calls == []


@pytest.mark.parametrize("healthy", [True, False])
def test_local_rollback_result_and_timestamp_persist(client, monkeypatch, healthy):
    record = local()
    calls = stub_docker(monkeypatch, healthy=healthy)
    result = client.post("/api/deploy/history/local/local-2/rollback", json={"approved": True})
    assert result.status_code == 200
    assert result.json()["status"] == ("ok" if healthy else "failed")
    restored = store.load_records()[record.deployment_id]
    assert restored.rollback_status == ("succeeded" if healthy else "failed")
    assert restored.rollback_completed_at is not None
    assert any(args[:2] == ["docker", "run"] and args[-1] == "app:recoder-rb-old" for args in calls)
    if healthy:
        calls.clear()
        assert client.post("/api/deploy/history/local/local-2/rollback", json={"approved": True}).status_code == 409
        assert not calls


def test_cancelled_health_probe_cannot_mark_rollback_success(monkeypatch):
    record = local()
    stub_docker(monkeypatch)
    monkeypatch.setattr(deploy, "_probe_local_http_health", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(deploy.rollback(deploy.RollbackRequest(deployment_id=record.deployment_id, require_current=True)))
    assert store.load_records()[record.deployment_id].rollback_status == "unknown"


def test_health_callbacks_persist_eligibility():
    record = local()
    asyncio.run(deploy._mark_rollback_candidate_unhealthy(record.deployment_id, {}))
    assert not store.load_records()[record.deployment_id].rollback_eligible


def test_main_registers_history_routes():
    from core.main import create_app
    app = create_app()
    app.state.session_token = "history-test-token"
    response = TestClient(app).get("/api/deploy/history", headers={"X-Session-Token": "history-test-token"})
    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_remote_methods_are_not_mislabeled_as_local(client):
    local(method=DeployMethod.SSH_DOCKER)
    assert client.get("/api/deploy/history").json()["entries"] == []


def test_new_deployment_while_waiting_for_lock_is_checked_again(monkeypatch):
    record = local()
    calls = stub_docker(monkeypatch)
    async def run():
        lock = await deploy._lock_for_container(record.container_name)
        await lock.acquire()
        task = asyncio.create_task(deploy.rollback(deploy.RollbackRequest(deployment_id=record.deployment_id, require_current=True)))
        await asyncio.sleep(0)
        local("newer", deployed_at=datetime(2026, 9, 24))
        lock.release()
        with pytest.raises(Exception) as exc:
            await task
        assert getattr(exc.value, "status_code", None) == 409
    asyncio.run(run())
    assert calls == []
