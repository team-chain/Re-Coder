"""생성 직후 실행 점검 — 빌드한 이미지를 ECS(staging)와 같은 구성(DB·모의 결제·시드)으로 잠깐 띄워 사용자 흐름의 API 를 불러 본다.

왜: 빌드 검증은 "컴파일된다" 까지만 본다. 실기기 생성 결과들은 빌드는 통과했지만 상품 목록 SQL 의 $ 번호가 어긋나 500,
주문 함수 인자 누락으로 401, 등 **실행해야 드러나는** 오류로 쇼핑몰이 동작하지 않았다(2.0.16 cdf7·c395). 그 상태로 Local
Docker·ECS 에 배포하면 배포는 "성공" 인데 화면은 오류다. 여기서 5xx·기동 실패를 잡아 서버 로그와 함께 AI 교정에 넘긴다.

원칙
- 4xx 는 오류로 보지 않는다(요청 모양을 추측하므로 검증 실패·인증 필요는 정상일 수 있다). 5xx·컨테이너 종료만 오류.
- 외부 서비스 키가 꼭 필요한 앱(ECS 계획이 막음)은 점검하지 않는다(skipped).
- 컨테이너는 끝나면 모두 지운다. 볼륨을 만들지 않는다.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PASSWORD = "Passw0rd!23"


def enabled() -> bool:
    return os.environ.get("RECODER_RUNTIME_CHECK", "1") != "0" and os.environ.get("RECODER_TEST_MODE") != "1"


def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False, creationflags=_FLAGS)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


# ── 경로 표(Express) ─────────────────────────────────────────────────────────

_MOUNT = re.compile(r"""\b(?:app|router|api|server)\.use\(\s*['"`](/[^'"`]*)['"`]\s*,\s*(?:[\w$.]+\s*,\s*)*([A-Za-z_$][\w$]*)\s*[,)]""")
_ROUTE = re.compile(r"""\b(?:router|app|r|routes|api)\.(get|post|put|patch|delete)\(\s*['"`](/[^'"`]*)['"`]""")
_IMPORT = re.compile(r"""import\s+([A-Za-z_$][\w$]*)\s+from\s*['"](\.{1,2}/[^'"]+)['"]|"""
                     r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")


def _server_files(files: dict[str, str], server: str) -> dict[str, str]:
    prefix = (server.strip("/") + "/") if server and server != "." else ""
    return {p: t for p, t in files.items()
            if p.startswith(prefix) and p.endswith((".ts", ".js", ".mjs", ".cjs")) and not p.endswith(".d.ts")
            and not re.search(r"(^|/)(node_modules|dist|build|tests?|__tests__)/", p)}


def _resolve(files: dict[str, str], rel: str, spec: str) -> Optional[str]:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
    stem = re.sub(r"\.(?:m?js|cjs|ts)$", "", base)
    for cand in (base, stem + ".ts", stem + ".js", stem + ".mjs", stem + "/index.ts", stem + "/index.js"):
        if cand in files:
            return cand
    return None


def route_table(files: dict[str, str], server: str) -> list[tuple[str, str, str]]:
    """[(METHOD, 전체 경로, 정의한 파일)] — app.use(prefix, router) 와 router.get(sub) 를 이어 붙인다."""
    srv = _server_files(files, server)
    routes: list[tuple[str, str, str]] = []
    mounted: dict[str, list[str]] = {}
    for rel, text in srv.items():
        imports = {}
        for m in _IMPORT.finditer(text):
            name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            target = _resolve(srv, rel, spec)
            if target:
                imports[name] = target
        for prefix, name in _MOUNT.findall(text):
            if name in imports:
                mounted.setdefault(imports[name], []).append(prefix.rstrip("/"))
    for rel, text in srv.items():
        prefixes = mounted.get(rel) or [""]
        for method, sub in _ROUTE.findall(text):
            for prefix in prefixes:
                full = (prefix + ("" if sub == "/" else sub)) or "/"
                if full.startswith("/api") or prefix:
                    routes.append((method.upper(), full, rel))
    return list(dict.fromkeys(routes))


# ── 요청 ───────────────────────────────────────────────────────────────────

def _request(base: str, method: str, path: str, body: Optional[dict] = None, token: str = "") -> tuple[Optional[int], object]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(200_000)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(20_000) if exc.fp else b""
        status = exc.code
    except Exception:  # noqa: BLE001 - 연결 실패
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        return status, None


def _find_key(obj, keys: tuple[str, ...], depth: int = 0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), (str, int)) and obj.get(k) not in ("", None):
                return obj[k]
        for v in obj.values():
            found = _find_key(v, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj[:3]:
            found = _find_key(v, keys, depth + 1)
            if found is not None:
                return found
    return None


def _first_id(obj):
    """목록 응답에서 첫 항목의 id."""
    items = obj
    if isinstance(obj, dict):
        for key in ("data", "items", "products", "menus", "menu", "results", "rows", "list", "content"):
            if isinstance(obj.get(key), (list, dict)):
                return _first_id(obj[key])
        return obj.get("id") if isinstance(obj.get("id"), (int, str)) else None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("id") or items[0].get("_id") or items[0].get("uuid")
    return None


def scenario(base: str, routes: list[tuple[str, str, str]], new_logs=None) -> list[dict]:
    """사용자 흐름: 목록 → 상세 → 가입 → 로그인 → (로그인 뒤) 목록 → 장바구니 담기 → 주문. 결과 [{method, path, status, file, logs?}].
    new_logs() 는 직전 호출 뒤로 앱이 남긴 로그 줄 — 실패한 요청에 그 요청 때의 로그만 붙인다."""
    results: list[dict] = []

    def hit(method, path, file, body=None, token=""):
        if new_logs:
            new_logs()
        status, data = _request(base, method, path, body, token)
        item = {"method": method, "path": path, "status": status, "file": file}
        if new_logs and (status is None or status >= 500):
            time.sleep(0.5)
            item["logs"] = new_logs()
        results.append(item)
        return status, data

    plain_gets = [(p, f) for m, p, f in routes if m == "GET" and ":" not in p and p.startswith("/api")
                  and not re.search(r"webhook|health|logout|admin|export|download|stream|events", p, re.I)]
    item_id = None
    for path, file in plain_gets[:8]:
        status, data = hit("GET", path, file)
        if status == 429:
            return results
        if item_id is None and status and status < 300 and re.search(r"product|menu|item|goods|post|book", path, re.I):
            item_id = _first_id(data)
    if item_id is not None:
        for m, p, f in routes:
            if m == "GET" and re.fullmatch(r"/api/[\w-]+/:[\w]+", p) and re.search(r"product|menu|item|goods|book", p, re.I):
                hit("GET", re.sub(r":\w+", str(item_id), p), f)
                break
    register = next(((p, f) for m, p, f in routes if m == "POST" and re.search(r"/(register|signup|sign-up|join)$", p, re.I)), None)
    login = next(((p, f) for m, p, f in routes if m == "POST" and re.search(r"/(login|signin|sign-in)$", p, re.I)), None)
    token = ""
    email = f"recoder{uuid.uuid4().hex[:8]}@example.com"
    if register:
        body = {"email": email, "password": _PASSWORD, "passwordConfirm": _PASSWORD, "confirmPassword": _PASSWORD,
                "password_confirmation": _PASSWORD, "name": "테스트", "username": "tester" + uuid.uuid4().hex[:6],
                "phone": "01012345678", "agreeTerms": True}
        status, data = hit("POST", register[0], register[1], body)
        token = str(_find_key(data, ("token", "accessToken", "access_token", "jwt")) or "") if status and status < 300 else ""
    if login and not token:
        status, data = hit("POST", login[0], login[1], {"email": email, "password": _PASSWORD})
        token = str(_find_key(data, ("token", "accessToken", "access_token", "jwt")) or "") if status and status < 300 else ""
    if not token:
        return results
    for path, file in plain_gets[:8]:
        status, _ = hit("GET", path, file, token=token)
        if status == 429:
            return results
    if item_id is not None:
        cart = next(((p, f) for m, p, f in routes if m == "POST" and re.fullmatch(r"/api/(?:cart|carts|basket)(?:/items?)?", p, re.I)), None)
        if cart:
            hit("POST", cart[0], cart[1], {"productId": item_id, "product_id": item_id, "menuItemId": item_id,
                                           "menu_item_id": item_id, "itemId": item_id, "quantity": 1}, token)
        order = next(((p, f) for m, p, f in routes if m == "POST" and re.fullmatch(r"/api/orders?", p, re.I)), None)
        if order:
            line = {"productId": item_id, "product_id": item_id, "menuItemId": item_id, "menu_item_id": item_id, "quantity": 1}
            hit("POST", order[0], order[1], {"items": [line], "shippingAddress": "서울시 강남구 1", "shipping_address": "서울시 강남구 1",
                                             "address": "서울시 강남구 1", "paymentMethod": "card", "payment_method": "card",
                                             "idempotency_key": uuid.uuid4().hex}, token)
    return results


# ── 로그 → 원인 파일 ─────────────────────────────────────────────────────────

_STACK = re.compile(r"(?:file://)?/app/(?:[\w.-]+/)*?((?:dist|build|src)/[\w./-]+\.(?:js|mjs|cjs|ts)):\d+")


def source_files(lines: list[str], files: dict[str, str], server: str) -> list[str]:
    """스택의 빌드 결과 경로(dist/services/x.js) → 프로젝트의 소스 파일(backend/src/services/x.ts)."""
    out: list[str] = []
    prefix = (server.strip("/") + "/") if server and server != "." else ""
    for line in lines:
        for built in _STACK.findall(line):
            stem = re.sub(r"\.(?:m?js|cjs|ts)$", "", re.sub(r"^(?:dist|build)/", "", built))
            for cand in (f"{prefix}src/{stem}.ts", f"{prefix}{stem}.ts", f"{prefix}src/{stem}.js", f"{prefix}{stem}.js",
                         f"{prefix}{stem}.mjs", f"{prefix}src/{stem}.mjs"):
                if cand in files and cand not in out:
                    out.append(cand)
                    break
    return out


_NOISE = re.compile(r"^\s*[\w$]+:\s*(?:undefined|null|true|false|-?\d+|'[^']*'|\[\]|\{\})?,?\s*$|^\s*[}\]],?\s*$|^\s*\{\s*$")


def useful_lines(lines: list[str], limit: int = 14) -> list[str]:
    """오류 객체 덤프(`detail: undefined,` 같은 줄)를 빼고 원인 문장·스택 위주로."""
    keep = list(dict.fromkeys(l.rstrip() for l in lines if l.strip() and not _NOISE.match(l)))
    important = [l for l in keep if re.search(r"error|exception|fail|cannot|undefined is|not a function|denied|violat|does not exist|invalid|\bat\s|"
                                              r"\bquery\b|\b(?:SELECT|INSERT|UPDATE|DELETE)\s", l, re.I)]
    return (important or keep)[-limit:]


def _related(file: str, files: dict[str, str]) -> list[str]:
    """라우트 파일이 부르는 서비스·모델 파일(같은 이름 줄기)."""
    text = files.get(file, "")
    out = []
    for spec in re.findall(r"""from\s*['"](\.{1,2}/[^'"]+)['"]""", text):
        target = _resolve(files, file, spec)
        if target and re.search(r"(service|model|repositor|controller|db|queries)", target, re.I) and target not in out:
            out.append(target)
    return out[:3]


# ── 실행 ───────────────────────────────────────────────────────────────────

def run(workspace: Path, image: str, *, timeout: int = 180) -> dict:
    import deploy_settings
    """{"status": passed|failed|skipped|unavailable, "checked": [...], "failures": [...], "note": str}."""
    started = time.monotonic()
    try:
        import docker_kit
        from build_readiness import ProjectFiles
        try:
            from agents import ecs_runtime  # type: ignore
        except ImportError:  # pragma: no cover
            from core.agents import ecs_runtime  # type: ignore
        info = docker_kit.layout(ProjectFiles(workspace))
    except Exception as exc:  # noqa: BLE001
        return {"status": "skipped", "note": f"구조를 확인하지 못했습니다: {exc}"}
    if not info:
        return {"status": "skipped", "note": "ReCoder 검증 Dockerfile 구조가 아니라 실행 점검을 하지 않았습니다."}
    port = int(info.get("port") or 3000)
    health = info.get("health") or "/"
    files: dict[str, str] = {}
    for p in workspace.rglob("*"):
        if p.is_file() and p.suffix in (".ts", ".js", ".mjs", ".cjs") and "node_modules" not in p.parts:
            try:
                files[p.relative_to(workspace).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    server = info.get("server") or "."
    try:
        plan = ecs_runtime.plan(str(workspace), key="recoder-runtime-check", environment="staging", provided=set(),
                                container_port=port)
    except Exception as exc:  # noqa: BLE001
        return {"status": "skipped", "note": f"실행 설정을 정하지 못했습니다: {exc}"}
    if plan.blocked:
        return {"status": "skipped", "note": plan.block_message()[0]}
    tag = f"recoder-rc-{uuid.uuid4().hex[:8]}"
    created: list[str] = []
    try:
        pause = _docker(["run", "-d", "--name", f"{tag}-net", "-p", f"127.0.0.1::{port}", "--entrypoint", "sleep",
                         image, "3600"], 60)
        if pause.returncode != 0:
            return {"status": "unavailable", "note": (pause.stderr or "")[:300]}
        created.append(f"{tag}-net")
        mapped = _docker(["port", f"{tag}-net", f"{port}/tcp"], 30).stdout.strip().splitlines()
        host_port = mapped[0].rsplit(":", 1)[-1] if mapped else ""
        if not host_port.isdigit():
            return {"status": "unavailable", "note": "포트를 연결하지 못했습니다."}
        sidecars = [dict(c, image=image if c.get("image") == ecs_runtime.APP_IMAGE else c.get("image")) for c in plan.sidecars]

        def start(c: dict, extra_env: Optional[dict] = None) -> bool:
            name = f"{tag}-{c['name']}"
            args = ["run", "-d", "--name", name, "--network", f"container:{tag}-net"]
            for e in c.get("environment", []) or []:
                args += ["-e", f"{e['name']}={e['value']}"]
            for k, v in (extra_env or {}).items():
                args += ["-e", f"{k}={v}"]
            if c.get("workingDirectory"):
                args += ["-w", c["workingDirectory"]]
            ep = c.get("entryPoint") or []
            if ep:
                args += ["--entrypoint", ep[0], c["image"], *ep[1:], *(c.get("command") or [])]
            else:
                args += [c["image"], *(c.get("command") or [])]
            done = _docker(args, 600)
            if done.returncode == 0:
                created.append(name)
            return done.returncode == 0

        for c in [s for s in sidecars if s.get("essential", True)]:
            if not start(c):
                return {"status": "unavailable", "note": f"{c['name']} 컨테이너를 시작하지 못했습니다(이미지를 내려받지 못했을 수 있음)."}
            hc = (c.get("healthCheck") or {}).get("command")
            if hc:
                shell = hc[1] if hc[0] == "CMD-SHELL" else " ".join(hc[1:])
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and _docker(["exec", f"{tag}-{c['name']}", "sh", "-c", shell], 20).returncode != 0:
                    time.sleep(2)
        seed = next((s for s in sidecars if not s.get("essential", True)), None)
        if seed and start(seed, plan.env):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and _docker(["inspect", "-f", "{{.State.Running}}", f"{tag}-{seed['name']}"], 20).stdout.strip() == "true":
                time.sleep(2)
        app_env = {"PORT": str(port), **plan.env}
        base = f"http://127.0.0.1:{host_port}"
        logs_of = lambda: (lambda d: (d.stdout or "") + "\n" + (d.stderr or ""))(_docker(["logs", "--tail", "200", f"{tag}-app"], 30))  # noqa: E731
        config_missing: list[str] = []
        for attempt in range(2):
            if not start({"name": "app", "image": image}, app_env):
                return {"status": "unavailable", "note": "앱 컨테이너를 시작하지 못했습니다."}
            up = False
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                status, _ = _request(base, "GET", health)
                if status is not None and status < 500:
                    up = True
                    break
                if _docker(["inspect", "-f", "{{.State.Running}}", f"{tag}-app"], 20).stdout.strip() != "true":
                    break
                time.sleep(2)
            if up:
                break
            #: 앱이 "X is required" 로 시작을 거부했다 — 코드 문제가 아니라 설정이 빠진 것이다(실기기 2.0.21 d22de705:
            #: getEnv('JWT_SECRET') 를 못 읽어 값을 안 넣었고, AI 가 6회 엉뚱한 곳을 고쳤다). 만들 수 있는 값은 만들어 한 번 더.
            absent = [n for n in missing_env(logs_of()) if n not in app_env]
            makeable = [n for n in absent if deploy_settings.kind_of(n) == "generate"]
            config_missing = [n for n in absent if n not in makeable]
            if attempt or not makeable:
                break
            for name in makeable:
                app_env[name] = secrets.token_hex(32)
            _docker(["rm", "-f", f"{tag}-app"], 60)
            created.remove(f"{tag}-app")
        if not up:
            lines = [l for l in logs_of().splitlines() if l.strip()][-25:]
            if config_missing:
                return {"status": "failed", "checked": [], "seconds": round(time.monotonic() - started),
                        "config_missing": config_missing,
                        "failures": [{"request": f"앱 시작({health})", "status": None, "lines": lines, "files": [],
                                      "config_missing": config_missing}]}
            entry = f"{server}/{info.get('entry')}" if server != "." else str(info.get("entry"))
            files_hit = source_files(lines, files, server)
            return {"status": "failed", "checked": [], "seconds": round(time.monotonic() - started),
                    "failures": [{"request": f"앱 시작({health})", "status": None, "lines": lines,
                                  "files": files_hit or [f for f in files if f.startswith(server.strip('/') + '/src/') and re.search(r"(server|index|app|main)\.[tj]s$", f)][:2] or [entry]}]}
        seen = {"n": len(logs_of().splitlines())}

        def new_logs() -> list[str]:
            lines = logs_of().splitlines()
            fresh, seen["n"] = lines[seen["n"]:], len(lines)
            return fresh

        checked = scenario(base, route_table(files, server), new_logs)
        failures = []
        done_requests: set[str] = set()
        for item in checked:
            if item["status"] is not None and item["status"] < 500:
                continue
            request = item['method'] + ' ' + re.sub(r'/\d+(?=/|$)', '/:id', item['path'])
            if request in done_requests:
                continue
            done_requests.add(request)
            log_lines = [l for l in item.get("logs") or [] if l.strip()]
            key = useful_lines(log_lines)
            hit_files = source_files(log_lines, files, server)
            route_file = item.get("file")
            target = list(dict.fromkeys(hit_files + ([route_file] + _related(route_file, files) if route_file else [])))
            #: DB 접속 도우미(query 래퍼)는 스택 맨 위에 늘 있지만 원인은 그것을 부른 쪽이다 — 다른 파일이 있으면 뺀다.
            helpers = [f for f in target if re.search(r"(^|/)(config|db|database|lib|utils?)/(database|db|index|pool|client|query)\.[tj]s$", f)]
            if len(target) > len(helpers):
                target = [f for f in target if f not in helpers]
            failures.append({"request": f"{item['method']} {item['path']}", "status": item["status"],
                             "lines": key, "files": target[:3]})
        for item in checked:
            item.pop("logs", None)
        return {"status": "failed" if failures else "passed", "checked": checked, "failures": failures,
                "seconds": round(time.monotonic() - started)}
    finally:
        for name in reversed(created):
            _docker(["rm", "-f", name], 60)


_MISSING_ENV = re.compile(
    r"(?i:environment variable|env var(?:iable)?|missing(?: required)?(?: env(?:ironment)?(?: variables?)?)?|required env(?:ironment)?(?: variables?)?)"
    r"[\s:'\"`]+([A-Z][A-Z0-9_]{2,63}(?:\s*,\s*[A-Z][A-Z0-9_]{2,63})*)\b"
    r"|\b([A-Z][A-Z0-9_]*_[A-Z0-9_]+|[A-Z]{3,})\b['\"`]?\s*(?i:is required|is missing|is not set|must be set|not set|required"
    r"|(?:이|가|이\(가\))\s*(?:없습니다|필요합니다|설정되지 않았습니다))")


def missing_env(log: str) -> list[str]:
    """기동 로그에서 "없어서 시작을 못 한" 환경변수 이름(중복 없이)."""
    names: list[str] = []
    for m in _MISSING_ENV.finditer(log or ""):
        for part in re.split(r"[,\s]+", m.group(1) or m.group(2) or ""):
            if part and part not in names and part not in ("PORT", "NODE_ENV", "ERROR", "WARN", "INFO"):
                names.append(part)
    return names


def issues(result: dict) -> list[dict]:
    """실행 점검 실패 → 파일별 고칠 문제(AI 교정 입력). 설정 누락은 코드 문제로 넘기지 않는다."""
    out = []
    for f in (result or {}).get("failures") or []:
        if f.get("config_missing"):
            names = ", ".join(f["config_missing"])
            out.append({
                "code": "GENERATED_RUNTIME_CONFIG", "severity": "warning", "file": ".env.example",
                "message": f"실행 점검: 앱이 필요한 설정({names})이 없어 시작하지 않았습니다. 코드 문제가 아니라 배포할 때 넣을 값입니다.",
                "fix": f"배포 화면의 설정에서 {names} 값을 넣으세요.",
            })
            continue
        lines = "\n".join(f.get("lines") or [])[:1500]
        status = f.get("status")
        what = (f"{f['request']} 요청이 {status} 오류를 냈습니다" if status else f"{f['request']} 에서 앱이 뜨지 않았습니다(컨테이너 종료)")
        for file in (f.get("files") or [])[:3]:
            out.append({
                "code": "GENERATED_RUNTIME_ERROR", "severity": "error", "file": file,
                "message": f"실행 점검: {what}. 서버 로그:\n{lines}",
                "fix": "로그의 원인을 이 파일에서 고치세요(SQL 자리표시자 번호·인자 수·없는 테이블/열·응답 형식 등). "
                       "검사·인증을 끄거나 오류를 삼켜 200 을 돌려주지 마세요.",
            })
    return out
