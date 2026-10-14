"""주소만 열리고 화면이 안 나오거나, 서버가 시작 직후 죽는 배포를 막는 장치(실기기 TEMP 쇼핑몰).

1) 모듈 형식(ESM·CommonJS) 혼용 — 판정·자동 수정 후 **실제 node 로 실행**해 확인한다.
2) 서버가 제공하는 화면 폴더를 루트 build 가 만들지 않음(build:client 만 있음).
3) 배포 후 화면 확인(screen_check) — HTTP 단계는 항상, 브라우저 단계는 PC 에 있을 때.
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import build_readiness as br
import screen_check

NODE = shutil.which("node")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _codes(root: Path) -> list[str]:
    return [i.code for i in br.analyze(root).issues]


def _run(root: Path, entry: str) -> str:
    proc = subprocess.run([NODE, entry], cwd=root, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-800:]
    return proc.stdout


def _fix_and_check(root: Path, entry: str, expected: str) -> None:
    issue = next(i for i in br.analyze(root).issues if i.code == "NODE_MODULE_FORMAT_MISMATCH")
    assert issue.severity == "error" and issue.auto_fix
    br.apply_fix(root, "NODE_MODULE_FORMAT_MISMATCH")
    assert "NODE_MODULE_FORMAT_MISMATCH" not in _codes(root)
    if NODE:
        assert expected in _run(root, entry)


# ---------------------------------------------------------------------------
# 1) 모듈 형식
# ---------------------------------------------------------------------------

def test_TEMP_쇼핑몰_type_module_ESM_진입에_CommonJS_라우트(tmp_path):
    """실기기 그대로: "type": "module" + server.js(import) + src/**(require/module.exports)."""
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "shop", "type": "module", "scripts": {"start": "node server.js"}}),
        "server.js": ("import path from 'path';\nimport { fileURLToPath } from 'url';\n"
                      "import products from './src/routes/products.js';\nimport { total } from './src/lib/calc.js';\n"
                      "const __filename = fileURLToPath(import.meta.url);\nconst __dirname = path.dirname(__filename);\n"
                      "console.log('OK', products.list().length, total([1, 2]));\n"),
        "src/routes/products.js": "const Product = require('../models/Product');\nmodule.exports = { list: () => Product.all };\n",
        "src/models/Product.js": "module.exports = { all: [1, 2, 3] };\n",
        "src/lib/calc.js": "const api = {};\napi.total = (xs) => xs.reduce((a, b) => a + b, 0);\nmodule.exports = api;\n",
    })
    _fix_and_check(root, "server.js", "OK 3 3")
    assert (root / "src/routes/products.cjs").is_file() and not (root / "src/routes/products.js").exists()
    backups = list((root / ".recoder" / "backups").iterdir())
    assert any("products.js" in b.name for b in backups)


def test_type_없는_ESM_진입_확장자_없는_import(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "b", "scripts": {"start": "node server.js"}}),
        "server.js": "import { greet } from './lib/greet';\nconsole.log('OK', greet('x'));\n",
        "lib/greet.js": "const p = require('./prefix');\nmodule.exports = { greet: (n) => p + n };\n",
        "lib/prefix.js": "module.exports = 'hi ';\n",
    })
    _fix_and_check(root, "server.js", "OK hi x")


def test_CommonJS_가_ESM_파일을_require_하면_CommonJS_로_바꾼다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "c", "scripts": {"start": "node index.js"}}),
        "index.js": "const db = require('./db');\nconst { fmt } = require('./util.js');\nconsole.log('OK', db.name, fmt(1));\n",
        "db.js": ("import path from 'path';\nimport { fileURLToPath } from 'url';\n"
                  "const __filename = fileURLToPath(import.meta.url);\nconst __dirname = path.dirname(__filename);\n"
                  "export default { name: 'db' + (__dirname ? '' : '?') };\n"),
        "util.js": "export function fmt(n) { return `#${n}`; }\n",
    })
    _fix_and_check(root, "index.js", "OK db #1")


def test_type_module_인데_CommonJS_진입이면_type_을_빼고_ESM_을_바꾼다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "d", "type": "module",
                                    "scripts": {"start": "node app.js", "seed": "node scripts/seed.js"}}),
        "app.js": "const cfg = require('./config');\nconst r = require('./routes/index');\nconsole.log('OK', cfg.port, r.list().join(','));\n",
        "config.js": "export default { port: 3000 };\n",
        "routes/index.js": "import { a } from './a.js';\nexport function list() { return [a, 'b']; }\n",
        "routes/a.js": "export const a = 'a';\n",
        "scripts/seed.js": "import fs from 'fs';\nconsole.log('seed', typeof fs.readFileSync);\n",
    })
    _fix_and_check(root, "app.js", "OK 3000 a,b")
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert "type" not in package and package["scripts"]["seed"] == "node scripts/seed.mjs"
    if NODE:
        assert "seed function" in _run(root, "scripts/seed.mjs")


def test_ESM_의_선언_없는_dirname(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "e", "type": "module", "scripts": {"start": "node server.js"}}),
        "server.js": "import path from 'path';\nconsole.log('OK', path.join(__dirname, 'public').endsWith('public'));\n",
    })
    _fix_and_check(root, "server.js", "OK true")


def test_import_와_require_가_섞인_파일(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "g", "type": "module", "scripts": {"start": "node server.js"}}),
        "server.js": "const os = require('os');\nimport { u } from './u.js';\nconst x = require('./x');\nconsole.log('OK', typeof os.cpus, u, x);\n",
        "u.js": "export const u = 'u';\n",
        "x.js": "module.exports = 7;\n",
    })
    _fix_and_check(root, "server.js", "OK function u 7")


def test_예전_Node_이미지면_type_없는_ESM_도_문제로_본다(tmp_path):
    files = {
        "package.json": json.dumps({"name": "o", "scripts": {"start": "node server.js"}}),
        "server.js": "import { a } from './a.js';\nconsole.log('OK', a);\n",
        "a.js": "export const a = 1;\n",
    }
    new = _write(tmp_path / "new", dict(files))
    assert "NODE_MODULE_FORMAT_MISMATCH" not in _codes(new)  # Node 22 는 문법을 보고 ESM 으로 실행한다
    old = _write(tmp_path / "old", {**files, "Dockerfile": "FROM node:18-alpine\nCMD [\"node\", \"server.js\"]\n"})
    assert "NODE_MODULE_FORMAT_MISMATCH" in _codes(old)
    br.apply_fix(old, "NODE_MODULE_FORMAT_MISMATCH")
    assert json.loads((old / "package.json").read_text(encoding="utf-8"))["type"] == "module"


@pytest.mark.parametrize("files", [
    {"package.json": json.dumps({"name": "h", "type": "module", "scripts": {"start": "node server.js"}}),
     "server.js": "import { a } from './a.js';\nconsole.log(a);\n", "a.js": "export const a = 1;\n"},
    {"package.json": json.dumps({"name": "i", "scripts": {"start": "node server.js"}}),
     "server.js": "const a = require('./a');\nconsole.log(a);\n", "a.js": "module.exports = 1;\n"},
    {"package.json": json.dumps({"name": "k", "type": "module", "scripts": {"start": "node server.js"}}),
     "server.js": "import { createRequire } from 'module';\nconst require = createRequire(import.meta.url);\n"
                  "const c = require('./c.cjs');\nconsole.log(c);\n", "c.cjs": "module.exports = 1;\n"},
    {"package.json": json.dumps({"name": "t", "scripts": {"start": "node dist/index.js", "build": "tsc"}}),
     "src/index.ts": "import x from './x';\n", "tsconfig.json": "{}"},
])
def test_음성대조_형식이_맞는_프로젝트는_조용하다(tmp_path, files):
    assert "NODE_MODULE_FORMAT_MISMATCH" not in _codes(_write(tmp_path, files))


def test_esm_to_cjs_변환_형태(tmp_path):
    out = br.esm_to_cjs(
        "import express, { Router as R } from 'express';\nimport * as fs from 'fs';\nimport './side.js';\n"
        "export const a = 1;\nexport async function f() {}\nconst b = 2;\nexport { b as bee };\nexport default function main() {}\n")
    assert "const express = require('express');" in out and "const { Router: R } = express;" in out
    assert "const fs = require('fs');" in out and "require('./side.js');" in out
    assert "module.exports = main;" in out and "module.exports.a = a;" in out and "module.exports.bee = b;" in out
    assert br.esm_to_cjs("export * from './x.js';\n") is None
    assert br.esm_to_cjs("const data = await load();\n") is None


# ---------------------------------------------------------------------------
# 2) 화면 빌드 연결
# ---------------------------------------------------------------------------

def _shop_with_client(tmp_path, root_scripts: dict) -> Path:
    return _write(tmp_path, {
        "package.json": json.dumps({"name": "shop", "scripts": root_scripts, "dependencies": {"express": "^4"}}),
        "server.js": ("const express = require('express');\nconst path = require('path');\nconst app = express();\n"
                      "app.get('/health', (q, s) => s.send('ok'));\n"
                      "app.use(express.static(path.join(__dirname, 'client', 'dist')));\n"
                      "app.get('*', (q, s) => s.sendFile(path.join(__dirname, 'client/dist/index.html')));\n"
                      "app.listen(process.env.PORT || 3000);\n"),
        "client/package.json": json.dumps({"name": "c", "scripts": {"dev": "vite", "build": "vite build"},
                                           "devDependencies": {"vite": "^5"}}),
        "client/index.html": "<div id=root></div><script type=module src=/src/main.jsx></script>",
        "client/src/main.jsx": "export {};\n",
    })


def test_build_client_만_있으면_화면이_빌드되지_않는다(tmp_path):
    root = _shop_with_client(tmp_path, {"start": "node server.js", "build:client": "cd client && npm run build"})
    issue = next(i for i in br.analyze(root).issues if i.code == "NODE_FRONTEND_NOT_BUILT")
    assert issue.severity == "error" and issue.auto_fix and "client/dist" in issue.message
    assert "NODE_STATIC_DIR_MISSING" not in _codes(root)  # 같은 원인을 두 번 말하지 않는다
    br.apply_fix(root, "NODE_FRONTEND_NOT_BUILT")
    scripts = json.loads((root / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["build"] == "npm run build:client"
    assert "NODE_FRONTEND_NOT_BUILT" not in _codes(root)


def test_빌드_스크립트가_없으면_prefix_로_빌드한다(tmp_path):
    root = _shop_with_client(tmp_path, {"start": "node server.js"})
    br.apply_fix(root, "NODE_FRONTEND_NOT_BUILT")
    assert json.loads((root / "package.json").read_text(encoding="utf-8"))["scripts"]["build"] == "npm --prefix client run build"


def test_음성대조_build_가_이미_화면을_빌드한다(tmp_path):
    root = _shop_with_client(tmp_path, {"start": "node server.js", "build": "cd client && npm install && npm run build"})
    codes = _codes(root)
    assert "NODE_FRONTEND_NOT_BUILT" not in codes and "NODE_STATIC_DIR_MISSING" not in codes


def test_화면_여부(tmp_path):
    assert br.expects_screen(_shop_with_client(tmp_path / "a", {"start": "node server.js"}))
    api = _write(tmp_path / "b", {"package.json": json.dumps({"name": "api", "scripts": {"start": "node s.js"}}),
                                  "s.js": "require('express')().listen(3000);\n"})
    assert not br.expects_screen(api)


# ---------------------------------------------------------------------------
# 3) 화면 확인
# ---------------------------------------------------------------------------

class _Site:
    def __init__(self, root: Path, spa_fallback: bool = True):
        folder = str(root)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=folder, **k)

            def send_head(self):
                if spa_fallback and not Path(self.translate_path(self.path)).exists():
                    self.path = "/index.html"
                return super().send_head()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def site(tmp_path):
    made = []

    def make(files: dict[str, str], **kw):
        s = _Site(_write(tmp_path / f"s{len(made)}", files), **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


INDEX = "<!doctype html><html><head><title>t</title><script type=module src=/app.js></script></head><body><div id=root></div></body></html>"


def test_스크립트_파일이_없으면_index_html_이_대신_온다(site):
    result = screen_check.check_screen(site({"index.html": INDEX}).url, wait_seconds=1, use_browser=False)
    assert not result.ok and result.code == "SCREEN_ASSET_MISSING"
    assert result.diagnosis()["title"].startswith("배포 주소는 열리지만")


def test_빌드하지_않은_개발용_index_html(site):
    page = "<!doctype html><html><body><div id=root></div><script type=module src=/src/main.jsx></script></body></html>"
    result = screen_check.check_screen(site({"index.html": page}).url, wait_seconds=1, use_browser=False)
    assert not result.ok and result.code == "SCREEN_UNBUILT"


def test_HTML_이_아닌_응답(site):
    result = screen_check.check_screen(site({"index.html": "{\"error\": \"Cannot GET /\"}"}, spa_fallback=False).url + "missing",
                                       wait_seconds=1, use_browser=False)
    assert not result.ok and result.code in ("SCREEN_NOT_HTML", "SCREEN_HTTP_ERROR")


@pytest.mark.skipif(screen_check.find_browser() is None, reason="PC 에 Chromium 계열 브라우저 없음")
def test_브라우저_단계_빈_화면_스크립트_오류_정상(site):
    blank = site({"index.html": INDEX, "app.js": "const a = 1;\n"})
    crash = site({"index.html": INDEX, "app.js": "const x = process.env.API;\ndocument.getElementById('root').textContent = 'x';\n"})
    good = site({"index.html": INDEX, "app.js": "document.getElementById('root').innerHTML = '<h1>상품</h1>';\n"})
    r_blank = screen_check.check_screen(blank.url, wait_seconds=1)
    r_crash = screen_check.check_screen(crash.url, wait_seconds=1)
    r_good = screen_check.check_screen(good.url, wait_seconds=1)
    assert not r_blank.ok and r_blank.code == "SCREEN_BLANK" and r_blank.checked == "browser"
    assert not r_crash.ok and r_crash.code == "SCREEN_SCRIPT_ERROR" and "process is not defined" in r_crash.console_errors[0]
    assert r_good.ok and "상품" in r_good.text_sample


def test_테스트_실행에서는_화면_확인을_끈다(monkeypatch):
    monkeypatch.setenv("RECODER_TEST_MODE", "1")
    monkeypatch.delenv("RECODER_SCREEN_CHECK", raising=False)
    assert not screen_check.enabled()
    monkeypatch.setenv("RECODER_SCREEN_CHECK", "1")
    assert screen_check.enabled()


# ---------------------------------------------------------------------------
# 생성 단계 자동 교정
# ---------------------------------------------------------------------------

def test_생성_결과의_모듈_형식과_화면_빌드를_자동_교정(tmp_path):
    import code_agent as ca
    ops = [
        {"action": "create", "file": "package.json", "language": "json", "rationale": "",
         "content": json.dumps({"name": "shop", "type": "module", "scripts": {"start": "node server.js",
                                "build:client": "cd client && npm run build"}, "dependencies": {"express": "^4"}})},
        {"action": "create", "file": "server.js", "language": "javascript", "rationale": "",
         "content": ("import express from 'express';\nimport path from 'path';\nimport { fileURLToPath } from 'url';\n"
                     "import products from './routes/products.js';\n"
                     "const __dirname = path.dirname(fileURLToPath(import.meta.url));\nconst app = express();\n"
                     "app.use('/api/products', products);\napp.get('/health', (q, s) => s.send('ok'));\n"
                     "app.use(express.static(path.join(__dirname, 'client/dist')));\napp.listen(3000);\n")},
        {"action": "create", "file": "routes/products.js", "language": "javascript", "rationale": "",
         "content": "const express = require('express');\nconst router = express.Router();\nmodule.exports = router;\n"},
        {"action": "create", "file": "client/package.json", "language": "json", "rationale": "",
         "content": json.dumps({"name": "c", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}})},
        {"action": "create", "file": "client/index.html", "language": "html", "rationale": "", "content": "<div id=root></div>"},
    ]
    fixed, notes = ca._autofix_ops(tmp_path, "", ops)
    by = {op["file"]: op for op in fixed}
    assert "routes/products.cjs" in by and "routes/products.js" not in by
    assert "./routes/products.cjs" in by["server.js"]["content"]
    assert json.loads(by["package.json"]["content"])["scripts"]["build"] == "npm run build:client"
    remaining = [i["code"] for i in ca._consistency_issues(tmp_path, "", fixed)]
    assert "NODE_MODULE_FORMAT_MISMATCH" not in remaining and "NODE_FRONTEND_NOT_BUILT" not in remaining


def test_파일_계획은_중간에_실패하면_원래대로_되돌린다(tmp_path, monkeypatch):
    root = _write(tmp_path, {"a.js": "module.exports = 1;\n", "b.js": "module.exports = 2;\n", "main.js": "x"})
    real_unlink = Path.unlink

    def flaky(self, *a, **k):
        if self.name == "b.js":
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(ValueError, match="원래대로"):
        br.apply_file_plan(root, {"main.js": "y"}, [("a.js", "a.cjs"), ("b.js", "b.cjs")])
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert (root / "main.js").read_text() == "x"
    assert not (root / "b.cjs").exists() and (root / "b.js").exists()
