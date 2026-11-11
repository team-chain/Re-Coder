"""3.1 계약 대조 — 설계도(아키텍처 계약)와 실제 코드가 같은지 근거(파일·줄)와 함께 본다."""
from __future__ import annotations

import json

import architecture as A
import contract_check as C

RAW = {
    "services": [
        {"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders"], "public": True},
        {"name": "orders", "kind": "api", "language": "python", "port": 8000, "databases": ["orders-db", "cache"]},
        {"name": "catalog", "kind": "api", "language": "node", "port": 3001, "databases": ["catalog-db"]},
        {"name": "worker", "kind": "worker", "language": "python", "databases": ["orders-db"]},
    ],
    "databases": [{"name": "orders-db", "engine": "postgres", "tables": ["orders"]}, {"name": "cache", "engine": "redis"},
                  {"name": "catalog-db", "engine": "mongodb"}],
    "endpoints": [
        {"service": "gateway", "method": "GET", "path": "/api/orders", "auth": True},
        {"service": "gateway", "method": "GET", "path": "/api/catalog/:id"},
        {"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
        {"service": "orders", "method": "POST", "path": "/api/orders", "auth": True},
        {"service": "orders", "method": "GET", "path": "/api/orders/{order_id}", "auth": True},
        {"service": "orders", "method": "DELETE", "path": "/api/orders/:id", "auth": True},
        {"service": "catalog", "method": "GET", "path": "/api/products"},
    ],
}

GATEWAY = """import express from "express";
import catalogRouter from "./routes/catalog.js";
const app = express();
const ORDERS = process.env.ORDERS_URL;
const { CATALOG_URL, PORT } = process.env;
const db = process.env["ORDERS_DB_URL"];
app.use("/api/catalog", catalogRouter);
app.get("/api/orders", async (req, res) => res.json(await (await fetch(ORDERS + "/api/orders")).json()));
app.get("/api/debug", (req, res) => res.json({ db }));
app.listen(PORT);
"""
CATALOG_ROUTER = """import { Router } from "express";
const router = Router();
router.get("/:id", async (req, res) => res.json(await (await fetch(process.env.CATALOG_URL + "/api/products")).json()));
export default router;
"""
ORDERS_MAIN = """import os
from fastapi import FastAPI
from app.routers import orders
from .routers.admin import router as admin_router

app = FastAPI()
app.include_router(orders.router, prefix="/api")
app.include_router(admin_router)
DB = os.environ["ORDERS_DB_URL"]
CACHE = os.getenv("CACHE_URL")
"""
ORDERS_ROUTER = """from fastapi import APIRouter
router = APIRouter(prefix="/orders")

@router.get("")
def list_orders(): ...

@router.post("/")
def create(): ...

@router.get("/{order_id}")
def one(order_id: int): ...
"""
ADMIN_ROUTER = """from fastapi import APIRouter
router = APIRouter(prefix="/admin")

@router.get("/stats")
def stats(): ...
"""
WORKER = """import os, psycopg
conn = psycopg.connect(os.environ.get("ORDERS_DB_URL"))
legacy = "postgres://app:pw@orders-db:5432/orders_db"
"""
CATALOG = """const express = require('express');
const app = express();
app.get('/api/products', (req, res) => res.json([]));
const url = process.env.CATALOG_DB_URL;
"""

FILES = {
    "services/gateway/src/server.js": GATEWAY,
    "services/gateway/src/routes/catalog.js": CATALOG_ROUTER,
    "services/gateway/src/server.test.js": "process.env.ORDERS_DB_URL",  # 테스트 파일은 보지 않는다
    "services/orders/app/__init__.py": "",
    "services/orders/app/main.py": ORDERS_MAIN,
    "services/orders/app/routers/__init__.py": "",
    "services/orders/app/routers/orders.py": ORDERS_ROUTER,
    "services/orders/app/routers/admin.py": ADMIN_ROUTER,
    "services/worker/app/worker.py": WORKER,
    "services/catalog/index.js": CATALOG,
}


def arch():
    a, _ = A.normalize(RAW)
    return a


def by_code(result):
    out = {}
    for f in result["findings"]:
        out.setdefault(f["code"], []).append(f)
    return out


def test_env_reads_cover_js_and_python_forms():
    assert [v for v, _ in C.env_reads(GATEWAY, False)] == ["ORDERS_URL", "ORDERS_DB_URL", "CATALOG_URL", "PORT"]
    assert [v for v, _ in C.env_reads(ORDERS_MAIN, True)] == ["ORDERS_DB_URL", "CACHE_URL"]


def test_routes_resolve_mounts_and_router_prefixes():
    js = {(m, C.norm_path(p)) for m, p, _, _ in C._js_routes({k: v for k, v in FILES.items() if k.startswith("services/gateway/")})}
    assert {("GET", "/api/orders"), ("GET", "/api/catalog/:p"), ("GET", "/api/debug")} <= js
    py = {(m, C.norm_path(p)) for m, p, _, _ in C._py_routes({k: v for k, v in FILES.items() if k.startswith("services/orders/")})}
    assert py == {("GET", "/api/orders"), ("POST", "/api/orders"), ("GET", "/api/orders/:p"), ("GET", "/admin/stats")}
    flask = C._py_routes({"a.py": "@app.route('/x/<int:id>', methods=['GET', 'PUT'])\ndef f(): ..."})
    assert {(m, C.norm_path(p)) for m, p, _, _ in flask} == {("GET", "/x/:p"), ("PUT", "/x/:p")}


def test_check_reports_violations_with_evidence():
    res = C.check(arch(), FILES)
    codes = by_code(res)
    foreign = codes["CONTRACT_DB_FOREIGN"]
    assert [(f["service"], f["file"], f["line"]) for f in foreign] == [("gateway", "services/gateway/src/server.js", 6)]
    assert "orders" in foreign[0]["message"] and foreign[0]["severity"] == "error"
    undeclared = codes["CONTRACT_CALL_UNDECLARED"]
    assert {f["service"] for f in undeclared} == {"gateway"} and "catalog" in undeclared[0]["message"]
    missing = codes["CONTRACT_ENDPOINT_MISSING"]
    assert [f["message"] for f in missing] == ["계약 API DELETE /api/orders/:id 를 orders 코드에서 찾지 못했습니다"]
    assert missing[0]["severity"] == "warning"
    extra = {f["message"] for f in codes["CONTRACT_ENDPOINT_EXTRA"]}
    assert extra == {"계약에 없는 API GET /api/debug", "계약에 없는 API GET /admin/stats"}
    hard = codes["CONTRACT_HARDCODED_HOST"]
    assert [(f["service"], f["line"]) for f in hard] == [("worker", 3)]
    unused = {(f["service"], f["code"]) for f in res["findings"] if f["code"].endswith("UNUSED")}
    assert unused == set()  # 계약의 의존·DB 는 모두 실제로 쓴다
    assert res["summary"] == {"error": 2, "warning": 2, "info": 2}
    edges = {(e["from"], e["to"], e["status"]) for e in res["edges"]}
    assert ("gateway", "orders", "ok") in edges and ("gateway", "orders-db", "violation") in edges
    assert ("gateway", "catalog", "violation") in edges and ("orders", "cache", "ok") in edges
    assert not any(f["file"].endswith(".test.js") for f in res["findings"])


def test_clean_project_has_no_errors_and_unused_contract_is_flagged():
    files = dict(FILES)
    files["services/gateway/src/server.js"] = GATEWAY.replace('const db = process.env["ORDERS_DB_URL"];', "const db = null;") \
        .replace("const { CATALOG_URL, PORT } = process.env;", "const { PORT } = process.env;")
    files["services/gateway/src/routes/catalog.js"] = CATALOG_ROUTER.replace("process.env.CATALOG_URL", '"/x"')
    files["services/orders/app/main.py"] = ORDERS_MAIN.replace('CACHE = os.getenv("CACHE_URL")', "")
    res = C.check(arch(), files)
    assert res["summary"]["error"] == 0
    assert any(f["code"] == "CONTRACT_DB_UNUSED" and f["service"] == "orders" for f in res["findings"])
    assert ("orders", "cache", "unused") in {(e["from"], e["to"], e["status"]) for e in res["edges"]}


def test_missing_service_folder_is_an_error_and_issues_skip_info():
    files = {k: v for k, v in FILES.items() if not k.startswith("services/catalog/")}
    res = C.check(arch(), files)
    assert any(f["code"] == "CONTRACT_SERVICE_MISSING" and f["service"] == "catalog" for f in res["findings"])
    issues = C.issues(arch(), FILES)
    assert issues and not any(i["code"] == "CONTRACT_ENDPOINT_EXTRA" for i in issues)
    assert any("server.js:6" in i["message"] for i in issues if i["code"] == "CONTRACT_DB_FOREIGN")


def test_check_workspace_reads_contract_and_files(tmp_path):
    assert C.check_workspace(tmp_path)["available"] is False
    (tmp_path / "architecture.json").write_text(json.dumps(RAW), encoding="utf-8")
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "services/gateway/node_modules/x").mkdir(parents=True)
    (tmp_path / "services/gateway/node_modules/x/index.js").write_text("process.env.ORDERS_DB_URL", encoding="utf-8")
    res = C.check_workspace(tmp_path)
    assert res["available"] and res["summary"]["error"] == 2
    assert {s["name"] for s in res["services"]} == {"gateway", "orders", "catalog", "worker"}
    assert not any("node_modules" in f["file"] for f in res["findings"])
