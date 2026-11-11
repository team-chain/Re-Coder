"""3.1 여러 서비스 프로젝트 로컬 배포 — 게이트·빌드·이미지 검사·기동·검증·기록·롤백(가짜 Docker·가짜 스캐너)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import architecture as A
import compose_check
import compose_deploy as D
import compose_kit as K

RAW = {
    "services": [{"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders"], "public": True},
                 {"name": "orders", "kind": "api", "language": "python", "port": 8000, "databases": ["orders-db"]}],
    "databases": [{"name": "orders-db", "engine": "postgres", "tables": ["orders"]}],
    "endpoints": [{"service": "gateway", "method": "GET", "path": "/api/orders", "auth": True}],
}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RECODER_COMPOSE_STORE", raising=False)


def project(tmp_path) -> Path:
    arch, _ = A.normalize(RAW)
    ws = tmp_path / "shop"
    for op in K.fixed_ops(arch):
        p = ws / op["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(op["content"], encoding="utf-8")
    (ws / "services/orders/app").mkdir(parents=True, exist_ok=True)
    (ws / "services/orders/app/main.py").write_text("import os\nDB = os.environ['ORDERS_DB_URL']\n", encoding="utf-8")
    (ws / "services/gateway/src").mkdir(parents=True, exist_ok=True)
    (ws / "services/gateway/src/server.js").write_text(
        "const ORDERS = process.env.ORDERS_URL;\napp.get('/api/orders', (q, s) => s.json([]));\n", encoding="utf-8")
    return ws


class Docker:
    """이미지 태그 표와 compose 명령을 흉내 낸다."""

    def __init__(self, *, up_ok=True, build_ok=True, unhealthy=()):
        self.images: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.up_ok, self.build_ok, self.unhealthy = up_ok, build_ok, set(unhealthy)
        self.builds = 0
        self.bad = None  # 이 번호의 빌드 이미지는 뜨지 않는다

    def __call__(self, args, timeout):
        self.calls.append(args)

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(args, rc, out, err)
        if args[:2] == ["image", "inspect"]:
            ref = args[-1]
            return done(out=self.images[ref] + "\n") if ref in self.images else done(1, "", "No such image")
        if args[0] == "tag":
            if args[1] not in self.images:
                return done(1, "", "No such image")
            self.images[args[2]] = self.images[args[1]]
            return done()
        if args[:2] == ["image", "rm"]:
            self.images.pop(args[2], None)
            return done()
        if args[0] != "compose":
            return done()
        cmd = args[args.index("--env-file") + 2]
        proj = args[args.index("-p") + 1]
        if cmd == "build":
            if not self.build_ok:
                return done(1, "", " => ERROR [orders 3/5] RUN pip install\nfailed to solve: exit code 1")
            self.builds += 1
            for svc in ("gateway", "orders"):
                self.images[f"{proj}-{svc}"] = f"sha256:{svc}-{self.builds}"
            return done()
        if cmd == "up":
            broken = (not self.up_ok) or (self.bad is not None and self.images.get(f"{proj}-orders", "").endswith(f"-{self.bad}"))
            self.unhealthy = {"orders"} if broken else set()
            return done(1, "", "container orders is unhealthy") if broken else done()
        if cmd == "ps":
            rows = [{"Service": n, "State": "running", "Health": "unhealthy" if n in self.unhealthy else "healthy"}
                    for n in ("gateway", "orders", "orders-db")]
            return done(out="\n".join(json.dumps(r) for r in rows))
        if cmd == "port":
            return done(out="127.0.0.1:3000\n") if args[-2] == "gateway" else done(1, "", "no port")
        if cmd == "exec":
            return done(out="orders\n")
        if cmd == "logs":
            return done(out="Traceback\nboom")
        return done()


def scanner(**over):
    async def scan(kind, ws, target):
        key = kind if kind == "gitleaks" else f"{kind}:{Path(str(target)).parent.name if kind == 'hadolint' else str(target).rsplit('-', 1)[-1]}"
        return over.get(key, over.get(kind, {"status": "ok", "critical_count": 0, "high_count": 0}))
    return scan


def http(method, url, body):
    return (401 if url.endswith("/api/orders") else 200), "{}"


def run(coro):
    return asyncio.run(coro)


def test_load_and_preview_only_for_recoder_compose(tmp_path):
    ws = project(tmp_path)
    assert D.load(ws)["services"][0]["name"] == "gateway"
    steps = [" ".join(s["args"]) for s in D.preview_steps(ws)]
    assert steps[0].startswith("gitleaks") and any("hadolint services/orders/Dockerfile" in s for s in steps)
    assert any(s.startswith("trivy image recoder-shop-") and s.endswith("-orders") for s in steps)
    assert not any("PASSWORD" in s for s in steps)  # 비밀값은 보이지 않는다
    other = tmp_path / "plain"
    other.mkdir()
    (other / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    assert D.load(other) is None and D.preview_steps(other) == []


def test_env_file_is_private_stable_and_prefers_complete_project_env(tmp_path):
    ws = project(tmp_path)
    arch = D.load(ws)
    path, source = D.env_file(ws, arch)
    first = path.read_text(encoding="utf-8")
    assert "ORDERS_DB_PASSWORD=" in first and "JWT_SECRET=" in first and str(path).startswith(str(tmp_path / "home"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert D.env_file(ws, arch)[0].read_text(encoding="utf-8") == first  # 같은 볼륨을 다시 쓰려면 같은 값
    (ws / ".env").write_text("ORDERS_DB_PASSWORD=<random-password>\nJWT_SECRET=x\n", encoding="utf-8")
    assert D.env_file(ws, arch)[0] == path  # 자리표시가 남은 .env 는 쓰지 않는다
    (ws / ".env").write_text("ORDERS_DB_PASSWORD=mine\nJWT_SECRET=mine2\n", encoding="utf-8")
    assert D.env_file(ws, arch) == (ws / ".env", "프로젝트 .env")


def test_success_records_pins_and_verifies(tmp_path):
    ws = project(tmp_path)
    docker = Docker()
    res = run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    assert res["status"] == "success", res
    assert res["url"] == "http://localhost:3000" and "검사" in res["message"]
    assert res["verification"]["passed"] is True
    names = {c["name"] for c in res["verification"]["checks"]}
    assert {"service:orders", "link:gateway->orders", "schema:orders-db", "test:gateway-GET-/api/orders-noauth"} <= names
    assert not any(n.startswith("port:") for n in names)  # 공개하지 않은 서비스 포트가 없다고 실패로 보지 않는다
    rec = D.history(ws)[0]
    assert rec["status"] == "success" and set(rec["pins"]) == {"gateway", "orders"}
    assert all(docker.images.get(tag) for tag in rec["pins"].values())


def test_gate_blocks_before_touching_running_services(tmp_path):
    ws = project(tmp_path)
    docker = Docker()
    res = run(D.deploy(ws, scan=scanner(gitleaks={"status": "ok", "critical_count": 2}), docker=docker, http=http))
    assert res["status"] == "failed" and res["stage"] == "scan" and "Gitleaks" in res["message"]
    assert docker.builds == 0 and not any("up" in c for c in docker.calls)
    res = run(D.deploy(ws, scan=scanner(gitleaks={"status": "error"}), docker=docker, http=http))
    assert res["status"] == "failed" and "완료하지 못했습니다" in res["message"]
    res = run(D.deploy(ws, scan=scanner(**{"hadolint:orders": {"status": "ok", "critical_count": 1}}), docker=docker, http=http))
    assert res["status"] == "failed" and "orders Hadolint" in res["message"] and docker.builds == 0


def test_image_gate_restores_previous_images_without_up(tmp_path):
    ws = project(tmp_path)
    docker = Docker()
    assert run(D.deploy(ws, scan=scanner(), docker=docker, http=http))["status"] == "success"
    before = dict(docker.images)
    ups = sum(1 for c in docker.calls if "up" in c)
    res = run(D.deploy(ws, scan=scanner(**{"trivy:orders": {"status": "ok", "critical_count": 3}}), docker=docker, http=http))
    assert res["status"] == "failed" and "orders 이미지" in res["message"]
    assert sum(1 for c in docker.calls if "up" in c) == ups  # 새 이미지로 띄우지 않았다
    proj = D.project_name(ws)
    assert docker.images[f"{proj}-orders"] == before[f"{proj}-orders"]  # 다음 up 이 막힌 이미지를 쓰지 않는다


def test_failed_start_rolls_back_to_previous_success(tmp_path):
    ws = project(tmp_path)
    docker = Docker()
    assert run(D.deploy(ws, scan=scanner(), docker=docker, http=http))["status"] == "success"
    first = D.history(ws)[0]
    docker.bad = docker.builds + 1
    res = run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    assert res["status"] == "failed" and res["rolled_back"] is True and "직전 버전으로 되돌렸습니다" in res["message"]
    proj = D.project_name(ws)
    assert docker.images[f"{proj}-orders"] == docker.images[first["pins"]["orders"]]
    assert D.history(ws)[0]["status"] == "failed"


def test_build_failure_points_to_service(tmp_path):
    ws = project(tmp_path)
    res = run(D.deploy(ws, scan=scanner(), docker=Docker(build_ok=False), http=http))
    assert res["status"] == "failed" and res["stage"] == "build" and "orders 이미지 빌드 실패" in res["message"]


def test_manual_rollback_needs_previous_and_goes_one_back(tmp_path):
    ws = project(tmp_path)
    docker = Docker()
    assert run(D.rollback(ws, docker=docker))["status"] == "failed"
    run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    first = D.history(ws)[0]
    run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    res = run(D.rollback(ws, docker=docker))
    assert res["status"] == "success" and res["rollback_to"] == first["id"]
    proj = D.project_name(ws)
    assert docker.images[f"{proj}-orders"] == "sha256:orders-1"


def test_deploy_routes_delegate_compose_projects(tmp_path, monkeypatch):
    from api.routes import deploy as routes

    ws = project(tmp_path)
    docker = Docker()
    monkeypatch.setattr(compose_check, "_docker", docker)
    monkeypatch.setattr(compose_check, "_http", http)
    scanned = []

    async def fake_scan(kind, workspace, target):
        scanned.append((kind, str(target)))
        return {"status": "ok", "critical_count": 0, "high_count": 0}
    monkeypatch.setattr(routes, "_execute_scan", fake_scan)
    for name in ("_deployment_plans", "_plans_pending_image_scan", "_plan_workspaces", "_container_locks"):
        monkeypatch.setattr(routes, name, {})
    plan = asyncio.run(routes.create_deployment_plan(routes.DeployPlanRequest(workspace_path=str(ws))))
    # 승인 화면: 서비스마다 Dockerfile 을 검사했고, 실행할 명령은 compose 순서다
    assert ("hadolint", str(ws / "services/orders/Dockerfile")) in scanned
    steps = [" ".join(st["args"]) for st in routes._command_steps(plan, str(ws))]
    assert any("docker compose" in st and " up " in st for st in steps), steps
    res = asyncio.run(routes.execute_deployment(routes.ExecuteRequest(plan_id=plan.plan_id, approved=True)))
    assert res["method"] == "local_docker_compose" and res["status"] == "success", res
    assert plan.plan_id not in routes._deployment_plans
    hist = asyncio.run(routes.stack_history(str(ws)))
    assert hist["entries"][0]["status"] == "success"
    with pytest.raises(routes.HTTPException):
        asyncio.run(routes.rollback_stack(routes.StackRollbackRequest(workspace_path=str(ws))))


def test_history_lists_stacks_and_rolls_back_latest_only(tmp_path, monkeypatch):
    from api.routes import deploy_history as H

    ws = project(tmp_path)
    docker = Docker()
    monkeypatch.setattr(compose_check, "_docker", docker)
    run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    only = [e for e in run(H.list_history(source="stack", limit=50)).entries]
    assert len(only) == 1 and not only[0].rollback.available and "이전 성공" in only[0].rollback.reason
    run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    entries = run(H.list_history(source="stack", limit=50)).entries
    newest, older = entries[0], entries[1]
    assert newest.rollback.available and not older.rollback.available
    with pytest.raises(H.HTTPException) as err:
        run(H.approve_history_rollback("stack", older.deployment_id, H.HistoryApproval(approved=True)))
    assert err.value.status_code == 409
    res = run(H.approve_history_rollback("stack", newest.deployment_id, H.HistoryApproval(approved=True)))
    assert res["status"] == "success"
    assert docker.images[f"{D.project_name(ws)}-orders"] == "sha256:orders-1"


def test_contract_violation_blocks_deploy_before_build(tmp_path):
    ws = project(tmp_path)
    (ws / "services/gateway/src/db.js").write_text("const pg = process.env.ORDERS_DB_URL;\n", encoding="utf-8")
    docker = Docker()
    res = run(D.deploy(ws, scan=scanner(), docker=docker, http=http))
    assert res["status"] == "failed" and "계약 위반" in res["message"] and "services/gateway/src/db.js:1" in res["message"]
    assert docker.builds == 0
