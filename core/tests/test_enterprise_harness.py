"""3.1 엔터프라이즈 하네스 — 다중 서비스·다중 DB 아키텍처 계약 → compose 검증 → 계약 통합 테스트 → 교정 루프.

실제 AI 와 이미지 저장소 없이 검증한다: 가짜 AI(작업별 응답), 가짜 Docker(명령별 응답), 실제 HTTP 서버(통합 테스트).
실제 컨테이너로 끝까지 띄우는 검증은 scripts/verify-enterprise-live.py(사용자 PC) 가 한다.
"""
from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

import architecture as A
import compose_check as C
import compose_kit as K

RAW = {
    "services": [
        {"name": "Web", "kind": "web", "language": "node", "depends_on": ["gateway"], "public": True},
        {"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders", "catalog", "worker"],
         "databases": ["cache"]},
        {"name": "orders", "kind": "api", "language": "python", "port": 8000,
         "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000", "databases": ["orders-db", "cache"]},
        {"name": "catalog", "kind": "api", "language": "node", "port": 3001, "databases": ["catalog-db", "nope"]},
        {"name": "worker", "kind": "worker", "language": "python", "databases": ["orders-db", "cache"], "depends_on": ["orders"]},
    ],
    "databases": [
        {"name": "orders-db", "engine": "postgresql", "tables": ["orders", "order_items", "bad name", "orders"]},
        {"name": "catalog-db", "engine": "mongo", "tables": ["ignored"]},
        {"name": "cache", "engine": "redis"},
        {"name": "search", "engine": "elasticsearch"},
    ],
    "endpoints": [
        {"service": "gateway", "method": "POST", "path": "/api/auth/login", "body": {"email": "a@b.c", "password": "pw"}},
        {"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
        {"service": "orders", "method": "POST", "path": "/api/orders", "auth": True, "body": {"items": [1]}},
        {"service": "catalog", "method": "GET", "path": "/api/products/:id"},
        {"service": "catalog", "method": "GET", "path": "/health"},
        {"service": "worker", "method": "GET", "path": "/jobs"},
        {"service": "orders", "method": "GET", "path": "/../etc/passwd"},
    ],
}


def arch():
    a, _ = A.normalize(RAW)
    return a


# ── ① 계약 정규화 ────────────────────────────────────────────────────────────

def test_normalize_names_engines_and_links():
    a, notes = A.normalize(RAW)
    names = [s["name"] for s in a["services"]]
    assert names == ["web", "gateway", "orders", "catalog", "worker"]
    assert [d["name"] for d in a["databases"]] == ["orders-db", "catalog-db", "cache"]
    assert A.database(a, "orders-db") == {"name": "orders-db", "engine": "postgres", "tables": ["orders", "order_items"], "purpose": ""}
    assert A.database(a, "catalog-db")["tables"] == []  # 문서형 DB 는 테이블 검사 대상이 아니다
    assert A.service(a, "gateway")["depends_on"] == ["orders", "catalog"]  # HTTP 없는 워커는 부를 수 없다
    assert A.service(a, "catalog")["databases"] == ["catalog-db"]
    assert A.service(a, "web")["port"] == 8080 and A.service(a, "worker")["port"] == 0
    assert A.service(a, "orders")["start"] == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    assert any("elasticsearch" in n for n in notes) and any("nope" in n for n in notes) and any("워커" in n for n in notes)
    paths = [(e["service"], e["method"], e["path"]) for e in a["endpoints"]]
    assert ("catalog", "GET", "/health") not in paths and ("worker", "GET", "/jobs") not in paths
    assert not any(".." in p for _, _, p in paths)
    assert A.normalize(a) == (a, [])  # 멱등


def test_normalize_breaks_cycles_and_rejects_empty():
    a, notes = A.normalize({"services": [{"name": "a", "kind": "api", "language": "node", "depends_on": ["b"]},
                                         {"name": "b", "kind": "api", "language": "node", "depends_on": ["a"]}],
                            "databases": [], "endpoints": []})
    assert not (A.service(a, "a")["depends_on"] and A.service(a, "b")["depends_on"])
    assert any("순환" in n for n in notes)
    assert A.normalize({"services": [], "databases": [], "endpoints": []})[0] is None
    assert A.normalize("x")[0] is None


def test_normalize_drops_shell_in_start_and_sets_public_entry():
    a, _ = A.normalize({"services": [{"name": "api", "kind": "api", "language": "python", "start": "python app.py; rm -rf /"}],
                        "databases": [], "endpoints": []})
    svc = A.service(a, "api")
    assert svc["start"][0] == "uvicorn" and svc["public"] is True
    assert not A.is_multi(a)


def test_wants_detects_enterprise_requests_only():
    assert A.wants("주문·상품 마이크로서비스와 PostgreSQL, Redis 로 쇼핑몰")
    assert A.wants("게이트웨이 뒤에 서비스 3개, DB 2개")
    assert A.wants("make an enterprise app with docker compose")
    assert A.wants("할 일 앱", decisions=[{"id": "db", "choice": "MongoDB"}])
    assert not A.wants("간단한 할 일 앱을 만들어 주세요")
    assert not A.wants("PostgreSQL 로 게시판")


def test_contract_text_fixes_names_and_variables():
    text = A.contract_text(arch())
    for needle in ("ORDERS_DB_URL", "CACHE_URL", "ORDERS_URL", "CATALOG_DB_URL", "services/orders/", "JWT_SECRET",
                   "GET /health", "테이블 orders, order_items", "POST /api/orders · 로그인 필요"):
        assert needle in text
    assert "Dockerfile·docker-compose.yml" in text


# ── 인프라 파일(compose_kit) ──────────────────────────────────────────────────

def test_compose_is_deterministic_and_has_no_secrets():
    a = arch()
    text = K.render_compose(a)
    assert text == K.render_compose(arch())
    doc = yaml.safe_load(text)
    svcs = doc["services"]
    assert set(svcs) == {"orders-db", "catalog-db", "cache", "web", "gateway", "orders", "catalog", "worker"}
    for db in ("orders-db", "catalog-db", "cache"):
        assert svcs[db]["healthcheck"]["test"] and svcs[db]["volumes"]
    assert svcs["orders"]["depends_on"] == {"orders-db": {"condition": "service_healthy"}, "cache": {"condition": "service_healthy"}}
    assert svcs["gateway"]["depends_on"]["orders"] == {"condition": "service_healthy"}
    assert svcs["orders"]["environment"]["ORDERS_DB_URL"] == "postgres://app:${ORDERS_DB_PASSWORD}@orders-db:5432/orders_db"
    assert svcs["gateway"]["environment"]["ORDERS_URL"] == "http://orders:8000"
    assert svcs["orders-db"]["environment"]["POSTGRES_PASSWORD"].startswith("${ORDERS_DB_PASSWORD:?")
    assert "ports" in svcs["web"] and "ports" not in svcs["orders"]  # 공개 서비스만 포트를 연다
    assert "JWT_SECRET" in svcs["orders"]["environment"] and "JWT_SECRET" in svcs["gateway"]["environment"]
    assert "JWT_SECRET" not in svcs["catalog"].get("environment", {})
    assert not re.search(r"[0-9a-f]{24,}", text)


def test_dockerfiles_are_non_root_and_pass_guard():
    import dockerfile_guard
    a = arch()
    for svc in a["services"]:
        text = K.render_dockerfile(svc)
        assert text.startswith(K.DOCKER_MARKER)
        assert dockerfile_guard.check(text) == [], (svc["name"], dockerfile_guard.check(text))
        if svc["kind"] != "web":
            assert "USER node" in text or "USER app" in text
    assert 'CMD ["uvicorn", "app.main:app"' in K.render_dockerfile(A.service(a, "orders"))
    assert "proxy_pass http://gateway:3000;" in K.render_nginx(a, A.service(a, "web"))


def test_env_example_and_verify_env_match_compose_variables():
    a = arch()
    names = set(re.findall(r"\$\{([A-Z_]+):\?", K.render_compose(a)))
    example = dict(line.split("=", 1) for line in K.env_example(a).splitlines() if "=" in line and not line.startswith("#"))
    assert names == set(example) == set(K.verify_env(a))
    assert all(v.startswith("<") for v in example.values())


@pytest.mark.skipif(not shutil.which("docker"), reason="docker 가 없다")
def test_compose_file_is_valid_for_docker_compose(tmp_path):
    a = arch()
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose 가 없다")
    for op in K.fixed_ops(a):
        p = tmp_path / op["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(op["content"], encoding="utf-8")
    (tmp_path / "v.yml").write_text(K.verify_override(a), encoding="utf-8")
    (tmp_path / "v.env").write_text("".join(f"{k}={v}\n" for k, v in K.verify_env(a).items()), encoding="utf-8")
    out = subprocess.run(["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "-f", str(tmp_path / "v.yml"),
                          "--env-file", str(tmp_path / "v.env"), "config", "--format", "json"], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    cfg = json.loads(out.stdout)
    assert cfg["services"]["orders"]["ports"][0]["host_ip"] == "127.0.0.1"
    missing = subprocess.run(["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "config"],
                             capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
    assert missing.returncode != 0 and "ORDERS_DB_PASSWORD" in missing.stderr  # 비밀값 없이는 시작하지 않는다


def test_fixed_ops_cover_infra_and_are_marked_fixed():
    a = arch()
    files = {op["file"] for op in K.fixed_ops(a)}
    assert {"docker-compose.yml", "architecture.json", ".env.example", "contract-tests.json", "scripts/contract-test.mjs",
            "ARCHITECTURE.md", "services/orders/Dockerfile", "services/web/nginx.conf"} <= files
    assert all(op["fixed"] for op in K.fixed_ops(a))
    assert K.is_fixed_path(a, "services/orders/Dockerfile") and not K.is_fixed_path(a, "services/orders/app/main.py")


# ── ③ 계약 통합 테스트 ───────────────────────────────────────────────────────

def test_test_cases_and_judge():
    cases = {c["id"]: c for c in A.test_cases(arch())}
    assert cases["orders-health"]["expect"] == "2xx" and "worker-health" not in cases
    assert cases["orders-GET-/api/orders-noauth"]["expect"] == "401|403"
    assert cases["catalog-GET-/api/products/:id"]["path"] == "/api/products/1"
    assert cases["catalog-GET-/api/products/:id"]["expect"] == "!5xx"
    assert cases["gateway-POST-/api/auth/login"]["expect"] == "!5xx,!404"
    assert A.judge("2xx", 204) and not A.judge("2xx", 302) and not A.judge("2xx", None)
    assert A.judge("401|403", 403) and not A.judge("401|403", 200)
    assert A.judge("!5xx,!404", 400) and not A.judge("!5xx,!404", 404) and not A.judge("!5xx", 502)
    assert A.judge("201", 201) and not A.judge("201", 200)


class _Api(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def _send(self):
        status = self.routes.get((self.command, self.path), 404)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _send

    def log_message(self, *a):  # noqa: D401
        pass


@pytest.fixture()
def api_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.mark.skipif(not shutil.which("node"), reason="node 가 없다")
def test_node_runner_matches_python_judge(tmp_path, api_server):
    a, _ = A.normalize({"services": [{"name": "orders", "kind": "api", "language": "node", "port": 3000, "public": True}],
                        "databases": [], "endpoints": [{"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
                                                       {"service": "orders", "method": "POST", "path": "/api/orders"}]})
    _Api.routes = {("GET", "/health"): 200, ("GET", "/api/orders"): 200, ("POST", "/api/orders"): 201}
    (tmp_path / "scripts").mkdir()
    (tmp_path / "contract-tests.json").write_text(K.contract_tests(a), encoding="utf-8")
    (tmp_path / "scripts" / "contract-test.mjs").write_text(K.CONTRACT_RUNNER, encoding="utf-8")
    env = {"PATH": __import__("os").environ["PATH"], "ORDERS_BASE_URL": f"http://127.0.0.1:{api_server.server_address[1]}",
           "NO_PROXY": "127.0.0.1"}
    out = subprocess.run(["node", str(tmp_path / "scripts" / "contract-test.mjs")], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert out.returncode == 1, out.stdout + out.stderr  # 인증 없이 열린 GET /api/orders 를 잡는다
    assert "FAIL GET orders/api/orders → 200" in out.stdout and "PASS POST orders/api/orders → 201" in out.stdout
    _Api.routes[("GET", "/api/orders")] = 401
    out = subprocess.run(["node", str(tmp_path / "scripts" / "contract-test.mjs")], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert out.returncode == 0, out.stdout


# ── ② compose 검증(가짜 Docker + 실제 HTTP) ────────────────────────────────────

def small_arch():
    a, _ = A.normalize({
        "services": [{"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders"], "public": True},
                     {"name": "orders", "kind": "api", "language": "python", "port": 8000, "databases": ["orders-db", "cache"]}],
        "databases": [{"name": "orders-db", "engine": "postgres", "tables": ["orders"]}, {"name": "cache", "engine": "redis"}],
        "endpoints": [{"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
                      {"service": "gateway", "method": "GET", "path": "/api/catalog"}]})
    return a


class FakeDocker:
    """compose 명령별 응답 — 기본은 전부 정상."""

    def __init__(self, port: int, **over):
        self.port = port
        self.calls: list[list[str]] = []
        self.over = over

    def __call__(self, args, timeout):
        self.calls.append(args)
        cmd = args[args.index("--env-file") + 2] if "--env-file" in args else args[1]
        rest = args[args.index("--env-file") + 2:] if "--env-file" in args else args[1:]

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(args, rc, out, err)
        if args[:2] == ["compose", "version"]:
            return done(out="Docker Compose version v2")
        if cmd in self.over:
            v = self.over[cmd]
            return v(rest) if callable(v) else done(*v)
        if cmd == "ps":
            rows = [{"Service": n, "State": "running", "Health": "healthy"} for n in ("gateway", "orders", "orders-db", "cache")]
            return done(out="\n".join(json.dumps(r) for r in rows))
        if cmd == "port":
            return done(out=f"127.0.0.1:{self.port}\n")
        if cmd == "exec" and rest[3:4] == ["psql"]:
            return done(out="orders\n")
        return done()


def _workspace(tmp_path, a):
    (tmp_path / "docker-compose.yml").write_text(K.render_compose(a), encoding="utf-8")
    return tmp_path


def _http_for(routes):
    def http(method, url, body):
        path = "/" + url.split("/", 3)[3]
        return routes.get((method, path), 404), "{}"
    return http


def test_compose_check_passes_and_cleans_up(tmp_path):
    a = small_arch()
    docker = FakeDocker(4000)
    http = _http_for({("GET", "/health"): 200, ("GET", "/api/orders"): 401, ("GET", "/api/catalog"): 200})
    res = C.run(_workspace(tmp_path, a), a, docker=docker, http=http, project="p1")
    assert res["status"] == "passed", res["failures"]
    names = {c["name"] for c in res["checks"]}
    assert {"db:orders-db", "service:orders", "link:gateway->orders", "schema:orders-db", "test:orders-GET-/api/orders-noauth"} <= names
    assert docker.calls[-1][-7:] == ["down", "-v", "--remove-orphans", "--rmi", "local", "--timeout", "5"]
    up = next(c for c in docker.calls if "up" in c)
    assert "--wait" in up and "--build" in up
    env_file = Path(up[up.index("--env-file") + 1])
    assert not env_file.exists()  # 이번 실행만 쓴 비밀값 파일은 지운다


def test_compose_check_reports_every_failure_kind(tmp_path):
    a = small_arch()

    def ps(rest):
        rows = [{"Service": "gateway", "State": "running", "Health": "healthy"},
                {"Service": "orders", "State": "running", "Health": "healthy"},
                {"Service": "orders-db", "State": "running", "Health": "healthy"},
                {"Service": "cache", "State": "running", "Health": "healthy"}]
        return subprocess.CompletedProcess(rest, 0, "\n".join(json.dumps(r) for r in rows), "")

    def exec_(rest):
        if "psql" in rest:
            return subprocess.CompletedProcess(rest, 0, "users\n", "")
        return subprocess.CompletedProcess(rest, 1, "TypeError: fetch failed ECONNREFUSED", "")

    docker = FakeDocker(4000, ps=ps, exec=exec_, logs=(0, 'File "/app/app/main.py", line 12, in list_orders\nKeyError', ""))
    http = _http_for({("GET", "/health"): 200, ("GET", "/api/orders"): 200, ("GET", "/api/catalog"): 500})
    res = C.run(_workspace(tmp_path, a), a, docker=docker, http=http, project="p2")
    kinds = sorted(f["kind"] for f in res["failures"])
    assert res["status"] == "failed" and kinds == ["link", "schema", "test", "test"]
    files = {"services/orders/app/main.py": "@app.get('/api/orders')\ndef list_orders(): ...",
             "services/orders/requirements.txt": "fastapi\n",
             "services/gateway/package.json": json.dumps({"scripts": {"start": "node src/server.js"}}),
             "services/gateway/src/server.js": "app.get('/api/catalog', h)", "services/orders/Dockerfile": "x"}
    issues = C.issues(res, a, files)
    by_code = {}
    for i in issues:
        by_code.setdefault(i["code"], []).append(i["file"])
    assert by_code["COMPOSE_LINK_FAILED"] == ["services/gateway/src/server.js"]
    assert by_code["COMPOSE_SCHEMA_MISSING"] == ["services/orders/app/main.py"]
    assert sorted(by_code["CONTRACT_TEST_FAILED"]) == ["services/gateway/src/server.js", "services/orders/app/main.py"]
    assert all(i["file"] != "services/orders/Dockerfile" for i in issues)  # 고정 파일은 지목하지 않는다
    auth = next(i for i in issues if "401" in i["fix"])
    assert "→ 200" in auth["message"]


def test_compose_check_service_down_uses_logs(tmp_path):
    a = small_arch()

    def ps(rest):
        rows = [{"Service": "gateway", "State": "running", "Health": "healthy"},
                {"Service": "orders", "State": "restarting", "Health": ""},
                {"Service": "orders-db", "State": "running", "Health": "healthy"},
                {"Service": "cache", "State": "running", "Health": "healthy"}]
        return subprocess.CompletedProcess(rest, 0, json.dumps(rows), "")

    docker = FakeDocker(4000, up=(1, "", "container orders is unhealthy"), ps=ps,
                        logs=(0, 'Traceback\n  File "/app/app/db.py", line 3\npsycopg.OperationalError: connection refused', ""))
    http = _http_for({("GET", "/health"): 200, ("GET", "/api/catalog"): 200})
    res = C.run(_workspace(tmp_path, a), a, docker=docker, http=http, project="p3")
    down = next(f for f in res["failures"] if f["kind"] == "service_down")
    assert down["service"] == "orders" and "재시작" in down["title"]
    issue = C.issues(res, a, {"services/orders/app/db.py": "x", "services/orders/app/main.py": "y"})[0]
    assert issue["code"] == "COMPOSE_SERVICE_DOWN" and issue["file"] == "services/orders/app/db.py"
    assert "ORDERS_DB_URL" in issue["fix"]


def test_compose_check_build_failure_and_unavailable(tmp_path):
    a = small_arch()
    log = "#12 [orders 4/6] RUN pip install\n => ERROR [orders 4/6] RUN pip install -r requirements.txt\nfailed to solve: process exit 1"
    res = C.run(_workspace(tmp_path, a), a, docker=FakeDocker(1, up=(1, "", log)), http=_http_for({}), project="p4")
    assert res["status"] == "failed" and res["failures"][0]["kind"] == "build" and res["failures"][0]["service"] == "orders"
    blocked = "failed to resolve reference \"docker.io/library/postgres:16-alpine\": 403 Forbidden"
    res = C.run(_workspace(tmp_path, a), a, docker=FakeDocker(1, up=(1, "", blocked)), http=_http_for({}), project="p5")
    assert res["status"] == "unavailable" and not res["failures"] and C.issues(res, a, {}) == []
    (tmp_path / "docker-compose.yml").unlink()
    assert C.run(tmp_path, a, docker=FakeDocker(1), http=_http_for({}))["status"] == "unavailable"
