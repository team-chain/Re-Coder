"""npm workspaces 모노레포 로컬 배포(실기기 TEMP 쇼핑몰, 2026-10).

루트 package.json 이 `"start": "npm start --workspace=backend"` 인 프로젝트에서
- Dockerfile 이 `CMD ["node", "index.js"]`(없는 파일)로 만들어져 컨테이너가 바로 죽었고,
- backend/package.json 의 express·pg 가 실행 이미지에 들어가지 않았고(워크스페이스는 루트에 설치),
- 앱은 5000 을 듣는데 Dockerfile 은 3000 을 열었다.
Trivy 결과는 AI 요약만 보여 줘서 어느 이미지의 무엇이 문제인지 알 수 없었다.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_readiness as br
import vuln_advice
from api.routes import deploy as d

ROOT_PKG = {
    "name": "shopping-mall", "private": True,
    "scripts": {
        "start": "npm start --workspace=backend",
        "build": "npm --prefix frontend run build && npm run build --workspace=frontend && npm run build --workspace=backend",
    },
    "workspaces": ["backend", "frontend"],
    "devDependencies": {"concurrently": "^8.2.2"},
}
BACKEND_PKG = {
    "name": "shopping-mall-backend", "main": "server.js", "type": "module",
    "scripts": {"start": "node server.js", "build": "echo ready"},
    "dependencies": {"express": "^4.18.0", "pg": "^8.11.0", "cors": "^2.8.5", "dotenv": "^16.3.0"},
}
FRONTEND_PKG = {
    "name": "shopping-mall-frontend", "type": "module",
    "scripts": {"dev": "vite", "build": "vite build"},
    "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
    "devDependencies": {"@vitejs/plugin-react": "^4.2.0", "vite": "^5.0.0"},
}
SERVER = """import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const PORT = process.env.PORT || 5000;
app.use(cors());
app.get('/api/health', (req, res) => res.json({ status: 'ok' }));
const distPath = path.join(__dirname, '../frontend/dist');
app.use(express.static(distPath));
app.get('*', (req, res) => res.sendFile(path.join(distPath, 'index.html')));
app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
"""
#: 사용자가 받은 Dockerfile 의 핵심 — CMD index.js, EXPOSE 3000, 루트 의존성만 설치.
OLD_DOCKERFILE = """FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./
RUN if [ -f yarn.lock ]; then \\
        yarn install --frozen-lockfile --production; \\
    elif [ -f package-lock.json ]; then \\
        npm ci --omit=dev; \\
    else \\
        npm install --omit=dev; \\
    fi

FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* ./
RUN npm install
COPY . .
RUN cd frontend && if [ -f package-lock.json ]; then npm ci || npm install; else npm install; fi
RUN npm run build --if-present && rm -rf node_modules
RUN rm -rf frontend/node_modules

FROM node:22-alpine AS runtime
RUN addgroup -g 1001 appgroup && adduser -u 1001 -G appgroup -s /bin/sh -D appuser
WORKDIR /app
RUN chown appuser:appgroup /app
COPY --from=builder --chown=appuser:appgroup /app ./
COPY --from=deps --chown=appuser:appgroup /app/node_modules ./node_modules
USER appuser
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \\
    CMD curl -f http://localhost:3000/api/health || exit 1
CMD ["node", "index.js"]
"""


def _project(tmp_path: Path, dockerfile: str | None = OLD_DOCKERFILE) -> Path:
    root = tmp_path / "TEMP"
    (root / "backend").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps(ROOT_PKG, indent=2), encoding="utf-8")
    (root / "backend" / "package.json").write_text(json.dumps(BACKEND_PKG, indent=2), encoding="utf-8")
    (root / "backend" / "server.js").write_text(SERVER, encoding="utf-8")
    (root / "frontend" / "package.json").write_text(json.dumps(FRONTEND_PKG, indent=2), encoding="utf-8")
    (root / "frontend" / "index.html").write_text("<div id=root></div><script type=module src=/src/main.jsx></script>", encoding="utf-8")
    (root / "frontend" / "src" / "main.jsx").write_text("console.log('hi')\n", encoding="utf-8")
    (root / ".dockerignore").write_text("**/node_modules\n.git\n", encoding="utf-8")
    if dockerfile is not None:
        (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return root


# ── start 스크립트가 다른 패키지로 넘기는 경우 ─────────────────────────────

@pytest.mark.parametrize("files,scripts,expected", [
    ({"backend/package.json": json.dumps({"scripts": {"start": "node server.js"}})},
     {"start": "npm start --workspace=backend"}, "backend/server.js"),
    ({"backend/package.json": json.dumps({"scripts": {"start": "node server.js"}})},
     {"start": "npm run start -w backend"}, "backend/server.js"),
    ({"backend/package.json": json.dumps({"name": "@shop/api", "scripts": {"start": "node src/index.js"}})},
     {"start": "npm start --workspace=@shop/api"}, "backend/src/index.js"),
    ({"server/package.json": json.dumps({"scripts": {"start": "node -r dotenv/config app.js"}})},
     {"start": "npm --prefix server start"}, "server/app.js"),
    ({"api/package.json": json.dumps({"scripts": {"start": "node index.js"}})},
     {"start": "cd api && npm start"}, "api/index.js"),
    ({"packages/api/package.json": json.dumps({"name": "api", "scripts": {"start": "node dist/main.js"}})},
     {"start": "yarn workspace api start"}, "packages/api/dist/main.js"),
    ({"apps/web/package.json": json.dumps({"name": "web", "main": "server.js"})},
     {"start": "pnpm --filter web start"}, "apps/web/server.js"),
    ({}, {"start": "npm run serve", "serve": "node app.js"}, "app.js"),
    ({}, {"start": "cd server && node index.js"}, "server/index.js"),
    ({}, {"start": "node server.js"}, "server.js"),
])
def test_start_entry_follows_delegation(files, scripts, expected):
    pf = br.ProjectFiles(Path("/nonexistent-recoder"), files)
    assert br._start_entry(scripts, pf) == expected


@pytest.mark.parametrize("scripts", [
    {"start": "npm start"},                       # 자기 자신 — 무한 반복하지 않는다
    {"start": "node ../outside.js"},              # 프로젝트 밖
    {"start": "npm start --workspace=missing"},   # 없는 워크스페이스
])
def test_start_entry_does_not_guess(scripts):
    assert br._start_entry(scripts, br.ProjectFiles(Path("/nonexistent-recoder"), {})) is None


def test_start_entry_without_files_keeps_old_behaviour():
    assert br._start_entry({"start": "npm start --workspace=backend"}) is None
    assert br._start_entry({"start": "node -r dotenv/config server.js"}) == "server.js"


# ── Dockerfile 생성이 실제 서버 파일·포트를 쓴다 ─────────────────────────────

def test_generated_dockerfile_targets_workspace_server(tmp_path):
    root = _project(tmp_path, dockerfile=None)
    from infra_agent import _detect_node_entry_and_port
    assert _detect_node_entry_and_port(root, ROOT_PKG) == ("backend/server.js", 5000)
    from project_scanner import get_project_scanner
    profile = get_project_scanner().scan(str(root))
    assert profile.default_run_command == "node backend/server.js"
    assert profile.default_port == 5000
    assert d._discover_node_entrypoint(str(root), d.StackType.NODE_EXPRESS, profile) == "backend/server.js"
    from agents.infra_agent import InfraAgent
    out = InfraAgent._enforce_safe_customisations({"START_SCRIPT": "index.js", "PORT": "3000"}, d.StackType.NODE_EXPRESS, profile)
    assert out["START_SCRIPT"] == "backend/server.js" and out["PORT"] == "5000"


# ── 이미 저장된 잘못된 Dockerfile 은 배포 직전에 고친다 ─────────────────────

def test_readiness_offers_cmd_fix_with_real_server(tmp_path):
    root = _project(tmp_path)
    issues = {i.code: i for i in br.analyze(root).issues}
    entry = issues["DOCKERFILE_ENTRY_MISSING"]
    assert entry.auto_fix and "backend/server.js" in entry.message
    assert "DOCKERFILE_PORT_MISMATCH" in issues


def test_cmd_fix_is_not_offered_when_server_is_unknown(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": "x", "dependencies": {"express": "4"}}), encoding="utf-8")
    (root / "Dockerfile").write_text('FROM node:22-alpine\nCMD ["node", "index.js"]\n', encoding="utf-8")
    issue = next(i for i in br.analyze(root).issues if i.code == "DOCKERFILE_ENTRY_MISSING")
    assert not issue.auto_fix
    with pytest.raises(ValueError):
        br.apply_fix(root, "DOCKERFILE_ENTRY_MISSING")


def test_pre_deploy_fix_chain_makes_monorepo_runnable(tmp_path):
    root = _project(tmp_path)
    applied = [f["code"] for f in d._auto_fix_before_build(str(root))]
    assert {"DOCKERFILE_ENTRY_MISSING", "DOCKERFILE_PORT_MISMATCH", "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING"} <= set(applied)
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["node", "backend/server.js"]' in text
    assert "EXPOSE 5000" in text and "localhost:5000/api/health" in text
    #: 워크스페이스는 루트에 설치된다 — 그 폴더에 설치해야 다음 COPY 가 깨지지 않는다.
    assert "cd backend && " in text and "--workspaces=false" in text
    assert "COPY --from=deps --chown=appuser:appgroup /app/backend/node_modules ./backend/node_modules" in text
    left = [i.code for i in br.analyze(root).issues if i.severity == "error"]
    assert left == []
    assert list((root / ".recoder" / "backups").rglob("Dockerfile*")), "원본 Dockerfile 백업이 없다"


def test_non_workspace_subproject_install_is_unchanged():
    text = OLD_DOCKERFILE
    out = br.add_runtime_subproject_install(text, "server")
    assert "--workspaces=false" not in out
    assert "npm install --omit=dev; fi" in out


def test_ports_follow_dockerfile_after_pre_deploy_fix(tmp_path):
    root = _project(tmp_path)
    d._auto_fix_before_build(str(root))
    plan = SimpleNamespace(ports={"3001": "3000"}, env={})
    note = d._align_ports_with_dockerfile(plan, root / "Dockerfile")
    assert plan.ports == {"3001": "5000"} and "3000 → 5000" in note
    source = Path(d.__file__).read_text(encoding="utf-8")
    block = source[source.index("pre_deploy_fixes = await asyncio.to_thread(_auto_fix_before_build"):]
    block = block[:block.index("build_failure = await _build_local_image")]
    assert "_align_ports_with_dockerfile(plan, fixed_dockerfile)" in block


@pytest.mark.skipif(not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
                    reason="Docker 데몬 필요")
def test_fixed_dockerfile_builds_and_serves(tmp_path):
    """실제로 빌드해 backend/node_modules 가 이미지에 들어가고 서버가 뜨는지(설치 위치 회귀 방지)."""
    root = _project(tmp_path)
    (root / "backend" / "server.js").write_text(SERVER.replace("import cors from 'cors';\n", "").replace("app.use(cors());\n", ""), encoding="utf-8")
    pkg = dict(BACKEND_PKG, dependencies={"express": "^4.18.0"})
    (root / "backend" / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (root / "frontend" / "package.json").write_text(json.dumps({"name": "fe", "scripts": {"build": "mkdir -p dist && echo ok > dist/index.html"}}), encoding="utf-8")
    d._auto_fix_before_build(str(root))
    tag = "recoder-test-monorepo:latest"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, str(root)], capture_output=True, text=True, timeout=900)
    if build.returncode != 0 and "pull" in (build.stderr or "").lower():
        pytest.skip("베이스 이미지를 받을 수 없다")
    assert build.returncode == 0, build.stderr[-2000:]
    try:
        ls = subprocess.run(["docker", "run", "--rm", "--entrypoint", "ls", tag, "/app/backend/node_modules/express"],
                            capture_output=True, text=True, timeout=60)
        assert ls.returncode == 0, ls.stderr
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


# ── Trivy 결과: 어느 이미지의 무엇이 문제인지 ─────────────────────────────

def test_trivy_headline_names_image_package_and_origin(tmp_path):
    root = _project(tmp_path)
    findings = [{"severity": "HIGH", "package": "axios", "installed": "0.19.2", "fixed": "0.21.1, 1.12.0",
                 "pkg_path": "app/node_modules/localtunnel/node_modules/axios/package.json", "class": "lang-pkgs"}] * 12
    line = vuln_advice.trivy_headline("temp:latest", "2026-10-03 02:55", findings, str(root))
    assert line.startswith("검사한 이미지 temp:latest · 2026-10-03 02:55 빌드 — HIGH 12건")
    assert "axios 0.19.2 → 1.12.0" in line and "localtunnel 이(가) 함께 설치한 하위 패키지" in line
    os_line = vuln_advice.trivy_headline("x:latest", "", [{"severity": "CRITICAL", "package": "openssl", "installed": "3.0",
                                                            "fixed": "3.1", "class": "os-pkgs"}])
    assert "CRITICAL 1건" in os_line and "베이스 이미지 OS 패키지" in os_line


def test_trivy_scan_summary_is_the_headline_not_ai_text(monkeypatch):
    async def created(_image):
        return "2026-10-03 02:55"
    monkeypatch.setattr(d, "_image_created", created)
    report = {"status": "ok", "scan_type": "trivy", "target": "temp:latest", "critical_count": 0, "high_count": 1,
              "medium_count": 0, "summary": "The container image contains 12 high-severity vulnerabilities",
              "findings": [{"severity": "HIGH", "package": "axios", "installed": "0.19.2", "fixed": "1.12.0",
                            "pkg_path": "app/node_modules/a/node_modules/axios/package.json", "class": "lang-pkgs"}]}
    out = asyncio.run(d._with_trivy_headline(report, ""))
    assert out["summary"].startswith("검사한 이미지 temp:latest · 2026-10-03 02:55 빌드")
    assert out["ai_summary"].startswith("The container image")
    clean = asyncio.run(d._with_trivy_headline({**report, "findings": [], "high_count": 0}, ""))
    assert clean["summary"].endswith("CRITICAL·HIGH 취약점 없음")


def test_plan_time_high_findings_say_previous_build():
    source = Path(d.__file__).read_text(encoding="utf-8")
    assert 'HIGH CVE(s) in {image} (이전에 빌드된 이미지 기준' in source
