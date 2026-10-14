"""2.0.14 — 실기기 2.0.13 쇼핑몰(job 4a06, Haiku): 설계 목록이 화면 30개와 모의 결제 서버 package.json 뿐이라 서버 없이
"완료" 됐고, 배포 점검은 스택을 몰라(unknown) FastAPI Dockerfile 을 자동 수정으로 제안해 Docker·ECS 모두 막혔다."""
import json
from pathlib import Path

import code_agent as ca
import gen_engine as ge
import payment_kit

#: 실기기 job 4a06 의 설계 목록(화면만) — 요약·약속에는 Express·Prisma·/api 가 있었다.
FRONT_ONLY = ["frontend/src/types/index.ts", "frontend/src/api/client.ts", "frontend/src/context/AuthContext.tsx",
              "frontend/src/pages/HomePage.tsx", "frontend/src/App.tsx", "frontend/src/main.tsx", "frontend/index.html",
              "frontend/package.json", "frontend/tsconfig.json",
              "mock-payment-server/package.json", "mock-payment-server/tsconfig.json"]
MANIFEST = {"summary": "PostgreSQL, Prisma ORM, Express.js 백엔드와 React 프론트엔드, JWT 인증, 모의 결제, 샘플 상품 20개",
            "contracts": "POST /api/auth/login · GET /api/products · GET /api/cart · POST /api/orders · POST /api/payments/intent · GET /api/health"}


def _files(paths):
    return [{"file": p, "layer": ge._guess_layer(p), "uses": []} for p in paths]


def test_contract_api_without_any_server_gets_a_backend_plan():
    gaps = {g["file"]: g for g in ge.plan_gaps(_files(FRONT_ONLY), MANIFEST, lambda p: False)}
    for need in ("backend/package.json", "backend/tsconfig.json", "backend/src/server.ts", "backend/prisma/schema.prisma",
                 "backend/src/db/client.ts", "backend/src/middleware/auth.ts", "backend/src/routes/products.ts",
                 "backend/src/routes/orders.ts", "backend/src/routes/auth.ts", "backend/src/scripts/seed.ts"):
        assert need in gaps, need
    assert "backend/src/routes/health.ts" not in gaps
    assert all(g["planned_by"] == "recoder" for g in gaps.values())


def test_sql_database_scaffold_without_prisma():
    manifest = {"summary": "Express + PostgreSQL 카페", "contracts": "GET /api/menus · POST /api/orders"}
    gaps = {g["file"] for g in ge.plan_gaps(_files(["frontend/src/App.jsx", "frontend/src/main.jsx"]), manifest, lambda p: False)}
    assert {"backend/package.json", "backend/src/db/connection.js", "backend/src/db/schema.sql",
            "backend/src/routes/menus.js", "backend/src/server.js"} <= gaps
    assert "backend/tsconfig.json" not in gaps


def test_frontend_only_request_gets_no_server():
    manifest = {"summary": "정적 소개 페이지", "contracts": "화면: /, /about"}
    assert not [g for g in ge.plan_gaps(_files(["frontend/src/App.tsx"]), manifest, lambda p: False) if g["file"].startswith("backend/")]


def test_plan_adds_root_deploy_files_once_backend_exists(monkeypatch):
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False, new_project=True, emit=lambda e: None)
    monkeypatch.setattr(eng, "_review_plan", lambda files, manifest: [])
    out = {f["file"] for f in eng._complete_plan(_files(FRONT_ONLY), MANIFEST)}
    assert {"package.json", "Dockerfile", ".dockerignore", "backend/src/server.ts"} <= out


def test_ai_mock_payment_server_is_dropped_even_without_server():
    kept, ops, _ = payment_kit.plan_with_kit(_files(FRONT_ONLY))
    assert not [f for f in kept if f["file"].startswith("mock-payment-server/")] and ops == []


def test_frontend_calling_api_without_server_is_reported():
    ops = [{"action": "create", "file": "frontend/src/api/client.ts", "content": "axios.create({ baseURL: '/api' })"},
           {"action": "create", "file": "mock-payment-server/src/index.ts", "content": "express()"}]
    issues = ca._server_missing_issues(ops)
    assert issues and issues[0]["code"] == "GENERATED_SERVER_MISSING" and issues[0]["severity"] == "error"
    ops.append({"action": "create", "file": "backend/src/server.ts", "content": "express()"})
    assert ca._server_missing_issues(ops) == []
