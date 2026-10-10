"""ReCoder 가 직접 쓰는 Dockerfile — 화면·서버를 폴더로 나눈 Node 앱(backend/ + frontend/ 등).

왜 필요한가
    AI 가 쓴 Dockerfile 이 매번 다른 방식으로 깨졌다(실기기 2026-10-10 TEMP):
      · lock 없이 `npm ci`, 빌드 단계에서 `--omit=dev` → `tsc: not found`
      · 실행 단계에 backend/package.json 을 복사하지 않고 `npm start --workspace=backend` → "No workspaces found"
      · 실행 단계에 서버 의존성 없음, 화면 빌드 결과 경로 불일치
      · 보안 검사 권고(DL3018·DL3059·DL3066·DL3025)
    구조를 알 수 있으면 ReCoder 가 검증한 한 가지 방식으로 쓴다(결제 모듈과 같은 "고정 파일").

방식
    build 단계: 저장소 전체를 복사 → 화면 폴더 설치·빌드 → 서버 폴더 설치·빌드 → 서버 개발 의존성 정리(prune).
                폴더마다 그 폴더에서 설치한다(--workspaces=false — 루트 workspaces 와 섞이지 않게).
    runtime 단계: OS 보안 패치, npm 제거(앱은 node 로 바로 뜸), 서버 폴더(운영 의존성 포함)와 화면 빌드 결과만,
                숫자 사용자(node=1000), HEALTHCHECK·CMD 는 JSON 형식.
    구조를 확신할 수 없으면 None — 그때는 기존 점검·자동 수정이 맡는다.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Mapping, Optional

SERVER_DIRS = ("backend", "server", "api")
UI_DIRS = ("frontend", "client", "web")
NODE_IMAGE = "node:22-alpine"
MARKER = "# ReCoder 검증 Dockerfile"


def _pkg(files, rel: str) -> Optional[dict]:
    try:
        data = json.loads(files.read(rel) or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _deps(pkg: dict) -> dict:
    out: dict = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            out.update(pkg[key])
    return out


def _start_entry(files, folder: str, pkg: dict) -> Optional[str]:
    """서버 폴더 기준 시작 파일(node 로 실행). 확실하지 않으면 None."""
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    for name in ("start", "start:prod", "serve"):
        cmd = str(scripts.get(name) or "")
        m = re.match(r"^\s*(?:NODE_ENV=\S+\s+)?node\s+(?:(?:-r|--require|--import|--loader)\s+\S+\s+|--[\w-]+(?:=\S+)?\s+)*([\w./-]+\.(?:c|m)?js)\s*$", cmd)
        if m:
            return re.sub(r"^\./", "", m.group(1))
    main = pkg.get("main")
    if isinstance(main, str) and main.endswith((".js", ".mjs", ".cjs")):
        return re.sub(r"^\./", "", main)
    return None


def _ui_out(files, folder: str, pkg: dict) -> Optional[str]:
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    build = str(scripts.get("build") or "")
    if not build:
        return None
    if re.search(r"\bvite\b", build) or "vite" in _deps(pkg):
        for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts"):
            text = files.read(f"{folder}/{name}") or ""
            m = re.search(r"""\boutDir\s*:\s*['"]([^'"]+)['"]""", text)
            if m:
                out = posixpath.normpath(posixpath.join(folder, m.group(1)))
                return None if out.startswith("..") or not out.startswith(folder + "/") else out
        return f"{folder}/dist"
    if re.search(r"react-scripts\s+build", build):
        return f"{folder}/build"
    return None


def layout(files) -> Optional[dict]:
    """{server, entry, ui, ui_out, server_build, port, health} — 확신할 수 있을 때만."""
    listing = files.files()
    server = next((d for d in SERVER_DIRS if files.exists(f"{d}/package.json")), None)
    if not server:
        return None
    spkg = _pkg(files, f"{server}/package.json")
    if spkg is None:
        return None
    if any(k in _deps(spkg) for k in ("next", "nuxt", "@nestjs/cli", "@remix-run/node")):
        return None  # 프레임워크 전용 실행 방식 — 기존 템플릿이 맡는다
    entry = _start_entry(files, server, spkg)
    if not entry:
        return None
    scripts = spkg.get("scripts") if isinstance(spkg.get("scripts"), dict) else {}
    server_build = bool(scripts.get("build"))
    #: 빌드 결과(dist/…)를 실행하면 build 가 있어야 하고, 그 외에는 파일이 실제로 있어야 한다
    built = re.match(r"^(dist|build|out|lib)/", entry)
    if built and not server_build:
        return None
    if not built and not files.exists(f"{server}/{entry}"):
        return None
    ui = next((d for d in UI_DIRS if files.exists(f"{d}/package.json")), None)
    ui_out = None
    if ui:
        upkg = _pkg(files, f"{ui}/package.json")
        ui_out = _ui_out(files, ui, upkg) if upkg else None
        if not ui_out:
            return None  # 화면 폴더가 있는데 빌드 방식을 모른다 — 추측하지 않는다
    try:
        from build_readiness import analyze
        r = analyze(files.root, files.overlay, dockerfile=None)
        port = r.app_port or 3000
        health = r.probe_path() or "/"
    except Exception:  # noqa: BLE001
        port, health = 3000, "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]*", health or "/"):
        health = "/"
    root_pkg = _pkg(files, "package.json") or {}
    #: 서버가 화면 폴더를 작업 폴더 기준 경로('frontend/dist', process.cwd())로 찾으면 프로젝트 루트에서 실행해야 한다
    #: (실기기 TEMP 2.0.6: express.static('frontend/dist') — 서버 폴더에서 실행하면 /app/backend/frontend/dist 를 찾는다).
    cwd_root = False
    if ui_out:
        lit = re.compile(rf"""['"`](?:\./)?{re.escape(ui_out)}(?:/[^'"`]*)?['"`]|process\.cwd\(\)\s*,\s*['"`](?:\./)?{re.escape(ui.rstrip('/'))}\b""")
        cwd_root = any(lit.search(files.read(p) or "") for p in listing
                       if p.startswith(server + "/") and p.endswith((".js", ".ts", ".mjs", ".cjs"))
                       and "/node_modules/" not in f"/{p}")
    prisma = prisma_schema(files, server, spkg)
    return {"server": server, "entry": entry, "ui": ui, "ui_out": ui_out, "server_build": server_build, "cwd_root": cwd_root,
            "prisma": prisma,
            "port": int(port), "health": health, "workspaces": bool(root_pkg.get("workspaces")),
            "server_lock": files.exists(f"{server}/package-lock.json"),
            "ui_lock": bool(ui) and files.exists(f"{ui}/package-lock.json")}


def prisma_schema(files, server: str, spkg: dict) -> Optional[str]:
    """서버가 Prisma 를 쓰면 스키마 파일(서버 폴더 기준 경로). 아니면 None.

    실기기(TEMP 2.0.8 점검): Prisma 앱은 `prisma generate` 없이 tsc 를 돌려 서버 빌드가 수십 건(`@prisma/client` 에
    Order·Payment 없음, tx 가 any)으로 실패했고, 실행 이미지에는 Prisma 엔진이 쓰는 openssl 도, 테이블도 없었다.
    """
    if "@prisma/client" not in _deps(spkg):
        return None
    conf = spkg.get("prisma") if isinstance(spkg.get("prisma"), dict) else {}
    cand = [str(conf.get("schema") or ""), "prisma/schema.prisma", "schema.prisma", "src/prisma/schema.prisma"]
    for rel in cand:
        rel = re.sub(r"^\./", "", rel.replace("\\", "/"))
        if rel and not rel.startswith("..") and files.exists(f"{server}/{rel}"):
            return rel
    if files.dir_has_files(f"{server}/prisma/schema"):
        return "prisma/schema"  # 여러 파일 스키마(prismaSchemaFolder)
    return None


#: 실행 직전에 Prisma 스키마의 테이블을 (없는 것만) 만들고 앱을 시작한다 — 빈 DB 에서 `relation does not exist` 로
#: 죽지 않게. SQL 은 빌드 단계에서 `prisma migrate diff --from-empty` 로 스키마에서 그대로 뽑는다(AI 가 쓴 migration.sql
#: 은 SQLite 문법이 섞여 있었다). 이미 있는 테이블·인덱스·제약은 건너뛴다(다시 배포해도 데이터 그대로).
PRISMA_START = r"""'use strict';
// ReCoder: Prisma 스키마의 테이블을 없는 것만 만든 뒤 앱을 시작합니다(빈 DB 에서 바로 뜨게).
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const entry = process.argv[2];
const sqlFile = path.join(__dirname, 'prisma', 'recoder-schema.sql');
async function ensureSchema() {
  if (!process.env.DATABASE_URL || !fs.existsSync(sqlFile)) return;
  let client;
  try {
    const { PrismaClient } = require('@prisma/client');
    client = new PrismaClient();
    for (let i = 0; ; i++) {
      try { await client.$connect(); break; } catch (e) {
        if (i >= 14) throw e;
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    const sql = fs.readFileSync(sqlFile, 'utf8').replace(/^\s*--.*$/gm, '');
    let applied = 0;
    for (const stmt of sql.split(/;\s*(?:\r?\n|$)/).map((s) => s.trim()).filter(Boolean)) {
      try { await client.$executeRawUnsafe(stmt); applied++; } catch (e) {
        const msg = String((e && e.message) || e);
        if (!/already exists|duplicate/i.test(msg)) console.warn('[recoder] schema statement skipped:', msg.split('\n').pop());
      }
    }
    if (applied) console.log('[recoder] database schema: ' + applied + ' statement(s) applied');
  } catch (e) {
    console.warn('[recoder] database schema check skipped:', (e && e.message) || e);
  } finally {
    if (client) await client.$disconnect().catch(() => {});
  }
}
ensureSchema().then(() => import(pathToFileURL(path.resolve(entry)).href)).catch((e) => { console.error(e); process.exit(1); });
"""
PRISMA_START_FILE = "recoder-start.cjs"


def _install(lock: bool) -> str:
    #: lock 이 있으면 그대로(npm ci), 없으면 설치. 폴더 단위로(루트 workspaces 와 섞이지 않게).
    return ("npm ci --no-audit --no-fund --workspaces=false" if lock else
            "npm install --no-audit --no-fund --workspaces=false")


def render(info: dict) -> str:
    server, ui, entry = info["server"], info.get("ui"), info["entry"]
    port, health = info["port"], info["health"]
    prisma = info.get("prisma")
    os_note = "# ReCoder: OS 패키지는 기본 이미지 태그가 버전을 정합니다(저장소가 옛 버전을 지우면 고정한 버전 때문에 빌드가 깨짐)"
    lines = [
        f"{MARKER} — 화면·서버 폴더형 Node 앱(서버 {server}/" + (f", 화면 {ui}/" if ui else "") + ").",
        "# 빌드 단계는 개발 도구까지 설치해 빌드하고, 실행 이미지에는 서버 운영 의존성과 화면 빌드 결과만 넣는다.",
        f"FROM {NODE_IMAGE} AS build",
        "WORKDIR /app",
    ]
    if prisma:
        lines += ["# Prisma 엔진은 openssl 로 플랫폼을 고른다(없으면 엔진을 잘못 받아 실행 중에 죽는다).",
                  os_note, "# hadolint ignore=DL3018", "RUN apk add --no-cache openssl"]
    lines += ["COPY . ."]
    verify = bool(info.get("verify"))

    def build_step(folder: str, install: str, extra_before: list[str], build: bool) -> list[str]:
        if not verify:
            steps = [install] + extra_before + (["npm run build --workspaces=false"] if build else [])
            return ["RUN " + " \\\n    && ".join(steps)]
        #: 생성 검증용 — 한 폴더의 빌드가 실패해도 다음 폴더까지 빌드해 오류를 한 번에 모은다(서버 오류가 화면 오류 뒤에
        #: 숨어 있다가 화면을 고치자 수십 건이 새로 나타났다). 경로는 저장소 기준으로 바꿔 찍는다.
        steps = [install] + extra_before + (["npm run build --workspaces=false"] if build else [])
        tag = folder.replace("/", "-")
        return [f"RUN ( {' && '.join(steps)} ) > /tmp/recoder-build-{tag}.log 2>&1; "
                f"echo $? > /tmp/recoder-build-{tag}.rc; "
                f"sed -E 's#^(src|app|lib|server|pages|components)/#{folder}/\\1/#' /tmp/recoder-build-{tag}.log > /tmp/recoder-build-{tag}.out"]

    if ui:
        lines += [f"WORKDIR /app/{ui}"] + build_step(ui, _install(info.get("ui_lock", False)), [], True)
    prisma_steps: list[str] = []
    if prisma:
        schema = prisma
        prisma_steps = [f"npx prisma generate --schema {schema}",
                        #: Prisma 7 은 --to-schema, 그 전은 --to-schema-datamodel. 표를 못 뽑아도 빌드는 막지 않는다
                        #: (시작 도우미가 건너뛰고, 표가 없으면 배포 진단이 알려 준다).
                        f"{{ npx prisma migrate diff --from-empty --to-schema {schema} --script > prisma/recoder-schema.sql 2>/dev/null"
                        f" || npx prisma migrate diff --from-empty --to-schema-datamodel {schema} --script > prisma/recoder-schema.sql"
                        f" || rm -f prisma/recoder-schema.sql; }}",
                        f"node -e \"require('fs').writeFileSync('{PRISMA_START_FILE}', Buffer.from('{_b64(PRISMA_START)}', 'base64'))\""]
        if not schema.startswith("prisma/"):
            prisma_steps.insert(1, "mkdir -p prisma")
    lines += [f"WORKDIR /app/{server}"]
    if verify:
        lines += build_step(server, _install(info.get("server_lock", False)), prisma_steps, bool(info.get("server_build")))
        #: 실패한 폴더의 빌드 출력은 이 단계가 찍는다 — 앞 단계가 캐시되면 그 출력이 로그에 다시 나오지 않는다.
        lines += ["RUN ok=1; for f in /tmp/recoder-build-*.rc; do [ \"$(cat \"$f\")\" = 0 ] || { ok=0; cat \"${f%.rc}.out\"; }; done; "
                  "[ \"$ok\" = 1 ] && npm prune --omit=dev --no-audit --no-fund --workspaces=false"]
    else:
        server_steps = [_install(info.get("server_lock", False))] + prisma_steps
        if info.get("server_build"):
            server_steps.append("npm run build --workspaces=false")
        server_steps.append("npm prune --omit=dev --no-audit --no-fund --workspaces=false")
        lines += ["RUN " + " \\\n    && ".join(server_steps)]
    runtime_os = ["RUN apk upgrade --no-cache \\"]
    if prisma:
        runtime_os = [os_note, "# hadolint ignore=DL3018", "RUN apk upgrade --no-cache \\", "    && apk add --no-cache openssl \\"]
    lines += ["",
              f"FROM {NODE_IMAGE}",
              "# OS 보안 패치를 적용하고, 앱은 node 로 바로 뜨므로 이미지에 번들된 npm·yarn(취약한 의존성 동봉)은 지운다.",
              *runtime_os,
              "    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx \\",
              "       /usr/local/bin/corepack /opt/yarn* /usr/local/bin/yarn /usr/local/bin/yarnpkg",
              #: 운영 모드로 실행(앱이 NODE_ENV=production 일 때만 빌드한 화면을 절대 경로로 내보내는 경우가 흔하다 —
              #: 실기기 카페: 비워 두자 개발용 상대 경로로 sendFile 해 첫 화면이 500). 데모 배포는 실행할 때 test 로 덮는다.
              f"ENV NODE_ENV=production PORT={port}",
              f"WORKDIR /app{'' if info.get('cwd_root') else '/' + server}",
              f"COPY --from=build --chown=1000:1000 /app/{server} /app/{server}"]
    if ui:
        lines.append(f"COPY --from=build --chown=1000:1000 /app/{info['ui_out']} /app/{info['ui_out']}")
    probe = ("require('http').get('http://127.0.0.1:'+(process.env.PORT||" + str(port) + ")+'" + health
             + "',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))")
    lines += ["# 공식 node 이미지의 node 사용자(숫자 ID)",
              "USER 1000",
              f"EXPOSE {port}",
              "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\",
              "  CMD " + json.dumps(["node", "-e", probe], ensure_ascii=False),
              "CMD " + json.dumps(_cmd(info))]
    return "\n".join(lines) + "\n"


def _cmd(info: dict) -> list[str]:
    server, entry = info["server"], info["entry"]
    target = f"{server}/{entry}" if info.get("cwd_root") else entry
    if info.get("prisma"):
        start = f"{server}/{PRISMA_START_FILE}" if info.get("cwd_root") else PRISMA_START_FILE
        return ["node", start, target]
    return ["node", target]


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


DOCKERIGNORE = """# ReCoder: Docker 빌드에서 제외할 파일
**/node_modules
**/dist
**/build
.git
.recoder
.vscode
coverage
*.log
.env
.env.*
!.env.example
Dockerfile*
.dockerignore
"""


def for_workspace(workspace, overlay: Optional[Mapping[str, Optional[str]]] = None) -> Optional[str]:
    """프로젝트(와 아직 쓰지 않은 파일)에서 ReCoder Dockerfile 내용. 구조를 확신할 수 없으면 None."""
    from pathlib import Path

    from build_readiness import ProjectFiles
    info = layout(ProjectFiles(Path(workspace), overlay))
    return render(info) if info else None


def is_recoder(text: Optional[str]) -> bool:
    return bool(text) and MARKER in (text or "")


def runtime_breaks(dockerfile: str, info: dict) -> list[str]:
    """AI 가 쓴 Dockerfile 의 실행 단계가 이 구조에서 확실히 실패하는 이유(사람이 읽을 문장). 없으면 빈 목록.

    실기기(TEMP 2.0.6): 실행 단계가 루트 package.json 과 backend/dist 만 복사하고 `npm start --workspace=backend`
    → "No workspaces found" 로 컨테이너가 계속 재시작. 서버 의존성(express 등)도 설치되지 않았다.
    """
    text = re.sub(r"\\\r?\n", " ", dockerfile or "")
    stages = re.split(r"(?im)^\s*FROM\s", text)
    final = stages[-1] if len(stages) > 1 else ""
    server = info["server"]
    reasons: list[str] = []
    cmd = " ".join(re.findall(r"(?im)^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", final))
    copies = re.findall(r"(?im)^\s*COPY\s+(.+)$", final)
    copied_all = any(re.match(r"(?:--[\w-]+=\S+\s+)*\.\s+\.?/?\s*$", c.strip()) for c in copies)

    def copies_server(what: str) -> bool:
        if copied_all:
            return True
        for c in copies:
            srcs = [s for s in c.split() if not s.startswith("--")][:-1]
            for src in srcs:
                src = re.sub(r"^/app/", "", src).strip("/")
                if src in (server, f"{server}/{what}") or (what == "package.json" and re.fullmatch(rf"{re.escape(server)}/package\*?\.json\*?", src)):
                    return True
        return False

    workspace_cmd = re.search(rf"""(?:--workspace[= ]|-w\s+|--prefix\s+)['"]?{re.escape(server)}\b""", cmd)
    if re.search(r"\bnpm\b", cmd):
        if re.search(r"rm\s+-rf[^\n]*?/usr/local/(?:lib/node_modules/npm|bin/npm)", final):
            reasons.append("실행 명령이 npm 인데 실행 단계에서 npm 을 지웠습니다")
        if workspace_cmd and not copies_server("package.json"):
            reasons.append(f"`npm … {server}` 로 시작하는데 실행 단계에 {server}/package.json 이 없습니다(No workspaces found)")
    if workspace_cmd or re.search(rf"""['"]?(?:\./)?{re.escape(server)}/""", cmd):
        installs_here = re.search(rf"(?im)^\s*RUN\b[^\n]*\b(?:npm|pnpm|yarn)\s+(?:ci|install|i)\b", final)
        def whole_server_from_stage(c: str) -> bool:
            parts = c.split()
            srcs = [p for p in parts if not p.startswith("--")][:-1]
            return any(p.startswith("--from") for p in parts) and any(
                re.sub(r"^/app/", "", s).strip("/") == server for s in srcs)
        node_modules_copied = any(re.search(r"node_modules", c) for c in copies) or copied_all \
            or any(whole_server_from_stage(c) for c in copies)
        if not node_modules_copied and not (installs_here and copies_server("package.json")):
            reasons.append(f"실행 단계에 {server}/ 의 운영 의존성(express 등)을 설치하거나 복사하지 않습니다(Cannot find module)")
    return reasons
