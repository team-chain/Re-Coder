"""배포 전 빌드·실행 가능성 점검과 빌드 실패 진단.

실기기 사례(test temp 게시판): package.json 의 build 가 `react-scripts build` 인데
src/ 가 없고, 서버는 `const PORT = 5000` 인데 Dockerfile 은 3000 을 열었다. Docker 는
몇 분 설치한 뒤 실패했고 화면에는 Dockerfile 줄 번호만 보였다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import build_failure  # noqa: E402
import build_readiness as br  # noqa: E402

TEMPLATE = (_CORE / "registry" / "file_templates" / "Dockerfile.node-express").read_text(encoding="utf-8")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _express_dockerfile(port: int = 3000, health: str = "/health", entry: str = "server.js") -> str:
    return (TEMPLATE.replace("{{NODE_VERSION}}", "22").replace("{{PORT}}", str(port))
            .replace("{{START_SCRIPT}}", entry).replace("{{HEALTH_CHECK_PATH}}", health)
            .replace("{{APP_NAME}}", "app"))


BOARD_SERVER = """const express = require('express');
const sqlite3 = require('sqlite3').verbose();
const cors = require('cors');
const bodyParser = require('body-parser');
const path = require('path');
const app = express();
const PORT = 5000;
app.use(cors());
app.use(express.static(path.join(__dirname, 'public')));
app.get('/api/posts', (req, res) => res.json([]));
app.listen(PORT, () => console.log(PORT));
"""


def _board(root: Path) -> Path:
    """test temp 와 같은 구조."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return _write(root, {
        "package.json": json.dumps({
            "name": "board-app", "main": "server.js",
            "scripts": {"start": "node server.js", "dev": "nodemon server.js",
                        "build": "react-scripts build", "client": "react-scripts start"},
            "dependencies": {"express": "^4.18.2", "sqlite3": "^5.1.6", "cors": "^2.8.5",
                             "body-parser": "^1.20.2", "react": "^18.2.0", "react-dom": "^18.2.0",
                             "react-scripts": "5.0.1", "axios": "^1.4.0"},
            "devDependencies": {"nodemon": "^3.0.1"},
        }, indent=2),
        "server.js": BOARD_SERVER,
        "public/index.html": "<div id='root'></div><script src='app.js' type='text/babel'></script>",
        "public/app.js": "const { useState } = React;",
        "Dockerfile": _express_dockerfile(),
    })


def _codes(readiness) -> dict[str, str]:
    return {i.code: i.severity for i in readiness.issues}


# ---------------------------------------------------------------------------
# 실기기 사례
# ---------------------------------------------------------------------------

def test_실기기_게시판의_네_가지_문제를_빌드_전에_짚는다(tmp_path):
    r = br.analyze(_board(tmp_path))
    assert r.runtime == "node" and r.app_port == 5000 and r.probe_path() == "/"
    assert _codes(r) == {
        "NODE_UNUSED_BUILD_SCRIPT": "error",
        #: sqlite3 5.x → tar CRITICAL — 실제 Trivy 게이트에서 막힌 사례(TEMP 쇼핑몰).
        "NODE_VULNERABLE_DEPENDENCY": "error",
        "DOCKERFILE_PORT_MISMATCH": "error",
        "DOCKERFILE_HEALTH_PATH_UNKNOWN": "warning",
        "DOCKERIGNORE_MISSING": "warning",
    }
    assert all(i.auto_fix for i in r.issues)


def test_자동_수정은_누른_것만_적용하고_원본을_남긴다(tmp_path):
    root = _board(tmp_path)
    original_package = (root / "package.json").read_text(encoding="utf-8")
    original_dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    for code in ("NODE_UNUSED_BUILD_SCRIPT", "NODE_VULNERABLE_DEPENDENCY", "DOCKERFILE_PORT_MISMATCH",
                 "DOCKERFILE_HEALTH_PATH_UNKNOWN", "DOCKERIGNORE_MISSING"):
        out = br.apply_fix(root, code)
        assert out["applied"] is True
        assert code not in {i["code"] for i in out["readiness"]["issues"]}
    assert br.analyze(root).issues == []
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"] == {"start": "node server.js", "dev": "nodemon server.js"}
    assert package["dependencies"]["react-scripts"] == "5.0.1"  # lock 과 어긋나지 않게 의존성은 그대로
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 5000" in dockerfile and "localhost:5000/ " in dockerfile and ":3000" not in dockerfile
    assert "**/node_modules" in (root / ".dockerignore").read_text(encoding="utf-8")
    backups = sorted((root / ".recoder" / "backups").iterdir())
    contents = [b.read_text(encoding="utf-8") for b in backups]
    assert original_package in contents and original_dockerfile in contents  # 두 번 고쳐도 첫 원본이 남는다
    assert len(backups) == 4  # package.json 두 번(스크립트·sqlite3) + Dockerfile 두 번
    # 이미 해결된 항목·자동 수정 대상이 아닌 항목
    assert br.apply_fix(root, "DOCKERIGNORE_MISSING")["applied"] is False
    with pytest.raises(ValueError):
        br.apply_fix(root, "NODE_UNDECLARED_DEPENDENCY")


def test_자동_수정은_CRLF_줄바꿈을_보존한다(tmp_path):
    root = _board(tmp_path)
    for name in ("package.json", "Dockerfile"):
        text = (root / name).read_text(encoding="utf-8").replace("\n", "\r\n")
        (root / name).write_bytes(text.encode("utf-8"))
    br.apply_fix(root, "NODE_UNUSED_BUILD_SCRIPT")
    br.apply_fix(root, "DOCKERFILE_PORT_MISMATCH")
    for name in ("package.json", "Dockerfile"):
        raw = (root / name).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


# ---------------------------------------------------------------------------
# 정상 프로젝트는 조용해야 한다 (음성 대조)
# ---------------------------------------------------------------------------

def test_음성대조_정상_Express_는_문제가_없다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "api", "scripts": {"start": "node src/server.js"},
                                    "dependencies": {"express": "^4", "@aws-sdk/client-s3": "^3"}}),
        "src/server.js": ("import express from 'express';\nimport { S3Client } from '@aws-sdk/client-s3';\n"
                          "import fs from 'node:fs/promises';\nimport { x } from './lib/x.js';\n"
                          "const app = express();\napp.get('/health', (q, s) => s.send('ok'));\n"
                          "const PORT = process.env.PORT || 8080;\napp.listen(PORT);\n"),
        "src/lib/x.js": "export const x = 1;",
        ".dockerignore": "node_modules\n",
        "Dockerfile": _express_dockerfile(8080, "/health", "src/server.js"),
    })
    r = br.analyze(root)
    assert r.issues == [] and r.app_port == 8080 and r.port_from_env and r.probe_path() == "/health"


def test_음성대조_src_가_있는_CRA_는_빌드_문제가_아니다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "web", "scripts": {"build": "react-scripts build"},
                                    "dependencies": {"react": "18", "react-dom": "18", "react-scripts": "5"}}),
        "src/index.jsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';",
        "public/index.html": "<div id=root></div>",
    })
    assert br.analyze(root).issues == []


def test_서버가_아닌_CRA_에_src_가_없으면_자동수정_없이_막는다(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"name": "web", "scripts": {"build": "react-scripts build"},
                                    "dependencies": {"react-scripts": "5"}}),
        "public/index.html": "<div id=root></div>",
    })
    issue = br.analyze(root).issues[0]
    assert (issue.code, issue.severity, issue.auto_fix) == ("NODE_BUILD_ENTRY_MISSING", "error", False)
    assert "src/index.js" in issue.message


@pytest.mark.parametrize("script,deps,expected", [
    ("vite build", {}, "NODE_BUILD_TOOL_MISSING"),
    ("tsc && node dist/x.js", {"typescript": "5"}, "NODE_BUILD_ENTRY_MISSING"),   # tsconfig 없음
    ("vite build", {"vite": "5"}, "NODE_BUILD_ENTRY_MISSING"),                    # index.html 없음
    ("npm run compile", {}, "NODE_BUILD_TOOL_MISSING"),                           # 다른 스크립트를 따라간다
    ("cross-env NODE_ENV=production next build", {"next": "14"}, "NODE_BUILD_ENTRY_MISSING"),
])
def test_빌드_스크립트가_실제로_돌_수_있는지_본다(tmp_path, script, deps, expected):
    scripts = {"build": script, "compile": "webpack"}
    _write(tmp_path, {"package.json": json.dumps({"name": "x", "scripts": scripts, "dependencies": deps})})
    assert _codes(br.analyze(tmp_path)).get(expected) == "error"


def test_선언되지_않은_패키지와_없는_시작_파일(tmp_path):
    _write(tmp_path, {
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node index.js"},
                                    "dependencies": {"express": "4"}}),
        "server.js": "const express=require('express');\nconst _ = require('lodash/fp');\nrequire('node:path');\n// require('commented-out')\n",
        "public/app.js": "import confetti from 'canvas-confetti';",   # 브라우저 파일은 제외
    })
    r = br.analyze(tmp_path)
    assert _codes(r) == {"NODE_START_ENTRY_MISSING": "error", "NODE_UNDECLARED_DEPENDENCY": "error"}
    dep = next(i for i in r.issues if i.code == "NODE_UNDECLARED_DEPENDENCY")
    assert "`lodash`" in dep.message and "commented-out" not in dep.message and "confetti" not in dep.message


@pytest.mark.parametrize("source,port,env", [
    ("const PORT = 5000;\napp.listen(PORT)", 5000, False),
    ("const PORT = process.env.PORT || 4000;", 4000, True),
    ("const port = Number(process.env.PORT ?? '3100');", 3100, True),
    ("app.listen(8081, () => {})", 8081, False),
    ("let port = parseInt('7000');", 7000, False),
])
def test_앱이_듣는_포트를_읽는다(source, port, env):
    assert br._detect_js_port(source) == (port, env)


def test_PORT_환경변수를_Dockerfile_ENV_로_맞췄으면_불일치가_아니다(tmp_path):
    dockerfile = _express_dockerfile(3000).replace("EXPOSE 3000", "ENV PORT=3000\nEXPOSE 3000")
    _write(tmp_path, {
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4"}}),
        "server.js": "const express=require('express');const app=express();app.get('/health',()=>{});app.listen(process.env.PORT || 5000);",
        "Dockerfile": dockerfile, ".dockerignore": "node_modules",
    })
    assert "DOCKERFILE_PORT_MISMATCH" not in _codes(br.analyze(tmp_path))


def test_가상_파일로_생성_결과를_미리_본다(tmp_path):
    root = _board(tmp_path)
    fixed = json.loads((root / "package.json").read_text(encoding="utf-8"))
    del fixed["scripts"]["build"], fixed["scripts"]["client"]
    fixed["dependencies"]["sqlite3"] = "^6.0.1"
    r = br.analyze(root, {"package.json": json.dumps(fixed), "Dockerfile": None})
    assert _codes(r) == {}
    r = br.analyze(root, {"src/index.js": "import React from 'react';", "Dockerfile": None})
    # src/index.js 가 생기면 CRA 빌드는 가능하다(남는 것은 sqlite3 5.x 취약 의존성뿐)
    assert _codes(r) == {"NODE_VULNERABLE_DEPENDENCY": "error"}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def test_FastAPI_헬스_경로와_누락_의존성(tmp_path):
    _write(tmp_path, {
        "requirements.txt": "fastapi==0.110\nuvicorn[standard]\n",
        "main.py": ("from fastapi import FastAPI\nimport yaml\nimport httpx\nfrom app import helpers\n"
                    "app = FastAPI()\n@app.get('/healthz')\ndef h():\n    return {}\n"),
        "app/helpers.py": "import os\n",
    })
    r = br.analyze(tmp_path)
    assert r.runtime == "python" and r.probe_path() == "/healthz"
    issue = r.issues[0]
    assert issue.code == "PY_UNDECLARED_DEPENDENCY" and issue.severity == "warning"
    assert "`pyyaml`" in issue.message and "`httpx`" in issue.message and "helpers" not in issue.message


def test_헬스_라우트가_없는_FastAPI_는_docs_로_확인한다(tmp_path):
    _write(tmp_path, {"requirements.txt": "fastapi\n", "main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    assert br.analyze(tmp_path).probe_path() == "/docs"
    _write(tmp_path, {"main.py": "from fastapi import FastAPI\napp = FastAPI(docs_url=None)\n"})
    assert br.analyze(tmp_path).probe_path() is None


def test_파이썬_문법_오류와_requirements_없음(tmp_path):
    _write(tmp_path, {"app.py": "import flask\ndef broken(:\n"})
    codes = _codes(br.analyze(tmp_path))
    assert codes == {"PY_SYNTAX_ERROR": "error"}
    _write(tmp_path, {"app.py": "import flask\n"})
    assert _codes(br.analyze(tmp_path)) == {"PY_REQUIREMENTS_MISSING": "error"}


# ---------------------------------------------------------------------------
# 생성기·헬스 판정이 같은 판단을 쓴다
# ---------------------------------------------------------------------------

def test_Dockerfile_생성용_포트_감지가_const_PORT_를_읽는다(tmp_path):
    from infra_agent import _detect_node_entry_and_port, discover_health_path
    root = _board(tmp_path)
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert _detect_node_entry_and_port(root, package) == ("server.js", 5000)
    # 헬스 라우트가 없고 정적 폴더를 제공하면 "/" 를 찌른다(없는 /health 대신).
    assert discover_health_path(str(root), "node-express", "/health") == "/"
    assert discover_health_path(str(root), "node-express", "/custom") == "/custom"  # 사람이 정한 값


# ---------------------------------------------------------------------------
# 빌드 실패 진단
# ---------------------------------------------------------------------------

REAL_LOG = """#13 [builder 6/6] RUN npm run build --if-present && rm -rf node_modules
#13 0.761
#13 0.761 > board-app@1.0.0 build
#13 0.761 > react-scripts build
#13 0.761
#13 2.847 Could not find a required file.
#13 2.848   Name: index.js
#13 2.848   Searched in: /app/src
#13 ERROR: process "/bin/sh -c npm run build --if-present && rm -rf node_modules" did not complete successfully: exit code: 1
------
 > [builder 6/6] RUN npm run build --if-present && rm -rf node_modules:
2.847 Could not find a required file.
2.848   Name: index.js
------
Dockerfile:47
--------------------
  46 |     # npm 자체의 --if-present 판정을 사용해 scripts.build만 정확히 실행한다.
  47 | >>> RUN npm run build --if-present && rm -rf node_modules
--------------------
ERROR: failed to build: failed to solve: process "/bin/sh -c npm run build --if-present && rm -rf node_modules" did not complete successfully: exit code: 1
"""


def test_빌드_실패에서_실제_원인_줄을_뽑는다():
    d = build_failure.diagnose(REAL_LOG)
    assert d.code == "CRA_ENTRY_MISSING" and "src/index.js" in d.cause
    assert "Could not find a required file." in d.lines and "Name: index.js" in d.lines
    assert not any(" | " in line for line in d.lines)  # Dockerfile 발췌는 원인이 아니다
    assert d.lines.count("Could not find a required file.") == 1
    assert d.step.startswith("npm run build")


def test_정적_점검이_짚은_원인이면_그_해결책을_앞세운다(tmp_path):
    issues = br.analyze(_board(tmp_path)).issues
    d = build_failure.diagnose(REAL_LOG, issues)
    assert "자동 수정" in d.fix and "src/ 가 없고" in d.cause


@pytest.mark.parametrize("log,code", [
    ("npm error `npm ci` can only install packages when your package.json and package-lock.json are in sync", "NPM_LOCK_OUT_OF_SYNC"),
    ("#9 1.2 Error: Cannot find module 'express'", "NODE_MODULE_NOT_FOUND"),
    ("#9 0.5 sh: vite: not found", "COMMAND_NOT_FOUND"),
    ("#10 3.1 gyp ERR! stack Error: not found: make", "NATIVE_MODULE_BUILD"),
    ("#7 ERROR: No matching distribution found for fastapix==9.9", "PIP_NO_DISTRIBUTION"),
    ("ERROR: failed to solve: failed to compute cache key: failed to calculate checksum: \"/requirements.txt\": not found", "COPY_SOURCE_MISSING"),
    ("failed to resolve source metadata for docker.io/library/node:22-alpine: i/o timeout", "BASE_IMAGE_PULL"),
    ("src/a.ts(3,1): error TS2304: Cannot find name 'x'.", "TYPESCRIPT_ERROR"),
    ("something odd\nexit code: 2", "UNKNOWN"),
])
def test_흔한_빌드_실패를_분류한다(log, code):
    d = build_failure.diagnose(log)
    assert d.code == code and d.title and d.fix and d.lines


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

TOKEN = "t" * 32


@pytest.fixture()
def client():
    import main
    app = main.create_app()
    app.state.session_token = TOKEN
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555))


def test_점검과_자동수정_API(client, tmp_path):
    root = _board(tmp_path)
    headers = {"X-Session-Token": TOKEN}
    body = client.post("/api/deploy/readiness", json={"workspace_path": str(root)}, headers=headers).json()
    assert {i["code"] for i in body["issues"]} >= {"NODE_UNUSED_BUILD_SCRIPT", "DOCKERFILE_PORT_MISMATCH"}
    fixed = client.post("/api/deploy/readiness/fix", json={"workspace_path": str(root), "code": "DOCKERFILE_PORT_MISMATCH"},
                        headers=headers)
    assert fixed.status_code == 200 and fixed.json()["applied"] is True
    refused = client.post("/api/deploy/readiness/fix", json={"workspace_path": str(root), "code": "PY_SYNTAX_ERROR"},
                          headers=headers)
    assert refused.status_code == 409
    bad = client.post("/api/deploy/readiness/fix", json={"workspace_path": str(root), "code": "../x"}, headers=headers)
    assert bad.status_code == 422


def test_Dockerfile_초안에_빌드_실패_예상과_dockerignore_안내가_붙고_저장시_생성된다(client, tmp_path, monkeypatch):
    import api.routes.deploy as deploy_routes
    root = _board(tmp_path)
    (root / "Dockerfile").unlink()
    monkeypatch.setattr(deploy_routes, "_get_infra_agent", lambda: None)
    headers = {"X-Session-Token": TOKEN}
    proposal = client.post("/api/deploy/dockerfile", json={"workspace_path": str(root)}, headers=headers).json()
    reasons = proposal["risk_reasons"]
    assert any("BLOCKER" in r and "react-scripts" in r for r in reasons)
    assert deploy_routes.SERVER_IGNORE_NOTICE in reasons
    assert "EXPOSE 5000" in proposal["content"]  # const PORT = 5000 을 읽었다
    assert "localhost:5000/ " in proposal["content"]  # 없는 /health 대신 실제로 200 인 "/"
    saved = client.post("/api/deploy/dockerfile/approve",
                        params={"proposal_id": proposal["proposal_id"], "approved": True,
                                "workspace_path": str(root)}, headers=headers).json()
    assert saved["status"] == "saved" and (root / ".dockerignore").is_file()


# ---------------------------------------------------------------------------
# 컨테이너 안 데이터 파일 (실기기 E2E: 빌드를 고친 뒤 SQLITE_CANTOPEN 으로 API 가 실패)
# ---------------------------------------------------------------------------

def test_비root_컨테이너가_작업_폴더에_DB_를_못_만드는_것을_짚고_고친다(tmp_path):
    root = _board(tmp_path)
    (root / "server.js").write_text(BOARD_SERVER.replace(
        "const app = express();", "const app = express();\nconst db = new sqlite3.Database('./board.db');"), encoding="utf-8")
    # 이전 버전 템플릿으로 만든 Dockerfile(작업 폴더 chown 없음) — 사용자의 test temp 와 같다.
    old = (root / "Dockerfile").read_text(encoding="utf-8").replace("RUN chown appuser:appgroup /app\n", "")
    (root / "Dockerfile").write_text(old, encoding="utf-8")
    codes = _codes(br.analyze(root))
    assert codes["DOCKERFILE_WORKDIR_NOT_WRITABLE"] == "error" and codes["DATA_IN_CONTAINER"] == "warning"
    br.apply_fix(root, "DOCKERFILE_WORKDIR_NOT_WRITABLE")
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert text.index("RUN chown appuser /app") < text.index("USER appuser")
    assert "DOCKERFILE_WORKDIR_NOT_WRITABLE" not in _codes(br.analyze(root))


def test_새_템플릿은_작업_폴더를_앱_사용자에게_준다(tmp_path):
    root = _board(tmp_path)
    (root / "server.js").write_text(BOARD_SERVER + "\nnew sqlite3.Database('./board.db');\n", encoding="utf-8")
    for name in ("Dockerfile.node-express", "Dockerfile.python-fastapi", "Dockerfile.node-next"):
        body = (_CORE / "registry" / "file_templates" / name).read_text(encoding="utf-8")
        assert "RUN chown appuser:appgroup /app" in body
    assert "DOCKERFILE_WORKDIR_NOT_WRITABLE" not in _codes(br.analyze(root, {"Dockerfile": _express_dockerfile(5000, "/")}))


def test_파이썬_SQLite_상대경로도_데이터_경고(tmp_path):
    _write(tmp_path, {"requirements.txt": "flask\n", "app.py": "import sqlite3\nfrom flask import Flask\napp = Flask(__name__)\nc = sqlite3.connect('data.db')\n@app.route('/')\ndef i():\n    return 'ok'\n"})
    r = br.analyze(tmp_path)
    assert _codes(r) == {"DATA_IN_CONTAINER": "warning"} and r.probe_path() == "/"


def test_시작_직후_종료된_컨테이너_로그도_진단한다(tmp_path):
    log = "node:internal/modules/cjs/loader:1228\n  throw err;\nError: Cannot find module 'dayjs'\nRequire stack:\n- /app/server.js"
    d = build_failure.diagnose(log, stage="run")
    assert d.code == "NODE_MODULE_NOT_FOUND" and "dayjs" in d.cause
    d = build_failure.diagnose("Database connection error: [Error: SQLITE_CANTOPEN: unable to open database file]", stage="run")
    assert d.code == "SQLITE_CANTOPEN"
    d = build_failure.diagnose("some crash", stage="run")
    assert d.title == "컨테이너 실행 실패"


def test_BuildKit_요약의_시간_접두어를_지운다():
    d = build_failure.diagnose("------\n > [builder 6/6] RUN npm run build:\n20.82 Could not find a required file.\n20.82   Name: index.js\n")
    assert "Could not find a required file." in d.lines and not any(l.strip() == "20.82" for l in d.lines)
