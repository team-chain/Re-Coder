"""2.0.10 — 설계에서 빠진 파일 때문에 개발 결과가 빌드되지 않던 문제(실기기 2.0.9 쇼핑몰, Haiku).

화면 30개는 다 만들었는데 서버는 유틸·헬스 등 10개뿐이었다 — server.ts·DB 연결·인증·상품/주문 라우트·Prisma 스키마가
설계 목록에 없었고, 만든 파일은 없는 `../db/client`·`../middleware/auth`·`../utils/errors` 를 불렀다.
"""
import json
import threading
from types import SimpleNamespace

import build_readiness as br
import gen_engine as ge
import node_fixups as nf
from build_readiness import ProjectFiles, analyze


def _files(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ProjectFiles(tmp_path)


# ── 없는 프로젝트 파일 찾기 ─────────────────────────────────────────────────

def test_missing_local_imports_lists_files_and_names():
    files = {
        "backend/src/routes/uploads.ts": ("import { Router } from 'express';\nimport { authenticateJWT } from '../middleware/auth';\n"
                                          "import { AppError } from '../utils/errors';\nimport { uploadToS3 } from '../utils/s3.js';\n"
                                          "import './styles.css';\n"),
        "backend/src/utils/s3.ts": "export const uploadToS3 = 1;\n",
        "backend/src/services/inventory.ts": "import { prisma } from '../db/client';\nimport * as orders from './orders';\n"
                                             "orders.create(); orders.cancel();\n",
        "frontend/src/pages/Home.tsx": "import Header from '../components/Header';\nimport { useCart } from '../hooks/useCart';\n",
    }
    out = ge.missing_local_imports(files)
    assert out["backend/src/middleware/auth.ts"]["names"] == ["authenticateJWT"]
    assert out["backend/src/utils/errors.ts"]["importers"] == ["backend/src/routes/uploads.ts"]
    assert out["backend/src/db/client.ts"]["names"] == ["prisma"]
    assert out["backend/src/services/orders.ts"]["names"] == ["cancel", "create"]
    assert out["frontend/src/components/Header.tsx"]["names"] == ["default(Header)"]
    assert out["frontend/src/hooks/useCart.ts"]["names"] == ["useCart"]
    assert "backend/src/utils/s3.ts" not in out  # NodeNext 식 './s3.js' 는 s3.ts 를 가리킨다
    assert not any(k.endswith(".css") for k in out)


def test_missing_local_imports_respects_files_already_on_disk():
    files = {"src/a.ts": "import { b } from './b';\n"}
    assert "src/b.ts" in ge.missing_local_imports(files)
    assert ge.missing_local_imports(files, lambda p: p == "src/b.ts") == {}


# ── 설계 빈 곳 ─────────────────────────────────────────────────────────────

def _shop_plan():
    """실기기 2.0.9 쇼핑몰 설계에서 서버 쪽에 있던 파일만."""
    paths = ["backend/package.json", "backend/tsconfig.json", "backend/src/payments/stripe.ts", "backend/src/utils/validation.ts",
             "backend/src/utils/payment.ts", "backend/src/utils/s3.ts", "backend/src/middleware/rateLimiter.ts",
             "backend/src/services/inventoryService.ts", "backend/src/routes/health.ts", "backend/src/routes/uploads.ts",
             "frontend/package.json", "frontend/index.html", "frontend/src/main.tsx", "frontend/src/App.tsx",
             "frontend/src/pages/HomePage.tsx"]
    return [{"file": p, "uses": [], "layer": ge._guess_layer(p)} for p in paths]


def test_plan_gaps_adds_entry_db_and_api_routes():
    contracts = ("GET /api/products, POST /api/auth/login, POST /api/orders, GET /api/cart, GET /api/admin/orders, "
                 "POST /api/uploads, POST /api/payments/intent, GET /api/health")
    added = {g["file"] for g in ge.plan_gaps(_shop_plan(), {"summary": "Prisma 쇼핑몰", "contracts": contracts})}
    assert {"backend/src/server.ts", "backend/prisma/schema.prisma", "backend/src/db/client.ts",
            "backend/src/routes/products.ts", "backend/src/routes/auth.ts", "backend/src/routes/orders.ts",
            "backend/src/routes/cart.ts", "backend/src/routes/admin.ts"} <= added
    #: 이미 있는 묶음(uploads·payments·health)과 화면 진입점은 더하지 않는다
    assert not added & {"backend/src/routes/uploads.ts", "backend/src/routes/payments.ts", "frontend/src/App.tsx"}


def test_plan_gaps_follows_uses_and_frontend_entry():
    plan = [{"file": "frontend/package.json", "uses": []},
            {"file": "frontend/src/pages/Cart.tsx", "uses": ["frontend/src/components/CartRow", "react", "../x"]}]
    added = {g["file"]: g for g in ge.plan_gaps(plan, {"summary": "", "contracts": ""})}
    assert "frontend/src/components/CartRow.tsx" in added  # 외부 패키지·상대 경로는 무시
    assert {"frontend/index.html", "frontend/src/main.tsx", "frontend/src/App.tsx"} <= set(added)
    assert not any(k in added for k in ("react", "../x"))


def test_plan_gaps_does_not_replace_existing_project_files():
    on_disk = {"backend/src/server.ts", "backend/src/db/client.ts", "backend/prisma/schema.prisma"}
    plan = [{"file": "backend/package.json", "uses": []}, {"file": "backend/src/routes/products.ts", "uses": []}]
    added = ge.plan_gaps(plan, {"summary": "prisma", "contracts": "GET /api/products"}, lambda p: p in on_disk)
    assert added == []


# ── 엔진: 검토 에이전트 + 다 만든 뒤 없는 파일 마저 만들기 ─────────────────────────────

def test_engine_reviews_plan_and_fills_missing_imports(monkeypatch):
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    plan = [{"file": "backend/package.json", "purpose": "", "layer": 0},
            {"file": "backend/src/routes/uploads.ts", "purpose": "", "layer": 1}]
    contents = {
        "backend/package.json": '{"name":"b","type":"module"}',
        "backend/tsconfig.json": '{"compilerOptions": {"strict": true}}',
        "backend/src/routes/uploads.ts": "import { Router } from 'express';\nimport { AppError } from '../utils/errors';\n"
                                         "const r = Router();\nr.post('/', () => { throw new AppError(400, 'bad'); });\nexport default r;\n",
        "backend/src/routes/products.ts": "import { Router } from 'express';\nexport default Router();\n",
        "backend/src/server.ts": "import express from 'express';\nimport uploads from './routes/uploads';\n"
                                 "import products from './routes/products';\nconst app = express();\n"
                                 "app.use('/api/uploads', uploads);\napp.use('/api/products', products);\napp.listen(3000);\n",
        "backend/src/utils/errors.ts": "export class AppError extends Error { constructor(public status: number, m: string) { super(m); } }\n",
    }
    seen: dict[str, str] = {}
    ops_by_operation: list[str] = []
    lock = threading.Lock()

    def call(self, prompt, schema, operation, max_tokens, agent="", raw=False):
        with lock:
            ops_by_operation.append(operation)
        if operation == "generate_code_manifest":
            return SimpleNamespace(text=json.dumps({"summary": "쇼핑몰", "contracts": "POST /api/uploads, GET /api/products",
                                                    "files": plan, "more": False}))
        if operation == "review_code_manifest":
            assert agent == "review" and "backend/src/routes/uploads.ts" in prompt
            return SimpleNamespace(text=json.dumps({"files": [{"file": "backend/src/routes/products.ts", "purpose": "상품 API",
                                                               "layer": 1}], "more": False}))
        wanted = prompt.rsplit("**아래 파일만**", 1)[1]
        fs = [f for f in contents if f"- {f}\n" in wanted + "\n"]
        with lock:
            for f in fs:
                seen[f] = prompt
        return SimpleNamespace(text=json.dumps({"summary": "", "ops": [{"action": "create", "file": f, "content": contents[f]}
                                                                        for f in fs]}))

    events: list[dict] = []
    eng = ge.LargeGeneration("요청", concurrency=2, security_review=False, new_project=True, emit=events.append)
    eng.call = call.__get__(eng)
    _data, ops, _ = eng.run()
    made = {o["file"] for o in ops}
    assert made == set(contents)  # 검토가 찾은 라우트 + 결정적으로 더한 진입점 + 불러오는데 없던 오류 유틸
    assert ops_by_operation.count("review_code_manifest") >= 1
    #: 마저 만든 파일은 불러오는 쪽 코드를 보고 쓴다(이름·인자 맞추기)
    assert "throw new AppError(400, 'bad')" in seen["backend/src/utils/errors.ts"]
    added = [e for e in events if e.get("step") == "plan_added"]
    assert any("backend/src/utils/errors.ts" in e["message"] for e in added)
    assert any(e.get("agent") == "review" for e in added)


# ── 결정적 교정 ─────────────────────────────────────────────────────────────

def test_esm_import_extensions_for_nodenext_module(tmp_path):
    files = _files(tmp_path, {
        "backend/package.json": '{"name":"b","type":"module"}',
        "backend/tsconfig.json": '{"compilerOptions": {"module": "NodeNext", "moduleResolution": "NodeNext"}}',
        "backend/src/server.ts": ("import health from './routes/health';\nimport { prisma } from './db';\n"
                                  "import { x } from './lib/x.js';\nimport pkg from '../package.json';\nimport 'dotenv/config';\n"
                                  "const m = await import('./routes/health');\nexport * from './missing';\n"),
        "backend/src/routes/health.ts": "export default 1;\n",
        "backend/src/db/index.ts": "export const prisma = 1;\n",
        "backend/src/lib/x.ts": "export const x = 1;\n",
    })
    notes, writes = nf.esm_import_extensions(files)
    out = writes["backend/src/server.ts"]
    assert "from './routes/health.js'" in out and "from './db/index.js'" in out and "import('./routes/health.js')" in out
    assert "from './lib/x.js'" in out and "'../package.json'" in out and "'./missing'" in out  # 이미 있음·JSON·없는 파일은 그대로
    assert notes


def test_esm_import_extensions_leaves_commonjs_alone(tmp_path):
    files = _files(tmp_path, {
        "package.json": '{"name":"b"}',
        "tsconfig.json": '{"compilerOptions": {"module": "NodeNext"}}',
        "src/a.ts": "import { b } from './b';\n", "src/b.ts": "export const b = 1;\n",
    })
    assert nf.esm_import_extensions(files) == ([], {})


def test_readiness_declares_unknown_packages_with_registry_version(tmp_path, monkeypatch):
    import node_manifests
    import npm_registry
    monkeypatch.setattr(node_manifests, "_registry_version",
                        lambda name: npm_registry.suggest("", ["1.0.0", "1.2.3", "2.0.0-beta.1"]))
    _files(tmp_path, {
        "package.json": json.dumps({"name": "b", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.21.2"}}),
        "server.js": "const express = require('express');\nconst multer = require('multer');\nconst z = require('left-pad-xyz');\n"
                     "const app = express();\napp.listen(3000);\n",
    })
    result = analyze(tmp_path)
    issue = next(i for i in result.issues if i.code == "NODE_UNDECLARED_DEPENDENCY")
    assert issue.auto_fix
    pkg = json.loads(result.fix_data["file_writes"]["NODE_UNDECLARED_DEPENDENCY"]["package.json"])
    assert pkg["dependencies"]["multer"] == "^2.0.2"         # 검증된 표
    assert pkg["dependencies"]["left-pad-xyz"] == "^1.2.3"   # 표에 없으면 레지스트리 최신 정식 버전


def test_real_shop_result_gets_fixable_build_issues(tmp_path):
    """실기기 결과물 일부 — 확장자·패키지는 자동 수정 대상, 없는 파일은 그대로 문제(생성 단계가 마저 만든다)."""
    _files(tmp_path, {
        "package.json": json.dumps({"name": "b", "type": "module", "main": "dist/server.js",
                                    "scripts": {"build": "tsc", "start": "node dist/server.js"},
                                    "dependencies": {"express": "^4.18.2"}, "devDependencies": {"typescript": "^5.3.3"}}),
        "tsconfig.json": json.dumps({"compilerOptions": {"module": "NodeNext", "moduleResolution": "NodeNext",
                                                         "rootDir": "./src", "outDir": "./dist"}}),
        "src/server.ts": "import express from 'express';\nimport uploads from './routes/uploads';\nconst app = express();\n"
                         "app.use('/api/uploads', uploads);\napp.listen(process.env.PORT || 3000);\n",
        "src/routes/uploads.ts": "import { Router } from 'express';\nimport multer from 'multer';\nimport { S3Client } from '@aws-sdk/client-s3';\n"
                                 "import { up } from '../utils/s3';\nconst r = Router();\nexport default r;\n",
        "src/utils/s3.ts": "export const up = 1;\n",
    })
    result = analyze(tmp_path)
    codes = {i.code: i for i in result.issues}
    assert codes["NODE_ESM_IMPORT_EXTENSION"].auto_fix
    assert codes["NODE_UNDECLARED_DEPENDENCY"].auto_fix
    pkg = json.loads(result.fix_data["file_writes"]["NODE_UNDECLARED_DEPENDENCY"]["package.json"])
    assert pkg["dependencies"]["@aws-sdk/client-s3"] == "^3.700.0" and pkg["dependencies"]["multer"] == "^2.0.2"
    assert "NODE_ESM_IMPORT_EXTENSION" in br.FILE_WRITE_FIXES


def test_fill_missing_files_creates_files_the_fix_round_cannot(monkeypatch):
    """전체 점검 중 부분 교정은 새 파일을 만들지 못한다 — 없는 파일은 만드는 쪽으로 넘긴다."""
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    ops = [{"action": "create", "file": "api/src/routes/a.ts", "content": "import { prisma } from '../db/client';\nprisma.user.findMany();\n"}]
    prompts = []

    def call(self, prompt, schema, operation, max_tokens, agent="", raw=False):
        prompts.append(prompt)
        return SimpleNamespace(text=json.dumps({"summary": "", "ops": [
            {"action": "create", "file": "api/src/db/client.ts", "content": "export const prisma = {} as any;\n"}]}))
    monkeypatch.setattr(ge.LargeGeneration, "call", call)
    out = ge.fill_missing_files("요청", ops, contracts="User 테이블", workers=1)
    assert [o["file"] for o in out] == ["api/src/routes/a.ts", "api/src/db/client.ts"]
    assert "prisma.user.findMany()" in prompts[0] and "User 테이블" in prompts[0]
    assert ge.fill_missing_files("요청", out, workers=1) == out  # 더 만들 것이 없으면 그대로
