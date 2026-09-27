"""실기기 쇼핑몰(분할 생성 결과): 파일끼리 어긋난 곳을 배포 전에 잡고 고친다 + DB 함께 띄우기."""
import json
import subprocess
from pathlib import Path

import pytest

import build_readiness as br
import code_agent as ca
import local_services


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


SHOP = {
    "package.json": json.dumps({"name": "shop", "main": "server/index.js",
                                "scripts": {"server": "cd server && node index.js", "build": "cd client && npm run build"}}),
    "server/package.json": json.dumps({"name": "shop-server", "dependencies": {"express": "^4", "pg": "^8", "dotenv": "^16"}}),
    "server/index.js": "require('dotenv').config();\nconst express = require('express');\nconst db = require('./db');\n"
                       "const app = express();\napp.get('/health', (q, s) => s.json({ ok: true }));\n"
                       "db.initialize().then(() => app.listen(process.env.PORT || 3001));\n",
    "server/db.js": "const { Pool } = require('pg');\nconst pool = new Pool({ connectionString: process.env.DATABASE_URL });\n"
                    "const initializeDatabase = async () => {};\nmodule.exports = {\n  pool,\n  initializeDatabase,\n};\n",
    "client/package.json": json.dumps({"name": "shop-client", "scripts": {"build": "vite build"},
                                       "dependencies": {"react": "^18", "react-dom": "^18", "@stripe/stripe-js": "^3", "@stripe/react-stripe-js": "^2"},
                                       "devDependencies": {"vite": "^5", "@vitejs/plugin-react": "^4"}}),
    "client/index.html": '<div id="root"></div><script type="module" src="/src/index.js"></script>',
    "client/src/index.js": "import React from 'react';\nimport ReactDOM from 'react-dom/client';\nimport App from './App';\nimport './index.css';\n"
                           "ReactDOM.createRoot(document.getElementById('root')).render(\n  <React.StrictMode>\n    <App />\n  </React.StrictMode>\n);\n",
    "client/src/App.jsx": "import { loadStripe } from '@stripe/js';\nimport { Elements } from '@stripe/react-stripe-js';\n"
                          "const key = process.env.REACT_APP_STRIPE_KEY;\nexport default function App() { return <Elements stripe={loadStripe(key)} />; }\n",
    ".env.example": "REACT_APP_STRIPE_KEY=pk_test\n",
    "Dockerfile": """FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN if [ -f package-lock.json ]; then \\
        npm ci --omit=dev; \\
    else \\
        npm install --omit=dev; \\
    fi
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN cd client && npm install
RUN npm run build --if-present && rm -rf node_modules
FROM node:20-alpine AS runtime
WORKDIR /app
COPY --from=builder --chown=appuser:appgroup /app ./
COPY --from=deps --chown=appuser:appgroup /app/node_modules ./node_modules
EXPOSE 3001
CMD ["node", "server/index.js"]
""",
}


@pytest.fixture
def shop(tmp_path):
    _write(tmp_path, SHOP)
    return tmp_path


def codes(root):
    return {i.code: i for i in br.analyze(root).issues}


def test_catches_what_broke_the_real_build(shop):
    found = codes(shop)
    assert found["NODE_VITE_JSX_IN_JS"].auto_fix
    assert found["NODE_VITE_PROCESS_ENV"].auto_fix
    assert "`@stripe/js` → `@stripe/stripe-js`" in found["NODE_IMPORT_PACKAGE_TYPO"].message
    assert found["NODE_LOCAL_IMPORT_MISSING"].auto_fix and "./index.css" in found["NODE_LOCAL_IMPORT_MISSING"].message
    assert "`initialize`" in found["NODE_IMPORT_NAME_MISSING"].message
    assert found["DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING"].auto_fix
    readiness = br.analyze(shop)
    assert readiness.services == ["postgres"] and "DATABASE_URL" in readiness.env_names
    assert readiness.runtime_subproject == "server"


def test_auto_fixes_leave_only_the_code_mismatch(shop):
    for code in ("NODE_LOCAL_IMPORT_MISSING", "NODE_VITE_JSX_IN_JS", "NODE_VITE_PROCESS_ENV",
                 "NODE_IMPORT_PACKAGE_TYPO", "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING"):
        assert br.apply_fix(shop, code)["applied"], code
    assert (shop / "client/src/index.jsx").is_file() and not (shop / "client/src/index.js").exists()
    assert '/src/index.jsx' in (shop / "client/index.html").read_text()
    app = (shop / "client/src/App.jsx").read_text()
    assert "import.meta.env.VITE_STRIPE_KEY" in app and "'@stripe/stripe-js'" in app
    assert "VITE_STRIPE_KEY" in (shop / ".env.example").read_text()
    docker = (shop / "Dockerfile").read_text()
    assert "RUN cd server && " in docker and "/app/server/node_modules ./server/node_modules" in docker
    assert docker.index("RUN cd server") < docker.index("FROM node:20-alpine AS builder")
    errors = {i.code for i in br.analyze(shop).issues if i.severity == "error"}
    assert errors == {"NODE_IMPORT_NAME_MISSING"}


def test_generated_ops_are_fixed_without_calling_ai(tmp_path):
    ops = [{"action": "create", "file": k, "content": v} for k, v in SHOP.items() if k != "Dockerfile"]
    fixed, notes = ca._autofix_ops(tmp_path, "", ops)
    files = {op["file"]: op["content"] for op in fixed}
    assert "client/src/index.jsx" in files and "client/src/index.js" not in files
    assert "client/src/index.css" in files
    assert "@stripe/stripe-js" in files["client/src/App.jsx"]
    errors = {i["code"] for i in ca._consistency_issues(tmp_path, "", fixed) if i["severity"] == "error"}
    assert errors == {"NODE_IMPORT_NAME_MISSING"}
    assert notes


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {}

    def __call__(self, args, timeout):
        self.calls.append(args)
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(args, 1 if not any(c[:3] == ["docker", "network", "create"] for c in self.calls[:-1]) else 0, "", "")
        if args[:2] == ["docker", "inspect"]:
            name = args[-1]
            return subprocess.CompletedProcess(args, 0, "true", "") if name in self.containers else subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["docker", "run"]:
            self.containers[args[args.index("--name") + 1]] = True
        return ok


def test_local_postgres_is_planned_and_started(tmp_path, monkeypatch):
    monkeypatch.setattr(local_services, "_HOME", tmp_path)
    env, notes = local_services.plan("shop", ["postgres"], ["DATABASE_URL", "PORT"])
    assert env["DATABASE_URL"].startswith("postgresql://recoder:") and "@shop-postgres:5432/app" in env["DATABASE_URL"]
    assert env["PGHOST"] == "shop-postgres" and notes
    again, _ = local_services.plan("shop", ["postgres"], [])
    assert again["PGPASSWORD"] == env["PGPASSWORD"]  # 같은 볼륨을 다시 쓰려면 같은 비밀번호
    docker = FakeDocker()
    args = local_services.ensure("shop", run=docker)
    assert args == ["--network", "recoder-shop"]
    run = next(c for c in docker.calls if c[:2] == ["docker", "run"])
    assert "postgres:16-alpine" in run and "shop-postgres-data:/var/lib/postgresql/data" in run
    assert f"POSTGRES_PASSWORD={env['PGPASSWORD']}" in run
    assert any(c[:3] == ["docker", "exec", "shop-postgres"] for c in docker.calls)
    assert local_services.network_args("shop", run=docker) == ["--network", "recoder-shop"]


def test_no_services_means_no_change(tmp_path, monkeypatch):
    monkeypatch.setattr(local_services, "_HOME", tmp_path)
    assert local_services.ensure("plain", run=FakeDocker()) == []
    assert local_services.network_args("plain", run=FakeDocker()) == []
