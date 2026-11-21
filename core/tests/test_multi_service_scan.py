"""여러 서비스 프로젝트의 보안 검사 — 루트 Dockerfile 이 없다고 건너뛰고 「안전」 이 되던 회귀(실기기 2026-11-21)."""
import asyncio
import json

from api.routes import deploy


def _project(tmp_path, with_contract=True):
    ws = tmp_path / "졸업작품"
    for svc in ("web", "gateway", "orders"):
        (ws / "services" / svc).mkdir(parents=True)
        (ws / "services" / svc / "Dockerfile").write_text("FROM node:22-alpine\n", encoding="utf-8")
    (ws / "services" / "web" / "node_modules" / "x").mkdir(parents=True)
    (ws / "services" / "web" / "node_modules" / "x" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    if with_contract:
        (ws / "architecture.json").write_text(json.dumps({"services": [
            {"name": n, "path": f"services/{n}"} for n in ("web", "gateway", "orders")]}), encoding="utf-8")
    return ws


def test_dockerfiles_of_every_service_are_targets_without_root_dockerfile(tmp_path):
    for contract in (True, False):
        ws = _project(tmp_path / str(contract), with_contract=contract)
        targets = deploy._service_scan_targets(ws, "hadolint")
        assert sorted(name for name, _ in targets) == ["gateway", "orders", "web"]
        assert all(path.endswith("Dockerfile") and "node_modules" not in path for _, path in targets)
        (ws / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        assert deploy._service_scan_targets(ws, "hadolint") == []  # 한 서비스 프로젝트는 예전 경로


def test_images_of_each_service_use_the_compose_project_name(tmp_path, monkeypatch):
    import compose_deploy
    ws = _project(tmp_path)
    proj = compose_deploy.project_name(ws)
    built = {f"{proj}-web:latest", f"{proj}-orders:latest"}
    monkeypatch.setattr(deploy, "_local_image_exists", lambda image: image in built)
    assert deploy._service_scan_targets(ws, "trivy") == [("web", f"{proj}-web:latest"), ("orders", f"{proj}-orders:latest")]
    built.add(deploy._default_image_name(str(ws)))
    assert deploy._service_scan_targets(ws, "trivy") == []


def test_merged_result_counts_every_service_and_never_hides_a_failed_one(tmp_path, monkeypatch):
    ws = _project(tmp_path)
    calls = []

    async def fake(scan_type, workspace_path, target):
        calls.append(target)
        target = target.replace("\\", "/")  # Windows 경로도 같은 규칙으로
        if target.endswith("orders/Dockerfile"):
            return {"status": "error", "findings": []}
        n = 1 if "web" in target else 0
        return {"status": "ok", "critical_count": 0, "high_count": n, "medium_count": 0,
                "findings": [{"code": "DL3002", "severity": "HIGH", "line": 3, "file": target}] * n}

    monkeypatch.setattr(deploy, "_execute_scan", fake)
    targets = deploy._service_scan_targets(ws, "hadolint")
    merged = asyncio.run(deploy._scan_each_service("hadolint", str(ws), targets))
    assert merged["status"] == "ok" and merged["high_count"] == 1
    assert merged["findings"][0]["file"] == "services/web/Dockerfile" and merged["findings"][0]["service"] == "web"
    assert merged["unverified_targets"] == ["orders"]
    assert "검사하지 못한 서비스: orders" in merged["summary"]
    assert len(calls) == 3

    async def broken(scan_type, workspace_path, target):
        return {"status": "error", "findings": []}

    monkeypatch.setattr(deploy, "_execute_scan", broken)
    none = asyncio.run(deploy._scan_each_service("hadolint", str(ws), targets))
    assert none["status"] == "unverified" and none["unverified_targets"] == ["web", "gateway", "orders"]
