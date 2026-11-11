"""여러 서비스로 된 프로젝트의 로컬 배포(3.1) — 보안 게이트 → 빌드 → 이미지 검사 → 기동 → 검증 → 기록, 실패하면 되돌린다.

단일 앱 로컬 배포(컨테이너 하나 + 동반 DB)와 같은 약속을 지킨다.
    · 사람이 승인한 뒤에만 실행한다(호출하는 라우트가 승인을 확인한다).
    · 보안 게이트: 비밀값(Gitleaks)·서비스별 Dockerfile(안전 검사 + Hadolint)·서비스별 이미지(Trivy). 심각(CRITICAL)이면
      **기존에 돌던 서비스를 건드리기 전에** 멈춘다. 검사를 못 돌린 것은 "미검증" 으로 남기고 통과로 세지 않는다.
    · 롤백: 배포 전에 지금 돌고 있는 서비스 이미지를 `recoder-rb` 태그로 보관한다. 새 버전이 뜨지 않으면 자동으로
      직전 버전으로 되돌리고, 나중에 사용자가 승인해 되돌릴 수도 있다. 데이터는 DB 볼륨에 남는다.
    · 비밀값: 프로젝트의 .env 가 compose 에 필요한 값을 모두 갖고 있으면 그것을 쓰고, 아니면 ReCoder 가 한 번 만든 값을
      ~/.recoder/compose/<프로젝트>.env(사용자 전용)에 보관해 같은 볼륨을 다시 쓸 수 있게 한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    import architecture as arch_mod
    import compose_check
    import compose_kit
    import dockerfile_guard
except ImportError:  # pragma: no cover - 패키지 실행
    from core import architecture as arch_mod  # type: ignore
    from core import compose_check, compose_kit, dockerfile_guard  # type: ignore

Scan = Callable[[str, str, Optional[str]], Awaitable[dict]]
_KEEP = 3


def _home() -> Path:
    return Path(os.environ.get("RECODER_HOME") or (Path.home() / ".recoder"))


def _records_file() -> Path:
    return Path(os.environ.get("RECODER_COMPOSE_STORE") or (_home() / "compose_deployments.json"))


def load(workspace: str | Path) -> Optional[dict]:
    """ReCoder 아키텍처 계약으로 만든 compose 프로젝트면 계약을, 아니면 None."""
    ws = Path(workspace or "")
    compose, contract = ws / "docker-compose.yml", ws / "architecture.json"
    try:
        if not compose.is_file() or not compose.read_text(encoding="utf-8", errors="replace").startswith(compose_kit.MARKER):
            return None
        arch, _ = arch_mod.normalize(json.loads(contract.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return arch


def project_name(workspace: str | Path) -> str:
    ws = Path(workspace).resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", ws.name.lower()).strip("-")[:30] or "app"
    return f"recoder-{slug}-{hashlib.sha256(str(ws).encode()).hexdigest()[:6]}"


def _required_vars(arch: dict) -> set[str]:
    return set(re.findall(r"\$\{([A-Z0-9_]+):\?", compose_kit.render_compose(arch)))


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def env_file(workspace: str | Path, arch: dict) -> tuple[Path, str]:
    """(compose 에 넘길 .env 경로, 출처 설명). 값을 화면·로그에 내보내지 않는다."""
    ws = Path(workspace)
    need = _required_vars(arch)
    own = _read_env(ws / ".env")
    if need and all(own.get(k) and not own[k].startswith("<") for k in need):
        return ws / ".env", "프로젝트 .env"
    path = _home() / "compose" / f"{project_name(ws)}.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    have = _read_env(path)
    fresh = compose_kit.verify_env(arch)
    merged = {**{k: v for k, v in fresh.items() if k in need}, **{k: v for k, v in have.items() if v}}
    text = "".join(f"{k}={v}\n" for k, v in sorted(merged.items()))
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, "ReCoder 가 만든 비밀값(사용자 폴더 · 다시 배포해도 같은 값)"


def _base(workspace: str | Path, env: Path) -> list[str]:
    return ["compose", "-p", project_name(workspace), "-f", str(Path(workspace) / "docker-compose.yml"), "--env-file", str(env)]


def preview_steps(workspace: str | Path) -> list[dict]:
    """승인 화면의 '실행할 명령' — 실제 실행과 같은 순서(값은 가린다)."""
    arch = load(workspace)
    if not arch:
        return []
    proj = project_name(workspace)
    steps = [{"args": ["gitleaks", "detect", "--source", "."], "note": "코드 속 비밀값 검사"}]
    steps += [{"args": ["hadolint", f"{s['path']}/Dockerfile"], "note": f"{s['name']} Dockerfile 검사"} for s in arch["services"]]
    steps.append({"args": ["docker", "compose", "-p", proj, "build"], "note": f"서비스 {len(arch['services'])}개 이미지 빌드"})
    steps += [{"args": ["trivy", "image", f"{proj}-{s['name']}"], "note": f"{s['name']} 이미지 취약점 검사"} for s in arch["services"]]
    steps.append({"args": ["docker", "compose", "-p", proj, "--env-file", "<비밀값 파일>", "up", "-d", "--wait"],
                  "note": f"DB {len(arch['databases'])}개 → 서비스 순서로 시작, 헬스 체크 통과까지 기다림(데이터는 볼륨에 유지)"})
    steps.append({"args": ["node", "scripts/contract-test.mjs"], "note": "연결·테이블·계약 통합 테스트"})
    return steps


def _records() -> list[dict]:
    try:
        data = json.loads(_records_file().read_text(encoding="utf-8"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(records: list[dict]) -> None:
    path = _records_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records[-200:], ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def history(workspace: str | Path) -> list[dict]:
    proj = project_name(workspace)
    return [r for r in reversed(_records()) if r.get("project") == proj]


def _image_id(docker, image: str) -> str:
    out = docker(["image", "inspect", "--format", "{{.Id}}", image], 30)
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _service_image(proj: str, svc: str) -> str:
    return f"{proj}-{svc}"


def _pin(docker, proj: str, svc: str, deployment_id: str) -> str:
    tag = f"recoder-rb/{proj}-{svc}:{deployment_id[:12]}"
    out = docker(["tag", _service_image(proj, svc), tag], 30)
    return tag if out.returncode == 0 else ""


def _restore(docker, base: list[str], arch: dict, record: dict) -> tuple[bool, str]:
    """기록의 서비스 이미지를 compose 이미지 이름으로 되돌려 다시 띄운다(빌드 없이)."""
    proj = record["project"]
    for svc, tag in (record.get("pins") or {}).items():
        if not tag:
            return False, f"{svc} 의 보관 이미지가 없습니다"
        out = docker(["tag", tag, _service_image(proj, svc)], 30)
        if out.returncode != 0:
            return False, f"{svc} 이미지를 되돌리지 못했습니다: {compose_check._tail(out.stderr, 2, 200)}"
    up = docker([*base, "up", "-d", "--no-build", "--wait", "--wait-timeout", "300"], 600)
    return up.returncode == 0, compose_check._tail((up.stdout or "") + (up.stderr or ""), 6, 800)


def _prune_pins(docker, proj: str, keep: set[str]) -> None:
    pins = [r for r in _records() if r.get("project") == proj and r.get("pins")]
    old = pins[:-_KEEP]
    for r in old:
        for tag in (r.get("pins") or {}).values():
            if tag and tag not in keep:
                docker(["image", "rm", tag], 30)
        r["pins"] = {}


def _blocking(report: dict, kind: str) -> tuple[bool, str]:
    """게이트 판정 — (막음, 이유). 검사를 못 돌렸으면 막지 않고 '미검증' 으로 둔다(승인 화면에서 이미 알렸다)."""
    if report.get("status") != "ok":
        return False, ""
    crit = int(report.get("critical_count", 0) or 0)
    return (crit > 0, f"{kind}: 심각 {crit}건") if crit else (False, "")


async def deploy(workspace: str | Path, *, scan: Scan, docker=None, http=None,
                 progress: Callable[[str, str], None] | None = None) -> dict:
    """승인된 다중 서비스 로컬 배포. 결과 모양은 단일 앱 로컬 배포와 같다(status·stage·message·diagnosis)."""
    import asyncio

    docker = docker or compose_check._docker
    http = http or compose_check._http
    say = progress or (lambda stage, msg: None)
    ws = Path(workspace)
    arch = load(ws)
    if not arch:
        return {"status": "failed", "stage": "plan", "message": "ReCoder 아키텍처 계약(docker-compose.yml·architecture.json)이 없습니다."}
    proj = project_name(ws)
    env, env_source = env_file(ws, arch)
    base = _base(ws, env)
    deployment_id = uuid.uuid4().hex
    gate: dict[str, Any] = {"blockers": [], "unverified": [], "reports": {}}

    def fail(stage: str, title: str, cause: str, fix: str, **extra: Any) -> dict:
        return {"status": "failed", "stage": stage, "message": f"{title} — {cause}", "deployment_id": deployment_id,
                "gate": gate, "diagnosis": {"code": f"STACK_{stage.upper()}", "title": title, "cause": cause, "fix": fix,
                                            "lines": [], "step": stage}, **extra}

    # 1. 소스 단계 게이트 — 기존 서비스는 아직 그대로다.
    say("scan", "코드 속 비밀값과 서비스별 Dockerfile 을 검사합니다")
    secret = await scan("gitleaks", str(ws), str(ws))
    gate["reports"]["gitleaks"] = {k: secret.get(k) for k in ("status", "critical_count", "high_count", "summary")}
    blocked, why = _blocking(secret, "Gitleaks 비밀값")
    if blocked:
        gate["blockers"].append(why)
    elif secret.get("status") != "ok":
        #: 단일 앱 배포와 같다 — 비밀값 검사를 끝내지 못하면 배포하지 않는다(이미지에 키가 들어가면 되돌릴 수 없다).
        gate["blockers"].append("Gitleaks 비밀값 검사를 완료하지 못했습니다")
    for svc in arch["services"]:
        path = ws / svc["path"] / "Dockerfile"
        try:
            unsafe = dockerfile_guard.check(path.read_text(encoding="utf-8"))
        except OSError:
            unsafe = []
            gate["blockers"].append(f"{svc['name']}: Dockerfile 이 없습니다")
        if unsafe:
            gate["blockers"].append(f"{svc['name']}: {dockerfile_guard.message(unsafe)}")
        report = await scan("hadolint", str(ws), str(path))
        gate["reports"][f"hadolint:{svc['name']}"] = {k: report.get(k) for k in ("status", "critical_count", "high_count")}
        blocked, why = _blocking(report, f"{svc['name']} Hadolint")
        if blocked:
            gate["blockers"].append(why)
        elif report.get("status") != "ok":
            gate["unverified"].append(f"Hadolint:{svc['name']}")
    if gate["blockers"]:
        return fail("scan", "보안 게이트에서 막았습니다", "; ".join(gate["blockers"]),
                    "보안 화면의 수정 제안을 적용한 뒤 다시 배포하세요. 기존에 돌던 서비스는 그대로입니다.")

    # 2. 지금 돌고 있는 버전 보관(롤백 대상)
    previous = next((r for r in history(ws) if r.get("status") == "success" and r.get("pins")), None)
    current_pins: dict[str, str] = {}
    for svc in arch["services"]:
        if _image_id(docker, _service_image(proj, svc["name"])):
            current_pins[svc["name"]] = _pin(docker, proj, svc["name"], f"pre-{deployment_id}")

    # 3. 빌드
    say("build", f"서비스 {len(arch['services'])}개 이미지를 빌드합니다")
    built = await asyncio.to_thread(docker, [*base, "build"], 1800)
    log = (built.stdout or "") + (built.stderr or "")
    if built.returncode != 0:
        unavailable = compose_check._UNAVAILABLE.search(log) and not compose_check._BUILD_FAIL.search(log)
        svc = compose_check._build_service(arch, log)
        return fail("build", "이미지를 받지 못했습니다" if unavailable else f"{svc or '서비스'} 이미지 빌드 실패",
                    compose_check._tail(log, 12, 1500),
                    "Docker Desktop·인터넷 연결을 확인하세요." if unavailable else "개발 화면에서 빌드 오류를 고친 뒤 다시 배포하세요.",
                    stderr=compose_check._tail(log, 40, 6000))

    # 4. 이미지 게이트 — 아직 기존 컨테이너를 바꾸지 않았다.
    say("scan", "빌드한 서비스 이미지의 취약점을 검사합니다")
    for svc in arch["services"]:
        report = await scan("trivy", "", _service_image(proj, svc["name"]))
        gate["reports"][f"trivy:{svc['name']}"] = {k: report.get(k) for k in ("status", "critical_count", "high_count")}
        blocked, why = _blocking(report, f"{svc['name']} 이미지")
        if blocked:
            gate["blockers"].append(why)
        elif report.get("status") != "ok":
            gate["unverified"].append(f"Trivy:{svc['name']}")
    if gate["blockers"]:
        _restore_tags(docker, proj, current_pins)
        return fail("scan", "이미지 보안 검사에서 막았습니다", "; ".join(gate["blockers"]),
                    "기본 이미지·의존성 버전을 올린 뒤 다시 배포하세요. 기존에 돌던 서비스는 그대로입니다.")

    # 5. 기동
    say("deploy", "DB → 서비스 순서로 시작하고 헬스 체크를 기다립니다")
    up = await asyncio.to_thread(docker, [*base, "up", "-d", "--no-build", "--wait", "--wait-timeout", "300", "--remove-orphans"], 900)
    result: dict[str, Any] = {"kind": "compose", "status": "checked", "passed": False, "services": [], "checks": [], "failures": [],
                              "output": ""}
    await asyncio.to_thread(compose_check.inspect, arch, base, docker, http, result)
    started_ok = up.returncode == 0 and not any(f["kind"] in ("service_down", "db_down") for f in result["failures"])
    record = {"id": deployment_id, "project": proj, "workspace": str(ws.resolve()), "at": datetime.now(timezone.utc).isoformat(),
              "services": [s["name"] for s in arch["services"]], "env_source": env_source,
              "checks": {"passed": sum(1 for c in result["checks"] if c["ok"]), "total": len(result["checks"])},
              "gate": {"unverified": gate["unverified"]}, "pins": {}}
    if not started_ok:
        down = [f["title"] for f in result["failures"]] or [compose_check._tail(up.stderr or up.stdout, 3, 300)]
        rolled = ""
        if previous or current_pins:
            say("rollback", "새 버전이 뜨지 않아 직전 버전으로 되돌립니다")
            target = previous if previous else {"project": proj, "pins": current_pins}
            ok, out = await asyncio.to_thread(_restore, docker, base, arch, target)
            rolled = "직전 버전으로 되돌렸습니다" if ok else f"되돌리기 실패: {out}"
        record.update(status="failed", failures=down[:10], rolled_back=rolled)
        records = _records() + [record]
        _save(records)
        return fail("deploy", "서비스가 정상적으로 시작하지 않았습니다", "; ".join(down[:5]) + (f" · {rolled}" if rolled else ""),
                    "결과의 서비스 로그를 확인하고 개발 화면에서 고친 뒤 다시 배포하세요.",
                    verification=result, rolled_back=bool(rolled and rolled.startswith("직전")))

    # 6. 성공 — 이번 버전을 보관하고 오래된 보관본을 정리한다.
    record["pins"] = {s["name"]: _pin(docker, proj, s["name"], deployment_id) for s in arch["services"]}
    record["status"] = "success" if not result["failures"] else "degraded"
    record["failures"] = [f["title"] for f in result["failures"]][:10]
    _save(_records() + [record])
    _prune_pins(docker, proj, set(record["pins"].values()))
    _restore_tags(docker, proj, {}, drop=current_pins)
    public = next((s for s in arch["services"] if s["public"] and s["port"]), None)
    url = f"http://localhost:{public['port']}" if public else ""
    result["status"] = "failed" if result["failures"] else "passed"
    result["passed"] = not result["failures"]
    msg = (f"서비스 {len(arch['services'])}개 · DB {len(arch['databases'])}개 실행 중"
           + (f" — {url}" if url else "") + f" · 검사 {record['checks']['passed']}/{record['checks']['total']} 통과")
    if result["failures"]:
        msg += " · 일부 검사 실패: " + "; ".join(record["failures"][:3])
    if gate["unverified"]:
        msg += " · 미검증: " + ", ".join(gate["unverified"])
    return {"status": "success", "stage": "done", "message": msg, "url": url, "deployment_id": deployment_id,
            "verification": result, "gate": gate, "env_source": env_source, "project": proj}


def _restore_tags(docker, proj: str, pins: dict[str, str], drop: dict[str, str] | None = None) -> None:
    """빌드 뒤 게이트에서 막혔을 때 — compose 이미지 이름을 배포 전 이미지로 돌려놓는다(다음 up 이 막힌 이미지를 쓰지 않게)."""
    for svc, tag in pins.items():
        if tag:
            docker(["tag", tag, _service_image(proj, svc)], 30)
            docker(["image", "rm", tag], 30)
    for tag in (drop or {}).values():
        if tag:
            docker(["image", "rm", tag], 30)


async def rollback(workspace: str | Path, deployment_id: str = "", *, docker=None) -> dict:
    """승인된 롤백 — 지정한(없으면 직전) 성공 배포의 이미지로 되돌린다. 데이터(볼륨)는 그대로."""
    import asyncio

    docker = docker or compose_check._docker
    ws = Path(workspace)
    arch = load(ws)
    if not arch:
        return {"status": "failed", "message": "ReCoder compose 프로젝트가 아닙니다."}
    done = [r for r in history(ws) if r.get("status") in ("success", "degraded") and r.get("pins")]
    if deployment_id:
        target = next((r for r in done if r["id"] == deployment_id), None)
    else:
        target = done[1] if len(done) > 1 else None
    if not target:
        return {"status": "failed", "message": "되돌릴 이전 성공 배포가 없습니다."}
    env, _ = env_file(ws, arch)
    ok, out = await asyncio.to_thread(_restore, docker, _base(ws, env), arch, target)
    rec = {"id": uuid.uuid4().hex, "project": target["project"], "workspace": str(ws.resolve()),
           "at": datetime.now(timezone.utc).isoformat(), "status": "rolled_back" if ok else "failed", "rollback_to": target["id"],
           "services": target.get("services"), "pins": target.get("pins") if ok else {}}
    _save(_records() + [rec])
    return {"status": "success" if ok else "failed", "rollback_to": target["id"],
            "message": f"{target['at'][:19]} 배포로 되돌렸습니다" if ok else f"되돌리지 못했습니다: {out}"}
