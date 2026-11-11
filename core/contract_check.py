"""계약 대조(3.1) — 아키텍처 계약(설계도)과 실제 코드가 같은지 정적으로 본다. 코드를 실행하지 않는다.

구조 분석이 "코드가 이렇게 생겼다" 만 보여 주면 맞는지 틀리는지 알 수 없다. 계약이 정답이므로, 코드가 그 정답을 지키는지를
근거(파일·줄)와 함께 보여 준다. 같은 결과를 생성 직후 점검·배포 게이트·코드 맵 화면이 함께 쓴다.

근거로 쓰는 것(추측하지 않는다)
    · 환경 변수 읽기 — 계약은 DB·서비스 주소를 `<이름>_URL` 로만 넘긴다. 서비스가 어떤 변수를 읽는지가 곧 무엇에 붙는지다.
      process.env.X · process.env["X"] · const { X } = process.env · os.environ["X"] · os.environ.get("X") · os.getenv("X")
    · 라우트 선언 — Express(app/router.get 과 app.use 마운트), FastAPI(@app.get · APIRouter(prefix) · include_router(prefix)),
      Flask(@app.route(methods=...)).
    · 주소 글자 — 코드에 박힌 localhost·서비스 이름·DB 이름 주소(환경 변수로 받아야 한다).

판정
    error   — 다른 서비스의 DB 에 직접 접속(CONTRACT_DB_FOREIGN), 계약에 없는 서비스 호출(CONTRACT_CALL_UNDECLARED),
              서비스 폴더 없음(CONTRACT_SERVICE_MISSING). 변수 이름이 정확한 근거라 배포 게이트가 막는다.
    warning — 계약 API 를 찾지 못함(CONTRACT_ENDPOINT_MISSING: 동적 라우트는 정적으로 못 볼 수 있어 실제 확인은 통합 테스트가 한다),
              주소 하드코딩(CONTRACT_HARDCODED_HOST), 계약에 있는데 쓰지 않는 의존·DB(CONTRACT_DEP_UNUSED · CONTRACT_DB_UNUSED).
    info    — 계약에 없는 API(CONTRACT_ENDPOINT_EXTRA).
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path
from typing import Optional

try:
    import architecture as arch_mod
except ImportError:  # pragma: no cover - 패키지 실행
    from core import architecture as arch_mod  # type: ignore

CODE_EXT = (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".py")
_SKIP = re.compile(r"(^|/)(node_modules|dist|build|__pycache__|\.venv|venv|coverage)/|(^|/)(tests?|__tests__)/|\.(test|spec)\.[jt]sx?$|(^|/)test_[^/]+\.py$")
_ENV_JS = re.compile(r"process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*['\"`]([A-Z][A-Z0-9_]*)['\"`]\s*\])")
_ENV_JS_DESTRUCT = re.compile(r"\{([^{}]{1,400})\}\s*=\s*process\.env\b")
_ENV_PY = re.compile(r"(?:os\.)?(?:environ(?:\.get)?\s*[\[(]\s*|getenv\s*\(\s*)['\"]([A-Z][A-Z0-9_]*)['\"]")
_PY_DECOR = re.compile(r"@(\w+)\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]*)['\"]")
_PY_ROUTE = re.compile(r"@(\w+)\.route\(\s*['\"]([^'\"]+)['\"](?:[^)]*methods\s*=\s*\[([^\]]*)\])?")
_PY_ROUTER = re.compile(r"(\w+)\s*=\s*APIRouter\(([^)]*)\)")
_PY_INCLUDE = re.compile(r"include_router\(\s*([\w.]+)(?:\s*,\s*prefix\s*=\s*['\"]([^'\"]*)['\"])?")


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def norm_path(path: str) -> str:
    """경로 변수 표기를 하나로 — :id · {id} · <int:id> → :p, 끝의 / 는 뺀다."""
    out = re.sub(r":[A-Za-z_]\w*|\{[^}]+\}|<[^>]+>", ":p", path or "/")
    out = re.sub(r"/{2,}", "/", out)
    return out.rstrip("/") or "/"


def env_reads(text: str, python: bool) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if python:
        for m in _ENV_PY.finditer(text):
            out.append((m.group(1), _line(text, m.start())))
    else:
        for m in _ENV_JS.finditer(text):
            out.append((m.group(1) or m.group(2), _line(text, m.start())))
        for m in _ENV_JS_DESTRUCT.finditer(text):
            for name in re.findall(r"\b([A-Z][A-Z0-9_]*)\b(?=\s*(?:[,:=}]|$))", m.group(1)):
                out.append((name, _line(text, m.start())))
    return out


def _js_routes(files: dict[str, str]) -> list[tuple[str, str, str, int]]:
    try:
        import runtime_check as rc
    except ImportError:  # pragma: no cover
        from core import runtime_check as rc  # type: ignore
    mounted: dict[str, list[str]] = {}
    for rel, text in files.items():
        imports = {}
        for m in rc._IMPORT.finditer(text):
            name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            target = rc._resolve(files, rel, spec)
            if target:
                imports[name] = target
        for prefix, name in rc._MOUNT.findall(text):
            if name in imports:
                mounted.setdefault(imports[name], []).append(prefix.rstrip("/"))
    out = []
    for rel, text in files.items():
        for m in rc._ROUTE.finditer(text):
            method, sub = m.group(1), m.group(2)
            for prefix in mounted.get(rel) or [""]:
                out.append((method.upper(), (prefix + ("" if sub == "/" else sub)) or "/", rel, _line(text, m.start())))
    return out


def _py_module_file(files: dict[str, str], importer: str, module: str) -> Optional[str]:
    """import 경로(app.routers.orders · .routers.orders) → 파일. 같은 서비스 안에서 끝부분이 맞는 파일을 고른다."""
    dots = len(module) - len(module.lstrip("."))
    path = module.lstrip(".").replace(".", "/")
    if dots:
        base = posixpath.dirname(importer)
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        cands = [posixpath.join(base, path) + ".py", posixpath.join(base, path, "__init__.py")]
        return next((c for c in cands if c in files), None)
    for rel in sorted(files, key=len):
        if rel.endswith("/" + path + ".py") or rel == path + ".py" or rel.endswith("/" + path + "/__init__.py"):
            return rel
    return None


def _py_routes(files: dict[str, str]) -> list[tuple[str, str, str, int]]:
    prefixes: dict[tuple[str, str], str] = {}        # (파일, 라우터 변수) → APIRouter(prefix)
    included: dict[tuple[str, str], list[str]] = {}  # (파일, 라우터 변수) → include_router(prefix) 들
    for rel, text in files.items():
        for name, args in _PY_ROUTER.findall(text):
            m = re.search(r"prefix\s*=\s*['\"]([^'\"]*)['\"]", args)
            prefixes[(rel, name)] = m.group(1) if m else ""
        modules: dict[str, str] = {}               # 별칭 → 모듈 파일 (orders → routers/orders.py)
        attrs: dict[str, tuple[str, str]] = {}     # 별칭 → (모듈 파일, 속성)
        for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import\s+\(?([^)\n]+)", text, re.M):
            for item in [x.strip() for x in m.group(2).split(",") if x.strip()]:
                name, _, alias = item.partition(" as ")
                name, alias = name.strip(), (alias.strip() or name.strip())
                sub = _py_module_file(files, rel, f"{m.group(1)}.{name}" if not m.group(1).endswith(".") else m.group(1) + name)
                if sub:
                    modules[alias] = sub
                    continue
                mod = _py_module_file(files, rel, m.group(1))
                if mod:
                    attrs[alias] = (mod, name)
        for m in re.finditer(r"^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?", text, re.M):
            mod = _py_module_file(files, rel, m.group(1))
            if mod:
                modules[m.group(2) or m.group(1).split(".")[-1]] = mod
        for ref, prefix in _PY_INCLUDE.findall(text):
            base, _, attr = ref.partition(".")
            if base in modules:
                key = (modules[base], attr or "router")
            elif base in attrs:
                key = attrs[base]
            else:
                key = (rel, ref)
            included.setdefault(key, []).append(prefix or "")
    out = []
    for rel, text in files.items():
        for m in _PY_DECOR.finditer(text):
            var, method, sub = m.group(1), m.group(2), m.group(3)
            own = prefixes.get((rel, var), "")
            for outer in included.get((rel, var)) or [""]:
                out.append((method.upper(), (outer + own + sub) or "/", rel, _line(text, m.start())))
        for m in _PY_ROUTE.finditer(text):
            methods = re.findall(r"['\"](\w+)['\"]", m.group(3) or "") or ["GET"]
            for method in methods:
                out.append((method.upper(), m.group(2), rel, _line(text, m.start())))
    return out


def _service_files(arch: dict, svc: dict, files: dict[str, str]) -> dict[str, str]:
    prefix = svc["path"] + "/"
    return {p: t for p, t in files.items() if p.startswith(prefix) and p.endswith(CODE_EXT) and not _SKIP.search(p)}


def check(arch: dict, files: dict[str, str]) -> dict:
    """{"findings": [...], "services": [{name, used_dbs, used_services, routes}], "edges": [{from, to, kind, status}]}"""
    findings: list[dict] = []
    services_out: list[dict] = []
    edges: list[dict] = []
    db_vars = {arch_mod.db_url_var(d["name"]): d["name"] for d in arch["databases"]}
    pw_vars = {arch_mod.password_var(d["name"]): d["name"] for d in arch["databases"]}
    svc_vars = {arch_mod.service_url_var(s["name"]): s["name"] for s in arch["services"]}
    host_names = [s["name"] for s in arch["services"]] + [d["name"] for d in arch["databases"]]
    host_re = re.compile(r"['\"`](?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://(?:[^'\"`@\s]*@)?"
                         r"(localhost|127\.0\.0\.1|" + "|".join(re.escape(h) for h in host_names) + r")(?::\d+)?[^'\"`]*['\"`]")

    def add(code, severity, svc, file, line, message, fix):
        findings.append({"code": code, "severity": severity, "service": svc, "file": file, "line": line,
                         "message": message, "fix": fix})

    for svc in arch["services"]:
        own = _service_files(arch, svc, files)
        name = svc["name"]
        if not own:
            add("CONTRACT_SERVICE_MISSING", "error", name, svc["path"], 0, f"계약의 서비스 {name} 코드가 없습니다({svc['path']}/)",
                f"{svc['path']}/ 에 서비스 코드를 만드세요.")
            services_out.append({"name": name, "used_dbs": [], "used_services": [], "routes": 0})
            continue
        used_dbs: dict[str, tuple[str, int]] = {}
        used_svcs: dict[str, tuple[str, int]] = {}
        for rel, text in own.items():
            for var, line in env_reads(text, rel.endswith(".py")):
                db = db_vars.get(var) or pw_vars.get(var)
                if db and db not in used_dbs:
                    used_dbs[db] = (rel, line)
                target = svc_vars.get(var)
                if target and target != name and target not in used_svcs:
                    used_svcs[target] = (rel, line)
            if svc["kind"] != "web":
                for m in host_re.finditer(text):
                    add("CONTRACT_HARDCODED_HOST", "warning", name, rel, _line(text, m.start()),
                        f"{name} 코드에 주소가 박혀 있습니다: {m.group(0)[1:-1][:80]}",
                        "주소는 계약의 환경 변수(<이름>_URL)로 읽으세요 — 배포 환경마다 주소가 다릅니다.")
        for db, (rel, line) in used_dbs.items():
            ok = db in svc["databases"]
            edges.append({"from": name, "to": db, "kind": "db", "status": "ok" if ok else "violation", "file": rel, "line": line})
            if not ok:
                owners = [s["name"] for s in arch["services"] if db in s["databases"]]
                add("CONTRACT_DB_FOREIGN", "error", name, rel, line,
                    f"{name} 가 계약상 {', '.join(owners) or '다른 서비스'} 의 DB {db} 에 직접 접속합니다",
                    f"{db} 의 데이터는 {', '.join(owners) or '그 서비스'} 의 API 로 받으세요(서비스마다 자기 DB).")
        for db in svc["databases"]:
            if db not in used_dbs:
                edges.append({"from": name, "to": db, "kind": "db", "status": "unused"})
                add("CONTRACT_DB_UNUSED", "warning", name, svc["path"], 0, f"계약상 {name} 가 쓰는 DB {db} 를 코드가 읽지 않습니다",
                    f"환경 변수 {arch_mod.db_url_var(db)} 로 접속하세요(또는 계약에서 빼세요).")
        for target, (rel, line) in used_svcs.items():
            ok = target in svc["depends_on"]
            edges.append({"from": name, "to": target, "kind": "service", "status": "ok" if ok else "violation", "file": rel, "line": line})
            if not ok:
                add("CONTRACT_CALL_UNDECLARED", "error", name, rel, line, f"{name} 가 계약에 없는 서비스 {target} 를 부릅니다",
                    f"계약의 depends_on 에 {target} 를 더하거나, 이미 의존하는 서비스를 거쳐 받으세요.")
        for dep in svc["depends_on"]:
            if dep not in used_svcs:
                edges.append({"from": name, "to": dep, "kind": "service", "status": "unused"})
                add("CONTRACT_DEP_UNUSED", "warning", name, svc["path"], 0, f"계약상 {name} → {dep} 호출이 코드에 없습니다",
                    f"환경 변수 {arch_mod.service_url_var(dep)} 로 부르세요(또는 계약에서 빼세요).")
        routes: list[tuple[str, str, str, int]] = []
        if svc["kind"] == "api":
            routes = _py_routes(own) if svc["language"] == "python" else _js_routes(own)
            have = {(m, norm_path(p)) for m, p, _, _ in routes}
            want = {(e["method"], norm_path(e["path"])) for e in arch["endpoints"] if e["service"] == name}
            for e in arch["endpoints"]:
                if e["service"] == name and (e["method"], norm_path(e["path"])) not in have:
                    add("CONTRACT_ENDPOINT_MISSING", "warning", name, svc["path"], 0,
                        f"계약 API {e['method']} {e['path']} 를 {name} 코드에서 찾지 못했습니다",
                        "계약의 경로·메서드를 그대로 구현하세요(동적으로 등록했다면 통합 테스트 결과를 보세요).")
            for method, path, rel, line in routes:
                key = (method, norm_path(path))
                if key not in want and norm_path(path) not in ("/health", "/"):
                    add("CONTRACT_ENDPOINT_EXTRA", "info", name, rel, line, f"계약에 없는 API {method} {path}",
                        "공개할 API 라면 계약에 더하세요(통합 테스트 대상이 됩니다).")
        services_out.append({"name": name, "used_dbs": sorted(used_dbs), "used_services": sorted(used_svcs),
                             "routes": len({(m, norm_path(p)) for m, p, _, _ in routes})})
    seen = set()
    unique = []
    for f in findings:
        key = (f["code"], f["service"], f["file"], f["line"], f["message"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    order = {"error": 0, "warning": 1, "info": 2}
    unique.sort(key=lambda f: (order[f["severity"]], f["service"], f["file"], f["line"]))
    return {"findings": unique, "services": services_out, "edges": edges,
            "summary": {k: sum(1 for f in unique if f["severity"] == k) for k in ("error", "warning", "info")}}


def issues(arch: dict, files: dict[str, str]) -> list[dict]:
    """생성 직후 점검용 — 오류만 고칠 문제로(경고는 결과에 남긴다)."""
    out = []
    for f in check(arch, files)["findings"]:
        if f["severity"] == "info":
            continue
        out.append({"code": f["code"], "severity": f["severity"], "file": f["file"] if f["line"] else f["file"],
                    "message": f["message"] + (f" ({f['file']}:{f['line']})" if f["line"] else ""), "fix": f["fix"]})
    return out


_SKIP_DIRS = {"node_modules", "dist", "build", "__pycache__", ".venv", "venv", "coverage", ".git", "test", "tests", "__tests__"}


def read_workspace(workspace: str | Path, arch: dict, limit: int = 2000) -> dict[str, str]:
    """서비스 폴더의 코드만 읽는다. node_modules 같은 폴더는 들어가지도 않고(큰 프로젝트에서 느려짐),
    심볼릭 링크는 따라가지 않는다(작업 폴더 밖 파일을 읽지 않게)."""
    root = Path(workspace)
    out: dict[str, str] = {}
    for svc in arch["services"]:
        base = root / svc["path"]
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if len(out) >= limit:
                    return out
                p = Path(dirpath) / name
                rel = p.relative_to(root).as_posix()
                if not rel.endswith(CODE_EXT) or _SKIP.search(rel) or p.is_symlink():
                    continue
                try:
                    out[rel] = p.read_text(encoding="utf-8", errors="replace")[:400_000]
                except OSError:
                    continue
    return out


def check_workspace(workspace: str | Path) -> dict:
    """코드 맵 화면용 — 계약이 없으면 available=False."""
    path = Path(workspace or "") / "architecture.json"
    try:
        arch, _ = arch_mod.normalize(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        arch = None
    if not arch:
        return {"available": False, "reason": "아키텍처 계약(architecture.json)이 없는 프로젝트입니다."}
    result = check(arch, read_workspace(workspace, arch))
    return {"available": True, "services": [{"name": s["name"], "kind": s["kind"], "language": s["language"],
                                             "databases": s["databases"], "depends_on": s["depends_on"]} for s in arch["services"]],
            "databases": [{"name": d["name"], "engine": d["engine"]} for d in arch["databases"]], **result}
