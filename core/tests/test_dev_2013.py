"""2.0.13 — 실기기 2.0.12 쇼핑몰(ECS 배포 성공)의 화면 품질: 스타일 없음·상품 0개·로딩 안 끝남·₩49 표시·결제 결과 빈 화면,
그리고 자동 수정을 하나씩 눌러야 하던 불편. 모두 실제 결과물을 브라우저로 끝까지(가입→장바구니→결제→주문 목록) 눌러 보며 찾았다."""
import json

import build_readiness as br
import node_fixups as nf
from build_readiness import ProjectFiles, analyze


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


TW_PAGE = ('export default function Home() { return <div className="min-h-screen bg-gray-50 flex flex-col">'
           '<header className="bg-white shadow"><div className="max-w-7xl mx-auto px-4 py-4 flex justify-between items-center">'
           '<h1 className="text-2xl font-bold text-blue-600">Shop</h1><button className="px-4 py-2 rounded bg-blue-600 text-white">로그인</button>'
           '</div></header></div>; }\n')


def _vite(root, extra=None, pkg_extra=None):
    pkg = {"name": "ui", "type": "module", "scripts": {"build": "vite build"},
           "devDependencies": {"vite": "^5.0.0", **(pkg_extra or {})}}
    return _write(root, {"package.json": json.dumps({"name": "app", "scripts": {"build": "npm --prefix frontend run build"}}),
                         "frontend/package.json": json.dumps(pkg),
                         "frontend/index.html": '<div id="root"></div><script type="module" src="/src/main.tsx"></script>',
                         "frontend/src/main.tsx": "import React from 'react';\nimport App from './App';\nimport './styles/index.css';\n",
                         "frontend/src/App.tsx": TW_PAGE,
                         "frontend/src/styles/index.css": "/* ReCoder: 코드가 불러오지만 AI 가 만들지 않은 스타일 파일 — 필요한 스타일을 채우세요. */\n",
                         **(extra or {})})


# ── Tailwind 연결 ────────────────────────────────────────────────────────────

def test_tailwind_classes_without_config_get_wired(tmp_path):
    _vite(tmp_path, pkg_extra={"tailwindcss": "^3.3.0", "postcss": "^8.4.0"})
    notes, writes = nf.tailwind_setup(ProjectFiles(tmp_path))
    assert "export default" in writes["frontend/tailwind.config.js"] and "./src/**/*" in writes["frontend/tailwind.config.js"]
    assert "tailwindcss: {}" in writes["frontend/postcss.config.js"]
    css = writes["frontend/src/styles/index.css"]
    assert css.startswith("@tailwind base;") and "ReCoder:" not in css
    assert json.loads(writes["frontend/package.json"])["devDependencies"]["autoprefixer"]
    _write(tmp_path, {k: v for k, v in writes.items()})
    assert nf.tailwind_setup(ProjectFiles(tmp_path)) == ([], {})


def test_tailwind_v4_uses_import_and_postcss_plugin(tmp_path):
    _vite(tmp_path, pkg_extra={"tailwindcss": "^4.1.0"})
    _notes, writes = nf.tailwind_setup(ProjectFiles(tmp_path))
    assert "frontend/tailwind.config.js" not in writes
    assert "@tailwindcss/postcss" in writes["frontend/postcss.config.js"]
    assert writes["frontend/src/styles/index.css"].startswith('@import "tailwindcss";')


def test_entry_without_stylesheet_gets_one(tmp_path):
    _vite(tmp_path, {"frontend/src/main.tsx": "import React from 'react';\nimport App from './App';\nrender(App);\n"})
    (tmp_path / "frontend/src/styles/index.css").unlink()
    _notes, writes = nf.tailwind_setup(ProjectFiles(tmp_path))
    assert "import './index.css';" in writes["frontend/src/main.tsx"]
    assert writes["frontend/src/index.css"].startswith("@tailwind base;")


def test_plain_css_app_is_not_touched_and_missing_style_gets_base_styles(tmp_path):
    _vite(tmp_path, {"frontend/src/App.tsx": 'export default () => <div className="product-card"><h1 className="title">x</h1></div>;\n'})
    assert nf.tailwind_setup(ProjectFiles(tmp_path)) == ([], {})
    content = nf.missing_style_content(ProjectFiles(tmp_path), "frontend/src/styles/other.css")
    assert "ReCoder 기본 스타일" in content and "button" in content


def test_missing_style_for_tailwind_screen_is_directives(tmp_path):
    _vite(tmp_path)
    assert nf.missing_style_content(ProjectFiles(tmp_path), "frontend/src/styles/x.css").startswith("@tailwind base;")


# ── 상태 훅 · API 경로 · 상태 확인 · 원화 ─────────────────────────────────────

USE_API = """import { useCallback, useRef } from 'react';
import client from '../api/client';

export function useApi<T = unknown>(options?: { onSuccess?: (d: unknown) => void }) {
  const stateRef = useRef<{ data: T | null; loading: boolean }>({
    data: null,
    loading: false,
  });

  const execute = useCallback(
    async (url: string): Promise<T | null> => {
      stateRef.current.loading = true;
      const response = await client.get<T>(url);
      stateRef.current.data = response.data;
      stateRef.current.loading = false;
      if (options?.onSuccess) {
        options.onSuccess(response.data);
      }
      return response.data;
    },
    [options]
  );

  return { data: stateRef.current.data, loading: stateRef.current.loading, execute };
}
"""
CLIENT = "import axios from 'axios';\nconst API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';\nconst client = axios.create({\n  baseURL: API_BASE_URL,\n});\nexport default client;\n"


def test_ref_only_hook_rerenders_and_reads_latest_options(tmp_path):
    _write(tmp_path, {"src/hooks/useApi.ts": USE_API})
    _notes, writes = nf.ref_state_hooks(ProjectFiles(tmp_path))
    new = writes["src/hooks/useApi.ts"]
    assert "import { useCallback, useRef, useReducer } from 'react';" in new
    assert "const [, recoderRerender] = useReducer((n: number) => n + 1, 0);" in new
    assert new.count("recoderRerender();") == 2           # 로딩 시작 · 결과 반영(연속 대입은 한 번)
    assert "recoder_options.current?.onSuccess" in new and "[options]" not in new
    assert "stateRef.current.data = null" not in new.split("useReducer")[1].split("useCallback")[0]


def test_hook_that_already_uses_state_is_left_alone(tmp_path):
    _write(tmp_path, {"src/hooks/useApi.ts": USE_API.replace("useRef } from", "useRef, useState } from") + "const [a] = useState(0);\n"})
    assert nf.ref_state_hooks(ProjectFiles(tmp_path)) == ([], {})


def test_api_prefix_through_wrapper_and_env_fallback(tmp_path):
    _write(tmp_path, {
        "package.json": '{"name":"ui","devDependencies":{"vite":"^5.0.0"}}',
        "src/api/client.ts": CLIENT, "src/hooks/useApi.ts": USE_API, ".env.example": "VITE_API_BASE_URL=/api\n",
        "src/pages/Home.tsx": "import { useApi } from '../hooks/useApi';\nconst { execute } = useApi();\nexecute(`/api/products?page=1`);\nexecute('/products/1');\n",
    })
    result = analyze(tmp_path, online=False)
    assert "NODE_CLIENT_API_DOUBLE_PREFIX" in [i.code for i in result.issues]
    names = result.fix_data["api_double_prefix"]["src/pages/Home.tsx"]
    fixed = br.api_prefix_rewrite((tmp_path / "src/pages/Home.tsx").read_text(encoding="utf-8"), names)
    assert "execute(`/products?page=1`)" in fixed and "execute('/products/1')" in fixed


def test_env_defined_base_url_is_not_flagged(tmp_path):
    _write(tmp_path, {
        "package.json": '{"name":"ui","devDependencies":{"vite":"^5.0.0"}}',
        "src/api/client.ts": CLIENT, "src/hooks/useApi.ts": USE_API, ".env": "VITE_API_BASE_URL=https://api.example.com\n",
        "src/pages/Home.tsx": "const { execute } = useApi();\nexecute('/api/products');\n",
    })
    assert "NODE_CLIENT_API_DOUBLE_PREFIX" not in [i.code for i in analyze(tmp_path, online=False).issues]


def test_health_alias_when_client_asks_api_health(tmp_path):
    _write(tmp_path, {
        "backend/src/app.ts": "import express from 'express';\nconst app = express();\napp.get('/health', (req, res) => res.json({ ok: true }));\n",
        "frontend/src/api/client.ts": CLIENT,
        "frontend/src/pages/PaymentResult.tsx": "executeHealth('/health', 'GET');\n",
    })
    _notes, writes = nf.health_alias(ProjectFiles(tmp_path))
    assert "app.get(['/health', '/api/health'], (req, res)" in writes["backend/src/app.ts"]


def test_krw_formatter_stops_dividing_by_100(tmp_path):
    _write(tmp_path, {"src/utils/currency.ts": (
        "export function centsToWon(cents: number): number {\n  return cents / 100;\n}\n"
        "export function wonToCents(won: number): number {\n  return Math.round(won * 100);\n}\n"
        "export function formatPrice(c: number) { return new Intl.NumberFormat('ko-KR', { style: 'currency', currency: 'KRW' }).format(centsToWon(c)); }\n"
        "export const pct = (x: number) => x / 100;\n")})
    _notes, writes = nf.krw_minor_units(ProjectFiles(tmp_path))
    new = writes["src/utils/currency.ts"]
    assert "return cents;" in new and "Math.round(won)" in new and "x / 100" in new


def test_usd_formatter_is_left_alone(tmp_path):
    _write(tmp_path, {"src/utils/currency.ts": "export function centsToDollars(c) { return c / 100; }\nconst f = { currency: 'USD' };\n"})
    assert nf.krw_minor_units(ProjectFiles(tmp_path)) == ([], {})


# ── 샘플 데이터 스크립트 ──────────────────────────────────────────────────────

def test_seed_script_outside_build_is_moved_in(tmp_path):
    _write(tmp_path, {
        "backend/package.json": json.dumps({"name": "api", "type": "module",
                                            "scripts": {"build": "tsc", "init-db": "tsx src/scripts/init-db.ts"}}),
        "backend/tsconfig.json": '{"compilerOptions":{"rootDir":"./src","outDir":"./dist"}}',
        "backend/src/db/connection.ts": "export const query = 1;\n",
        "backend/src/db/schema.sql": "CREATE TABLE x (id int);\n",
        "backend/scripts/init-db.ts": ("import path from 'path';\nimport { query } from '../src/db/connection.js';\n"
                                       "const schemaPath = path.join(__dirname, '../src/db/schema.sql');\n"),
    })
    _notes, writes = nf.seed_script_into_build(ProjectFiles(tmp_path))
    moved = writes["backend/src/scripts/init-db.ts"]
    assert moved.startswith("// @ts-nocheck")
    assert "from '../db/connection.js'" in moved
    assert "path.join(__dirname, '../../src/db/schema.sql')" in moved     # dist/scripts 에서 원본 자원
    assert "import '../src/scripts/init-db.js';" in writes["backend/scripts/init-db.ts"]
    assert "backend/package.json" not in writes                             # 경로가 이미 새 위치를 가리킴


def test_seed_command_runs_init_db_from_the_build(tmp_path):
    import local_services
    _write(tmp_path, {
        "Dockerfile": "# ReCoder 검증 Dockerfile — 화면·서버 폴더형 Node 앱\nFROM node:22-alpine\n",
        "backend/package.json": json.dumps({"name": "api", "scripts": {"build": "tsc", "start": "node dist/server.js",
                                                                       "init-db": "tsx src/scripts/init-db.ts"}}),
        "backend/tsconfig.json": '{"compilerOptions":{"rootDir":"./src","outDir":"./dist"}}',
        "backend/src/server.ts": "import express from 'express';\nconst app = express();\napp.listen(3000);\n",
        "backend/src/scripts/init-db.ts": "console.log(1);\n",
    })
    seed = local_services.seed_command(str(tmp_path))
    assert seed is None or seed["script"].endswith("dist/scripts/init-db.js")


# ── 한 번에 고치기 ────────────────────────────────────────────────────────────

def test_apply_all_fixes_applies_everything_and_reports(tmp_path):
    _vite(tmp_path, pkg_extra={"tailwindcss": "^3.3.0"})
    _write(tmp_path, {"frontend/src/utils/currency.ts": (
        "export function centsToWon(cents: number): number {\n  return cents / 100;\n}\n"
        "export const f = { currency: 'KRW' };\n")})
    result = br.apply_all_fixes(tmp_path)
    assert set(result["codes"]) >= {"NODE_TAILWIND_NOT_CONFIGURED", "NODE_KRW_MINOR_UNITS"}
    assert (tmp_path / "frontend/tailwind.config.js").is_file()
    assert "return cents;" in (tmp_path / "frontend/src/utils/currency.ts").read_text(encoding="utf-8")
    assert not [r for r in result["remaining"] if r["code"] in result["codes"]]
    assert br.apply_all_fixes(tmp_path)["codes"] == []                      # 두 번 눌러도 안전


def test_fix_all_endpoint(tmp_path):
    import main
    from fastapi.testclient import TestClient
    app = main.create_app()
    app.state.session_token = "t" * 32
    client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555))
    _vite(tmp_path, pkg_extra={"tailwindcss": "^3.3.0"})
    resp = client.post("/api/deploy/readiness/fix-all", json={"workspace_path": str(tmp_path),
                                                                 "codes": ["NODE_TAILWIND_NOT_CONFIGURED"]},
                       headers={"X-Session-Token": "t" * 32})
    assert resp.status_code == 200 and "NODE_TAILWIND_NOT_CONFIGURED" in resp.json()["codes"]


def test_health_alias_goes_to_the_app_router_not_the_mock_server(tmp_path):
    _write(tmp_path, {
        "backend/src/app.ts": "import express from 'express';\nimport healthRouter from './routes/health.js';\nconst app = express();\napp.use('/', healthRouter);\n",
        "backend/src/routes/health.ts": "import { Router } from 'express';\nconst router = Router();\nrouter.get('/health', (q, s) => s.json({}));\nexport default router;\n",
        "backend/src/mockPaymentServer.ts": "import express from 'express';\nconst app = express();\napp.get('/health', (q, s) => s.json({}));\n",
        "frontend/src/pages/Pay.tsx": "fetch('/api/health');\n",
    })
    _notes, writes = nf.health_alias(ProjectFiles(tmp_path))
    assert list(writes) == ["backend/src/routes/health.ts"]
    assert "router.get(['/health', '/api/health']," in writes["backend/src/routes/health.ts"]


def test_router_already_under_api_is_left_alone(tmp_path):
    _write(tmp_path, {
        "backend/src/app.ts": "import express from 'express';\nimport healthRouter from './routes/health.js';\nconst app = express();\napp.use('/api', healthRouter);\n",
        "backend/src/routes/health.ts": "import { Router } from 'express';\nconst router = Router();\nrouter.get('/health', (q, s) => s.json({}));\n",
        "frontend/src/pages/Pay.tsx": "fetch('/api/health');\n",
    })
    assert nf.health_alias(ProjectFiles(tmp_path)) == ([], {})
