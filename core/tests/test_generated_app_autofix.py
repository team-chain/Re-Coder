"""실기기(2026-09-30) AI 생성 쇼핑몰이 로컬 Docker 배포에서 막힌 원인들 — 판정·자동 수정·배포 직전 적용.

생성물(TEMP/docs)에서 실제로 나온 어긋남:
  - vite outDir '../dist' 인데 서버는 frontend/dist 를 제공, 루트 build 없음(build:frontend 만)
  - const CartContext 를 선언만 하고 export 안 함 / Header 는 export function 인데 App 은 기본 가져오기
  - api 객체 안의 getProducts 를 import { getProducts } 로 가져옴
  - axios baseURL '/api' 인데 client.get('/api/products') 로 또 붙임
  - 프로젝트가 하위 폴더(docs/)에 있는데 루트를 배포 → 예전 이미지가 그대로 돌았다
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import build_readiness as br
from api.routes import deploy as d


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


SHOP = {
    "package.json": json.dumps({"name": "shop", "scripts": {"start": "node server.js", "build:frontend": "cd frontend && npm run build"},
                                "dependencies": {"express": "^4", "axios": "^1"}}),
    "server.js": ("const express = require('express');\nconst path = require('path');\nconst app = express();\n"
                  "app.get('/health', (q, s) => s.json({ ok: true }));\n"
                  "const distPath = path.join(__dirname, 'frontend', 'dist');\napp.use(express.static(distPath));\n"
                  "app.get('*', (q, s) => s.sendFile(path.join(distPath, 'index.html')));\napp.listen(process.env.PORT || 3001);\n"),
    "frontend/package.json": json.dumps({"name": "f", "type": "module", "scripts": {"dev": "vite", "build": "vite build"},
                                         "dependencies": {"react": "^18", "axios": "^1"}, "devDependencies": {"vite": "^5"}}),
    "frontend/vite.config.js": "import { defineConfig } from 'vite'\nexport default defineConfig({\n  build: {\n    outDir: '../dist',\n    emptyOutDir: true\n  }\n})\n",
    "frontend/index.html": "<div id=root></div><script type=module src=/src/main.jsx></script>",
    "frontend/src/main.jsx": "import App from './App'\nconsole.log(App)\n",
    "frontend/src/App.jsx": ("import Header from './components/Header'\nimport { CartContext } from './context/CartContext'\n"
                             "import { getProducts } from './api/client'\nexport default function App() { return Header && CartContext && getProducts }\n"),
    "frontend/src/components/Header.jsx": "export function Header() { return null }\n",
    "frontend/src/context/CartContext.jsx": "import { createContext } from 'react'\nconst CartContext = createContext()\nexport function CartProvider() { return null }\n",
    "frontend/src/api/client.js": ("import axios from 'axios';\nconst API_BASE_URL = '/api';\nconst client = axios.create({\n  baseURL: API_BASE_URL,\n});\n"
                                   "export const api = {\n  getProducts: () => client.get('/products'),\n};\nexport default client;\n"),
    "frontend/src/pages/Cart.jsx": "import client from '../api/client'\nexport default async function Cart() { return client.get(`/api/products/${1}`) }\n",
}


def _codes(root):
    return {i.code: i for i in br.analyze(root).issues}


def _fix_all(root):
    applied = []
    for _ in range(8):
        todo = [i for i in br.analyze(root).issues if i.auto_fix]
        if not todo:
            break
        br.apply_fix(root, todo[0].code)
        applied.append(todo[0].code)
    return applied


def test_생성_쇼핑몰의_어긋남을_모두_찾고_자동으로_고친다(tmp_path):
    root = _write(tmp_path, SHOP)
    codes = _codes(root)
    assert codes["NODE_FRONTEND_NOT_BUILT"].auto_fix and "outDir" in codes["NODE_FRONTEND_NOT_BUILT"].message
    assert codes["NODE_IMPORT_NAME_MISSING"].auto_fix
    assert codes["NODE_CLIENT_API_DOUBLE_PREFIX"].auto_fix
    _fix_all(root)
    assert not [i for i in br.analyze(root).issues if i.severity == "error"]
    assert "outDir: 'dist'" in (root / "frontend/vite.config.js").read_text()
    assert json.loads((root / "package.json").read_text())["scripts"]["build"] == "npm run build:frontend"
    assert "export const CartContext" in (root / "frontend/src/context/CartContext.jsx").read_text()
    assert "export default Header;" in (root / "frontend/src/components/Header.jsx").read_text()
    client = (root / "frontend/src/api/client.js").read_text()
    assert "export const getProducts = (...args) =>" in client and "res.data" in client
    assert "client.get(`/products/${1}`)" in (root / "frontend/src/pages/Cart.jsx").read_text()
    #: 이미 서버가 제공하는 폴더를 또 제공하도록 서버를 고치지 않는다(path.join 으로 나눠 적은 경로)
    assert (root / "server.js").read_text().count("express.static") == 1


def test_음성대조_맞게_쓴_코드는_건드리지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "ok", "scripts": {"start": "node server.js", "build": "npm --prefix web run build"}}),
        "server.js": "const express=require('express');const path=require('path');const app=express();app.use(express.static(path.join(__dirname,'web','dist')));app.listen(3000)\n",
        "web/package.json": json.dumps({"name": "w", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}),
        "web/index.html": "<div id=root></div>",
        "web/src/a.js": "import axios from 'axios';\nconst http = axios.create({ baseURL: '/api' });\nexport default http;\nexport const list = () => http.get('/items');\n",
        "web/src/b.js": "import http, { list } from './a.js';\nexport default function B() { return [http.get('/items'), list()] }\n",
    })
    codes = _codes(root)
    for code in ("NODE_FRONTEND_NOT_BUILT", "NODE_IMPORT_NAME_MISSING", "NODE_CLIENT_API_DOUBLE_PREFIX", "NODE_FRONTEND_NOT_SERVED"):
        assert code not in codes, codes.get(code)


def test_vite_outDir_재작성_형태():
    assert br.vite_out_dir_rewrite("export default { build: { outDir: '../dist' } }", "dist") == "export default { build: { outDir: 'dist' } }"
    assert "outDir: 'public'" in br.vite_out_dir_rewrite("export default defineConfig({ build: { sourcemap: true } })", "public")
    assert "build: { outDir: 'x' }" in br.vite_out_dir_rewrite("export default defineConfig({\n  plugins: []\n})", "x")
    assert br.vite_out_dir_rewrite("export default { build: { outDir: OUT } }", "dist") is None


def test_하위_폴더_프로젝트는_승인_전에_폴더를_알려_준다(tmp_path):
    _write(tmp_path / "docs", {"package.json": "{}", "server.js": "x"})
    assert d._nested_project_dir(str(tmp_path)) == "docs"
    assert "docs" in d._no_project_message(str(tmp_path))
    _write(tmp_path / "other", {"package.json": "{}"})
    assert d._nested_project_dir(str(tmp_path)) is None  # 둘이면 고르지 않는다


def _plan():
    from schemas import ActionType, DeployMethod, DeploymentPlan
    return DeploymentPlan(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN, image="shop:latest",
                          container_name="shop", ports={"3000": "3000"})


def test_Dockerfile_없는_프로젝트는_예전_이미지를_쓰지_않고_지금_코드로_빌드한다(monkeypatch, tmp_path):
    """실기기: Dockerfile 이 없는 폴더를 배포하자 로컬에 남은 예전 이미지가 돌아 이미 고친 오류가 다시 났다."""
    root = _write(tmp_path, {"package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4"}}),
                             "server.js": "require('express')().get('/health',(q,s)=>s.send('ok')).listen(process.env.PORT||3000)\n"})
    monkeypatch.setattr(d, "_local_image_exists", lambda image: True)
    calls = []

    class _Proc:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(d.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Proc())
    assert asyncio.run(d._build_local_image(_plan(), str(root))) is None
    assert calls and calls[0][:2] == ["docker", "build"], "예전 이미지를 그대로 썼다"
    assert (root / "Dockerfile").is_file() and (root / ".dockerignore").is_file()


def test_하위_폴더에_프로젝트가_있으면_빌드하지_않고_폴더를_알려_준다(monkeypatch, tmp_path):
    _write(tmp_path / "docs", {"package.json": "{}", "server.js": "x"})
    monkeypatch.setattr(d, "_local_image_exists", lambda image: True)
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("빌드·실행 금지")))
    r = asyncio.run(d._build_local_image(_plan(), str(tmp_path)))
    assert r["status"] == "failed" and "docs" in r["message"] and r["diagnosis"]["code"] == "NO_PROJECT_FILES"


def test_배포_직전_자동_수정은_오류와_화면_문제만_고친다(tmp_path):
    root = _write(tmp_path, SHOP)
    applied = d._auto_fix_before_build(str(root))
    codes = {a["code"] for a in applied}
    assert {"NODE_FRONTEND_NOT_BUILT", "NODE_IMPORT_NAME_MISSING", "NODE_CLIENT_API_DOUBLE_PREFIX"} <= codes
    assert all(a["changed"] for a in applied)
    assert not [i for i in br.analyze(root).issues if i.severity == "error"]
    assert (root / ".recoder" / "backups").is_dir()


def test_생성_단계에서도_같은_어긋남을_고친다(tmp_path):
    import code_agent as ca
    ops = [{"action": "create", "file": rel, "content": text, "language": "", "rationale": ""} for rel, text in SHOP.items()]
    fixed, notes = ca._autofix_ops(tmp_path, "", ops)
    remaining = [i["code"] for i in ca._consistency_issues(tmp_path, "", fixed) if i["severity"] == "error"]
    assert remaining == [], remaining
    assert any("outDir" in n for n in notes) and any("/api 중복" in n for n in notes)
