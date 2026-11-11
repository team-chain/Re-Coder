"""다중 서비스 실행 검증(3.1) — 생성한 프로젝트를 docker compose 로 **전부 함께** 띄우고 계약대로 동작하는지 본다.

runtime_check 는 앱 컨테이너 하나 + DB 를 띄워 API 를 불러 봤다. 서비스가 여럿이면 하나씩은 빌드·기동돼도 서로 못 찾거나
(주소·변수 이름 어긋남), 다른 서비스의 DB 테이블이 없거나, 게이트웨이를 거치면 500 이 나는 것이 함께 띄워야만 드러난다.

검사(순서대로, 실패해도 가능한 데까지 모두 본다)
    1. 빌드 — 서비스마다 Dockerfile 로 이미지를 만든다(실패하면 빌드 로그로 그 서비스 파일을 고친다).
    2. 기동 — compose `--wait` 로 DB healthcheck → 서비스 healthcheck 를 기다린다. 멈추거나 계속 재시작하는 서비스는 로그와 함께 실패.
    3. 연결 — 서비스 A 안에서 계약의 `B_URL` 로 B 의 /health 를 부른다(이름·포트·네트워크가 맞는가).
    4. 스키마 — 관계형 DB 에 계약의 테이블이 생겼는가(서비스가 시작할 때 만들어야 한다).
    5. 통합 테스트 — 계약에서 만든 경우(architecture.test_cases)를 실제 HTTP 로 보낸다.
    끝나면 컨테이너·볼륨·검증 이미지를 모두 지운다.

원칙
    · Docker·이미지 저장소에 닿지 못한 것은 코드 문제가 아니다 → "unavailable"(교정하지 않는다).
    · 비밀번호는 이번 실행만 쓰는 임의 값을 임시 파일로 넘기고, 작업 폴더에 쓰지 않는다.
    · 포트는 127.0.0.1 의 임의 포트로만 연다(사용자의 다른 앱과 겹치지 않게).
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import architecture as arch_mod
    import compose_kit
except ImportError:  # pragma: no cover - 패키지 실행
    from core import architecture as arch_mod  # type: ignore
    from core import compose_kit  # type: ignore

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
Runner = Callable[[list[str], int], subprocess.CompletedProcess]
Http = Callable[[str, str, Optional[dict]], tuple[Optional[int], str]]

_UNAVAILABLE = re.compile(r"(pull access denied|failed to resolve reference|manifest unknown|toomanyrequests|"
                          r"tls handshake|i/o timeout|no such host|connection refused.*docker|cannot connect to the docker daemon|"
                          r"error during connect|docker daemon is not running|403 forbidden|: forbidden)", re.I)
_BUILD_FAIL = re.compile(r"(failed to solve|error building|=> ERROR \[|target [\w-]+: failed|executor failed running)", re.I)


def enabled() -> bool:
    return os.environ.get("RECODER_COMPOSE_CHECK", "1") != "0" and os.environ.get("RECODER_TEST_MODE") != "1"


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False, creationflags=_FLAGS)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, str(exc.stdout or ""), f"시간 초과({timeout}초)")
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 — 3xx 는 그대로 상태로 본다
        return None


def _http(method: str, url: str, body: Optional[dict]) -> tuple[Optional[int], str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"} if data is not None else {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:  # noqa: S310 — 127.0.0.1 의 검증 컨테이너만
            return resp.status, resp.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(2000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return exc.code, text
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def _tail(text: str, lines: int = 40, limit: int = 4000) -> str:
    out = "\n".join(str(text or "").strip().splitlines()[-lines:])
    return out[-limit:]


def _ps(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except ValueError:
        rows = []
        for line in raw.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


def _build_service(arch: dict, log: str) -> str:
    """빌드 로그에서 실패한 서비스 이름."""
    names = [s["name"] for s in arch["services"]]
    for pattern in (r"target ([\w-]+): failed", r"ERROR \[([\w-]+) ", r"failed to solve.*?([\w-]+)/Dockerfile",
                    r"Service ([\w-]+)\s+Building.*?\n.*?ERROR"):
        for m in re.finditer(pattern, log, re.I | re.S):
            if m.group(1) in names:
                return m.group(1)
    found = [n for n in names if re.search(rf"\b{re.escape(n)}\b.*(error|failed)", log, re.I)]
    return found[0] if found else ""


def _source_files(workspace: Path, arch: dict, limit: int = 400) -> dict[str, str]:
    out: dict[str, str] = {}
    for svc in arch["services"]:
        root = workspace / svc["path"]
        for p in sorted(root.rglob("*")) if root.is_dir() else []:
            if len(out) >= limit:
                return out
            if p.is_file() and p.suffix in (".ts", ".js", ".mjs", ".cjs", ".py") and not ({"node_modules", "dist", "__pycache__"} & set(p.parts)):
                try:
                    out[p.relative_to(workspace).as_posix()] = p.read_text(encoding="utf-8", errors="replace")[:200_000]
                except OSError:
                    continue
    return out


def run(workspace: Path, arch: dict, *, timeout: int = 900, docker: Runner | None = None, http: Http | None = None,
        project: str = "", acceptance=None) -> dict:
    """{"kind": "compose", "status": passed|failed|unavailable, "passed", "services", "checks", "failures", "output"}"""
    docker = docker or _docker
    http = http or _http
    started = time.monotonic()
    result: dict[str, Any] = {"kind": "compose", "status": "unavailable", "passed": False, "services": [], "checks": [],
                              "failures": [], "output": ""}
    workspace = Path(workspace)
    compose_file = workspace / "docker-compose.yml"
    if not compose_file.is_file():
        result["output"] = "docker-compose.yml 이 없어 다중 서비스 검증을 하지 않았습니다."
        return result
    version = docker(["compose", "version"], 30)
    if version.returncode != 0:
        result["output"] = "Docker Compose 를 쓸 수 없어 다중 서비스 검증을 하지 않았습니다: " + _tail(version.stderr or version.stdout, 3)
        return result
    project = project or f"recoder-verify-{uuid.uuid4().hex[:10]}"
    with tempfile.TemporaryDirectory(prefix="recoder-compose-") as tmp:
        env_file = Path(tmp) / "verify.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in compose_kit.verify_env(arch).items()), encoding="utf-8")
        override = Path(tmp) / "verify.override.yml"
        override.write_text(compose_kit.verify_override(arch), encoding="utf-8")
        base = ["compose", "-p", project, "-f", str(compose_file), "-f", str(override), "--env-file", str(env_file)]
        try:
            _check(arch, base, docker, http, result, timeout, started, acceptance=acceptance,
                   files=_source_files(workspace, arch) if acceptance is not None else None)
        finally:
            down = docker([*base, "down", "-v", "--remove-orphans", "--rmi", "local", "--timeout", "5"], 300)
            if down.returncode != 0:
                result["cleanup"] = "검증 컨테이너 정리 실패: " + _tail(down.stderr or down.stdout, 3)
    result["seconds"] = round(time.monotonic() - started, 1)
    ok = sum(1 for c in result["checks"] if c["ok"])
    if result["status"] != "unavailable":
        result["status"] = "failed" if result["failures"] else "passed"
    result["passed"] = result["status"] == "passed"
    if result["status"] != "unavailable":
        result["output"] = (f"다중 서비스 검증 — 서비스 {len(arch['services'])}개 · DB {len(arch['databases'])}개, "
                            f"검사 {ok}/{len(result['checks'])} 통과"
                            + ("" if not result["failures"] else " · 실패: " + "; ".join(f["title"] for f in result["failures"][:6])))
    return result


def _fail(result: dict, kind: str, service: str, title: str, detail: str = "", logs: str = "", **extra: Any) -> None:
    result["failures"].append({"kind": kind, "service": service, "title": title, "detail": detail, "logs": logs, **extra})


def _check(arch: dict, base: list[str], docker: Runner, http: Http, result: dict, timeout: int, started: float,
           acceptance=None, files: Optional[dict] = None) -> None:
    wait = max(60, min(600, timeout // 2))
    up = docker([*base, "up", "-d", "--build", "--wait", "--wait-timeout", str(wait)], timeout)
    log = (up.stdout or "") + "\n" + (up.stderr or "")
    if up.returncode != 0 and _UNAVAILABLE.search(log) and not _BUILD_FAIL.search(log):
        result["status"] = "unavailable"
        result["output"] = "이미지 저장소·Docker 에 닿지 못해 다중 서비스 검증을 하지 못했습니다(코드 문제 아님): " + _tail(log, 4, 600)
        return
    result["status"] = "checked"
    if up.returncode != 0 and _BUILD_FAIL.search(log):
        svc = _build_service(arch, log)
        _fail(result, "build", svc, f"{svc or '서비스'} 이미지 빌드 실패", logs=_tail(log, 60, 6000))
        result["checks"].append({"name": f"build:{svc or '?'}", "ok": False})
        return
    inspect(arch, base, docker, http, result, require_ports=True, acceptance=acceptance, files=files)


def inspect(arch: dict, base: list[str], docker: Runner, http: Http, result: dict, *, require_ports: bool = False,
            acceptance=None, files: Optional[dict] = None) -> None:
    """이미 떠 있는 compose 프로젝트를 본다 — 상태·연결·스키마·통합 테스트(검증·배포가 함께 쓴다).

    require_ports=False(배포): 공개하지 않은 서비스는 포트가 없으므로 그 서비스의 HTTP 시험은 건너뛴다."""
    rows = {r.get("Service"): r for r in _ps(docker([*base, "ps", "-a", "--format", "json"], 60).stdout)}
    logs_cache: dict[str, str] = {}

    def logs(name: str) -> str:
        if name not in logs_cache:
            out = docker([*base, "logs", "--no-color", "--tail", "80", name], 60)
            logs_cache[name] = _tail((out.stdout or "") + (out.stderr or ""), 80, 6000)
        return logs_cache[name]

    healthy: set[str] = set()
    for db in arch["databases"]:
        row = rows.get(db["name"]) or {}
        ok = row.get("State") == "running" and row.get("Health") in ("healthy", "", None)
        result["services"].append({"name": db["name"], "kind": "db", "state": row.get("State"), "health": row.get("Health")})
        result["checks"].append({"name": f"db:{db['name']}", "ok": ok})
        if ok:
            healthy.add(db["name"])
        else:
            _fail(result, "db_down", db["name"], f"DB {db['name']} 가 준비되지 않음", f"상태 {row.get('State')}/{row.get('Health')}",
                  logs(db["name"]))
    for svc in arch["services"]:
        row = rows.get(svc["name"]) or {}
        state, health = row.get("State"), row.get("Health")
        ok = state == "running" and (health in ("healthy", "", None))
        result["services"].append({"name": svc["name"], "kind": svc["kind"], "state": state, "health": health})
        result["checks"].append({"name": f"service:{svc['name']}", "ok": ok})
        if ok:
            healthy.add(svc["name"])
            continue
        why = {"exited": f"종료됨(코드 {row.get('ExitCode')})", "restarting": "계속 재시작", "created": "시작 못 함"}.get(
            str(state), f"상태 {state}/{health}")
        if health == "unhealthy":
            why = f"{svc['health'] or '/'} 응답 실패(unhealthy)"
        _fail(result, "service_down", svc["name"], f"{svc['name']} {why}", logs=logs(svc["name"]))
    ports: dict[str, str] = {}
    for svc in arch["services"]:
        if svc["port"] and svc["name"] in healthy:
            out = docker([*base, "port", svc["name"], str(svc["port"])], 30)
            host = (out.stdout or "").strip().splitlines()[-1:] or [""]
            if re.fullmatch(r"[\d.]+:\d+", host[0].strip()):
                ports[svc["name"]] = host[0].strip()
            elif require_ports:
                #: 포트를 못 찾으면 통합 테스트를 건너뛴 채 "통과" 로 보이면 안 된다 — 검증 환경 문제로 남긴다.
                result["checks"].append({"name": f"port:{svc['name']}", "ok": False})
                _fail(result, "infra", "", f"{svc['name']} 검증 포트를 찾지 못함", _tail(out.stdout + out.stderr, 3, 300))
    for svc in arch["services"]:
        if svc["name"] not in healthy:
            continue
        for dep in svc["depends_on"]:
            target = arch_mod.service(arch, dep)
            if dep not in healthy or not target:
                continue
            var = arch_mod.service_url_var(dep)
            path = target["health"] or "/"
            if svc["language"] == "node" and svc["kind"] != "web":
                cmd = ["node", "-e", f"fetch(process.env.{var}+'{path}').then(r=>{{console.log(r.status);process.exit(r.status<500?0:1)}})"
                                     ".catch(e=>{console.log(String(e));process.exit(1)})"]
            elif svc["language"] == "python":
                cmd = ["python", "-c", "import os,urllib.request,urllib.error\ntry:\n r=urllib.request.urlopen(os.environ['" + var +
                       "']+'" + path + "',timeout=5);print(r.status)\nexcept urllib.error.HTTPError as e:\n print(e.code);"
                       "raise SystemExit(0 if e.code<500 else 1)\nexcept Exception as e:\n print(e);raise SystemExit(1)"]
            else:
                cmd = ["wget", "-q", "-O-", f"http://{dep}:{target['port']}{path}"]
            out = docker([*base, "exec", "-T", svc["name"], *cmd], 30)
            ok = out.returncode == 0
            result["checks"].append({"name": f"link:{svc['name']}->{dep}", "ok": ok})
            if not ok:
                _fail(result, "link", svc["name"], f"{svc['name']} → {dep} 연결 실패", _tail(out.stdout + out.stderr, 4, 400),
                      target=dep, var=var)
    for db in arch["databases"]:
        if not db["tables"] or db["name"] not in healthy:
            continue
        name = compose_kit.db_name(db["name"])
        if db["engine"] == "postgres":
            cmd = ["psql", "-U", compose_kit.DB_USER, "-d", name, "-Atc",
                   "select table_name from information_schema.tables where table_schema='public'"]
        else:
            cmd = ["sh", "-c", f'mysql -u {compose_kit.DB_USER} -p"$MYSQL_PASSWORD" -N -e "show tables" {name} 2>/dev/null']
        out = docker([*base, "exec", "-T", db["name"], *cmd], 30)
        have = {line.strip().lower() for line in (out.stdout or "").splitlines() if line.strip()}
        missing = [t for t in db["tables"] if t.lower() not in have]
        result["checks"].append({"name": f"schema:{db['name']}", "ok": out.returncode == 0 and not missing})
        if out.returncode != 0 or missing:
            owners = [s["name"] for s in arch["services"] if db["name"] in s["databases"]]
            _fail(result, "schema", owners[0] if owners else "", f"{db['name']} 테이블 없음: {', '.join(missing) or '조회 실패'}",
                  f"있는 테이블: {', '.join(sorted(have)) or '없음'}", db=db["name"], missing=missing)
    for case in arch_mod.test_cases(arch):
        host = ports.get(case["service"])
        if not host:
            continue
        status, text = http(case["method"], f"http://{host}{case['path']}", case.get("body"))
        ok = arch_mod.judge(case["expect"], status)
        result["checks"].append({"name": f"test:{case['id']}", "ok": ok, "status": status, "expect": case["expect"]})
        if not ok:
            what = "연결 실패" if status is None else str(status)
            _fail(result, "test", case["service"], f"{case['method']} {case['service']}{case['path']} → {what} (기대 {case['expect']})",
                  f"{case['why']} · 응답: {text[:300]}", case=case, status=status)
            if status is not None and status >= 500:
                result["failures"][-1]["logs"] = logs(case["service"])
    if acceptance is not None and ports:
        _acceptance(arch, ports, acceptance, files or {}, result)


def _endpoint_service(arch: dict, method: str, path: str) -> Optional[str]:
    bare = path.split("?", 1)[0]
    for e in arch["endpoints"]:
        segs = ["[^/]+" if seg.startswith((":", "{")) else re.escape(seg) for seg in e["path"].split("/")]
        pattern = "^" + "/".join(segs) + "/?$"
        if e["method"] == method and re.match(pattern, bare):
            return e["service"]
    return None


def _acceptance(arch: dict, ports: dict[str, str], acceptance, files: dict[str, str], result: dict) -> None:
    """명세 하네스(테스트 리코더) — 승인한 합격 조건을 함께 띄운 서비스들에 시험한다.

    요청은 계약의 API 표로 그 API 를 가진 서비스에 보낸다(없으면 사용자가 접속하는 공개 API 로)."""
    try:
        import runtime_check
        import code_test
    except ImportError:  # pragma: no cover
        from core import runtime_check, code_test  # type: ignore
    entry = next((s["name"] for s in arch["services"] if s["public"] and s["kind"] == "api" and s["name"] in ports), None) \
        or next((s["name"] for s in arch["services"] if s["kind"] == "api" and s["name"] in ports), None)
    routes = []
    for e in arch["endpoints"]:
        svc = arch_mod.service(arch, e["service"])
        if svc and e["service"] in ports:
            routes.append((e["method"], e["path"], _route_file(svc, e["path"], files) or _entry(svc, files)))

    def request(method, path, body=None, token=""):
        target = _endpoint_service(arch, method, path) or entry
        if not target or target not in ports:
            return None, None
        return runtime_check._request(f"http://{ports[target]}", method, path, body, token)

    try:
        outcome = acceptance.execute(request, routes, files, None)
    except Exception as exc:  # noqa: BLE001 — 시험 자체가 깨져도 검증 판정을 망가뜨리지 않는다
        result["acceptance_error"] = str(exc)[:300]
        return
    result["acceptance"] = {k: outcome.get(k) for k in ("passed", "failed", "unverifiable", "total")}
    result["checks"].append({"name": "acceptance", "ok": not outcome.get("failed"),
                             "passed": outcome.get("passed"), "total": outcome.get("total")})
    for f in code_test.failures(outcome):
        owned = [p for p in f.get("files") or [] if arch_mod.service_of(arch, p)]
        svc = arch_mod.service_of(arch, owned[0]) if owned else arch_mod.service(arch, entry or "")
        _fail(result, "acceptance", svc["name"] if svc else "", f"합격 조건 {f['criterion']} 실패 — {f.get('title') or f['request']}",
              f.get("message") or "", logs="\n".join(f.get("lines") or []), files=owned, criterion=f["criterion"])


# ── 실패 → 고칠 파일 ─────────────────────────────────────────────────────────

_ENTRY = ("src/index.ts", "src/server.ts", "src/app.ts", "src/main.ts", "src/index.js", "src/server.js", "src/app.js",
          "index.js", "server.js", "app.js", "main.js", "app/main.py", "main.py", "app/worker.py", "worker.py", "app/__init__.py")


def _entry(svc: dict, files: dict[str, str]) -> str:
    pkg = files.get(f"{svc['path']}/package.json")
    if pkg:
        try:
            data = json.loads(pkg)
            main = str(data.get("main") or "")
            start = str((data.get("scripts") or {}).get("start") or "")
            m = re.search(r"node\s+(?:--\S+\s+)*([\w./-]+\.(?:m?js|cjs|ts))", start)
            for cand in filter(None, [main, m.group(1) if m else ""]):
                cand = posixpath.normpath(cand)
                for guess in (cand, re.sub(r"^dist/", "src/", cand).replace(".js", ".ts"), re.sub(r"^dist/", "src/", cand)):
                    if f"{svc['path']}/{guess}" in files:
                        return f"{svc['path']}/{guess}"
        except ValueError:
            pass
    if svc["language"] == "python" and svc["start"]:
        mod = next((p for p in svc["start"] if re.fullmatch(r"[\w.]+:[\w]+", p)), "")
        if mod:
            cand = f"{svc['path']}/{mod.split(':')[0].replace('.', '/')}.py"
            if cand in files:
                return cand
    for rel in _ENTRY:
        if f"{svc['path']}/{rel}" in files:
            return f"{svc['path']}/{rel}"
    own = sorted(p for p in files if p.startswith(svc["path"] + "/") and p.endswith((".ts", ".js", ".py", ".mjs")))
    return own[0] if own else (f"{svc['path']}/package.json" if svc["language"] == "node" else f"{svc['path']}/requirements.txt")


def _from_logs(svc: dict, logs: str, files: dict[str, str]) -> list[str]:
    found: list[str] = []
    pattern = r'(?:/app/|File "/app/|(?<![\w/.-])(?=(?:src|app|lib|routes|services|db)/))([\w./-]+\.(?:ts|tsx|js|mjs|cjs|py))'
    for m in re.finditer(pattern, logs or ""):
        rel = m.group(1)
        for cand in (rel, re.sub(r"^dist/", "src/", rel).replace(".js", ".ts"), re.sub(r"^dist/", "src/", rel)):
            path = f"{svc['path']}/{cand}"
            if path in files and path not in found and "node_modules" not in path:
                found.append(path)
                break
    return found


def _route_file(svc: dict, path: str, files: dict[str, str]) -> str:
    """API 경로 글자가 들어 있는 파일(그 라우트를 정의한 곳)."""
    parts = [p for p in path.split("/") if p and not p.startswith(":") and not p.startswith("{")]
    tail = "/" + "/".join(parts[-2:]) if parts else path
    for p, text in sorted(files.items()):
        if p.startswith(svc["path"] + "/") and p.endswith((".ts", ".js", ".py", ".mjs")) and (path in text or tail in text):
            return p
    return ""


def issues(result: dict, arch: dict, files: dict[str, str]) -> list[dict]:
    """compose 검증 실패 → 파일별 고칠 문제(교정 루프가 쓰는 모양). 고정 파일(compose·Dockerfile)은 지목하지 않는다."""
    out: list[dict] = []
    if not isinstance(result, dict) or result.get("status") != "failed":
        return out
    for f in result.get("failures") or []:
        svc = arch_mod.service(arch, f.get("service") or "")
        if f["kind"] == "db_down" or not svc:
            out.append({"code": "COMPOSE_DB_DOWN" if f["kind"] == "db_down" else "COMPOSE_FAILED", "severity": "warning",
                        "file": "docker-compose.yml", "message": f["title"] + (f"\n{f['logs'][-800:]}" if f.get("logs") else ""),
                        "fix": "Docker 자원(메모리·디스크)과 DB 로그를 확인하세요."})
            continue
        logs = f.get("logs") or ""
        blamed = _from_logs(svc, logs, files)
        if f["kind"] == "test":
            route = _route_file(svc, f["case"]["path"], files)
            blamed = blamed or ([route] if route else [])
        if f["kind"] == "acceptance":
            blamed = [p for p in f.get("files") or [] if p in files] + blamed
        target = (blamed or [_entry(svc, files)])[0]
        if compose_kit.is_fixed_path(arch, target):
            target = _entry(svc, files)
        message = f["title"] + (f" — {f['detail']}" if f.get("detail") else "") + (f"\n서비스 로그:\n{logs[-1500:]}" if logs else "")
        fix = {
            "build": "빌드 로그의 오류를 고치세요(package.json scripts·의존성·import 경로). Dockerfile 은 ReCoder 가 관리합니다.",
            "service_down": f"서비스가 0.0.0.0:{svc['port'] or 'PORT'} 에서 뜨고 계약의 환경 변수({', '.join(arch_mod.env_for(arch, svc))})"
                            f"만으로 시작하게 고치세요. DB 가 늦게 준비돼도 재시도하고, /health 는 DB 연결 후 200.",
            "link": f"다른 서비스는 환경 변수 {f.get('var', '')} 로 부르세요(주소를 코드에 쓰지 않기).",
            "schema": f"서비스가 시작할 때 테이블 {', '.join(f.get('missing') or [])} 를 만들게 하세요(CREATE TABLE IF NOT EXISTS 또는 마이그레이션).",
            "acceptance": "승인한 합격 조건을 만족하도록 기능을 고치세요. 시험을 통과시키려고 검사·인증을 끄거나 값을 하드코딩하지 마세요.",
            "test": ("로그인 토큰이 없으면 401 을 돌려주세요(인증 미들웨어)." if "401" in str((f.get("case") or {}).get("expect")) else
                     "계약의 API 경로·메서드를 그대로 구현하고 서버 오류를 고치세요(입력 검증은 4xx)."),
        }.get(f["kind"], "서비스 로그의 원인을 고치세요.")
        out.append({"code": {"build": "COMPOSE_BUILD_FAILED", "service_down": "COMPOSE_SERVICE_DOWN", "link": "COMPOSE_LINK_FAILED",
                             "schema": "COMPOSE_SCHEMA_MISSING", "test": "CONTRACT_TEST_FAILED",
                             "acceptance": "ACCEPTANCE_FAILED"}.get(f["kind"], "COMPOSE_FAILED"),
                    "severity": "error", "file": target, "message": message, "fix": fix,
                    "request": f.get("title")})
    return out
