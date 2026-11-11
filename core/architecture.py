"""다중 서비스·다중 DB 아키텍처 계약(3.1) — 대규모 생성이 "앱 하나 + DB 하나" 를 넘는 구성을 끝까지 맞춰 만들게 한다.

왜 필요한가
    지금까지의 약속(contracts)은 자유 글이었다. 서비스가 여럿이고 DB 가 여럿이면 서비스 이름·폴더·포트·DB 접속 변수 이름이
    파일마다 조금씩 달라져, 하나하나는 빌드되는데 함께 띄우면 서로 찾지 못했다. 그래서 구성(서비스·DB·연결·API)을
    **구조화된 계약**으로 먼저 받고, ReCoder 가 정규화해 이름·변수·포트를 하나로 정한다.

    · AI 는 계약의 **내용**(어떤 서비스·어떤 DB·어떤 API)만 정한다.
    · 이름 규칙·접속 변수·Dockerfile·docker-compose.yml 은 ReCoder 가 계약에서 결정적으로 만든다(compose_kit) —
      AI 가 쓴 인프라 파일이 매번 다른 방식으로 깨졌던 경험(docker_kit)과 같은 이유다.
    · 계약은 검증(compose_check)과 통합 테스트(contract-tests.json)의 기준이 된다.

계약 모양(정규화 후)
    {"services": [{"name", "path", "kind": api|worker|web, "language": node|python, "port", "health",
                   "start", "output", "public", "depends_on": [...], "databases": [...]}],
     "databases": [{"name", "engine": postgres|mysql|mongodb|redis, "tables": [...]}],
     "endpoints": [{"service", "method", "path", "auth", "expect", "body"}]}

    서비스 간 연결은 depends_on 이고, 서비스 A 는 B 를 환경 변수 `B_URL`(http://b:포트)로 부른다.
    DB 접속은 환경 변수 `<DB 이름>_URL` 이다(예: orders-db → ORDERS_DB_URL).
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Iterable, Optional

ENGINES: dict[str, dict[str, Any]] = {
    "postgres": {"port": 5432, "label": "PostgreSQL", "tables": True},
    "mysql": {"port": 3306, "label": "MySQL", "tables": True},
    "mongodb": {"port": 27017, "label": "MongoDB", "tables": False},
    "redis": {"port": 6379, "label": "Redis", "tables": False},
}
_ENGINE_ALIASES = {"postgresql": "postgres", "pg": "postgres", "psql": "postgres", "mariadb": "mysql",
                   "mongo": "mongodb", "valkey": "redis", "elasticache": "redis"}
KINDS = ("api", "worker", "web")
LANGUAGES = ("node", "python")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_SERVICES = 12
MAX_DATABASES = 8
MAX_ENDPOINTS = 80
MAX_TABLES = 40
_NAME = re.compile(r"[^a-z0-9-]+")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

#: 다중 서비스·다중 DB 를 원하는 요청인지 — 이 말이 있을 때만 아키텍처 계약 단계를 더한다(작은 앱은 예전 흐름 그대로).
_WANTS = re.compile(
    r"(마이크로\s*서비스|micro-?services?|서비스\s*(?:여러|분리|[2-9]\s*개)|여러\s*(?:개의\s*)?(?:서비스|DB|데이터베이스)|"
    r"(?:DB|데이터베이스)\s*(?:여러|[2-9]\s*개)|다중\s*(?:서비스|DB|데이터베이스)|멀티\s*(?:서비스|DB)|"
    r"엔터프라이즈|enterprise|docker[\s-]*compose|컴포즈|"
    r"(?:api\s*)?게이트웨이|api[\s-]*gateway|워커|worker|메시지\s*큐|message\s*queue|"
    r"redis|레디스|mongo(?:db)?|몽고|mysql|mariadb|postgres(?:ql)?.{0,40}(?:redis|mongo|mysql)|"
    r"(?:redis|mongo|mysql).{0,40}postgres)",
    re.I,
)


def wants(text: str, decisions: Iterable[Any] | None = None) -> bool:
    """요청(과 고른 설계 결정)이 여러 서비스·여러 DB 구성을 원하는가."""
    blob = str(text or "")
    for d in decisions or []:
        try:
            blob += "\n" + json.dumps(d, ensure_ascii=False)
        except (TypeError, ValueError):
            blob += "\n" + str(d)
    return bool(_WANTS.search(blob))


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "services": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
            "language": {"type": "string", "enum": list(LANGUAGES)}, "port": {"type": "integer"},
            "start": {"type": "string"}, "public": {"type": "boolean"}, "purpose": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "databases": {"type": "array", "items": {"type": "string"}},
        }, "required": ["name", "kind", "language"]}},
        "databases": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "engine": {"type": "string", "enum": list(ENGINES)},
            "purpose": {"type": "string"}, "tables": {"type": "array", "items": {"type": "string"}},
        }, "required": ["name", "engine"]}},
        "endpoints": {"type": "array", "items": {"type": "object", "properties": {
            "service": {"type": "string"}, "method": {"type": "string", "enum": list(METHODS)},
            "path": {"type": "string"}, "auth": {"type": "boolean"}, "expect": {"type": "integer"},
            "body": {"type": "object"},
        }, "required": ["service", "method", "path"]}},
    },
    "required": ["services", "databases", "endpoints"],
}


def prompt(context: str) -> str:
    """아키텍처 계약을 받는 설계 지시."""
    return context + f"""

[대규모 생성 0단계 — 아키텍처 계약] 이 앱은 서비스·DB 가 여럿일 수 있습니다. **파일 내용은 쓰지 말고** 아래 JSON 만 주세요.
- services: 독립 배포 단위(컨테이너). name 은 영문 소문자·숫자·하이픈(예: gateway, orders, catalog, worker, web).
  kind: api(HTTP API) · worker(큐·배치 처리, HTTP 없음) · web(브라우저 화면 — 빌드된 정적 파일).
  language: node 또는 python. port: api/web 의 내부 포트. start: python 서비스의 실행 명령(예: uvicorn app.main:app --host 0.0.0.0 --port 8000).
  public: 사용자가 직접 접속하는 서비스만 true(보통 web 과 gateway). depends_on: 이 서비스가 부르는 다른 서비스 name.
  databases: 이 서비스가 쓰는 DB name(서비스마다 자기 DB 를 갖는 것이 원칙, 공유가 꼭 필요할 때만 같이 씀).
- databases: name(예: orders-db, catalog-db, cache), engine({'/'.join(ENGINES)}), tables(관계형이면 만들 테이블 이름).
- endpoints: 서비스마다 실제로 구현할 HTTP API(테스트 기준). service, method, path(예: /api/orders, /api/orders/:id),
  auth(로그인 토큰이 필요하면 true), body(POST/PUT 예시 요청). 각 api 서비스에 GET /health 는 쓰지 마세요(ReCoder 가 넣습니다).
요청이 작으면 서비스 1개·DB 1개여도 됩니다. 실제로 필요한 만큼만 나누세요(최대 서비스 {MAX_SERVICES}개 · DB {MAX_DATABASES}개)."""


def _slug(value: Any, limit: int = 30) -> str:
    text = _NAME.sub("-", str(value or "").strip().lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:limit].strip("-")
    if text and not text[0].isalpha():
        text = "s-" + text
    return text[:limit].strip("-")


def env_name(name: str) -> str:
    """서비스·DB 이름 → 환경 변수 접두사(orders-db → ORDERS_DB)."""
    return re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_")


def db_url_var(db: str) -> str:
    return f"{env_name(db)}_URL"


def service_url_var(service: str) -> str:
    return f"{env_name(service)}_URL"


def password_var(db: str) -> str:
    return f"{env_name(db)}_PASSWORD"


def _port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1024 <= port <= 65535 else default


def _start(value: Any) -> list[str]:
    """python 실행 명령 → 인자 목록. 셸 연산자가 있으면 버린다(컨테이너 CMD 는 셸 없이 JSON 배열로 쓴다)."""
    if isinstance(value, list) and value and all(isinstance(p, str) for p in value):
        value = shlex.join(value)
    text = str(value or "").strip()
    if not text or re.search(r"[;&|`$<>]", text):
        return []
    try:
        parts = shlex.split(text)
    except ValueError:
        return []
    return parts[:24] if parts and all(len(p) <= 200 for p in parts) else []


def _cycle(services: dict[str, dict]) -> Optional[list[str]]:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(name: str) -> Optional[list[str]]:
        state[name] = 1
        path.append(name)
        for dep in services[name]["depends_on"]:
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if not state.get(dep):
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[name] = 2
        return None

    for name in services:
        if not state.get(name):
            found = visit(name)
            if found:
                return found
    return None


def normalize(raw: Any) -> tuple[Optional[dict], list[str]]:
    """AI 가 준 계약(비신뢰) → (정규화한 계약 | None, 바로잡은 내용). 쓸 수 없으면 None.

    멱등이다 — 정규화한 계약을 다시 넣으면 같은 값이 나온다."""
    notes: list[str] = []
    if not isinstance(raw, dict):
        return None, ["계약이 JSON 객체가 아닙니다"]
    dbs: dict[str, dict] = {}
    for item in raw.get("databases") or []:
        if not isinstance(item, dict):
            continue
        name = _slug(item.get("name"))
        engine = str(item.get("engine") or "").strip().lower()
        engine = _ENGINE_ALIASES.get(engine, engine)
        if not name or engine not in ENGINES:
            notes.append(f"DB 「{item.get('name')}」 — 지원하지 않는 종류({item.get('engine')})라 뺐습니다")
            continue
        if name in dbs:
            continue
        if len(dbs) >= MAX_DATABASES:
            notes.append(f"DB 는 최대 {MAX_DATABASES}개까지만 씁니다")
            break
        tables = []
        if ENGINES[engine]["tables"]:
            for t in item.get("tables") or []:
                t = str(t or "").strip()
                if _TABLE.match(t) and t.lower() not in {x.lower() for x in tables}:
                    tables.append(t)
            tables = tables[:MAX_TABLES]
        dbs[name] = {"name": name, "engine": engine, "tables": tables,
                     "purpose": str(item.get("purpose") or "").strip()[:160]}
    services: dict[str, dict] = {}
    for item in raw.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = _slug(item.get("name"))
        if not name or name in dbs:
            if name:
                notes.append(f"서비스 「{name}」 — DB 와 이름이 같아 뺐습니다")
            continue
        if name in services:
            continue
        if len(services) >= MAX_SERVICES:
            notes.append(f"서비스는 최대 {MAX_SERVICES}개까지만 씁니다")
            break
        kind = str(item.get("kind") or "api").strip().lower()
        kind = kind if kind in KINDS else "api"
        language = str(item.get("language") or "node").strip().lower()
        language = language if language in LANGUAGES else "node"
        if kind == "web" and language != "node":
            notes.append(f"서비스 「{name}」 — 화면(web)은 node 로 빌드합니다")
            language = "node"
        default_port = {"web": 8080, "worker": 0}.get(kind, 8000 if language == "python" else 3000)
        port = 0 if kind == "worker" else _port(item.get("port"), default_port)
        start = _start(item.get("start")) if language == "python" else []
        if language == "python" and not start:
            start = (["python", "-m", "app.worker"] if kind == "worker" else
                     ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)])
        services[name] = {
            "name": name, "path": f"services/{name}", "kind": kind, "language": language, "port": port,
            "health": "/health" if kind == "api" else ("/" if kind == "web" else ""),
            "start": start, "output": "dist" if kind == "web" else "",
            "public": bool(item.get("public")) and kind != "worker",
            "purpose": str(item.get("purpose") or "").strip()[:200],
            "depends_on": [_slug(d) for d in item.get("depends_on") or [] if _slug(d)],
            "databases": [_slug(d) for d in item.get("databases") or [] if _slug(d)],
        }
    if not services:
        return None, notes + ["서비스가 하나도 없습니다"]
    for svc in services.values():
        deps = []
        for dep in svc["depends_on"]:
            if dep == svc["name"] or dep in deps:
                continue
            if dep not in services:
                notes.append(f"서비스 「{svc['name']}」 — 없는 서비스 「{dep}」 의존을 뺐습니다")
                continue
            if services[dep]["kind"] == "worker":
                notes.append(f"서비스 「{svc['name']}」 — HTTP 가 없는 워커 「{dep}」 는 부를 수 없어 뺐습니다")
                continue
            deps.append(dep)
        svc["depends_on"] = deps
        used = []
        for db in svc["databases"]:
            if db in dbs and db not in used:
                used.append(db)
            elif db not in dbs:
                notes.append(f"서비스 「{svc['name']}」 — 없는 DB 「{db}」 를 뺐습니다")
        svc["databases"] = used
    cycle = _cycle(services)
    while cycle:
        a, b = cycle[-2], cycle[-1]
        services[a]["depends_on"].remove(b)
        notes.append(f"서비스 의존이 순환합니다({' → '.join(cycle)}) — {a} → {b} 를 뺐습니다")
        cycle = _cycle(services)
    for name, db in dbs.items():
        if not any(name in s["databases"] for s in services.values()):
            notes.append(f"DB 「{name}」 — 쓰는 서비스가 없습니다")
    if not any(s["public"] for s in services.values()):
        entry = next((s for s in services.values() if s["kind"] == "web"), None) or \
            next((s for s in services.values() if s["kind"] == "api" and not any(s["name"] in o["depends_on"] for o in services.values())), None) or \
            next((s for s in services.values() if s["kind"] != "worker"), None)
        if entry:
            entry["public"] = True
    endpoints: list[dict] = []
    seen: set = set()
    for item in raw.get("endpoints") or []:
        if not isinstance(item, dict):
            continue
        svc = _slug(item.get("service"))
        method = str(item.get("method") or "GET").strip().upper()
        path = "/" + str(item.get("path") or "").strip().lstrip("/")
        if svc not in services or services[svc]["kind"] != "api" or method not in METHODS:
            continue
        if not re.fullmatch(r"/[A-Za-z0-9_\-/.:{}]*", path) or ".." in path or len(path) > 160:
            continue
        if path.rstrip("/") == "/health":
            continue
        key = (svc, method, re.sub(r"[:{][^/]+", ":p", path))
        if key in seen:
            continue
        seen.add(key)
        if len(endpoints) >= MAX_ENDPOINTS:
            notes.append(f"API 는 최대 {MAX_ENDPOINTS}개까지만 시험합니다")
            break
        expect = item.get("expect")
        expect = expect if isinstance(expect, int) and 100 <= expect <= 599 else None
        body = item.get("body") if isinstance(item.get("body"), dict) and method in ("POST", "PUT", "PATCH") else None
        if body is not None and len(json.dumps(body, ensure_ascii=False)) > 2000:
            body = None
        endpoints.append({"service": svc, "method": method, "path": path, "auth": bool(item.get("auth")),
                          "expect": expect, "body": body})
    arch = {"services": list(services.values()), "databases": list(dbs.values()), "endpoints": endpoints}
    return arch, notes


def is_multi(arch: Optional[dict]) -> bool:
    """서비스가 둘 이상이거나 DB 가 둘 이상 — 이때 ReCoder 가 compose 로 함께 띄워 검증한다."""
    return bool(arch) and (len(arch.get("services") or []) > 1 or len(arch.get("databases") or []) > 1)


def service(arch: dict, name: str) -> Optional[dict]:
    return next((s for s in arch.get("services") or [] if s["name"] == name), None)


def database(arch: dict, name: str) -> Optional[dict]:
    return next((d for d in arch.get("databases") or [] if d["name"] == name), None)


def env_for(arch: dict, svc: dict) -> dict[str, str]:
    """서비스가 받는 환경 변수 이름 → 설명(값은 compose 가 넣는다)."""
    if svc["kind"] == "web":
        return {}  # 화면은 빌드한 정적 파일 — API 는 같은 주소의 /api 로 부른다(nginx 가 넘김)
    out = {"PORT": f"이 서비스가 들을 포트({svc['port']})"} if svc["port"] else {}
    for db in svc["databases"]:
        d = database(arch, db)
        if d:
            out[db_url_var(db)] = f"{ENGINES[d['engine']]['label']} 접속 주소({db})"
    for dep in svc["depends_on"]:
        out[service_url_var(dep)] = f"서비스 {dep} 의 주소(http://{dep}:{service(arch, dep)['port']})"
    if svc["kind"] == "api" and any(e["auth"] for e in arch.get("endpoints") or [] if e["service"] == svc["name"]):
        out["JWT_SECRET"] = "로그인 토큰 서명 비밀값"
    return out


def contract_text(arch: dict) -> str:
    """설계·생성 프롬프트에 붙이는 계약 — 모든 에이전트가 글자 그대로 따른다."""
    lines = ["", "", "[아키텍처 계약 — ReCoder 가 확정. 이름·폴더·포트·환경 변수 이름을 글자 그대로 쓰세요]"]
    for s in arch["services"]:
        head = f"- 서비스 {s['name']} ({s['kind']}, {s['language']}) 폴더 {s['path']}/"
        if s["port"] and s["kind"] != "web":
            head += f" · 포트 {s['port']}(환경 변수 PORT 로 받기, 0.0.0.0 에서 듣기)"
        lines.append(head + (f" · {s['purpose']}" if s["purpose"] else ""))
        env = env_for(arch, s)
        if env:
            lines.append("    환경 변수: " + ", ".join(f"{k}({v})" for k, v in env.items()))
        if s["kind"] == "api":
            lines.append("    GET /health — 쓰는 DB 연결이 모두 되면 200 {\"status\":\"ok\"}, 아니면 503. 시작할 때 DB 가 아직 "
                         "준비되지 않았으면 몇 번 기다렸다 다시 연결(바로 종료하지 않기).")
        if s["kind"] == "worker":
            lines.append("    HTTP 서버 없이 계속 도는 프로세스(작업 큐·주기 작업). 오류가 나도 프로세스를 끝내지 말고 기록 후 다시 시도.")
        if s["kind"] == "web":
            lines.append(f"    빌드 결과를 {s['output']}/ 에 냅니다(vite build). API 는 같은 주소의 /api 로 부릅니다(ReCoder 가 연결).")
        if s["kind"] == "web":
            lines.append(f"    {s['path']}/package.json 의 scripts 에 build(필수, vite build) — 서버 코드는 쓰지 않습니다.")
        elif s["language"] == "node":
            lines.append(f"    {s['path']}/package.json 의 scripts 에 start(필수) — TypeScript 면 build=tsc 와 start=node dist/...")
        else:
            lines.append(f"    {s['path']}/requirements.txt 와 실행 명령 `{' '.join(s['start'])}` 이 동작하는 모듈 구조.")
    for d in arch["databases"]:
        owners = [s["name"] for s in arch["services"] if d["name"] in s["databases"]]
        text = f"- DB {d['name']} ({ENGINES[d['engine']]['label']}) — 접속 변수 {db_url_var(d['name'])} · 사용 {', '.join(owners) or '없음'}"
        if d["tables"]:
            text += f" · 테이블 {', '.join(d['tables'])}(서비스가 시작할 때 없으면 만들기 — 이미 있으면 그대로)"
        lines.append(text)
    for e in arch["endpoints"]:
        lines.append(f"- API {e['service']}: {e['method']} {e['path']}" + (" · 로그인 필요(토큰 없으면 401)" if e["auth"] else "")
                     + (f" · 요청 예 {json.dumps(e['body'], ensure_ascii=False)}" if e.get("body") else ""))
    lines.append("- Dockerfile·docker-compose.yml·.env.example·contract-tests.json 은 ReCoder 가 만듭니다. 쓰지 마세요. "
                 "비밀번호·키를 코드에 쓰지 말고 환경 변수로만 읽으세요.")
    return "\n".join(lines)


def required_files(arch: dict) -> list[dict]:
    """서비스마다 빠지면 안 되는 파일(설계 목록에 없으면 더한다)."""
    out = []
    for s in arch["services"]:
        if s["language"] == "node":
            out.append({"file": f"{s['path']}/package.json", "layer": 0, "size": "small",
                        "purpose": f"{s['name']} 의존성과 scripts(" + ("build=vite build" if s["kind"] == "web" else "start") + ")"})
        else:
            out.append({"file": f"{s['path']}/requirements.txt", "layer": 0, "size": "small",
                        "purpose": f"{s['name']} 파이썬 의존성(버전 고정)"})
    return out


def service_of(arch: dict, path: str) -> Optional[dict]:
    """파일 경로가 속한 서비스."""
    norm = str(path or "").replace("\\", "/").lstrip("/")
    return next((s for s in arch.get("services") or [] if norm == s["path"] or norm.startswith(s["path"] + "/")), None)


def test_cases(arch: dict) -> list[dict]:
    """계약에서 결정적으로 만드는 통합 테스트(③). 같은 규칙을 compose_check 와 프로젝트의 contract-test 실행기가 쓴다.

    · 서비스마다 /health → 200
    · 로그인 필요 API 를 토큰 없이 부르면 401/403(2xx 면 인증 없이 열린 것)
    · 나머지 API 는 5xx 가 아니고, 경로 변수가 없는 API 는 404 가 아니어야 한다(라우트가 실제로 있는가)
    """
    cases: list[dict] = []
    for s in arch["services"]:
        if s["kind"] in ("api", "web") and s["port"]:
            cases.append({"id": f"{s['name']}-health", "service": s["name"], "method": "GET", "path": s["health"] or "/",
                          "expect": "2xx", "why": "서비스가 떠 있고 DB 연결이 됨"})
    for e in arch["endpoints"]:
        path = re.sub(r":[A-Za-z_]\w*|\{[^}]+\}", "1", e["path"])
        base = {"service": e["service"], "method": e["method"], "path": path}
        if e.get("body") is not None:
            base["body"] = e["body"]
        if e["auth"]:
            cases.append({**base, "id": f"{e['service']}-{e['method']}-{e['path']}-noauth", "expect": "401|403",
                          "why": "로그인 없이는 거절"})
        elif e.get("expect"):
            cases.append({**base, "id": f"{e['service']}-{e['method']}-{e['path']}", "expect": str(e["expect"]),
                          "why": "계약의 응답 코드"})
        else:
            has_param = path != e["path"]
            cases.append({**base, "id": f"{e['service']}-{e['method']}-{e['path']}",
                          "expect": "!5xx" if has_param else "!5xx,!404", "why": "라우트가 있고 서버 오류가 없음"})
    return cases


def judge(expect: str, status: Optional[int]) -> bool:
    """테스트 기대값 판정 — '2xx', '401|403', '201', '!5xx', '!5xx,!404'."""
    if status is None:
        return False
    for rule in str(expect).split(","):
        rule = rule.strip()
        if rule.startswith("!"):
            bad = rule[1:]
            if (bad.endswith("xx") and str(status)[0] == bad[0]) or bad == str(status):
                return False
        elif "|" in rule:
            if str(status) not in rule.split("|"):
                return False
        elif rule.endswith("xx"):
            if str(status)[0] != rule[0]:
                return False
        elif rule != str(status):
            return False
    return True
