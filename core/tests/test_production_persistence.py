"""Regression cases found by actual Fargate deployments, not just a DB mock."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from core import aws_rds, build_readiness
from core.agents import ecs_build, ecs_runtime
from core.agents.ecs_agent import ECSAgent
from core.schemas import ECSDeployRecord, ECSDeployRequest, SecurityScanResult, SecurityFinding


def test_mixed_vite_python_sqlite_cannot_deploy_as_production(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"vite":"*"}}', encoding="utf-8")
    (tmp_path / "db.py").write_text('from sqlalchemy.engine import URL\nurl = URL.create("sqlite+pysqlite", database=path)', encoding="utf-8")
    (tmp_path / "Dockerfile").write_text('FROM python:3.13-alpine\nVOLUME /data\n', encoding="utf-8")
    readiness = build_readiness.analyze(tmp_path)
    assert readiness.sqlite_sources == ["db.py"]
    plan = ecs_runtime.plan(str(tmp_path), key="test", environment="production", provided={"DATABASE_URL"},
                            container_port=8000, create=False, managed_db=True)
    assert plan.blocked
    assert any(m["name"] == "DATABASE_STORAGE" for m in plan.missing)
    assert "VOLUME" in plan.block_message()[1]


def test_python_postgres_is_detected_in_mixed_project(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"vite":"*"}}', encoding="utf-8")
    (tmp_path / "db.py").write_text('import os\nurl = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://")', encoding="utf-8")
    plan = ecs_runtime.plan(str(tmp_path), key="test", environment="production", provided=set(),
                            container_port=8000, create=False, managed_db=True)
    assert not plan.blocked
    assert "DATABASE_URL" in plan.managed_db["names"]
    assert not plan.sidecars


def test_rds_connections_verify_hostname_and_certificate():
    values = aws_rds.connection({"Address": "db.example.com", "Port": 5432}, "synthetic/password", ["DATABASE_URL"])
    query = parse_qs(urlsplit(values["DATABASE_URL"]).query)
    assert query == {"sslmode": ["verify-full"], "sslrootcert": [aws_rds.CA_PATH]}
    assert values["PGSSLMODE"] == "verify-full"
    init = aws_rds.init_container("", plain_url=values["DATABASE_URL"], region="ap-northeast-2")
    assert len(json.dumps(init)) < 16000
    assert "base64 -d" in init["command"][0]


def test_sqlalchemy_driver_declared_only_in_requirements(tmp_path):
    (tmp_path / "db.py").write_text('import os\nfrom sqlalchemy import create_engine\nengine = create_engine(os.environ["DATABASE_URL"])', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text('sqlalchemy\npsycopg[binary]>=3.2\n', encoding="utf-8")
    plan = ecs_runtime.plan(str(tmp_path), key="test", environment="production", provided=set(),
                            container_port=8000, create=False, managed_db=True)
    assert not plan.blocked and plan.managed_db


def test_rds_build_injects_ca_without_modifying_source(tmp_path):
    original = 'FROM python:3.13-alpine\nUSER 1001\nCMD ["python", "app.py"]\n'
    (tmp_path / "Dockerfile").write_text(original, encoding="utf-8")
    (tmp_path / ".dockerignore").write_text('*\n!app.py\n', encoding="utf-8")
    captured = []
    def runner(args, **kwargs):
        context = Path(args[args.index("--build-context") + 1].split("=", 1)[1])
        dockerfile = Path(args[args.index("-f") + 1])
        assert (context / "rds-ca.pem").read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")
        assert 'COPY --from=recoder-rds-tls' in dockerfile.read_text(encoding="utf-8")
        assert 'PGSSLMODE=verify-full' in dockerfile.read_text(encoding="utf-8")
        captured.append(context)
        return 0, "ok", ""
    ecs_build.build_image(str(tmp_path), "qa", runner=runner, rds_tls=True)
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == original
    assert not captured[0].exists()


def test_high_vulnerabilities_block_production_but_remain_demo_warning():
    result = SecurityScanResult(findings=[SecurityFinding(tool="trivy", severity="high", title="CVE-test")])
    result.compute_pass()
    assert ECSAgent._scan_gate_message(result) is None
    assert "high" in ECSAgent._scan_gate_message(result, block_high=True)[0]
    assert ECSAgent._scan_gate_message(None, block_high=True)


@pytest.mark.parametrize("title,severity", [("CVE-sidecar", "high"), ("trivy_scan_failed", "info")])
def test_production_checks_sidecar_findings_and_scan_failures(monkeypatch, title, severity):
    from core.agents import ecs_agent
    from core import shield as shield_mod
    req = ECSDeployRequest(project_id="test", cluster="test", service="test", environment="production")
    agent = ECSAgent()
    agent._runtime[id(req)] = SimpleNamespace(sidecars=[{"image": "db:test"}], managed_db={"init_sql_b64": "sql"})
    agent._shield[id(req)] = SimpleNamespace(on=True)
    seen = []

    async def scan_all(*, image, **kwargs):
        seen.append(image)
        findings = [SecurityFinding(tool="trivy", severity=severity, title=title)] if image == "db:test" else []
        result = SecurityScanResult(findings=findings)
        result.compute_pass()
        return result

    monkeypatch.setattr(ecs_agent.security_scanner, "scan_all", scan_all)
    rec = asyncio.run(agent._step_security_scan(req, ECSDeployRecord(), "app:test"))
    assert set(seen) == {"app:test", "db:test", shield_mod.IMAGE, aws_rds.INIT_IMAGE}
    assert ECSAgent._scan_gate_message(rec.scan_result, block_high=True)


def test_vendored_findings_do_not_add_unrelated_top_level_dependency(tmp_path):
    from core import security_fix
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    reports = {"trivy": {"findings": [{"type": "python-pkg", "package": "urllib3", "installed": "1.0",
        "fixed": "2.8.0", "pkg_path": "usr/local/lib/python3.13/site-packages/pip/_vendor/urllib3"}]}}
    proposals = security_fix.plan(str(tmp_path), reports)
    assert proposals and all(not p.auto for p in proposals)
    assert "내장" in proposals[0].detail
    assert not any(p.files == ["requirements.txt"] for p in proposals)
