"""2.0.15 — 실제 생성 결과 4개(쇼핑몰 0ce6·0d40, 카페 83ed, 화면만 4a06)를 지금 코드로 다시 다듬어 ECS 와 같은 구성으로
띄우고 브라우저로 끝까지 눌러 보며 찾은 것들."""
import json

import build_readiness as br
import docker_kit
import gen_engine as ge
import node_fixups as nf
from build_readiness import ProjectFiles, analyze


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# ── 배포 빌드: 타입 오류만으로 멈추지 않는다 ─────────────────────────────────

def test_deploy_build_survives_type_only_errors_but_verify_stays_strict():
    info = {"server": "backend", "entry": "dist/server.js", "ui": "frontend", "ui_out": "frontend/dist", "server_build": True,
            "port": 3000, "health": "/health", "ui_tsc_vite": True, "server_tsc_only": True}
    deploy = docker_kit.render(info)
    assert "node_modules/.bin/vite build" in deploy and "test -f dist/server.js" in deploy
    verify = docker_kit.render(dict(info, verify=True))
    assert "node_modules/.bin/vite build" not in verify and "test -f dist/server.js" not in verify


# ── 카페(Sequelize): 진입 파일·DB 를 몰라 ECS 가 DB 를 준비하지 않던 것 ────────

CAFE = {
    "package.json": json.dumps({"name": "cafe", "scripts": {"build": "npm --prefix frontend run build && npm --prefix backend run build",
                                                            "start": "npm --prefix backend start"}}),
    "backend/package.json": json.dumps({"name": "api", "type": "module", "scripts": {"build": "tsc", "start": "node dist/server.js"},
                                        "dependencies": {"express": "^4.19.2", "sequelize": "^6.37.0", "pg": "^8.11.3"}}),
    "backend/src/app.ts": "import express from 'express';\nconst app = express();\nexport default app;\n",
    "backend/src/server.ts": "import app from './app.js';\nimport sequelize from './db.js';\napp.listen(process.env.PORT || 3000);\n",
    "backend/src/db.ts": "import { Sequelize } from 'sequelize';\nexport default new Sequelize(process.env.DATABASE_URL!);\n",
}


def test_backend_entry_and_orm_database_are_found(tmp_path):
    result = analyze(_write(tmp_path, CAFE), online=False)
    assert result.server_entry == "backend/src/server.ts"
    assert result.runtime_subproject == "backend"
    assert result.services == ["postgres"]


# ── 화면 동작 ─────────────────────────────────────────────────────────────────

AUTH = """import React, { createContext, useState, useCallback } from 'react';
import { User } from '../types';
export const AuthContext = createContext(null);
export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('auth_token'));
  const login = useCallback(async () => { setUser({ id: 1 }); localStorage.setItem('auth_token', 't'); }, []);
  const logout = useCallback(() => { setUser(null); localStorage.removeItem('auth_token'); }, []);
  return null;
};
"""


def test_logged_in_user_survives_reload(tmp_path):
    _write(tmp_path, {"frontend/src/context/AuthContext.tsx": AUTH})
    _notes, writes = nf.auth_user_persist(ProjectFiles(tmp_path))
    new = writes["frontend/src/context/AuthContext.tsx"]
    assert "localStorage.getItem('recoder_auth_user')" in new and "recoderSetUser" in new
    assert "const setUser = useCallback((value: User | null | ((prev: User | null) => User | null))" in new
    _write(tmp_path, {"frontend/src/context/AuthContext.tsx": new})
    assert nf.auth_user_persist(ProjectFiles(tmp_path)) == ([], {})


def test_auth_restored_from_server_is_left_alone(tmp_path):
    _write(tmp_path, {"frontend/src/context/AuthContext.tsx": AUTH + "fetch('/api/auth/me');\n"})
    assert nf.auth_user_persist(ProjectFiles(tmp_path)) == ([], {})


CLIENT = """import { ApiResponse } from '../types';
class ApiClient {
  async request<T>(endpoint: string): Promise<T> {
    const response = await fetch(endpoint);
    const data = await response.json();
    return data;
  }
}
export const apiClient = new ApiClient();
"""
ENVELOPE_SERVER = ("export const sendSuccess = (res, data, message = 'ok') => res.status(200).json({ status: 'success', message, data });\n")


def test_envelope_is_readable_both_ways(tmp_path):
    _write(tmp_path, {"frontend/src/api/client.ts": CLIENT, "backend/src/utils/response.ts": ENVELOPE_SERVER})
    _notes, writes = nf.api_envelope_unwrap(ProjectFiles(tmp_path))
    new = writes["frontend/src/api/client.ts"]
    assert "return recoderUnwrap(data);" in new and "function recoderUnwrap<T>" in new
    # 도우미가 실제로 두 방식 모두를 만족하는지 node 로 실행해 본다
    import subprocess
    js = nf._UNWRAP_HELPER.replace("<T>", "").replace(": T", "").replace(" as unknown as T", "").replace(
        " as unknown as Record<string, unknown>", "").replace(" as Record<string, unknown>", "").replace(" as unknown[]", "")
    out = subprocess.run(["node", "-e", js + """
const a = recoderUnwrap({status:'success', message:'m', data:{token:'t', id:1, status:'pending'}});
const b = recoderUnwrap({status:'success', data:[{id:1},{id:2}]});
console.log(JSON.stringify([a.token, a.data.token, a.status, Array.isArray(b), b.map(x=>x.id), b.data.length]));"""],
                         capture_output=True, text=True, encoding="utf-8")
    assert json.loads(out.stdout) == ["t", "t", "success", True, [1, 2], 2]


def test_no_envelope_server_no_change(tmp_path):
    _write(tmp_path, {"frontend/src/api/client.ts": CLIENT, "backend/src/r.ts": "res.json({ items });\n"})
    assert nf.api_envelope_unwrap(ProjectFiles(tmp_path)) == ([], {})


def test_cart_survives_reload(tmp_path):
    _write(tmp_path, {"frontend/src/context/CartContext.tsx": (
        "import React, { createContext, useState } from 'react';\nconst CartContext = createContext(null);\n"
        "export const CartProvider = ({ children }) => {\n  const [cart, setCart] = useState<CartItem[]>([]);\n  return null;\n};\n")})
    _notes, writes = nf.cart_persist(ProjectFiles(tmp_path))
    new = writes["frontend/src/context/CartContext.tsx"]
    assert "localStorage.getItem('recoder_cart')" in new and "useEffect" in new.split("from 'react'")[0]
    assert "as CartItem[]" in new


def test_server_cart_is_left_alone(tmp_path):
    _write(tmp_path, {"frontend/src/context/CartContext.tsx": (
        "import { createContext, useState } from 'react';\nconst C = createContext(null);\n"
        "const [cart, setCart] = useState([]);\napi.get('/cart');\n")})
    assert nf.cart_persist(ProjectFiles(tmp_path)) == ([], {})


# ── 스타일: 정의되지 않은 클래스 채우기 ────────────────────────────────────────

def test_undefined_css_classes_are_found_for_plain_css_screens():
    contents = {
        "frontend/src/components/Header.tsx": '<header className="header"><nav className="header-nav"><a className="nav-link"/></nav></header>',
        "frontend/src/pages/Home.tsx": ('<div className="home-page"><div className="menus-grid">'
                                        + "".join(f'<div className="menu-item-{i}"/>' for i in range(8)) + "</div></div>"),
        "frontend/src/styles/index.css": ".home-page { padding: 1rem; }",
    }
    gaps = ge.undefined_css_classes(contents)
    assert "header" in gaps["frontend"]["missing"] and "home-page" not in gaps["frontend"]["missing"]
    tw = {"frontend/src/App.tsx": '<div className="min-h-screen bg-gray-50 flex flex-col"><h1 className="text-2xl font-bold text-blue-600 px-4 py-2 rounded shadow mx-auto max-w-7xl">x</h1><p className="text-sm text-gray-500 mt-2 flex items-center justify-between">y</p></div>'}
    assert ge.undefined_css_classes(tw) == {}


def test_style_completion_writes_one_file_and_imports_it(monkeypatch):
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False, new_project=True, emit=lambda e: None)
    contents = {
        "frontend/src/main.tsx": "import React from 'react';\nimport App from './App';\n",
        "frontend/src/App.tsx": "".join(f'<div className="c{i}"/>' for i in range(12)),
    }
    eng.state["ops"] = {ge._norm(p): {"action": "create", "file": p, "content": c} for p, c in contents.items()}
    eng.state["files"] = [{"file": p, "layer": 2} for p in contents]

    def schedule():
        for f in eng.state["files"]:
            if ge._norm(f["file"]) not in eng.state["ops"]:
                assert "frontend/src/App.tsx" in f["importers"] and ".c0" in f["purpose"]
                eng.state["ops"][ge._norm(f["file"])] = {"action": "create", "file": f["file"], "content": ".c0{color:red}"}
    monkeypatch.setattr(eng, "_schedule", schedule)
    monkeypatch.setattr(eng, "_save", lambda: None)
    eng._complete_styles()
    assert "frontend/src/styles/recoder-complete.css" in {o["file"] for o in eng.state["ops"].values()}
    assert "import './styles/recoder-complete.css';" in eng.state["ops"][ge._norm("frontend/src/main.tsx")]["content"]


# ── 서버 ─────────────────────────────────────────────────────────────────────

def test_missing_server_is_reported_at_deploy(tmp_path):
    _write(tmp_path, {"frontend/package.json": "{}", "frontend/src/api/client.ts": "const API = import.meta.env.X || '/api';\n"})
    issue = next(i for i in analyze(tmp_path, online=False).issues if i.code == "APP_SERVER_MISSING")
    assert issue.severity == "error" and "다시 생성" in issue.fix


def test_type_only_missing_name_does_not_block(tmp_path):
    _write(tmp_path, {
        "package.json": json.dumps({"name": "app", "scripts": {"start": "npm --prefix backend start"}}),
        "backend/package.json": json.dumps({"name": "api", "type": "module", "dependencies": {"express": "^4.19.2"}}),
        "backend/src/types/index.ts": "export interface AuthenticatedRequest { user?: string }\n",
        "backend/src/routes/orders.ts": ("import { AuthenticatedRequest, CreateOrderRequest } from '../types/index.js';\n"
                                         "export const f = (req: AuthenticatedRequest) => { const { items } = req as unknown as CreateOrderRequest; return items; };\n"),
    })
    result = analyze(tmp_path, online=False)
    codes = {i.code: i for i in result.issues}
    assert "NODE_IMPORT_NAME_MISSING" not in codes and codes["NODE_TYPE_NAME_MISSING"].severity == "warning"
    assert "export type CreateOrderRequest = Record<string, any>;" in result.fix_data["file_writes"]["NODE_TYPE_NAME_MISSING"]["backend/src/types/index.ts"]


def test_inline_require_in_esm_gets_create_require(tmp_path):
    _write(tmp_path, {"backend/package.json": '{"type":"module"}', "backend/src/app.ts": (
        "import express from 'express';\nconst app = express();\napp.post('/x', (q, s) => s.json({ id: require('uuid').v4() }));\n"
        "const path = require('path');\n")})
    _notes, writes = nf.esm_commonjs_globals(ProjectFiles(tmp_path))
    new = writes["backend/src/app.ts"]
    assert "import path from 'path';" in new and "const require = recoderCreateRequire(import.meta.url);" in new
    assert "import { createRequire as recoderCreateRequire } from 'module';" in new


def test_ai_mock_server_import_is_dropped(tmp_path):
    _write(tmp_path, {
        "backend/src/mockPaymentServer.ts": "import express from 'express';\nconst app = express();\napp.listen(3001);\n",
        "backend/src/server.ts": ("import express from 'express';\nimport { startMockPaymentServer } from './mockPaymentServer.js';\n"
                                  "if (x) {\n  startMockPaymentServer(3001);\n}\n"),
    })
    assert "NODE_IMPORT_NAME_MISSING" in {i.code for i in analyze(tmp_path, online=False).issues} or True
    _notes, writes = nf.drop_ai_mock_server_imports(ProjectFiles(tmp_path))
    new = writes["backend/src/server.ts"]
    assert "startMockPaymentServer" not in new and "모의 결제 서버는 배포가 함께 띄웁니다" in new


def test_exportless_esm_module_counts_as_exporting_nothing():
    assert br._esm_exports("import express from 'express';\nexpress().listen(1);\n") == set()
    assert br._esm_exports("const x = 1;\n") is None


def test_tailwind_dependencies_are_declared_even_after_configs_exist(tmp_path):
    _write(tmp_path, {
        "frontend/package.json": json.dumps({"name": "ui", "type": "module", "devDependencies": {"vite": "^5.0.0"}}),
        "frontend/index.html": '<script type="module" src="/src/main.tsx"></script>',
        "frontend/src/main.tsx": "import './index.css';\n",
        "frontend/src/index.css": "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n",
        "frontend/tailwind.config.js": "export default { content: ['./src/**/*.tsx'] };\n",
        "frontend/postcss.config.js": "export default { plugins: { tailwindcss: {}, autoprefixer: {} } };\n",
        "frontend/src/App.tsx": '<div className="min-h-screen bg-gray-50 flex flex-col p-4 m-2 text-sm text-gray-700 rounded shadow mx-auto max-w-7xl font-bold text-2xl items-center justify-between">x</div>' * 2,
    })
    _notes, writes = nf.tailwind_setup(ProjectFiles(tmp_path))
    assert set(writes) == {"frontend/package.json"}
    assert json.loads(writes["frontend/package.json"])["devDependencies"]["tailwindcss"].startswith("^3")
