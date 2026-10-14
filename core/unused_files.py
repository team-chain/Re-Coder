"""AI 가 만들었지만 어디에서도 쓰지 않는 파일 찾기(아키텍처 맵의 "참조 0 · 고립" 과 같은 뜻).

보수적으로 판단한다 — 확실히 아무도 쓰지 않는 것만 고른다.
  · JS/TS 소스만 본다. 시작 파일(main·index·server·app), 설정·테스트·마이그레이션·스크립트, 타입 선언은 뺀다.
  · 파일 이름(확장자 뺀 이름)이 다른 코드·설정 파일 어디에도 나오지 않아야 한다(문서는 세지 않는다).
  · 폴더를 통째로 읽어 불러오는(readdirSync) 코드가 있으면 그 프로젝트 쪽은 판단하지 않는다.
  · 파일 위치가 곧 주소인 프레임워크(Next·Nuxt·Remix·SvelteKit·Astro 등)의 pages·app·routes 는 판단하지 않는다.

두 종류로 나눈다.
  · route  — 서버 API 파일(Express Router 등)인데 서버가 등록하지 않았다 → 그 기능이 실제로 동작하지 않는다.
             서버 진입 파일을 고칠 **오류**로 낸다(일관성 점검이 등록하게 한다).
  · module — 화면·공통 모듈인데 아무도 불러오지 않는다 → 빼도 동작이 같다. 같은 API 주소를 직접 부르는 화면이
             있으면 "그 화면이 이 모듈을 쓰게" 한 번 고쳐 보고(consumers), 남으면 적용에서 기본으로 뺀다.
"""

from __future__ import annotations

import json
import posixpath
import re

_SOURCE = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_REFERENCING = _SOURCE + (".vue", ".svelte", ".astro", ".html", ".json", ".yml", ".yaml", ".toml", ".sh")
_ENTRY_STEMS = {"main", "index", "server", "app", "App", "entry", "worker", "service-worker", "sw", "setupTests",
                "setupProxy", "reportWebVitals", "init-db", "seed", "migrate", "instrument", "middleware",
                "instrumentation", "page", "layout", "route", "loading", "error", "not-found", "template", "default"}
_SKIP_PARTS = {"test", "tests", "__tests__", "__mocks__", "e2e", "cypress", "migrations", "seeds", "seeders",
               "scripts", "bin", "public", "static", "node_modules", "dist", "build", "docs"}
#: 파일 위치가 곧 주소인 프레임워크 — pages·app·routes 아래 파일은 아무도 import 하지 않아도 쓰인다.
_FILE_ROUTING_DEPS = re.compile(r"^(next|nuxt|nuxt3|@remix-run/.+|@react-router/dev|@sveltejs/kit|astro|gatsby|expo-router|"
                                r"@tanstack/router-plugin|@tanstack/start|vite-plugin-pages|@vercel/node)$")
_FILE_ROUTING_DIRS = {"pages", "app", "routes", "api"}
_ENDPOINT = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/:{}$]+?)(?:[?'"`]|\$\{)""")
_ROUTE_CODE = re.compile(r"\bexpress\.Router\(|\bRouter\(\s*\)|\brouter\.(?:get|post|put|patch|delete|use|route)\(|"
                         r"\bfastify\.(?:get|post|put|patch|delete|route)\(|\bnew\s+Hono\(")
_SERVER_ENTRY = re.compile(r"\bexpress\(\s*\)|\bfastify\(|\bnew\s+Koa\(|\bnew\s+Hono\(")


def _norm(path: str) -> str:
    return re.sub(r"^(\./)+", "", str(path or "").replace("\\", "/")).lstrip("/")


def _stem(path: str) -> str:
    return posixpath.basename(path).split(".", 1)[0]


def _side(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else ""


def _candidate(path: str) -> bool:
    low = path.lower()
    if not low.endswith(_SOURCE) or low.endswith(".d.ts"):
        return False
    parts = low.split("/")
    if any(p in _SKIP_PARTS for p in parts[:-1]):
        return False
    base = posixpath.basename(path)
    if re.search(r"\.(test|spec|config|stories|setup)\.", base) or base.startswith(
            ("vite.", "jest.", "babel.", "eslint", "tailwind.", "postcss.", "webpack.", "+", "_app.", "_document.", "[")):
        return False
    return _stem(path) not in _ENTRY_STEMS


def _file_routing_roots(files: dict[str, str]) -> set[str]:
    """파일 기반 주소 프레임워크를 쓰는 프로젝트 폴더(package.json 위치)."""
    roots = set()
    for path, text in files.items():
        if posixpath.basename(path) != "package.json":
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})} if isinstance(data, dict) else {}
        if any(_FILE_ROUTING_DEPS.match(str(name)) for name in deps):
            roots.add(posixpath.dirname(path))
    return roots


def _routed_by_location(path: str, roots: set[str]) -> bool:
    for root in roots:
        prefix = f"{root}/" if root else ""
        if path.startswith(prefix):
            rest = path[len(prefix):].split("/")
            if any(p in _FILE_ROUTING_DIRS for p in rest[:-1][:3]):
                return True
    return False


def _endpoints(text: str) -> set[str]:
    out = set()
    for m in _ENDPOINT.finditer(text or ""):
        path = re.sub(r"/(?::\w+|\{[^}]*\}|\$\{?[^/]*)$", "", m.group(1).rstrip("/"))
        if path.count("/") >= 2:
            out.add(path)
    return out


def find(ops: list[dict]) -> list[dict]:
    """[{file, kind: route|module, consumers, entry, endpoints, exports}] — 아무도 쓰지 않는 생성 파일."""
    files = {_norm(op.get("file")): str(op.get("content") or "") for op in ops or []
             if op.get("action") != "delete" and isinstance(op.get("content"), str)}
    raw = {_norm(op.get("file")): str(op.get("file")) for op in ops or []}
    code = {p: t for p, t in files.items() if p.lower().endswith(_REFERENCING) or posixpath.basename(p) in ("Dockerfile", "Procfile")}
    dynamic_roots = {_side(p) for p, t in code.items() if "readdirSync" in t and re.search(r"\brequire\(|\bimport\(", t)}
    routing_roots = _file_routing_roots(files)
    out: list[dict] = []
    for path in sorted(files):
        if not _candidate(path) or _side(path) in dynamic_roots or _routed_by_location(path, routing_roots):
            continue
        stem = _stem(path)
        word = re.compile(rf"(?<![\w$-]){re.escape(stem)}(?![\w$-])")
        if any(word.search(text) for other, text in code.items() if other != path):
            continue
        content = files[path]
        side = _side(path)
        kind = "route" if _ROUTE_CODE.search(content) or re.search(r"(^|/)(routes|controllers)/", path) and "router" in content.lower() else "module"
        entry = next((p for p, t in sorted(code.items()) if p != path and _side(p) == side and p.lower().endswith(_SOURCE)
                      and _SERVER_ENTRY.search(t)), "") if kind == "route" else ""
        mine = _endpoints(content)
        #: 같은 쪽(같은 최상위 폴더)에서 그 주소를 **부르는** 파일만 — 서버의 라우트 등록(app.use('/api/orders'))은 아니다.
        consumers = sorted(other for other, text in code.items()
                           if other != path and other.lower().endswith(_SOURCE) and _side(other) == side
                           and not re.search(r"\bexpress\b|\bRouter\(|\bapp\.(?:use|get|post|put|patch|delete)\(", text)
                           and mine & _endpoints(text)) if kind == "module" else []
        names = re.findall(r"\bexport\s+(?:async\s+)?(?:const|let|function\*?|class)\s+([\w$]+)", content)
        out.append({"file": raw.get(path, path), "kind": kind, "consumers": [raw.get(c, c) for c in consumers[:3]],
                    "entry": raw.get(entry, entry), "endpoints": sorted(mine)[:5], "exports": names[:8]})
    return out


def _import_path(target: str, importer: str) -> str:
    rel = posixpath.relpath(_norm(target), posixpath.dirname(_norm(importer)) or ".")
    rel = rel if rel.startswith(".") else "./" + rel
    return re.sub(r"\.(jsx?|tsx?|mjs|cjs)$", "", rel)


def route_issues(unused: list[dict]) -> list[dict]:
    """서버가 등록하지 않은 API 파일 — 그 기능이 실제로 동작하지 않으므로 서버 진입 파일을 고칠 오류."""
    issues = []
    for u in unused:
        if u.get("kind") != "route" or not u.get("entry"):
            continue
        rel = _import_path(u["file"], u["entry"])
        issues.append({
            "code": "SERVER_ROUTE_NOT_MOUNTED", "severity": "error", "file": u["entry"],
            "message": f"{u['file']} 의 API 를 서버가 등록하지 않아 그 기능이 동작하지 않습니다",
            "fix": f"이 파일에서 '{rel}' 를 불러와 app.use('/api/{_stem(u['file'])}', …) 처럼 등록하세요"
                   "(그 파일이 내보내는 형태 그대로, 다른 API 와 같은 규칙의 경로로).",
        })
    return issues


def wiring_issues(unused: list[dict]) -> list[dict]:
    """같은 API 를 직접 부르는 파일에 "만들어 둔 모듈을 쓰라" 고 고칠 문제(부분 교정용 — 그 파일을 고친다)."""
    issues = []
    unused_paths = {u["file"] for u in unused}
    for u in unused:
        consumers = [c for c in u.get("consumers") or [] if c not in unused_paths]
        if u.get("kind") == "route" or not consumers or not u.get("exports"):
            continue
        consumer = consumers[0]
        rel = _import_path(u["file"], consumer)
        issues.append({
            "code": "UNUSED_GENERATED_FILE", "severity": "error", "file": consumer,
            "message": f"이 파일이 {', '.join(u['endpoints'][:3])} 를 직접 부르는데, 같은 일을 하려고 만든 {u['file']} 는 아무도 쓰지 않습니다",
            "fix": f"'{rel}' 의 {', '.join(u['exports'])} 을(를) import 해서 직접 호출 대신 쓰세요(그 파일에 실제로 있는 이름만). 화면 동작은 바꾸지 마세요.",
        })
    return issues
