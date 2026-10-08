"""전체 점검(2026-10)에서 재현한 문제들 — 고친 동작을 고정한다.

배포: Dockerfile 없는 프로젝트의 포트, 프록시 PC 의 헬스 확인, 같은 폴더 이름의 다른 프로젝트,
웹 컴포넌트 화면, S3 설정 적용 대기, 덮어쓰기 백업.
자동 수정: 맞는 코드를 망가뜨리던 경우(uploads 폴더 비우기, 함수 안 import, 중복 이름, 반쪽 변환 등).
보안: 코드 생성 프롬프트로 .env·키가 그대로 나가던 문제.
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_readiness as br
import code_agent as ca
import context_gate
import deployment_inputs as di
import s3_byo
import screen_check
from agents.deploy_agent import DeployAgent
from api.routes import deploy as d
from api.routes import deploy_s3

NODE = shutil.which("node")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
    return root


def _node_check(path: Path) -> None:
    if NODE:
        r = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ── 배포 ─────────────────────────────────────────────────────────────────

def test_Dockerfile_없는_Flask_는_템플릿과_같은_포트로_계획한다(tmp_path):
    _write(tmp_path, {"requirements.txt": "flask\ngunicorn\n",
                      "app.py": "from flask import Flask\napp = Flask(__name__)\n@app.get('/health')\ndef h(): return 'ok'\n"})
    template_port = d._template_runtime_port(str(tmp_path))
    assert template_port is not None
    assert DeployAgent._detect_port(str(tmp_path)) == (template_port, template_port)


def test_음성대조_Dockerfile_이_있으면_그_EXPOSE_를_쓴다(tmp_path):
    _write(tmp_path, {"requirements.txt": "flask\n", "app.py": "x", "Dockerfile": "FROM python:3.11\nEXPOSE 7000\n"})
    assert d._template_runtime_port(str(tmp_path)) is None
    assert DeployAgent._detect_port(str(tmp_path)) == (7000, 7000)


def test_빌드때_만든_Dockerfile_포트로_컨테이너_포트를_맞춘다(tmp_path):
    from schemas import ActionType, DeployMethod, DeploymentPlan
    plan = DeploymentPlan(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN, image="a:latest",
                          container_name="a", ports={"8000": "8000"}, env={"PORT": "8000"})
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\nEXPOSE 5000\n")
    note = d._align_ports_with_dockerfile(plan, tmp_path / "Dockerfile")
    assert note and plan.ports == {"8000": "5000"} and plan.env["PORT"] == "5000"
    assert d._align_ports_with_dockerfile(plan, tmp_path / "Dockerfile") is None  # 이미 맞으면 그대로


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_시스템_프록시가_있어도_로컬_헬스_확인은_직접_연결한다(monkeypatch):
    from preflight.runtime import http_probe
    server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        ok, status, _ = http_probe("127.0.0.1", server.server_address[1], "/health")
        assert ok and status == 200
    finally:
        server.shutdown()


def test_같은_폴더_이름의_다른_프로젝트는_컨테이너를_나눈다(monkeypatch, tmp_path):
    a, b = tmp_path / "work" / "shop", tmp_path / "other" / "shop"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    labels = {"shop": di.workspace_fingerprint(str(a))}

    def fake_run(cmd, **kw):
        name = cmd[-1]
        if name in labels:
            return SimpleNamespace(returncode=0, stdout=labels[name] + "\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
    monkeypatch.setattr(di.subprocess, "run", fake_run)
    assert di.workspace_deploy_name(str(a)) == "shop"
    other = di.workspace_deploy_name(str(b))
    assert other.startswith("shop-") and other != "shop"
    #: 라벨 없는 예전 컨테이너는 같은 프로젝트로 본다(예전 동작 유지)
    labels["shop"] = "<no value>"
    assert di.workspace_deploy_name(str(b)) == "shop"


def test_웹_컴포넌트_화면은_빈_화면으로_단정하지_않는다():
    dom = "<html><head></head><body><my-app></my-app><script type=module src=/a.js></script></body></html>"
    result = screen_check.ScreenResult(ok=True, url="http://localhost:1/")
    original = screen_check._render
    screen_check._render = lambda *a, **k: (dom, [])
    try:
        screen_check._browser_stage("http://localhost:1/", result, "chrome", dom)
    finally:
        screen_check._render = original
    assert result.ok and result.warnings


def test_음성대조_내용_없는_div_만_있으면_빈_화면이다():
    dom = "<html><body><div id=root></div><script src=/a.js></script></body></html>"
    result = screen_check.ScreenResult(ok=True, url="http://localhost:1/")
    original = screen_check._render
    screen_check._render = lambda *a, **k: (dom, [])
    try:
        screen_check._browser_stage("http://localhost:1/", result, "chrome", dom)
    finally:
        screen_check._render = original
    assert result.ok is False and result.code == "SCREEN_BLANK"


def test_새_S3_버킷의_403_404_는_설정이_적용될_때까지_다시_본다(monkeypatch):
    replies = iter([(404, "text/html", b"NoSuchWebsiteConfiguration"), (200, "text/html", b"<html><body>hi</body></html>")])
    monkeypatch.setattr(screen_check, "_get", lambda url, **k: next(replies))
    monkeypatch.setattr(screen_check.time, "sleep", lambda s: None)
    result = screen_check.ScreenResult(ok=True, url="")
    markup = screen_check._http_stage("http://recoder-x.s3-website.ap-northeast-2.amazonaws.com/", result, 10)
    assert markup and "hi" in markup


def test_음성대조_로컬_404_는_기다리지_않는다(monkeypatch):
    calls = []
    monkeypatch.setattr(screen_check, "_get", lambda url, **k: calls.append(url) or (404, "text/html", b"nope"))
    result = screen_check.ScreenResult(ok=True, url="")
    assert screen_check._http_stage("http://localhost:3000/", result, 10) is None
    assert len(calls) == 1


def test_두_번_덮어써도_사용자가_고친_원본_백업은_남는다(tmp_path):
    target = tmp_path / "Dockerfile"
    target.write_text("# 손으로 고침\nFROM a\n")
    first = SimpleNamespace(content="FROM b\n", target_path="Dockerfile", workspace_path=str(tmp_path), file_type="dockerfile", risk_reasons=[])
    d._write_proposal_to_workspace(first, str(tmp_path), "p1", overwrite=True)
    second = SimpleNamespace(content="FROM c\n", target_path="Dockerfile", workspace_path=str(tmp_path), file_type="dockerfile", risk_reasons=[])
    d._write_proposal_to_workspace(second, str(tmp_path), "p2", overwrite=True)
    backups = sorted(p.read_text() for p in tmp_path.glob("Dockerfile.recoder-prev*"))
    assert "# 손으로 고침\nFROM a\n" in backups and "FROM b\n" in backups


def test_짝수_바이트_CP949_파일을_UTF16_으로_잘못_읽지_않는다(tmp_path):
    target = tmp_path / "Dockerfile"
    raw = "# 한글 설명입니다\nFROM node\n".encode("cp949")
    if len(raw) % 2:
        raw += b"\n"
    target.write_bytes(raw)
    proposal = SimpleNamespace(content="FROM x\n", target_path="Dockerfile")
    conflict = d._existing_file_conflict(proposal, target)
    assert "한글" in conflict["existing_content"]


# ── 자동 수정이 맞는 코드를 망가뜨리지 않는다 ─────────────────────────────

def test_uploads_폴더로_빌드_결과를_돌리지_않는다(tmp_path):
    _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js", "build": "npm --prefix client run build"}}),
        "server.js": "const express=require('express');const path=require('path');const app=express();\n"
                     "app.use('/uploads', express.static(path.join(__dirname,'uploads')));app.listen(3000)\n",
        "client/package.json": json.dumps({"name": "c", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}),
        "client/vite.config.js": "export default { build: {} }\n",
        "client/index.html": "<div id=root></div>",
    })
    plan = br.frontend_build_plan(br.ProjectFiles(tmp_path), json.loads((tmp_path / "package.json").read_text())["scripts"], ["server.js"])
    assert not plan or "uploads" not in json.dumps(plan, ensure_ascii=False)


def test_dirname_정의는_함수_안이_아니라_파일_맨_위에_넣는다(tmp_path):
    text = ("import express from 'express'\nimport path from 'path' // node builtin\nconst app = express()\n"
            "function dbFile(name) {\n  return path.join(__dirname, name)\n}\napp.listen(3000)\n")
    out = br.esm_dirname_shim(text)
    assert out.startswith("import { fileURLToPath")
    _write(tmp_path, {"s.mjs": out})
    _node_check(tmp_path / "s.mjs")


def test_같은_파일_이름의_CommonJS_두_개를_가져와도_이름이_겹치지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "x", "type": "module", "scripts": {"start": "node server.js"}}),
        "server.js": "import { listUsers } from './routes/user.js';\nimport { findUser } from './models/user.js';\n"
                     "console.log(typeof listUsers, typeof findUser);\n",
        "routes/user.js": "function mk(){ return { listUsers: () => 1 } }\nmodule.exports = mk();\n",
        "models/user.js": "const o = {}; o.findUser = () => 2;\nmodule.exports = Object.assign({}, o);\n",
    })
    br.apply_fix(root, "NODE_MODULE_FORMAT_MISMATCH")
    text = (root / "server.js").read_text()
    assert "__recoder_user_2" in text
    if NODE:
        r = subprocess.run([NODE, "server.js"], cwd=root, capture_output=True, text=True, timeout=20)
        assert r.returncode == 0 and "function function" in r.stdout, r.stderr


def test_import_와_module_exports_를_섞은_파일은_반쪽만_고치지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "x", "type": "module", "scripts": {"start": "node server.js"}}),
        "server.js": "import http from 'http';\nconst { greet } = require('./greet.cjs');\n"
                     "const server = http.createServer((q, s) => s.end(greet()));\nmodule.exports = server;\n",
        "greet.cjs": "module.exports = { greet: () => 'hi' };\n",
    })
    fmt = br.analyze(root).fix_data.get("module_format")
    assert fmt is None or fmt["auto"] is False
    issue = next((i for i in br.analyze(root).issues if i.code == "NODE_MODULE_FORMAT_MISMATCH"), None)
    assert issue is None or not issue.auto_fix


def test_type_module_을_뺄_때_Vite_화면_소스_이름은_바꾸지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "type": "module", "scripts": {"build": "vite build", "start": "node server.js"},
                                    "dependencies": {"express": "^4"}, "devDependencies": {"vite": "^5"}}),
        "server.js": "const express = require('express');\nconst path = require('path');\nconst app = express();\n"
                     "app.use(express.static(path.join(__dirname, 'dist')));\napp.listen(process.env.PORT || 3000);\n",
        "vite.config.js": "import { defineConfig } from 'vite';\nexport default defineConfig({});\n",
        "index.html": "<div id=app></div>\n<script type=\"module\" src=\"/src/main.js\"></script>\n",
        "src/main.js": "import { greet } from './greet.js';\ndocument.getElementById('app').textContent = greet();\n",
        "src/greet.js": "export const greet = () => 'hi';\n",
    })
    br.apply_fix(root, "NODE_MODULE_FORMAT_MISMATCH")
    assert (root / "src/main.js").is_file() and not (root / "src/main.mjs").exists()


@pytest.mark.parametrize("source", [
    "export const db = await connect();\n",
    "try { await connect() } catch (e) {}\nexport default 1;\n",
    "for await (const x of gen()) {}\n",
    "const cfg = (await import('./c.js')).default;\n",
    "import pkg from './package.json' with { type: 'json' };\nexport default pkg;\n",
    "export const a = 1, b = 2;\n",
    "const tpl = `\nimport x from 'y'\n`;\nexport default tpl;\n",
])
def test_CommonJS_로_확실히_못_바꾸는_ESM_은_건드리지_않는다(source):
    assert br.esm_to_cjs(source) is None


def test_음성대조_함수_안의_await_는_CommonJS_로_바꾼다(tmp_path):
    source = ("import express from 'express'\nconst app = express()\n"
              "app.get('/', async (req, res) => { const x = await q(); res.json(x) })\n"
              "export async function f() { await g() }\nexport const h = async () => await g()\n"
              "class A { async m() { await z() } }\nexport default app\n")
    out = br.esm_to_cjs(source)
    assert out is not None and "require('express')" in out
    _write(tmp_path, {"a.cjs": out})
    _node_check(tmp_path / "a.cjs")


def test_createRequire_를_쓰는_ESM_서버에_dirname_을_넣지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js", "build": "npm --prefix web run build"}}),
        "server.js": "import express from 'express';\nimport { createRequire } from 'module';\nconst require = createRequire(import.meta.url);\n"
                     "const app = express();\napp.get('/api/x', (q, s) => s.json({}));\napp.listen(3000);\n",
        "web/package.json": json.dumps({"name": "w", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}),
        "web/index.html": "<div id=root></div>",
    })
    issue = next((i for i in br.analyze(root).issues if i.code == "NODE_FRONTEND_NOT_SERVED"), None)
    assert issue is None or not issue.auto_fix


def test_UTF8_이_아닌_파일이_있으면_아무것도_바꾸지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js"}, "dependencies": {}}),
        "Dockerfile": "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nCMD [\"node\",\"server.js\"]\n",
        "server.js": "import http from 'node:http';\nimport db from './db.js';\nhttp.createServer((q, s) => s.end(String(db.n))).listen(3000);\n",
        "db.js": "const { n } = require('./util');\nmodule.exports = { n };\n",
        "util.js": "// 한글 주석 (CP949)\nmodule.exports = { n: 1 };\n".encode("cp949"),
    })
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="UTF-8"):
        br.apply_fix(root, "NODE_MODULE_FORMAT_MISMATCH")
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert after == before


def test_pg_를_쓰는_파일마다_숫자_파서를_넣는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js"}, "dependencies": {"pg": "^8", "express": "^4"}}),
        "server.js": "const { Pool } = require('pg');\nconst pool = new Pool();\nconst express = require('express');\n"
                     "const app = express();\napp.get('/p', async (q, s) => s.json((await pool.query('select price from products')).rows.map(r => r.price.toFixed(2))));\napp.listen(3000);\n",
        "scripts/seed.js": "const { Client } = require('pg');\n(async () => { const c = new Client(); await c.connect(); await c.query('CREATE TABLE IF NOT EXISTS products (price NUMERIC(10,2))'); await c.end(); })();\n",
    })
    if not any(i.code == "NODE_PG_NUMERIC_STRINGS" for i in br.analyze(root).issues):
        pytest.skip("이 프로젝트에서는 NUMERIC 경고가 나오지 않는다")
    br.apply_fix(root, "NODE_PG_NUMERIC_STRINGS")
    assert "setTypeParser" in (root / "server.js").read_text()
    assert "setTypeParser" in (root / "scripts/seed.js").read_text()


def test_환경변수로_정한_baseURL_은_api_중복으로_보지_않는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js", "build": "npm --prefix web run build"}}),
        "server.js": "require('express')().listen(3000)\n",
        "web/package.json": json.dumps({"name": "w", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}, "dependencies": {"axios": "^1"}}),
        "web/index.html": "<div id=root></div>",
        "web/src/api.js": "import axios from 'axios';\nconst api = axios.create({ baseURL: import.meta.env.VITE_API_ORIGIN || '/api' });\n"
                          "export const list = () => api.get('/api/products');\nexport default api;\n",
    })
    assert "NODE_CLIENT_API_DOUBLE_PREFIX" not in {i.code for i in br.analyze(root).issues}


@pytest.mark.parametrize("script,entry", [
    ("node -r dotenv/config server.js", "server.js"),
    ("node --require dotenv/config src/index.js", "src/index.js"),
    ("node --watch server.js", "server.js"),
    ("nodemon --watch src src/index.js", "src/index.js"),
    ("node server.js", "server.js"),
])
def test_node_옵션_값을_진입_파일로_보지_않는다(script, entry):
    assert br._start_entry({"start": script}) == entry


def test_템플릿_문자열_안의_import_는_의존성이_아니다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "a", "scripts": {"start": "node gen.js"}}),
        "gen.js": "const tpl = `import x from 'some-lib'`;\nconsole.log(tpl);\n",
    })
    assert not any("some-lib" in i.message for i in br.analyze(root).issues)


def test_Tailwind_용_package_json_이_있는_Django_는_Python_앱이다(tmp_path):
    root = _write(tmp_path, {
        "manage.py": "import os\n", "requirements.txt": "django\n",
        "package.json": json.dumps({"name": "t", "scripts": {"build:css": "tailwindcss -o static/out.css"}, "devDependencies": {"tailwindcss": "^3"}}),
    })
    assert br.detect_runtime(br.ProjectFiles(root)) == "python"
    _write(tmp_path / "n", {"requirements.txt": "x\n", "package.json": json.dumps({"scripts": {"start": "node s.js"}})})
    assert br.detect_runtime(br.ProjectFiles(tmp_path / "n")) == "node"


def test_생성_교정이_이미_있는_js_를_jsx_로_옮기면_옛_경로가_새_파일을_가리킨다(tmp_path):
    _write(tmp_path, {
        "package.json": json.dumps({"name": "web", "scripts": {"build": "vite build"}, "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
                                    "devDependencies": {"vite": "^5.0.0", "@vitejs/plugin-react": "^4.0.0"}}),
        "index.html": "<div id=root></div><script type=module src=/src/main.jsx></script>",
        "src/main.jsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\nimport App from './App';\n"
                        "createRoot(document.getElementById('root')).render(<App />);\n",
        "src/App.js": "import React from 'react';\nexport default function App() { return React.createElement('h1', null, 'Old'); }\n",
    })
    ops = [{"action": "edit", "file": "src/App.js", "language": "javascript", "rationale": "",
            "content": "import React from 'react';\nexport default function App() {\n  return (\n    <div>hi</div>\n  );\n}\n"}]
    fixed, _notes = ca._autofix_ops(tmp_path, "", ops)
    by_file = {op["file"]: op["content"] for op in fixed}
    #: 새 내용은 .jsx 로, 디스크에 남는 옛 .js 는 새 파일을 다시 내보내 './App' 이 어느 쪽으로 풀려도 새 코드가 쓰인다
    assert "<div>hi</div>" in by_file["src/App.jsx"]
    assert "export { default } from './App.jsx'" in by_file["src/App.js"] and "<div>" not in by_file["src/App.js"]
    for rel, content in by_file.items():
        _write(tmp_path, {rel: content})
    assert "NODE_VITE_JSX_IN_JS" not in {i.code for i in br.analyze(tmp_path).issues}


# ── 코드 생성 프롬프트의 비밀 ─────────────────────────────────────────────

def test_열린_env_파일_본문은_AI_로_보내지_않는다():
    prompt = ca._build_code_prompt("로그인 추가", [], {"path": "server/.env", "content": "AWS_SECRET_ACCESS_KEY=abcd1234"})
    assert "abcd1234" not in prompt


def test_코드_속_키는_가려서_보내고_결과에서_되돌린다():
    secrets: dict = {}
    key, url = "AKIA" + "ABCDEFGHIJKLMNOP", "postgres://admin:Hunter2pass@localhost:5432/app"
    text = f"const k = '{key}';\nconst url = '{url}';\nconst password = req.body.password;\n"
    masked = context_gate.scrub_code_context("src/db.js", text, secrets)
    assert key not in masked and "Hunter2pass" not in masked and "req.body.password" in masked
    ops = [{"file": "src/db.js", "content": masked + "// new\n"}]
    ca._restore_secrets_in_ops(ops, secrets)
    assert ops[0]["content"] == text + "// new\n"


def test_분석용_마스킹이_대문자_비밀_변수와_접속_문자열을_가린다():
    import asyncio
    sample = "SECRET_KEY=django-insecure-abc\nDB_PASS=\"hello world\"\nPOSTGRES_URL=postgres://admin:Hunter2pass@db/app\n"
    result = asyncio.run(context_gate.ContextGate().mask(sample))
    texts = [str(v) for v in (result.model_dump().values() if hasattr(result, "model_dump") else [result])]
    masked = max(texts, key=len)
    legacy = context_gate.mask_secrets(sample)
    for secret in ("django-insecure-abc", "hello world", "Hunter2pass"):
        assert secret not in masked and secret not in legacy


# ── S3 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,ctype", [("a.pdf", "application/pdf"), ("s.xml", "application/xml"),
                                         ("d.csv", "text/csv; charset=utf-8"), ("f.ttf", "font/ttf"), ("x.unknown", "application/octet-stream")])
def test_S3_업로드_형식(name, ctype):
    assert s3_byo.content_type(name) == ctype


def test_계정_퍼블릭_액세스_차단은_권한표가_아니라_설정을_안내한다():
    class Err(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "...because public policies are blocked by the BlockPublicPolicy block public access setting."}}
    text = deploy_s3._aws_error_detail(Err(), "버킷 정책 설정") if deploy_s3._aws_error_detail.__code__.co_argcount >= 2 else deploy_s3._aws_error_detail(Err())
    assert "퍼블릭 액세스 차단" in text


# ── 동반 Postgres 초기화 SQL ──────────────────────────────────────────────

def test_프로젝트의_init_sql_을_찾는다(tmp_path):
    import local_services
    assert local_services.find_init_sql(str(tmp_path)) is None
    _write(tmp_path, {"server/init.sql": "CREATE TABLE IF NOT EXISTS products (id serial primary key);\n"})
    assert local_services.find_init_sql(str(tmp_path)).name == "init.sql"
    _write(tmp_path / "q", {"init.sql": "-- 테이블 없음\nSELECT 1;\n"})
    assert local_services.find_init_sql(str(tmp_path / "q")) is None


def _psql_runner(table_count: str):
    calls = []

    def run(args, timeout=0):
        calls.append(args)
        if "-tAc" in args:
            return subprocess.CompletedProcess(args, 0, table_count + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    return run, calls


def test_빈_DB_에만_init_sql_을_실행한다(tmp_path, monkeypatch):
    import local_services
    sql = _write(tmp_path, {"init.sql": "CREATE TABLE t (id int);\n"}) / "init.sql"
    executed = []
    monkeypatch.setattr(local_services, "_exec_sql", lambda name, text, timeout=120: executed.append((name, text)) or subprocess.CompletedProcess([], 0, "", ""))
    run, _ = _psql_runner("0")
    local_services._initialize_postgres("shop-postgres", sql, None, run)
    assert executed == [("shop-postgres", "CREATE TABLE t (id int);\n")]
    executed.clear()
    run, _ = _psql_runner("3")  # 재배포 — 이미 테이블이 있다
    local_services._initialize_postgres("shop-postgres", sql, None, run)
    assert executed == []
