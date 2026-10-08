"""배포 준비 점검·자동 수정이 **정상 코드를 잘못 고치지 않는지** — 적대적 검토에서 나온 오탐 사례 회귀 테스트."""
import json
from pathlib import Path

import pytest

import build_readiness as br
import code_agent as ca
import local_services
from api.routes.deploy import _entrypoint_from_node_command


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")


def codes(root):
    return {i.code: i for i in br.analyze(root).issues}


# ---------------------------------------------------------------------------
# 패키지 이름 오타 추정
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,declared,imported,expected", [
    ("@stripe/js", {"@stripe/stripe-js", "@stripe/react-stripe-js"}, {"@stripe/react-stripe-js"}, "@stripe/stripe-js"),
    ("@stripe/js", {"@stripe/stripe-js", "@stripe/react-stripe-js"}, set(), "@stripe/stripe-js"),
    ("@aws-sdk/client-s3", {"@aws-sdk/client-dynamodb"}, set(), None),        # 공통 접두어만 같다
    ("react", {"react-dom"}, {"react-dom"}, None),                              # 이미 쓰는 패키지로는 바꾸지 않는다
    ("expres", {"express"}, set(), "express"),
    ("@mui/icon-material", {"@mui/icons-material"}, set(), "@mui/icons-material"),
    ("lodash", {"axios", "express"}, set(), None),
    # 이름을 그대로 품고 덧붙인 다른 패키지로는 바꾸지 않는다
    ("@stripe/stripe-js", {"@stripe/react-stripe-js"}, set(), None),
    ("@mui/material", {"@mui/icons-material"}, set(), None),
    ("passport", {"passport-jwt"}, set(), None),
    ("redis", {"ioredis"}, set(), None),
    ("uuid", {"uuidv4"}, set(), None),
    ("date-fns", {"date-fns-tz"}, set(), None),
    ("react-router", {"react-router-dom"}, set(), None),
    ("socket.io", {"socket.io-client"}, set(), None),
    ("bcyrpt", {"bcrypt"}, set(), "bcrypt"),
])
def test_closest_declared(name, declared, imported, expected):
    assert br._closest_declared(name, declared, imported) == expected


# ---------------------------------------------------------------------------
# JSX-in-.js: 서버 파일·설정 파일·비교 연산은 건드리지 않는다
# ---------------------------------------------------------------------------

VITE_APP = {
    "package.json": {"name": "app", "scripts": {"build": "vite build", "start": "node server.js"},
                     "dependencies": {"express": "^4", "react": "^18", "react-dom": "^18"},
                     "devDependencies": {"vite": "^5", "@vitejs/plugin-react": "^4"}},
    "index.html": '<div id="root"></div><script type="module" src="/src/main.jsx"></script>',
    "src/main.jsx": "import React from 'react';\nexport default function A() { return <div />; }\n",
}


def test_server_js_with_generics_and_comparisons_is_not_jsx(tmp_path):
    _write(tmp_path, {**VITE_APP,
                      "server.js": "const express = require('express');\nconst app = express();\n"
                                   "const ok = (a, b) => a < b && b > a;\napp.get('/health', (q, s) => s.json({ ok }));\n"
                                   "app.listen(process.env.PORT || 3000);\n",
                      "vite.config.js": "import { defineConfig } from 'vite';\nexport default defineConfig({ server: { port: Number(process.env.PORT) } });\n"})
    found = codes(tmp_path)
    assert "NODE_VITE_JSX_IN_JS" not in found
    assert "NODE_VITE_PROCESS_ENV" not in found


def test_react_component_in_js_is_flagged(tmp_path):
    _write(tmp_path, {**VITE_APP,
                      "src/App.js": "import React from 'react';\nexport default function App() {\n  return (\n    <div>hi</div>\n  );\n}\n"})
    assert codes(tmp_path)["NODE_VITE_JSX_IN_JS"].auto_fix


# ---------------------------------------------------------------------------
# import 이름 검사: TypeScript·재수출·펼침은 판단하지 않는다
# ---------------------------------------------------------------------------

def test_ts_exports_and_js_extension_imports_resolve(tmp_path):
    _write(tmp_path, {
        "package.json": {"name": "s", "type": "module", "scripts": {"start": "node dist/index.js"}, "dependencies": {"express": "^4"}},
        "src/index.ts": "import { createApp } from './app.js';\nimport type { Config } from './config.js';\ncreateApp();\n",
        "src/app.ts": "export const createApp = () => {};\n",
        "src/config.ts": "export interface Config { port: number }\n",
    })
    found = codes(tmp_path)
    assert "NODE_LOCAL_IMPORT_MISSING" not in found
    assert "NODE_IMPORT_NAME_MISSING" not in found


def test_reexports_and_spread_exports_are_not_judged():
    assert br._esm_exports("export * from './x';\nexport const a = 1;") is None
    assert br._esm_exports("export const { a, b } = obj;") is None
    assert br._cjs_exports("module.exports = { ...base, a };") is None
    assert br._cjs_exports("module.exports = router;") is None
    assert br._cjs_exports("module.exports = {\n  pool,\n  async initialize() {},\n};") is None  # 메서드 본문 — 판단하지 않는다
    assert br._cjs_exports("module.exports = {\n  pool,\n  initializeDatabase,\n};\nmodule.exports.extra = 1;") == {"pool", "initializeDatabase", "extra"}


def test_context_with_spread_value_is_not_judged(tmp_path):
    _write(tmp_path, {**VITE_APP,
                      "src/Ctx.jsx": "import { createContext } from 'react';\nexport const CartContext = createContext();\n"
                                     "export function P({ children }) { const state = {}; return <CartContext.Provider value={{ ...state, add }}>{children}</CartContext.Provider>; }\n",
                      "src/Use.jsx": "import { useContext } from 'react';\nimport { CartContext } from './Ctx';\n"
                                     "export default function U() { const { items, add } = useContext(CartContext); return <div>{items}</div>; }\n"})
    assert "NODE_CONTEXT_MEMBER_MISSING" not in codes(tmp_path)


# ---------------------------------------------------------------------------
# localhost 직접 호출: 설정·테스트·템플릿 리터럴은 정상
# ---------------------------------------------------------------------------

MONOREPO = {
    "package.json": {"name": "shop", "scripts": {"start": "node server/index.js", "build": "cd client && npm run build"}},
    "server/package.json": {"name": "srv", "dependencies": {"express": "^4"}},
    "server/index.js": "const express = require('express');\nconst app = express();\napp.get('/api/products', (q, s) => s.json([]));\napp.listen(3001);\n",
    "client/package.json": {"name": "cli", "scripts": {"build": "vite build"}, "dependencies": {"react": "^18", "axios": "^1"},
                            "devDependencies": {"vite": "^5"}},
    "client/index.html": '<script type="module" src="/src/main.jsx"></script>',
    "client/src/main.jsx": "export default 1;\n",
}


def test_localhost_in_config_and_tests_is_fine_but_in_screen_code_is_flagged(tmp_path):
    _write(tmp_path, {**MONOREPO,
                      "client/vite.config.js": "export default { server: { proxy: { '/api': 'http://localhost:3001' } } };\n",
                      "client/src/__tests__/api.test.js": "fetch('http://localhost:3001/api/products');\n",
                      "client/src/api.js": "const base = `http://localhost:${port}`;\nexport default base;\n"})
    assert "NODE_CLIENT_HARDCODED_LOCALHOST" not in codes(tmp_path)
    _write(tmp_path, {"client/src/api2.js": "import axios from 'axios';\nexport const api = axios.create({ baseURL: 'http://localhost:3001/api' });\n"})
    issue = codes(tmp_path)["NODE_CLIENT_HARDCODED_LOCALHOST"]
    assert issue.auto_fix and "client/src/api2.js" in issue.message


def test_relative_api_rewrite_keeps_template_variables():
    assert br.relative_api_rewrite("axios.create({ baseURL: 'http://localhost:3001/api' })") == "axios.create({ baseURL: '/api' })"
    assert br.relative_api_rewrite("fetch(`http://localhost:3001/api/items/${id}`)") == "fetch(`/api/items/${id}`)"
    assert br.relative_api_rewrite("const u = 'http://localhost:3001';") == "const u = '';"
    assert br.relative_api_rewrite("const u = 'http://localhost:3001' + path;") == "const u = '' + path;"
    for keep in ("const u = `http://localhost:${PORT}/api`;", "const u = `http://localhost:${PORT}`;"):
        assert br.relative_api_rewrite(keep) == keep


# ---------------------------------------------------------------------------
# <a href> → <Link>: 다운로드·새 창·API·파일·다른 Link 는 그대로
# ---------------------------------------------------------------------------

def test_router_link_rewrite_skips_non_route_anchors():
    src = ("import { Routes } from 'react-router-dom';\n"
           "<a href=\"/products\">목록</a>\n"
           "<a href=\"/api/export\">CSV</a>\n"
           "<a href=\"/files/manual.pdf\" download>설명서</a>\n"
           "<a href=\"/help\" target=\"_blank\">도움말</a>\n")
    out = br.router_link_rewrite(src)
    assert '<Link to="/products">목록</Link>' in out
    assert '<a href="/api/export">CSV</a>' in out
    assert 'download>설명서</a>' in out and 'target="_blank">도움말</a>' in out
    assert out.startswith("import { Routes, Link } from 'react-router-dom';")
    assert out.count("Link }") == 1


def test_router_link_rewrite_leaves_files_using_another_link():
    src = "import Link from 'next/link';\n<a href=\"/x\">x</a>\n"
    assert br.router_link_rewrite(src) == src


def test_router_link_rewrite_other_link_guard_is_per_line():
    src = "import { Link } from 'react-router-dom'\nimport axios from 'axios'\n<a href=\"/x\">x</a>\n"
    assert '<Link to="/x">x</Link>' in br.router_link_rewrite(src)


def test_router_link_rewrite_does_not_duplicate_existing_link_import():
    src = "import { Link, useNavigate } from 'react-router-dom';\n<a href=\"/x\">x</a>\n"
    out = br.router_link_rewrite(src)
    assert out.count("Link") == 3  # import 한 번 + <Link ...></Link>
    assert "import { Link } from" not in out


# ---------------------------------------------------------------------------
# pg NUMERIC 파서: 기존 import 바인딩을 재사용한다
# ---------------------------------------------------------------------------

def test_pg_numeric_parser_reuses_binding():
    cjs = "const { Pool } = require('pg');\nconst pool = new Pool();\n"
    out = br.pg_numeric_parser_rewrite(cjs)
    assert "require('pg').types.setTypeParser(1700" in out and out.index("setTypeParser") < out.index("new Pool")
    esm = "import pg from 'pg';\nconst pool = new pg.Pool();\n"
    out = br.pg_numeric_parser_rewrite(esm)
    assert "pg.types.setTypeParser(1700" in out and "import recoderPg" not in out
    esm_named = "import { Pool } from 'pg';\n"
    out = br.pg_numeric_parser_rewrite(esm_named)
    assert "import recoderPg from 'pg';" in out and "recoderPg.types.setTypeParser" in out
    assert br.pg_numeric_parser_rewrite(out) == out  # 두 번 적용해도 한 번만
    typed = "import type { PoolConfig } from 'pg';\nimport { Pool } from 'pg';\n"
    out = br.pg_numeric_parser_rewrite(typed)
    assert "type.types" not in out and "recoderPg.types.setTypeParser" in out


# ---------------------------------------------------------------------------
# 서버가 화면 제공: 404 처리기 앞, express 앱 변수 사용, 라우트보다 앞의 로거는 무시
# ---------------------------------------------------------------------------

def test_serve_frontend_goes_before_404_handler_and_uses_express_app():
    src = ("const express = require('express');\nconst http = require('http');\nconst app = express();\n"
           "const server = http.createServer(app);\n"
           "app.use((req, res, next) => { next(); });\n"
           "app.use('/api/products', require('./routes/products'));\n"
           "app.get('/health', (req, res) => res.json({ ok: true }));\n"
           "app.use((req, res) => res.status(404).json({ error: 'Not found' }));\n"
           "app.use((err, req, res, next) => res.status(500).end());\n"
           "server.listen(PORT);\n")
    out = br.serve_frontend(src, "server/index.js", "client/dist")
    assert "app.use(require('express').static(recoderUiDir))" in out and "server.use(" not in out
    assert out.index("/health") < out.index("recoderUiDir") < out.index("404")
    assert "'../client/dist'" in out


def test_serve_frontend_factory_app_and_wrapped_server():
    factory = ("const express = require('express');\nfunction createApp() { const app = express(); return app; }\n"
               "const server = createApp();\nserver.get('/api/x', (q, s) => s.json({}));\nserver.listen(3000);\n")
    out = br.serve_frontend(factory, "index.js", "dist")
    assert "server.use(require('express').static" in out and "\napp." not in out
    wrapped = ("const express = require('express');\nconst admin = express();\nconst app = express();\n"
               "const server = require('http').createServer(app);\napp.get('/api/x', (q, s) => s.json({}));\n"
               "app.use((req, res) => res.status(404).end());\nserver.listen(3000);\n")
    out = br.serve_frontend(wrapped, "index.js", "dist")
    assert "app.use(require('express').static" in out and "server.use(" not in out and "admin.use(" not in out
    assert out.index("/api/x") < out.index("recoderUiDir") < out.index("404")


def test_serve_frontend_router_variable_counts_as_route():
    src = ("const express = require('express');\nconst app = express();\n"
           "app.use((req, res, next) => { next(); });\napp.use(cors());\napp.use(express.json());\n"
           "app.use(routes);\napp.use((req, res) => res.status(404).json({}));\napp.listen(3000);\n")
    out = br.serve_frontend(src, "index.js", "dist")
    assert out.index("app.use(routes)") < out.index("recoderUiDir") < out.index("404")


@pytest.mark.parametrize("tail", ["app.use('*', (req, res) => res.status(404).end());\n",
                                  "app.use(notFoundHandler);\napp.use(errorHandler);\n",
                                  "app.all('*', (req, res) => res.status(404).end());\n", ""])
def test_serve_frontend_before_each_catch_all_shape(tail):
    src = "const express = require('express');\nconst app = express();\napp.get('/api/x', (req, res) => res.json({}));\n" + tail + "app.listen(3000);\n"
    out = br.serve_frontend(src, "index.js", "dist")
    assert out.index("/api/x") < out.index("recoderUiDir") < out.index("app.listen")
    if tail:
        assert out.index("recoderUiDir") < out.index(tail.splitlines()[0])


# ---------------------------------------------------------------------------
# 동반 DB 환경변수 매핑
# ---------------------------------------------------------------------------

def test_companion_kinds_do_not_clobber_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(local_services, "_HOME", tmp_path)
    names = ["DB_HOST", "DB_PORT", "DB_URL", "MONGODB_URI", "REDIS_HOST", "REDIS_URL", "DB_POOL_SIZE", "DB_SSL"]
    env, _ = local_services.plan("shop", ["postgres", "redis"], names)
    assert env["DB_HOST"] == "shop-postgres" and env["DB_PORT"] == "5432" and env["DB_URL"].startswith("postgresql://")
    assert env["REDIS_HOST"] == "shop-redis" and env["REDIS_URL"] == "redis://shop-redis:6379"
    assert "MONGODB_URI" not in env and "DB_POOL_SIZE" not in env and "DB_SSL" not in env
    env, _ = local_services.plan("mern", ["mongodb", "redis"], names)
    assert env["MONGODB_URI"].startswith("mongodb://mern-mongodb") and env["DB_HOST"] == "mern-mongodb"
    assert env["REDIS_URL"].startswith("redis://")
    env, _ = local_services.plan("cache", ["redis"], names)
    assert set(env) == {"REDIS_URL", "REDIS_HOST"}


def test_app_env_maps_split_db_variables_and_urls():
    env = local_services.app_env("shop", "postgres", "pw", ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_PORT",
                                                             "DATABASE_URL", "PG_CONNECTION_STRING", "STRIPE_SECRET_KEY", "PORT"])
    assert env["DB_HOST"] == "shop-postgres" and env["DB_USER"] == "recoder" and env["DB_PASSWORD"] == "pw"
    assert env["DB_NAME"] == "app" and env["DB_PORT"] == "5432"
    assert env["DATABASE_URL"].startswith("postgresql://recoder:pw@shop-postgres:5432/app")
    assert env["PG_CONNECTION_STRING"] == env["DATABASE_URL"]
    assert "STRIPE_SECRET_KEY" not in env and "PORT" not in env
    mongo = local_services.app_env("shop", "mongodb", "pw", ["MONGO_HOST", "MONGODB_URI"])
    assert mongo["MONGO_HOST"] == "shop-mongodb" and mongo["MONGODB_URI"].startswith("mongodb://shop-mongodb:27017/app")


# ---------------------------------------------------------------------------
# 경로 정규화·CMD 진입점
# ---------------------------------------------------------------------------

def test_norm_op_path_strips_all_leading_dots_and_slashes():
    assert ca._norm_op_path("././src/App.jsx") == "src/app.jsx"
    assert ca._norm_op_path(".\\src\\App.jsx") == "src/app.jsx"
    assert ca._norm_op_path("/src//App.jsx") == "src/app.jsx"


@pytest.mark.parametrize("cmd,expected", [
    ("node index.js", "index.js"),
    ("cd server && node index.js", "server/index.js"),
    ("cd server && cd src && node index.js", "server/src/index.js"),
    ("cd /app && node index.js", None),
    ("cd .. && node index.js", None),
    ("NODE_ENV=production node --enable-source-maps dist/main.js", "dist/main.js"),
])
def test_entrypoint_from_node_command(cmd, expected):
    assert _entrypoint_from_node_command(cmd) == expected
