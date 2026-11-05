"""No AWS mutation: exercise remote evidence validation and the real ECS pipeline boundary."""
import asyncio
import copy
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import managed_build
from core.schemas import ManagedBuildReference, ECSDeployRequest, ECSDeployStatus
from core.agents.ecs_agent import ECSAgent

ACCOUNT, REGION = "123456789012", "us-east-1"
REF = ManagedBuildReference(run_id="rc-rt-" + "a" * 24,
                           build_id="recoder-container-v1:11111111-2222-3333-4444-555555555555",
                           source_sha256="b" * 64)
DIGEST = "sha256:" + "c" * 64


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY app.js /app.js\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log('fixture')", encoding="utf-8")
    sbom = json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [{"name": "fixture", "version": "1"}]}).encode()
    report = dict(schema=1, runId=REF.run_id, buildId=REF.build_id, sourceSha256=REF.source_sha256, status="completed",
                  image=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{managed_build.REPOSITORY}@{DIGEST}", imageDigest=DIGEST,
                  sourceFiles=managed_build.source_manifest(str(tmp_path)), sbomSha256=hashlib.sha256(sbom).hexdigest(),
                  policy={"decision": "allow"}, checks=[dict(tool=tool, status="completed", findings=[], count=0,
                                                            criticalCount=0, highCount=0, blockingCount=0)
                                                      for tool in ["gitleaks", "hadolint", "dependencies", "image"]])
    bucket = f"recoder-container-{ACCOUNT}-{REGION}"
    build = dict(id=REF.build_id, projectName="recoder-container-v1", buildStatus="SUCCEEDED",
                 endTime=datetime.now(timezone.utc),
                 arn=f"arn:aws:codebuild:{REGION}:{ACCOUNT}:build/{REF.build_id}",
                 source={"type": "S3", "location": f"{bucket}/sources/{REF.run_id}.zip"},
                 environment={"environmentVariables": [{"name": k, "value": v} for k, v in [
                     ("RECODER_RUN_ID", REF.run_id), ("RECODER_SOURCE_SHA256", REF.source_sha256), ("RECODER_REPORT_BUCKET", bucket)]]})
    clients = {"sts": Mock(), "codebuild": Mock(), "s3": Mock(), "ecr": Mock()}
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    clients["codebuild"].batch_get_builds.return_value = {"builds": [build]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": DIGEST}]}

    def get_object(**kwargs):
        assert kwargs["Bucket"] == bucket
        key = kwargs["Key"]
        assert key in [f"reports/{REF.run_id}/report.json", f"reports/{REF.run_id}/sbom.json"]
        return {"Body": io.BytesIO(sbom if key.endswith("sbom.json") else json.dumps(report).encode())}
    clients["s3"].get_object.side_effect = get_object
    return tmp_path, clients, report, build, sbom


def test_valid_remote_image_and_sbom_need_no_local_docker(evidence, monkeypatch):
    root, clients, report, _, sbom = evidence
    verified = managed_build.verify_build(REF, str(root), REGION, clients)
    assert verified.report["image"] == report["image"]
    assert verified.scan_result().passed
    from core import sbom as sbom_module
    monkeypatch.setattr(sbom_module, "_SBOM_DIR", root / ".recoder" / "sbom")
    record = verified.save_sbom()
    assert record.package_count == 1
    assert record.sbom_hash == hashlib.sha256(sbom).hexdigest()
    assert open(record.sbom_path, "rb").read() == sbom


@pytest.mark.parametrize("change", [
    lambda r: r.update(status="running"),
    lambda r: r.update(image="attacker.example/image:v1"),
    lambda r: r.update(runId="rc-rt-" + "f" * 24),
    lambda r: r["policy"].update(decision="deny"),
    lambda r: r["checks"][0].update(status="failed"),
    lambda r: r["checks"][0].update(count=1, highCount=1, blockingCount=1),
    lambda r: r["checks"][1].update(count=1, highCount=1, blockingCount=1),
    lambda r: r["checks"][3].update(count=201, criticalCount=1, blockingCount=1),
    lambda r: r["checks"][3].update(count=True),
    lambda r: r["checks"][3].update(findings=[{"title": "hidden critical", "severity": "critical"}]),
    lambda r: r["checks"].append(copy.deepcopy(r["checks"][0])),
    lambda r: r["sourceFiles"][0].update(path="../Dockerfile"),
    lambda r: r.update(sbomSha256="d" * 64),
])
def test_untrusted_or_incomplete_evidence_is_rejected(evidence, change):
    root, clients, report, _, _ = evidence
    change(report)
    with pytest.raises(ValueError):
        managed_build.verify_build(REF, str(root), REGION, clients)


@pytest.mark.parametrize("field,value", [("buildStatus", "FAILED"), ("projectName", "other"), ("arn", "arn:other"), ("endTime", datetime.now(timezone.utc)-timedelta(days=2)), ("endTime", None)])
def test_aws_authority_wins_over_success_in_report(evidence, field, value):
    root, clients, _, build, _ = evidence
    build[field] = value
    with pytest.raises(ValueError):
        managed_build.verify_build(REF, str(root), REGION, clients)
    clients["s3"].get_object.assert_not_called()


def test_other_snapshot_account_or_digest_cannot_be_deployed(evidence):
    root, clients, _, build, _ = evidence
    build["source"]["location"] = "other-bucket/sources/file.zip"
    with pytest.raises(ValueError):
        managed_build.verify_build(REF, str(root), REGION, clients)


def test_changed_added_deleted_sources_block_but_local_secrets_and_outputs_do_not(evidence):
    root, clients, _, _, _ = evidence
    verified = managed_build.verify_build(REF, str(root), REGION, clients)
    (root / ".env").write_text("SECRET=local-only")
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text("output")
    verified.assert_sources(str(root))
    (root / "new.js").write_text("changed")
    with pytest.raises(ValueError, match="변경"):
        verified.assert_sources(str(root))
    (root / "new.js").unlink()
    (root / "app.js").unlink()
    with pytest.raises(ValueError, match="변경"):
        verified.assert_sources(str(root))


def test_ecr_digest_is_verified_independently(evidence):
    root, clients, _, _, _ = evidence
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:" + "f" * 64}]}
    with pytest.raises(ValueError, match="ECR"):
        managed_build.verify_build(REF, str(root), REGION, clients)


def prepare_agent(monkeypatch, evidence):
    root, clients, _, _, _ = evidence
    agent = ECSAgent()
    monkeypatch.setattr(agent, "_clients", lambda _: clients)
    monkeypatch.setattr(agent, "_plan_runtime", lambda _: SimpleNamespace(blocked=False, notes=[], services=[]))
    request = ECSDeployRequest(project_id="test", cluster="test", service="test", workspace_path=str(root),
                               region=REGION, managed_build=REF, run_preflight=False, shield=False, desired_count=0)
    return agent, request


def test_verification_fails_before_any_deployed_resource_can_change(evidence, monkeypatch):
    agent, request = prepare_agent(monkeypatch, evidence)
    evidence[3]["buildStatus"] = "FAILED"
    provision = AsyncMock()
    monkeypatch.setattr(agent, "_step_provision", provision)
    result = asyncio.run(agent.deploy(request))
    assert result.status == ECSDeployStatus.FAILED
    assert "검증 실패" in result.error_message
    provision.assert_not_awaited()
    assert agent._managed == {}


@pytest.mark.parametrize("disabled", ["run_security_scan", "generate_sbom"])
def test_managed_build_cannot_disable_security(evidence, monkeypatch, disabled):
    agent, request = prepare_agent(monkeypatch, evidence)
    setattr(request, disabled, False)
    provision = AsyncMock()
    monkeypatch.setattr(agent, "_step_provision", provision)
    result = asyncio.run(agent.deploy(request))
    assert result.status == ECSDeployStatus.FAILED
    assert "끌 수 없습니다" in result.error_message
    provision.assert_not_awaited()


def test_pipeline_reuses_verified_evidence_and_preserves_service_approval_flow(evidence, monkeypatch):
    agent, request = prepare_agent(monkeypatch, evidence)
    from core import sbom as sbom_module
    monkeypatch.setattr(sbom_module, "_SBOM_DIR", evidence[0] / ".recoder" / "sbom")
    # Only external mutations are substituted; real managed verification/scans/SBOM stages run.
    monkeypatch.setattr(agent, "_step_provision", AsyncMock(return_value=object()))
    monkeypatch.setattr(agent, "_step_database_ready", AsyncMock())
    register = AsyncMock(return_value=("task:2", "task:1"))
    service = AsyncMock()
    monkeypatch.setattr(agent, "_step_register_task_definition", register)
    monkeypatch.setattr(agent, "_step_ensure_service", service)
    monkeypatch.setattr(agent, "_step_alerts", AsyncMock())
    from core.agents import ecs_build
    from core.security_scan import security_scanner
    monkeypatch.setattr(ecs_build, "build_and_push", Mock(side_effect=AssertionError("local Docker must not run")))
    monkeypatch.setattr(security_scanner, "scan_all", AsyncMock(side_effect=AssertionError("local scanner must not run")))
    result = asyncio.run(agent.deploy(request))
    assert result.status == ECSDeployStatus.SUCCEEDED, result.error_message
    assert result.image_uri == evidence[2]["image"]
    assert result.scan_result.passed and result.sbom.package_count == 1
    assert register.await_args.args[2] == evidence[2]["image"]
    service.assert_awaited_once()
    assert agent._managed == {}


def test_extension_request_keeps_only_a_strict_reference():
    from api.routes.deploy_ecs import ExtensionEcsDeployRequest, to_core_request
    request = to_core_request(ExtensionEcsDeployRequest(managed_build=REF))
    assert request.managed_build == REF
    with pytest.raises(ValueError):
        ExtensionEcsDeployRequest(managed_build={**REF.model_dump(), "passed": True})
