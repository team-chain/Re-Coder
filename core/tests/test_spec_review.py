"""명세 화면(요구사항 · 설계 · 작업) — 계약을 기능으로 묶고 코드 · 통합 테스트 · AI 검토로 상태를 붙인다."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import compose_deploy
import contract_check
import spec_review as R

ARCH = {
    "services": [
        {"name": "web", "kind": "web", "port": 3000, "public": True, "depends_on": ["gateway"]},
        {"name": "gateway", "kind": "api", "language": "node", "port": 8080, "public": True, "depends_on": ["orders"]},
        {"name": "orders", "kind": "api", "language": "node", "port": 3003, "databases": ["orders-db"]},
    ],
    "databases": [{"name": "orders-db", "engine": "postgres"}],
    "endpoints": [
        {"service": "gateway", "method": "POST", "path": "/api/auth/login", "auth": False, "expect": 200},
        {"service": "gateway", "method": "GET", "path": "/api/orders", "auth": True, "expect": 200},
        {"service": "gateway", "method": "POST", "path": "/api/products", "auth": False, "expect": 201},
        {"service": "gateway", "method": "GET", "path": "/api/products/:id/reviews", "auth": False},
        {"service": "orders", "method": "GET", "path": "/orders", "auth": True, "expect": 200},
    ],
}
GATEWAY = """
import { Controller, Get, Post } from '@nestjs/common';
@Controller('api')
export class GatewayController {
  constructor() { requireEnv('ORDERS_URL'); }
  @Post('auth/login') login() {}
  @Get('orders') list() {}
  @Post('products') create() {}
  @Get('products/:id/reviews') reviews() {}
}
"""
ORDERS = """
@Controller('orders')
export class OrdersController {
  @Get() mine() {}
}
const url = requireEnv('ORDERS_DB_URL');
"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    ws = tmp_path / "shop"
    (ws / "services/gateway/src").mkdir(parents=True)
    (ws / "services/orders/src").mkdir(parents=True)
    (ws / "services/web/src").mkdir(parents=True)
    (ws / "architecture.json").write_text(json.dumps(ARCH), encoding="utf-8")
    (ws / "services/gateway/src/gateway.controller.ts").write_text(GATEWAY, encoding="utf-8")
    (ws / "services/orders/src/orders.controller.ts").write_text(ORDERS, encoding="utf-8")
    (ws / "README.md").write_text("# 쇼핑몰\n회원 · 상품 · 주문", encoding="utf-8")
    monkeypatch.setenv("RECODER_SPEC_DIR", str(tmp_path / "spec"))
    monkeypatch.setenv("RECODER_COMPOSE_STORE", str(tmp_path / "deploys.json"))
    return ws


def test_nest_routes_are_found_with_controller_prefix_and_global_prefix():
    files = {"a/main.ts": "app.setGlobalPrefix('v1');", "a/x.controller.ts": "@Controller('orders')\nclass X {\n@Get(':id') a(){}\n@Post() b(){}\n}"}
    got = sorted((m, p) for m, p, _, _ in contract_check._nest_routes(files))
    assert got == [("GET", "/v1/orders/:id"), ("POST", "/v1/orders")]


def test_env_helpers_and_nginx_wired_web_are_not_reported_unused(project):
    found = contract_check.check_workspace(str(project))
    codes = {(f["code"], f["service"]) for f in found["findings"]}
    assert ("CONTRACT_ENDPOINT_MISSING", "gateway") not in codes
    assert ("CONTRACT_DEP_UNUSED", "gateway") not in codes
    assert ("CONTRACT_DEP_UNUSED", "web") not in codes
    assert ("CONTRACT_DB_UNUSED", "orders") not in codes


def test_spec_groups_public_apis_into_features_and_marks_code(project):
    spec = R.build(str(project))
    labels = [f["label"] for f in spec["features"]]
    assert labels[:4] == ["회원가입 · 로그인", "상품", "리뷰", "주문 · 결제"]
    assert labels[-1] == "서비스 간 호출"
    assert all(e["implemented"] for e in spec["endpoints"])
    review = next(f for f in spec["features"] if f["label"] == "리뷰")
    assert review["endpoints"][0]["path"] == "/api/products/:id/reviews"
    # 실행 기록이 없으면 상태는 모름 + "로컬 실행" 작업
    assert {f["status"] for f in spec["features"]} == {"unknown"}
    assert any(t["id"] == "run:local" for t in spec["tasks"])


def test_rule_tasks_flag_unauthenticated_writes_but_not_login(project):
    titles = [t["title"] for t in R.build(str(project))["tasks"]]
    assert any("POST /api/products" in t and "로그인 없이" in t for t in titles)
    assert not any("/api/auth/login" in t for t in titles)


def test_last_local_run_results_become_criterion_status(project):
    spec = R.build(str(project))
    cases = {c["test"]: c for f in spec["features"] for c in f["criteria"]}
    login = next(k for k in cases if k.endswith("/api/auth/login"))
    orders = next(k for k in cases if k.endswith("GET-/api/orders-noauth"))
    record = {"id": "x", "project": compose_deploy.project_name(project), "workspace": str(project), "at": "2026-11-19T00:00:00+00:00",
              "status": "degraded", "check_results": [{"name": f"test:{login}", "ok": True, "status": 200},
                                                      {"name": f"test:{orders}", "ok": False, "status": 200}]}
    Path(compose_deploy._records_file()).write_text(json.dumps([record]), encoding="utf-8")
    spec = R.build(str(project))
    feats = {f["label"]: f for f in spec["features"]}
    assert feats["주문 · 결제"]["status"] == "fail"
    assert feats["회원가입 · 로그인"]["status"] == "ok"
    failed = [t for t in spec["tasks"] if t["id"].startswith("test:")]
    assert failed and "실제 200" in failed[0]["detail"]
    assert spec["tests"] == {"passed": 1, "total": 2, "at": "2026-11-19T00:00:00+00:00", "status": "degraded"}
    assert spec["deploy"]["status"] == "degraded" and spec["deploy"]["url"].startswith("http://localhost:")
    assert set(spec["deploy"]["services"].values()) == {"unknown"}


def test_project_summary_shows_each_service_from_last_run(project):
    assert R.build(str(project))["deploy"] is None
    arch = json.loads((project / "architecture.json").read_text(encoding="utf-8"))
    first, second = arch["services"][0]["name"], arch["services"][1]["name"]
    record = {"id": "y", "project": compose_deploy.project_name(project), "workspace": str(project), "at": "2026-11-19T01:00:00+00:00",
              "status": "failed", "failures": ["x 시작 못 함"],
              "check_results": [{"name": f"service:{first}", "ok": True}, {"name": f"service:{second}", "ok": False}]}
    Path(compose_deploy._records_file()).write_text(json.dumps([record]), encoding="utf-8")
    d = R.build(str(project))["deploy"]
    assert d["services"][first] == "ok" and d["services"][second] == "fail"
    assert d["url"] == "" and d["failures"] == ["x 시작 못 함"]


def test_ai_review_is_saved_merged_and_dropped_when_contract_changes(project):
    seen = {}

    async def complete(prompt: str) -> str:
        seen["prompt"] = prompt
        return "생각...\n" + json.dumps({
            "summary": "주문 흐름은 갖췄고 환불이 없습니다",
            "requirements": [{"feature": "주문 · 결제", "criteria": [
                {"cond": "WHEN", "text": "주문하면 재고를 예약하고 결제한다", "status": "ok"},
                {"cond": "WHEN", "text": "결제 완료 주문을 취소하면 환불한다", "status": "missing"}]}],
            "tasks": [{"title": "결제 완료 주문 환불", "detail": "환불 API 와 재고 복원", "feature": "주문 · 결제", "severity": "high"}],
            "missing": ["환불"]}, ensure_ascii=False)

    spec = asyncio.run(R.run_review(str(project), complete))
    assert "쇼핑몰" in seen["prompt"] and "POST /api/products" in seen["prompt"]
    order = next(f for f in spec["features"] if f["label"] == "주문 · 결제")
    assert order["criteria"][0]["source"] == "ai" and order["criteria"][1]["status"] == "missing"
    assert order["status"] in ("warn", "fail")
    assert any(t["source"] == "ai" and t["title"] == "결제 완료 주문 환불" for t in spec["tasks"])
    assert spec["review"]["fresh"] is True and spec["review"]["summary"].startswith("주문 흐름")
    # 계약이 바뀌면 옛 검토는 붙이지 않는다(낡았다고 표시)
    arch = json.loads((project / "architecture.json").read_text(encoding="utf-8"))
    arch["endpoints"].append({"service": "gateway", "method": "GET", "path": "/api/cart", "auth": True})
    (project / "architecture.json").write_text(json.dumps(arch), encoding="utf-8")
    spec = R.build(str(project))
    assert spec["review"]["fresh"] is False
    assert not any(t["source"] == "ai" for t in spec["tasks"])


def test_bad_ai_output_is_an_error_not_a_crash(project):
    async def complete(prompt: str) -> str:
        return "죄송합니다"
    with pytest.raises(ValueError):
        asyncio.run(R.run_review(str(project), complete))


def test_no_contract_is_reported_plainly(tmp_path):
    assert R.build(str(tmp_path))["available"] is False
