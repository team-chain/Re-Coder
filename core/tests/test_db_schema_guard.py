"""같은 컨테이너 이름으로 다른 앱을 배포했던 DB — 띄우기 전에 구조를 비교하고, 데모는 데모 전용 DB 를 쓴다."""
from __future__ import annotations

import http.server
import subprocess
import threading
from pathlib import Path

import pytest

import deploy_settings
import local_services as ls
from api.routes import deploy as routes

SQL = """CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  email VARCHAR(255) UNIQUE NOT NULL,
  price NUMERIC(10, 2) NOT NULL DEFAULT 0, -- 금액
  CONSTRAINT users_email_check CHECK (email <> '')
);
create table products (id serial primary key, name text, stock integer, FOREIGN KEY (id) REFERENCES users(id));
"""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "_HOME", tmp_path / "ls")
    monkeypatch.setattr(ls, "_docker_name_exists", lambda name: False)


def fake_psql(rows: list[str], code: int = 0):
    def run(args, timeout=0):
        return subprocess.CompletedProcess(args, code, "\n".join(rows), "")
    return run


def test_초기화_SQL에서_테이블과_열을_읽는다():
    assert ls.expected_schema(SQL) == {"users": {"id", "email", "price"}, "products": {"id", "name", "stock"}}


def test_빈_DB와_맞는_DB는_통과_다른_앱의_DB는_멈춘다(tmp_path):
    sql = tmp_path / "schema.sql"
    sql.write_text(SQL)
    assert ls.schema_status("shop", sql, fake_psql([]))["ok"] is True
    good = ["users|id", "users|email", "users|price", "products|id", "products|name", "products|stock"]
    assert ls.schema_status("shop", sql, fake_psql(good))["ok"] is True
    other = ["products|id", "products|name", "products|image", "todos|id", "todos|title"]
    bad = ls.schema_status("shop", sql, fake_psql(other))
    assert bad["ok"] is False and bad["missing_tables"] == ["users"]
    assert bad["missing_columns"] == {"products": ["stock"]} and bad["other_tables"] == ["todos"]
    assert ls.schema_status("shop", sql, fake_psql([], code=1))["ok"] is True  # 확인 못 하면 막지 않는다


def test_새_DB로_시작하면_다음_세대_이름을_쓰고_예전_DB는_그대로(tmp_path):
    assert ls.service_container("shop", "postgres") == "shop-postgres"
    assert ls.start_new_db("shop") == "shop-postgres-2"
    assert ls.service_container("shop", "postgres") == "shop-postgres-2"
    env, _ = ls.plan("shop", ["postgres"], ["DATABASE_URL"])
    assert "@shop-postgres-2:5432/" in env["DATABASE_URL"]
    assert ls._load("shop")["variants"] == {"real": 1}  # plan() 이 세대 정보를 지우지 않는다


def test_새_DB는_예전에_남은_같은_이름의_컨테이너나_볼륨을_건너뛴다(monkeypatch):
    taken = {"shop-postgres-2", "shop-postgres-3"}
    monkeypatch.setattr(ls, "_docker_name_exists", lambda name: name in taken)
    assert ls.start_new_db("shop") == "shop-postgres-4"
    assert ls._load("shop")["variants"] == {"real": 3}


def test_도커_이름_확인은_컨테이너와_데이터_볼륨을_본다(monkeypatch):
    monkeypatch.undo()
    seen = []
    def run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0 if args[1] == "volume" else 1, "", "")
    monkeypatch.setattr(ls.subprocess, "run", run)
    assert ls._docker_name_exists("shop-postgres-2") is True
    assert seen == [["docker", "container", "inspect", "shop-postgres-2"], ["docker", "volume", "inspect", "shop-postgres-2-data"]]
    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker")))
    assert ls._docker_name_exists("x") is False


def test_데모는_데모_전용_DB를_쓴다(monkeypatch):
    monkeypatch.setattr(ls, "_demo", lambda c: True)
    assert ls.service_container("shop", "postgres") == "shop-postgres-demo"
    assert ls.start_new_db("shop") == "shop-postgres-demo-2"
    monkeypatch.setattr(ls, "_demo", lambda c: False)
    assert ls.service_container("shop", "postgres") == "shop-postgres"


def test_그대로_사용은_같은_DB_같은_구조에서만_다시_묻지_않는다():
    ls.accept_schema("shop", "fp1")
    assert ls.schema_accepted("shop", "fp1") and not ls.schema_accepted("shop", "fp2")
    ls.start_new_db("shop")
    assert not ls.schema_accepted("shop", "fp1")


def test_조회_API_경로를_찾는다(tmp_path):
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/src/server.js").write_text(
        "app.use('/api/auth', a);\napp.use('/api/products', p);\napp.use('/api/webhooks', w);\napp.get('/api/config', c);\n"
        "app.use('/api/admin', x);\n")
    assert routes._api_probe_paths(str(tmp_path)) == ["/api/products", "/api/config"]


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(500 if self.path == "/api/products" else 200)
        self.end_headers()


def test_조회_API가_500이면_로그에서_DB_구조_문제로_진단한다(tmp_path, monkeypatch):
    (tmp_path / "server.js").write_text("app.use('/api/products', p);\n")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    real_run = subprocess.run
    monkeypatch.setattr(routes.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, "Get products error: error: column \"stock\" does not exist\n  code: '42703',\n", "")
        if args[:2] == ["docker", "logs"] else real_run(args, **kw))
    plan = type("P", (), {"ports": {str(port): "3001"}, "container_name": "shop"})()
    try:
        out = routes._probe_app_api(plan, str(tmp_path))
    finally:
        srv.shutdown()
    assert out["status"] == "error" and out["http_status"] == 500 and out["path"] == "/api/products"
    assert out["diagnosis"]["code"] == "DB_SCHEMA_MISMATCH" and "`stock`" in out["diagnosis"]["cause"]
