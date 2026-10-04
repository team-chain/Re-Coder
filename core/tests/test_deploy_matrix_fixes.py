"""로컬 Docker 배포 매트릭스(2026-10-03)에서 실제로 깨진 경우들 — 고친 동작을 고정한다.

- 루트 package.json 에 설치할 패키지가 없는 모노레포(client/ + server/): 실행 단계의
  `COPY --from=deps /app/node_modules` 가 "not found" 로 빌드를 깨뜨렸다.
- Next.js: 런타임 단계의 `npm install -g npm@latest` 가 npm 12 이후 "Cannot find module
  'promise-retry'" 로 실패했다(node 20·22 이미지 모두 실측).
- TypeScript(`node dist/index.js`)·`Number(process.env.PORT) || 8080`: 포트를 못 읽어 3000 으로 열었다.
- 시작하자마자 throw 하는 서버: 재시작 정책 때문에 "느린 시작"으로 판정돼 이전 버전이 복구되지 않았고,
  진단은 "컨테이너 실행 실패"뿐이었다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import build_readiness as br
from api.routes import deploy as d
from build_failure import diagnose

TEMPLATE = (Path(__file__).resolve().parents[1] / "registry" / "file_templates" / "Dockerfile.node-express").read_text(encoding="utf-8")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_template_always_has_node_modules_for_the_runtime_copy():
    deps_stage = TEMPLATE.split("FROM node:{{NODE_VERSION}}-alpine AS builder")[0]
    assert "RUN mkdir -p node_modules" in deps_stage


def test_empty_root_package_gets_node_modules_fix(tmp_path):
    old = TEMPLATE.replace("RUN mkdir -p node_modules\n", "")
    root = _write(tmp_path / "board", {
        "package.json": json.dumps({"name": "board", "scripts": {"start": "cd server && npm start", "build": "cd client && npm install && npm run build"}}),
        "server/package.json": json.dumps({"scripts": {"start": "node index.js"}, "dependencies": {"express": "^4"}}),
        "server/index.js": "const express=require('express');const path=require('path');const app=express();"
                           "app.use(express.static(path.join(__dirname,'../client/dist')));app.listen(process.env.PORT||4000);",
        "client/package.json": json.dumps({"scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}),
        "Dockerfile": old,
    })
    codes = {i.code: i for i in br.analyze(root).issues}
    assert codes["DOCKERFILE_DEPS_DIR_MISSING"].auto_fix
    #: 빌드가 만드는 폴더(../client/dist)는 지금 없어도 경고하지 않는다.
    assert "NODE_STATIC_DIR_MISSING" not in codes
    br.apply_fix(root, "DOCKERFILE_DEPS_DIR_MISSING")
    assert "RUN mkdir -p node_modules" in (root / "Dockerfile").read_text(encoding="utf-8")
    assert "DOCKERFILE_DEPS_DIR_MISSING" not in {i.code for i in br.analyze(root).issues}


def test_dev_dependencies_or_workspaces_do_not_trigger_the_fix(tmp_path):
    old = TEMPLATE.replace("RUN mkdir -p node_modules\n", "")
    for pkg in ({"devDependencies": {"concurrently": "^8"}}, {"workspaces": ["backend"]}):
        root = _write(tmp_path / str(len(list(tmp_path.iterdir()))), {"package.json": json.dumps({"name": "x", **pkg}), "Dockerfile": old})
        assert "DOCKERFILE_DEPS_DIR_MISSING" not in {i.code for i in br.analyze(root).issues}


NEXT_OLD = """FROM node:20-alpine AS builder
WORKDIR /app
COPY . .
RUN npm ci && npm run build

FROM node:20-alpine AS runtime
# 번들 npm 을 최신으로 — npm install -g npm@latest 는 옛 안내(주석은 무시)
RUN apk upgrade --no-cache && npm install -g npm@latest
WORKDIR /app
COPY --from=builder /app ./
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s \\
    CMD curl -f http://localhost:3000/ || exit 1
CMD ["npm", "start"]
"""


def test_next_npm_self_upgrade_is_removed_and_next_runs_with_node(tmp_path):
    root = _write(tmp_path / "next", {"package.json": json.dumps({"scripts": {"start": "next start"}, "dependencies": {"next": "14"}}),
                                      "Dockerfile": NEXT_OLD})
    issue = next(i for i in br.analyze(root).issues if i.code == "DOCKERFILE_NPM_SELF_UPGRADE")
    assert issue.auto_fix and issue.severity == "error"
    br.apply_fix(root, "DOCKERFILE_NPM_SELF_UPGRADE")
    out = (root / "Dockerfile").read_text(encoding="utf-8")
    code_lines = [l for l in out.splitlines() if not l.lstrip().startswith("#")]
    assert not any("npm install -g npm" in l for l in code_lines)
    assert "RUN apk upgrade --no-cache" in out
    assert 'CMD ["node", "node_modules/next/dist/bin/next", "start", "-H", "0.0.0.0", "-p", "3000"]' in out
    assert "rm -rf /usr/local/lib/node_modules/npm" in out
    assert "CMD curl -f http://localhost:3000/" in out, "HEALTHCHECK 줄을 실행 명령으로 착각했다"


def test_app_started_by_npm_keeps_npm_but_drops_the_upgrade(tmp_path):
    text = "FROM node:22-alpine\nWORKDIR /app\nCOPY . .\nRUN npm install -g npm@latest\nCMD [\"npm\", \"start\"]\n"
    out = br.drop_npm_self_upgrade(text, next_app=False)
    assert "npm install -g npm" not in out and 'CMD ["npm", "start"]' in out
    assert "node_modules/npm" not in out, "npm 으로 뜨는 앱에서 npm 을 지웠다"


def test_templates_never_upgrade_npm_in_place():
    tpl = Path(__file__).resolve().parents[1] / "registry" / "file_templates"
    for name in ("Dockerfile.node-express", "Dockerfile.node-next", "Dockerfile.node-static"):
        lines = [l for l in (tpl / name).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
        assert not any(br._NPM_SELF_UPGRADE.search(l) for l in lines), name


def test_wrapped_port_and_typescript_entry(tmp_path):
    assert br._detect_js_port("const port = Number(process.env.PORT) || 8080;") == (8080, True)
    assert br._detect_js_port("app.listen(parseInt(process.env.PORT, 10) || 5050)") == (5050, True)
    root = _write(tmp_path / "ts", {
        "package.json": json.dumps({"scripts": {"build": "tsc", "start": "node dist/index.js"}, "dependencies": {"express": "^4"}}),
        "tsconfig.json": '{ // 주석\n "compilerOptions": { "outDir": "dist", "rootDir": "src", },\n}',
        "src/index.ts": "import express from 'express';\nconst app = express();\nconst port = Number(process.env.PORT) || 8080;\napp.listen(port);\n",
    })
    from infra_agent import _detect_node_entry_and_port
    assert _detect_node_entry_and_port(root, json.loads((root / "package.json").read_text())) == ("dist/index.js", 8080)
    assert br.analyze(root).app_port == 8080


def test_run_failures_name_the_real_error():
    node = "/app/index.js:6\nthrow new Error('boom at start');\n^\n\nError: boom at start\n    at Object.<anonymous> (/app/index.js:6:7)\n"
    d = diagnose(node, stage="run")
    assert d.code == "APP_START_ERROR" and "boom at start" in d.cause and "index.js:6" in d.cause
    db = diagnose("Error: connect ECONNREFUSED 127.0.0.1:5432\n    at TCPConnectWrap.afterConnect", stage="run")
    assert db.code == "APP_DB_LOCALHOST" and "PostgreSQL" in db.title and "DATABASE_URL" in db.fix
    py = diagnose('Traceback (most recent call last):\n  File "/app/main.py", line 3, in <module>\n    import x\nKeyError: \'SECRET\'', stage="run")
    assert py.code == "APP_START_ERROR" and "main.py:3" in py.cause
    #: 이미 아는 원인(모듈 없음)은 그 규칙이 먼저다.
    assert diagnose("Error: Cannot find module '/app/index.js'", stage="run").code == "NODE_MODULE_NOT_FOUND"


def test_subfolder_install_falls_back_to_cd_when_the_workdir_is_unknown():
    """WORKDIR 를 모르는 Dockerfile(변수·상대 경로)에서는 예전처럼 `RUN cd` 로 넣는다."""
    lines = ["FROM node:22-alpine AS deps", "WORKDIR $APP_HOME", "RUN npm ci"]
    assert br._stage_workdir(lines, 3) is None
    assert br._in_folder("api", "npm ci", None) == ["RUN cd api && npm ci"]
    assert br._stage_workdir(["FROM a", "WORKDIR /srv/", "FROM b AS c", "RUN x"], 4) is None, "단계가 바뀌면 WORKDIR 도 새로"
    assert br._in_folder("api", "npm ci", "/srv") == ["WORKDIR /srv/api", "RUN npm ci", "WORKDIR /srv"]


SEED = """await pool.query(`INSERT INTO products (name, image) VALUES
  ('Headphones', 'https://via.placeholder.com/300?text=Headphones'),
  ('Lamp', 'http://via.placeholder.com/150x150/09f/fff.png')`);
"""


def test_dead_placeholder_images_are_flagged_and_rewritten(tmp_path):
    """2026-10-04 실기기: 생성한 쇼핑몰 상품 이미지가 전부 깨졌다(via.placeholder.com 은 서비스 종료)."""
    assert br.dead_image_rewrite("src='https://via.placeholder.com/400'") == "src='https://placehold.co/400'"
    assert br.dead_image_rewrite("https://placeimg.com/640/480/tech") == "https://picsum.photos/640/480"
    assert br.dead_image_rewrite("https://placehold.co/400 https://example.com/placeholder.com/x") == \
        "https://placehold.co/400 https://example.com/placeholder.com/x", "살아 있는 주소·다른 도메인은 그대로"
    root = _write(tmp_path / "shop", {
        "package.json": json.dumps({"scripts": {"start": "node server.js"}, "dependencies": {"express": "^4"}}),
        "server.js": "require('express')().listen(process.env.PORT || 3000);",
        "db.js": SEED,
        "src/Card.jsx": "export default () => <img src={p.image || 'https://via.placeholder.com/400'} />;\n",
        "node_modules/x/index.js": "'https://via.placeholder.com/1'",
    })
    issue = next(i for i in br.analyze(root).issues if i.code == "APP_DEAD_IMAGE_HOST")
    assert issue.severity == "warning" and issue.auto_fix and "DB" in issue.fix
    assert "node_modules" not in issue.message
    br.apply_fix(root, "APP_DEAD_IMAGE_HOST")
    assert "https://placehold.co/300?text=Headphones" in (root / "db.js").read_text(encoding="utf-8")
    assert "https://placehold.co/150x150/09f/fff.png" in (root / "db.js").read_text(encoding="utf-8")
    assert "https://placehold.co/400" in (root / "src/Card.jsx").read_text(encoding="utf-8")
    assert "APP_DEAD_IMAGE_HOST" not in {i.code for i in br.analyze(root).issues}
    #: 배포 직전 자동 수정 대상이 아니다(경고) — 사용자가 누를 때만 바꾼다.
    assert "APP_DEAD_IMAGE_HOST" not in d._PRE_DEPLOY_WARNING_FIXES


def test_generated_code_gets_live_placeholder_images(tmp_path):
    import code_agent as ca
    ops = [{"action": "create", "file": "package.json", "content": json.dumps({"dependencies": {"express": "^4"}}), "language": "", "rationale": ""},
           {"action": "create", "file": "db.js", "content": SEED, "language": "", "rationale": ""}]
    fixed, notes = ca._autofix_ops(tmp_path, "", ops)
    db = next(o for o in fixed if o["file"] == "db.js")["content"]
    assert "via.placeholder.com" not in db and "placehold.co/300?text=Headphones" in db
    assert any("placehold.co" in n for n in notes)


@pytest.mark.skipif(not shutil.which("hadolint"), reason="hadolint 필요")
def test_rendered_templates_do_not_turn_the_security_gate_red(tmp_path):
    """ReCoder 가 만든 Dockerfile 이 게이트에서 "높음"(경고 중 권고가 아닌 것) 이나 오류를 내지 않는다."""
    import re as _re
    from api.routes.deploy import _HADOLINT_ADVISORY
    tpl = Path(__file__).resolve().parents[1] / "registry" / "file_templates"
    values = {"PORT": "3000", "APP_NAME": "app", "HEALTH_CHECK_PATH": "/health", "NODE_VERSION": "22",
              "START_SCRIPT": "server.js", "APP_TARGET": "main:app", "OUTPUT_DIR": "dist",
              "PYTHON_VERSION": "3.12"}
    for path in sorted(tpl.glob("Dockerfile.*")):
        text = _re.sub(r"{{\s*(\w+)\s*}}", lambda m: values[m.group(1)], path.read_text(encoding="utf-8"))
        target = tmp_path / path.name
        target.write_text(text, encoding="utf-8")
        out = subprocess.run(["hadolint", "--format", "json", "--no-fail", str(target)], capture_output=True, text=True)
        issues = json.loads(out.stdout or "[]")
        red = [(i["code"], i["line"]) for i in issues
               if i["level"] == "error" or (i["level"] == "warning" and i["code"] not in _HADOLINT_ADVISORY)]
        assert not red, (path.name, red)
