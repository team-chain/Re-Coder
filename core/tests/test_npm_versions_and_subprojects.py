"""실기기 쇼핑몰(TEMP): 없는 npm 버전·client/ 하위 프로젝트·화면 미제공·DB 경고."""
import json
from pathlib import Path

import pytest

import build_failure
import build_readiness as br
import npm_registry


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


ROOT_PKG = {
    "name": "shopping-mall", "main": "server/index.js",
    "scripts": {"server": "node server/index.js", "build": "cd client && npm run build"},
    "dependencies": {"express": "^4.18.2", "pg": "^8.11.3", "jsonwebtoken": "^9.1.2"},
}
CLIENT_PKG = {
    "name": "shopping-mall-client",
    "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0", "react-router-dom": "^6.20.0", "axios": "^1.6.2"},
    "scripts": {"build": "react-scripts build"}, "devDependencies": {"react-scripts": "5.0.1"},
}
SERVER = """const express = require('express');
const { Pool } = require('pg');
const app = express();
const PORT = process.env.PORT || 5000;
const pool = new Pool({ connectionString: process.env.DATABASE_URL || 'postgresql://user:password@localhost:5432/shop' });
app.get('/health', (req, res) => res.json({ status: 'ok' }));
app.get('/api/products', async (req, res) => res.json([]));
app.listen(PORT, () => console.log('up'));
"""
DOCKERFILE = """FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build --if-present && rm -rf node_modules
FROM node:22-alpine
WORKDIR /app
COPY --from=builder /app ./
EXPOSE 5000
HEALTHCHECK CMD curl -f http://localhost:5000/health || exit 1
CMD ["node", "server/index.js"]
"""


@pytest.fixture
def mall(tmp_path):
    _write(tmp_path, {
        "package.json": json.dumps(ROOT_PKG, indent=2),
        "client/package.json": json.dumps(CLIENT_PKG, indent=2),
        "client/src/index.js": "import React from 'react';\nimport ReactDOM from 'react-dom/client';\nimport App from './App';\n",
        "client/src/App.jsx": "import axios from 'axios';\nimport { BrowserRouter } from 'react-router-dom';\n",
        "client/public/index.html": "<div id=\"root\"></div>",
        "server/index.js": SERVER,
        "Dockerfile": DOCKERFILE,
        ".dockerignore": "**/node_modules\n",
    })
    return tmp_path


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("RECODER_NPM_REGISTRY", "1")
    published = {"jsonwebtoken": ["8.5.1", "9.0.0", "9.0.2", "9.0.3"], "express": ["4.18.2", "4.21.2"], "pg": ["8.11.3", "8.13.0"]}
    monkeypatch.setattr(npm_registry, "fetch_versions", lambda name, timeout=6.0: published.get(name))
    return published


def test_client_imports_are_checked_against_client_package_json(mall):
    codes = {i.code for i in br.analyze(mall).issues}
    assert "NODE_UNDECLARED_DEPENDENCY" not in codes


def test_client_import_missing_from_client_manifest_names_the_right_folder(mall):
    (mall / "client/src/Extra.jsx").write_text("import dayjs from 'dayjs';\n", encoding="utf-8")
    issue = next(i for i in br.analyze(mall).issues if i.code == "NODE_UNDECLARED_DEPENDENCY")
    assert "client/package.json" in issue.message and "cd client && npm install dayjs" in issue.fix


def test_semver_ranges():
    versions = ["8.5.1", "9.0.0", "9.0.2", "10.0.0-beta.1"]
    ok = lambda spec: any(npm_registry.satisfies(v, npm_registry.parse_range(spec)) for v in versions)
    assert not ok("^9.1.2") and ok("^9.0.0") and ok("~9.0.1") and ok("9.x") and ok("^8 || ^9")
    assert not ok(">=10") and ok("1.0.0 - 9.0.0")
    assert npm_registry.parse_range("latest") is None and npm_registry.parse_range("file:../x") is None
    assert npm_registry.suggest("^9.1.2", versions) == "^9.0.2"


def test_nonexistent_version_is_an_error_and_auto_fixes(mall, registry):
    issue = next(i for i in br.analyze(mall, online=True).issues if i.code == "NODE_DEPENDENCY_VERSION_NOT_FOUND")
    assert issue.severity == "error" and issue.auto_fix and "^9.0.3" in issue.message
    out = br.apply_fix(mall, "NODE_DEPENDENCY_VERSION_NOT_FOUND")
    assert out["applied"]
    assert json.loads((mall / "package.json").read_text())["dependencies"]["jsonwebtoken"] == "^9.0.3"
    assert not any(i.code == "NODE_DEPENDENCY_VERSION_NOT_FOUND" for i in br.analyze(mall, online=True).issues)


def test_offline_registry_never_blocks(mall, monkeypatch):
    monkeypatch.setenv("RECODER_NPM_REGISTRY", "1")
    monkeypatch.setattr(npm_registry, "fetch_versions", lambda name, timeout=6.0: None)
    assert not any(i.code == "NODE_DEPENDENCY_VERSION_NOT_FOUND" for i in br.analyze(mall, online=True).issues)


def test_subproject_build_requires_installing_client_deps_and_fix_inserts_it(mall):
    readiness = br.analyze(mall)
    assert readiness.subprojects == ["client"]
    assert any(i.code == "DOCKERFILE_SUBPROJECT_DEPS_MISSING" and i.auto_fix for i in readiness.issues)
    br.apply_fix(mall, "DOCKERFILE_SUBPROJECT_DEPS_MISSING")
    text = (mall / "Dockerfile").read_text()
    lines = text.splitlines()
    install = next(i for i, l in enumerate(lines) if l == "WORKDIR /app/client")
    assert lines[install + 1].startswith("RUN if [ -f package-lock.json ]") and lines[install + 2] == "WORKDIR /app"
    build = next(i for i, l in enumerate(lines) if "npm run build" in l)
    cleanup = next(i for i, l in enumerate(lines) if l == "RUN rm -rf client/node_modules")
    assert install < build < cleanup
    assert not any(i.code == "DOCKERFILE_SUBPROJECT_DEPS_MISSING" for i in br.analyze(mall).issues)


def test_script_that_installs_client_itself_needs_nothing(mall):
    pkg = dict(ROOT_PKG, scripts={"build": "cd client && npm install && npm run build"})
    (mall / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    assert br.analyze(mall).subprojects == []


def test_frontend_not_served_is_fixed_by_serving_the_build(mall):
    issue = next(i for i in br.analyze(mall).issues if i.code == "NODE_FRONTEND_NOT_SERVED")
    assert issue.severity == "warning" and issue.auto_fix and issue.file == "server/index.js"
    br.apply_fix(mall, "NODE_FRONTEND_NOT_SERVED")
    text = (mall / "server/index.js").read_text()
    assert "join(__dirname, '../client/build')" in text
    assert text.index("recoderUiDir") < text.index("app.listen(")
    assert text.index("app.get('/health'") < text.index("recoderUiDir")
    assert (mall / ".recoder/backups").is_dir()
    assert not any(i.code == "NODE_FRONTEND_NOT_SERVED" for i in br.analyze(mall).issues)


def test_localhost_postgres_is_provisioned_instead_of_warned(mall):
    readiness = br.analyze(mall)
    assert readiness.services == ["postgres"]
    assert not any(i.code == "NODE_EXTERNAL_SERVICE" for i in readiness.issues)


def test_build_failure_explains_etarget_and_command_not_found():
    etarget = "#10 6.9 npm error code ETARGET\n#10 6.9 npm error notarget No matching version found for jsonwebtoken@^9.1.2.\n"
    d = build_failure.diagnose(etarget)
    assert d.code == "NPM_VERSION_NOT_FOUND" and "jsonwebtoken@^9.1.2" in d.cause
    assert build_failure.diagnose("#18 0.3 sh: line 1: react-scripts: command not found").code == "COMMAND_NOT_FOUND"
    assert build_failure.diagnose("#18 0.3 sh: react-scripts: not found").code == "COMMAND_NOT_FOUND"


def test_generated_dockerfile_gets_client_install(mall):
    from api.routes import deploy
    from schemas import InfraFileProposal, FileType, RiskLevel, ApprovalLevel
    proposal = InfraFileProposal(file_type=FileType.DOCKERFILE, target_path="Dockerfile", content=DOCKERFILE,
                                 risk_level=RiskLevel.LOW, approval_level=ApprovalLevel.CONFIRM)
    updated = deploy._with_subproject_installs(str(mall), proposal)
    assert "WORKDIR /app/client\nRUN if [ -f package-lock.json ]" in updated.content
    assert "RUN rm -rf client/node_modules" in updated.content
