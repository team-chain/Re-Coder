"""아키텍처 계약 → 인프라 파일(3.1) — docker-compose.yml · 서비스별 Dockerfile · .env.example · 통합 테스트 정의.

AI 가 쓴 인프라 파일은 매번 다른 방식으로 깨졌다(docker_kit 참고). 서비스·DB 가 여럿이면 더 그렇다 — 이름·포트·접속 변수가
파일마다 어긋난다. 그래서 계약(architecture.normalize 결과)에서 **결정적으로** 만든다. 같은 계약이면 언제나 같은 글자다.

원칙
    · 비밀값은 파일에 쓰지 않는다. compose 는 `${X_PASSWORD:?...}` 로 .env 에서 받고, 없으면 시작하지 않는다.
    · DB 는 서비스마다 자기 볼륨을 갖고, healthcheck 가 통과해야 그 DB 를 쓰는 서비스가 시작한다.
    · 앱 컨테이너는 root 가 아닌 사용자로 돈다. 사용자가 직접 접속하는 서비스(public)만 포트를 연다.
    · 검증용 설정(verify_override)은 배포 파일과 따로다 — 모든 서비스 포트를 127.0.0.1 의 임의 포트로만 연다.
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

import yaml

try:
    import architecture as arch_mod
except ImportError:  # pragma: no cover - 패키지 실행
    from core import architecture as arch_mod  # type: ignore

MARKER = "# ReCoder 검증 compose"
DOCKER_MARKER = "# ReCoder 검증 Dockerfile"
NODE_IMAGE = "node:22-alpine"
PYTHON_IMAGE = "python:3.12-slim"
WEB_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"
DB_IMAGES = {"postgres": "postgres:16-alpine", "mysql": "mysql:8.4", "mongodb": "mongo:7", "redis": "redis:7-alpine"}
DB_USER = "app"


def db_name(name: str) -> str:
    """DB 서비스 이름 → 데이터베이스 이름(orders-db → orders_db)."""
    return arch_mod.env_name(name).lower()


def db_url(db: dict, password: str) -> str:
    host, name, engine = db["name"], db_name(db["name"]), db["engine"]
    port = arch_mod.ENGINES[engine]["port"]
    if engine == "postgres":
        return f"postgres://{DB_USER}:{password}@{host}:{port}/{name}"
    if engine == "mysql":
        return f"mysql://{DB_USER}:{password}@{host}:{port}/{name}"
    if engine == "mongodb":
        return f"mongodb://{DB_USER}:{password}@{host}:{port}/{name}?authSource=admin"
    return f"redis://:{password}@{host}:{port}/0"


def _required(var: str) -> str:
    return "${" + var + ":?set " + var + " in .env}"


def _db_service(db: dict) -> dict:
    pw = _required(arch_mod.password_var(db["name"]))
    name = db_name(db["name"])
    engine = db["engine"]
    base: dict = {"image": DB_IMAGES[engine], "restart": "unless-stopped",
                  "volumes": [f"{db['name']}-data:" + {"postgres": "/var/lib/postgresql/data", "mysql": "/var/lib/mysql",
                                                         "mongodb": "/data/db", "redis": "/data"}[engine]]}
    if engine == "postgres":
        base["environment"] = {"POSTGRES_DB": name, "POSTGRES_USER": DB_USER, "POSTGRES_PASSWORD": pw}
        base["healthcheck"] = {"test": ["CMD-SHELL", f"pg_isready -U {DB_USER} -d {name}"]}
    elif engine == "mysql":
        root = _required(arch_mod.env_name(db["name"]) + "_ROOT_PASSWORD")
        base["environment"] = {"MYSQL_DATABASE": name, "MYSQL_USER": DB_USER, "MYSQL_PASSWORD": pw, "MYSQL_ROOT_PASSWORD": root}
        base["healthcheck"] = {"test": ["CMD-SHELL", f"mysqladmin ping -h 127.0.0.1 -u {DB_USER} --password=$$MYSQL_PASSWORD --silent"]}
    elif engine == "mongodb":
        base["environment"] = {"MONGO_INITDB_ROOT_USERNAME": DB_USER, "MONGO_INITDB_ROOT_PASSWORD": pw, "MONGO_INITDB_DATABASE": name}
        base["healthcheck"] = {"test": ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]}
    else:
        base["environment"] = {"REDIS_PASSWORD": pw}
        base["command"] = ["sh", "-c", 'exec redis-server --appendonly yes --requirepass "$$REDIS_PASSWORD"']
        base["healthcheck"] = {"test": ["CMD-SHELL", "REDISCLI_AUTH=$$REDIS_PASSWORD redis-cli ping | grep -q PONG"]}
    base["healthcheck"].update({"interval": "5s", "timeout": "5s", "retries": 30, "start_period": "10s"})
    return base


def _health_test(svc: dict) -> Optional[list[str]]:
    if svc["kind"] == "web":
        return ["CMD", "wget", "-q", "--spider", f"http://127.0.0.1:{svc['port']}/"]
    if svc["kind"] != "api":
        return None
    if svc["language"] == "node":
        return ["CMD", "node", "-e",
                f"fetch('http://127.0.0.1:{svc['port']}{svc['health']}').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
    return ["CMD", "python", "-c",
            f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{svc['port']}{svc['health']}', timeout=4)"]


def _needs_jwt(arch: dict, svc: dict) -> bool:
    if svc["kind"] != "api":
        return False
    authed = {e["service"] for e in arch.get("endpoints") or [] if e["auth"]}
    return bool(authed) and (svc["name"] in authed or any(d in authed for d in svc["depends_on"])
                             or any(svc["name"] in s["depends_on"] for s in arch["services"] if s["name"] in authed))


def _app_service(arch: dict, svc: dict) -> dict:
    out: dict = {"build": {"context": f"./{svc['path']}"}, "restart": "unless-stopped"}
    env: dict = {}
    if svc["port"] and svc["kind"] != "web":
        env["PORT"] = str(svc["port"])
    if svc["language"] == "node" and svc["kind"] != "web":
        env["NODE_ENV"] = "production"
    for name in svc["databases"]:
        db = arch_mod.database(arch, name)
        env[arch_mod.db_url_var(name)] = db_url(db, "${" + arch_mod.password_var(name) + "}")
    for dep in svc["depends_on"]:
        target = arch_mod.service(arch, dep)
        env[arch_mod.service_url_var(dep)] = f"http://{dep}:{target['port']}"
    if _needs_jwt(arch, svc):
        env["JWT_SECRET"] = _required("JWT_SECRET")
    if env:
        out["environment"] = env
    depends: dict = {}
    for name in svc["databases"]:
        depends[name] = {"condition": "service_healthy"}
    for dep in svc["depends_on"]:
        target = arch_mod.service(arch, dep)
        depends[dep] = {"condition": "service_healthy" if _health_test(target) else "service_started"}
    if depends:
        out["depends_on"] = depends
    test = _health_test(svc)
    if test:
        out["healthcheck"] = {"test": test, "interval": "5s", "timeout": "5s", "retries": 30, "start_period": "20s"}
    if svc["public"] and svc["port"]:
        out["ports"] = [f"{svc['port']}:{svc['port']}"]
    return out


def render_compose(arch: dict) -> str:
    """계약 → docker-compose.yml(배포·로컬 실행용)."""
    services: dict = {}
    for db in arch["databases"]:
        services[db["name"]] = _db_service(db)
    for svc in arch["services"]:
        services[svc["name"]] = _app_service(arch, svc)
    doc = {"name": "app", "services": services}
    if arch["databases"]:
        doc["volumes"] = {f"{db['name']}-data": {} for db in arch["databases"]}
    head = (f"{MARKER} — architecture.json 계약에서 만들었습니다. 서비스·DB 를 바꾸려면 계약을 고친 뒤 다시 만드세요.\n"
            "# 비밀번호는 .env 에 넣습니다(.env.example 참고). .env 는 저장소에 올리지 마세요.\n")
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200)


def _install() -> str:
    return "RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi"


def render_dockerfile(svc: dict) -> str:
    head = f"{DOCKER_MARKER} — {svc['name']} ({svc['kind']}, {svc['language']})\n"
    if svc["kind"] == "web":
        return head + "\n".join([
            f"FROM {NODE_IMAGE} AS build", "WORKDIR /app", "COPY package*.json ./", _install(), "COPY . .", "RUN npm run build", "",
            f"FROM {WEB_IMAGE}", "COPY nginx.conf /etc/nginx/conf.d/default.conf",
            f"COPY --from=build /app/{svc['output']} /usr/share/nginx/html", f"EXPOSE {svc['port']}", ""])
    if svc["language"] == "node":
        lines = [f"FROM {NODE_IMAGE} AS build", "WORKDIR /app", "COPY package*.json ./", _install(), "COPY . .",
                 "RUN npm run build --if-present && npm prune --omit=dev", "",
                 f"FROM {NODE_IMAGE}", "RUN apk upgrade --no-cache", "ENV NODE_ENV=production", "WORKDIR /app",
                 "COPY --from=build --chown=node:node /app ./", "USER node"]
        if svc["port"]:
            lines.append(f"EXPOSE {svc['port']}")
        return head + "\n".join(lines + ['CMD ["npm", "start"]', ""])
    lines = [f"FROM {PYTHON_IMAGE}", "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1", "WORKDIR /app",
             "COPY requirements.txt ./", "RUN pip install --no-cache-dir -r requirements.txt", "COPY . .",
             "RUN useradd --create-home --uid 10001 app && chown -R app:app /app", "USER app"]
    if svc["port"]:
        lines.append(f"EXPOSE {svc['port']}")
    return head + "\n".join(lines + ["CMD " + json.dumps(svc["start"]), ""])


def render_nginx(arch: dict, svc: dict) -> str:
    """화면 서비스 — 빌드한 정적 파일 + /api 를 첫 API 서비스로 넘김 + 새로 고침해도 화면(SPA)."""
    api = next((arch_mod.service(arch, d) for d in svc["depends_on"] if (arch_mod.service(arch, d) or {}).get("kind") == "api"), None)
    proxy = ""
    if api:
        proxy = (f"    location /api/ {{\n        proxy_pass http://{api['name']}:{api['port']};\n"
                 "        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n    }\n")
    return ("# ReCoder 검증 nginx 설정\nserver {\n"
            f"    listen {svc['port']};\n    server_tokens off;\n    root /usr/share/nginx/html;\n"
            + proxy +
            "    location / {\n        try_files $uri $uri/ /index.html;\n    }\n}\n")


DOCKERIGNORE = "node_modules\ndist\n.env\n.env.*\n!.env.example\n.git\n__pycache__\n*.pyc\n.venv\n"


def env_example(arch: dict) -> str:
    lines = ["# ReCoder — docker compose 가 읽는 비밀값. 이 파일을 .env 로 복사하고 값을 바꾸세요(.env 는 저장소에 올리지 않습니다).",
             "# 값 만들기 예: openssl rand -hex 24"]
    for db in arch["databases"]:
        lines.append(f"{arch_mod.password_var(db['name'])}=<random-password>")
        if db["engine"] == "mysql":
            lines.append(f"{arch_mod.env_name(db['name'])}_ROOT_PASSWORD=<random-password>")
    if any(_needs_jwt(arch, s) for s in arch["services"]):
        lines.append("JWT_SECRET=<random-secret>")
    return "\n".join(lines) + "\n"


def verify_env(arch: dict) -> dict[str, str]:
    """검증 실행 한 번만 쓰는 비밀값(URL 에 넣어도 안전한 16진 문자)."""
    out: dict[str, str] = {}
    for db in arch["databases"]:
        out[arch_mod.password_var(db["name"])] = secrets.token_hex(16)
        if db["engine"] == "mysql":
            out[arch_mod.env_name(db["name"]) + "_ROOT_PASSWORD"] = secrets.token_hex(16)
    if any(_needs_jwt(arch, s) for s in arch["services"]):
        out["JWT_SECRET"] = secrets.token_hex(32)
    return out


def verify_override(arch: dict) -> str:
    """검증용 덧씌우기 — 모든 HTTP 서비스를 127.0.0.1 의 임의 포트로만 연다(배포 포트와 겹치지 않게)."""
    lines = ["services:"]
    for svc in arch["services"]:
        if svc["port"]:
            lines += [f"  {svc['name']}:", f"    ports: !override", f"      - \"127.0.0.1::{svc['port']}\""]
    return "\n".join(lines) + "\n" if len(lines) > 1 else "services: {}\n"


def contract_tests(arch: dict) -> str:
    services = {s["name"]: {"port": s["port"], "base_env": f"{arch_mod.env_name(s['name'])}_BASE_URL", "public": s["public"]}
                for s in arch["services"] if s["port"]}
    doc = {"version": 1, "note": "ReCoder 가 아키텍처 계약에서 만든 통합 테스트. scripts/contract-test.mjs 로 실행합니다.",
           "services": services, "cases": arch_mod.test_cases(arch)}
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


CONTRACT_RUNNER = r"""#!/usr/bin/env node
// ReCoder 통합 테스트 실행기 — contract-tests.json 의 경우를 실제 서비스에 보냅니다(Node 18 이상, 의존성 없음).
// 사용: docker compose up -d --wait 후 `node scripts/contract-test.mjs`
// 공개하지 않은 서비스는 <서비스>_BASE_URL 환경 변수로 주소를 알려 주세요(예: ORDERS_BASE_URL=http://localhost:4001).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const spec = JSON.parse(readFileSync(join(root, "contract-tests.json"), "utf8"));

function judge(expect, status) {
  if (status == null) return false;
  for (const raw of String(expect).split(",")) {
    const rule = raw.trim();
    const s = String(status);
    if (rule.startsWith("!")) {
      const bad = rule.slice(1);
      if ((bad.endsWith("xx") && s[0] === bad[0]) || bad === s) return false;
    } else if (rule.includes("|")) {
      if (!rule.split("|").includes(s)) return false;
    } else if (rule.endsWith("xx")) {
      if (s[0] !== rule[0]) return false;
    } else if (rule !== s) return false;
  }
  return true;
}

let failed = 0, skipped = 0;
for (const c of spec.cases) {
  const svc = spec.services[c.service];
  const base = process.env[svc.base_env] || (svc.public ? `http://localhost:${svc.port}` : "");
  if (!base) { skipped++; console.log(`SKIP ${c.id} — ${svc.base_env} 가 없습니다`); continue; }
  let status = null;
  try {
    const res = await fetch(base + c.path, {
      method: c.method, redirect: "manual",
      headers: c.body ? { "content-type": "application/json" } : {},
      body: c.body ? JSON.stringify(c.body) : undefined,
      signal: AbortSignal.timeout(10000),
    });
    status = res.status;
  } catch (e) { status = null; }
  const ok = judge(c.expect, status);
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"} ${c.method} ${c.service}${c.path} → ${status ?? "연결 실패"} (기대 ${c.expect}: ${c.why})`);
}
console.log(`\n${spec.cases.length - failed - skipped} 통과 · ${failed} 실패 · ${skipped} 건너뜀`);
process.exit(failed ? 1 : 0);
"""


def architecture_doc(arch: dict) -> str:
    lines = ["# 아키텍처", "", "ReCoder 가 아키텍처 계약(`architecture.json`)에서 만든 문서입니다.", "", "```mermaid", "flowchart LR"]
    for s in arch["services"]:
        lines.append(f"  {s['name'].replace('-', '_')}[\"{s['name']} · {s['kind']}\"]")
    for d in arch["databases"]:
        lines.append(f"  {d['name'].replace('-', '_')}[(\"{d['name']} · {arch_mod.ENGINES[d['engine']]['label']}\")]")
    for s in arch["services"]:
        for dep in s["depends_on"]:
            lines.append(f"  {s['name'].replace('-', '_')} --> {dep.replace('-', '_')}")
        for db in s["databases"]:
            lines.append(f"  {s['name'].replace('-', '_')} --> {db.replace('-', '_')}")
    lines += ["```", "", "| 서비스 | 종류 | 언어 | 포트 | 공개 | 의존 | DB |", "|---|---|---|---|---|---|---|"]
    for s in arch["services"]:
        lines.append(f"| {s['name']} | {s['kind']} | {s['language']} | {s['port'] or '-'} | {'예' if s['public'] else '-'} | "
                     f"{', '.join(s['depends_on']) or '-'} | {', '.join(s['databases']) or '-'} |")
    lines += ["", "## 실행", "", "```bash", "cp .env.example .env   # 값을 바꾸세요", "docker compose up -d --build --wait",
              "node scripts/contract-test.mjs", "```", ""]
    return "\n".join(lines)


def fixed_ops(arch: dict) -> list[dict]:
    """계약에서 ReCoder 가 쓰는 고정 파일들 — AI 교정이 덮지 않는다(fixed)."""
    def op(path: str, content: str, language: str, why: str) -> dict:
        return {"action": "create", "file": path, "content": content, "language": language, "fixed": True, "rationale": why}

    ops = [op("docker-compose.yml", render_compose(arch), "yaml", "ReCoder 검증 compose — 서비스·DB·연결을 계약대로"),
           op("architecture.json", json.dumps(arch, ensure_ascii=False, indent=2) + "\n", "json", "아키텍처 계약(ReCoder 확정)"),
           op(".env.example", env_example(arch), "", "compose 가 읽는 비밀값 이름(값은 넣지 않음)"),
           op("contract-tests.json", contract_tests(arch), "json", "계약에서 만든 통합 테스트"),
           op("scripts/contract-test.mjs", CONTRACT_RUNNER, "javascript", "통합 테스트 실행기(의존성 없음)"),
           op("ARCHITECTURE.md", architecture_doc(arch), "markdown", "구성도와 실행 방법")]
    for svc in arch["services"]:
        ops.append(op(f"{svc['path']}/Dockerfile", render_dockerfile(svc), "dockerfile", f"ReCoder 검증 Dockerfile — {svc['name']}"))
        ops.append(op(f"{svc['path']}/.dockerignore", DOCKERIGNORE, "", "Docker 빌드에서 제외할 파일"))
        if svc["kind"] == "web":
            ops.append(op(f"{svc['path']}/nginx.conf", render_nginx(arch, svc), "nginx", "화면 서버 설정(/api 연결·SPA)"))
    return ops


FIXED_NAMES = {"docker-compose.yml", "architecture.json", ".env.example", "contract-tests.json", "scripts/contract-test.mjs",
               "ARCHITECTURE.md"}


def is_fixed_path(arch: dict, path: str) -> bool:
    norm = str(path or "").replace("\\", "/").lstrip("/")
    if norm in FIXED_NAMES:
        return True
    svc = arch_mod.service_of(arch, norm)
    return bool(svc) and norm[len(svc["path"]) + 1:] in {"Dockerfile", ".dockerignore", "nginx.conf"}
