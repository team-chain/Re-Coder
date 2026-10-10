"""빠진 package.json 을 코드에서 만든다 — AI 가 workspaces 폴더(backend/·frontend/)의 package.json 을 빼먹은 경우.

실기기(2026-10-10 TEMP 쇼핑몰): 루트 package.json 은 workspaces: [backend, frontend] 인데 두 폴더에 package.json 이
없었다. express·pg·react 같은 의존성이 어디에도 없어 Docker 빌드의 `COPY backend/package*.json` · `npm ci` 가 깨지고,
pg 가 선언되지 않아 배포가 PostgreSQL 을 함께 띄우지 못하고 DATABASE_URL 을 직접 넣으라고 했다.

지어내지 않는다 — 코드가 실제로 불러오는 패키지만 넣고, 버전은 아래 표(검증된 안정 버전)를 쓴다.
표에 없는 패키지는 "latest" 로 두고 경고로 알린다(설치는 되지만 고정 버전이 아니다).
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Iterable, Mapping

#: 자주 쓰는 패키지의 안정 버전(주 버전 고정). 실제 생성 앱에서 나온 것 위주.
KNOWN_VERSIONS: dict[str, str] = {
    # 서버
    "express": "^4.21.2", "cors": "^2.8.5", "dotenv": "^16.4.5", "helmet": "^7.1.0", "morgan": "^1.10.0",
    "express-rate-limit": "^7.4.0", "express-validator": "^7.2.0", "cookie-parser": "^1.4.7", "compression": "^1.7.4",
    "jsonwebtoken": "^9.0.2", "bcryptjs": "^2.4.3", "bcrypt": "^5.1.1", "uuid": "^9.0.1", "zod": "^3.23.8",
    "pg": "^8.13.0", "mysql2": "^3.11.3", "mongoose": "^8.7.0", "mongodb": "^6.9.0", "redis": "^4.7.0", "ioredis": "^5.4.1",
    "better-sqlite3": "^11.3.0", "sqlite3": "^5.1.7", "knex": "^3.1.0", "sequelize": "^6.37.3", "@prisma/client": "^5.20.0",
    "stripe": "^14.25.0", "multer": "^1.4.5-lts.1", "nodemailer": "^6.9.15", "axios": "^1.7.7", "joi": "^17.13.3",
    "express-session": "^1.18.1", "connect-pg-simple": "^10.0.0", "winston": "^3.15.0", "pino": "^9.4.0",
    # 화면
    "react": "^18.3.1", "react-dom": "^18.3.1", "react-router-dom": "^6.27.0", "react-router": "^6.27.0",
    "zustand": "^4.5.5", "@tanstack/react-query": "^5.59.0", "react-hook-form": "^7.53.0", "clsx": "^2.1.1",
    "@stripe/stripe-js": "^4.8.0", "@stripe/react-stripe-js": "^2.8.1", "vue": "^3.5.12", "vue-router": "^4.4.5",
    "pinia": "^2.2.4",
}
#: 개발 도구(빌드·실행에만 필요)
DEV_VERSIONS: dict[str, str] = {
    "typescript": "^5.6.3", "tsx": "^4.19.1", "vite": "^5.4.9", "@vitejs/plugin-react": "^4.3.3",
    "@vitejs/plugin-vue": "^5.1.4", "@types/node": "^20.16.11", "@types/express": "^4.17.21", "@types/cors": "^2.8.17",
    "@types/jsonwebtoken": "^9.0.7", "@types/bcryptjs": "^2.4.6", "@types/bcrypt": "^5.0.2", "@types/pg": "^8.11.10",
    "@types/uuid": "^9.0.8", "@types/react": "^18.3.11", "@types/react-dom": "^18.3.1", "@types/cookie-parser": "^1.4.7",
    "@types/multer": "^1.4.12", "@types/nodemailer": "^6.4.16", "@types/morgan": "^1.9.9", "@types/compression": "^1.7.5",
    "@types/express-session": "^1.18.0", "@types/better-sqlite3": "^7.6.11",
}
#: 자기 타입을 함께 주는 패키지 — @types 를 따로 넣지 않는다
_TYPED = {"express-rate-limit", "helmet", "zod", "axios", "stripe", "mysql2", "mongoose", "mongodb", "redis", "ioredis",
          "zustand", "@tanstack/react-query", "react-hook-form", "clsx", "react-router-dom", "react-router", "vue",
          "vue-router", "pinia", "@stripe/stripe-js", "@stripe/react-stripe-js", "dotenv", "knex", "sequelize",
          "@prisma/client", "winston", "pino", "express-validator", "joi"}
_NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram", "dns",
    "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
}
_IMPORTS = (
    re.compile(r"""\bimport\s+(?:type\s+)?(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+[\w*{}\s,]+\s+from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
)
_CODE = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue")


def package_of(spec: str) -> str | None:
    spec = (spec or "").strip()
    if not spec or spec.startswith((".", "/", "~", "#", "@/", "node:", "http:", "https:", "data:", "virtual:")):
        return None
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 and parts[1] else None
    name = spec.split("/")[0]
    return None if name in _NODE_BUILTINS else name


def imported_packages(files: Mapping[str, str]) -> set[str]:
    found: set[str] = set()
    for path, text in files.items():
        if not path.lower().endswith(_CODE) or not isinstance(text, str):
            continue
        body = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        for pattern in _IMPORTS:
            for spec in pattern.findall(body):
                pkg = package_of(spec)
                if pkg:
                    found.add(pkg)
    return found


def _rel(folder: str, path: str) -> str:
    return path[len(folder) + 1:] if folder and path.startswith(folder + "/") else path


def infer_package_json(folder: str, files: Mapping[str, str], root_package: dict | None = None) -> tuple[str, list[str]]:
    """folder 의 코드에서 package.json 을 만든다 → (JSON 문자열, 버전을 모르는 패키지 목록).

    files: 이 폴더 안 파일(프로젝트 루트 기준 경로) → 내용.
    """
    root_package = root_package if isinstance(root_package, dict) else {}
    inside = {p: t for p, t in files.items() if p.startswith(folder + "/")}
    rels = {_rel(folder, p): t for p, t in inside.items()}
    pkgs = imported_packages(inside)
    ts = any(p.endswith((".ts", ".tsx")) and not p.endswith(".d.ts") for p in rels)
    vite = any(re.match(r"vite\.config\.(js|ts|mjs|cjs)$", p) for p in rels) or "vite" in pkgs
    vue = "vue" in pkgs
    deps: dict[str, str] = {}
    dev: dict[str, str] = {}
    unknown: list[str] = []
    build_only = {"vite", "@vitejs/plugin-react", "@vitejs/plugin-vue", "typescript", "tsx"}
    for pkg in sorted(pkgs):
        if pkg in build_only:
            dev[pkg] = DEV_VERSIONS[pkg]
        elif pkg.startswith("@types/"):
            dev[pkg] = DEV_VERSIONS.get(pkg, "latest")
        else:
            deps[pkg] = KNOWN_VERSIONS.get(pkg, "latest")
            if pkg not in KNOWN_VERSIONS:
                unknown.append(pkg)
    scripts: dict[str, str] = {}
    entry_src = next((e for e in ("src/server.ts", "src/index.ts", "src/app.ts", "server.ts", "index.ts",
                                  "src/server.js", "src/index.js", "server.js", "index.js", "app.js") if e in rels), "")
    if vite:
        dev.setdefault("vite", DEV_VERSIONS["vite"])
        if "react" in deps:
            dev.setdefault("@vitejs/plugin-react", DEV_VERSIONS["@vitejs/plugin-react"])
        if vue:
            dev.setdefault("@vitejs/plugin-vue", DEV_VERSIONS["@vitejs/plugin-vue"])
        #: 타입 검사(tsc)는 빌드를 막지 않게 vite build 만 — AI 가 쓴 엄격한 tsconfig 의 미사용 변수 등으로 배포가 깨지지 않게
        scripts.update({"dev": "vite", "build": "vite build", "preview": "vite preview"})
        config = next((t for p, t in rels.items() if re.match(r"vite\.config\.[cm]?[jt]s$", p)), "")
        if re.search(r"""\bminify\s*:\s*['"]terser['"]""", config or ""):
            dev.setdefault("terser", "^5.36.0")  # vite 5 는 terser 를 함께 설치하지 않는다
    elif entry_src:
        if ts:
            out = re.sub(r"^src/", "", entry_src)[:-3] + ".js"
            outdir = _ts_outdir(rels.get("tsconfig.json"))
            scripts.update({"build": "tsc", "start": f"node {posixpath.join(outdir, out)}",
                            "dev": f"tsx watch {entry_src}"})
        else:
            scripts.update({"start": f"node {entry_src}"})
    if ts:
        dev.setdefault("typescript", DEV_VERSIONS["typescript"])
        if not vite:
            dev.setdefault("tsx", DEV_VERSIONS["tsx"])
            dev.setdefault("@types/node", DEV_VERSIONS["@types/node"])
        for pkg in list(deps):
            types = f"@types/{pkg.replace('@', '').replace('/', '__')}" if pkg.startswith("@") else f"@types/{pkg}"
            if pkg not in _TYPED and types in DEV_VERSIONS:
                dev.setdefault(types, DEV_VERSIONS[types])
    esm = root_package.get("type") == "module" or any(re.search(r"^\s*(import|export)\s", t or "", re.M)
                                                       for p, t in rels.items() if p.endswith((".js", ".mjs")))
    name = re.sub(r"[^a-z0-9._-]+", "-", f"{root_package.get('name') or 'app'}-{posixpath.basename(folder)}".lower()).strip("-")
    package: dict = {"name": name, "version": "1.0.0", "private": True}
    if esm or ts:
        package["type"] = "module"
    if scripts:
        package["scripts"] = scripts
    if deps:
        package["dependencies"] = dict(sorted(deps.items()))
    if dev:
        package["devDependencies"] = dict(sorted(dev.items()))
    return json.dumps(package, ensure_ascii=False, indent=2) + "\n", unknown


def _ts_outdir(tsconfig: str | None) -> str:
    try:
        cfg = json.loads(re.sub(r"//[^\n]*", "", tsconfig or "{}"))
        out = str(((cfg or {}).get("compilerOptions") or {}).get("outDir") or "dist")
    except ValueError:
        out = "dist"
    return re.sub(r"^\./", "", out).rstrip("/") or "dist"


def workspace_folders(root_package: dict, listing: Iterable[str]) -> list[str]:
    """루트 package.json 의 workspaces 가 가리키는, 실제로 파일이 있는 폴더."""
    spec = root_package.get("workspaces") if isinstance(root_package, dict) else None
    if isinstance(spec, dict):
        spec = spec.get("packages")
    patterns = [str(p).strip().strip("/") for p in (spec or []) if isinstance(p, str)]
    files = list(listing)
    out: list[str] = []
    for pat in patterns:
        if pat.endswith("/*"):
            base = pat[:-2]
            subs = sorted({f[len(base) + 1:].split("/")[0] for f in files if f.startswith(base + "/") and f.count("/") > base.count("/") + 1})
            out += [f"{base}/{s}" for s in subs]
        elif "*" not in pat:
            out.append(pat)
    return [f for f in dict.fromkeys(out) if any(p.startswith(f + "/") for p in files)]
