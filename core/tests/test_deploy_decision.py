"""AI-DLC 배포 대상 결정: 감지 추천과 ADR 산출을 검증한다."""
from __future__ import annotations

import asyncio

try:
    from api.routes import deploy
except ImportError:  # pragma: no cover - package 실행 호환
    from core.api.routes import deploy


def test_deploy_preflight_recommends_ecs_for_fastapi_with_sqlite(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "app.sqlite3").write_text("", encoding="utf-8")

    result = deploy._deployment_preflight(str(tmp_path))

    assert result["app_kind"] == "server"
    assert result["recommended_target"] == "ecs"
    assert "FastAPI 서버" in result["evidence"]
    assert "SQLite 데이터 저장" in result["evidence"]


def test_deploy_preflight_recommends_s3_for_static_site(tmp_path):
    (tmp_path / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"devDependencies":{"vite":"latest"}}', encoding="utf-8")

    result = deploy._deployment_preflight(str(tmp_path))

    assert result["app_kind"] == "static"
    assert result["recommended_target"] == "s3"
    assert "정적 HTML 엔트리" in result["evidence"]


def test_deployment_choice_builds_adr_with_selected_target_and_evidence(tmp_path):
    adr = deploy._build_deployment_decision_adr(
        str(tmp_path),
        "ecs",
        ["FastAPI 서버", "SQLite 데이터 저장"],
    )

    assert adr["file"] == "docs/adr/ADR-001-deployment-target.md"
    assert "ECS 컨테이너" in adr["content"]
    assert "FastAPI 서버, SQLite 데이터 저장" in adr["content"]
    assert "S3 정적 호스팅" in adr["content"]


def test_deploy_decision_routes_are_registered():
    paths = {route.path for route in deploy.router.routes}
    assert "/api/deploy/preflight" in paths
    assert "/api/deploy/decision" in paths
    assert "/api/deploy/remediations/{proposal_id}/apply" in paths


def test_deploy_preflight_includes_block_reasons_and_fixes(tmp_path):
    """배포 화면은 감지 정보뿐 아니라 정적 검사 차단 사유도 받아야 한다."""
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    result = asyncio.run(deploy.deploy_preflight(
        deploy.DeployPreflightRequest(workspace_path=str(tmp_path))
    ))

    assert "blocked" in result
    assert "reasons" in result
    assert "fixes" in result
    assert result["blocked"] is True
    assert result["reasons"]
    assert {"code", "message", "fix", "remediation_available"} <= result["reasons"][0].keys()

    dockerfile_issue = next(reason for reason in result["reasons"] if reason["code"] == "MISSING_DOCKERFILE")
    assert dockerfile_issue["remediation_available"] is True
    assert dockerfile_issue["proposal_id"]

    applied = asyncio.run(deploy.apply_deployment_remediation(
        dockerfile_issue["proposal_id"],
        deploy.DeploymentRemediationApplyRequest(workspace_path=str(tmp_path)),
    ))
    assert applied["success"] is True
    assert (tmp_path / "Dockerfile").is_file()
