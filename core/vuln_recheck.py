"""
ReCoder Core — 배포 후 취약점 재검사 (2.0.28)

배포할 때 만든 부품 목록(SBOM, CycloneDX)을 하루에 한 번 공개 취약점 DB(OSV)와 다시 대조해, 배포 이후 **새로 발표된**
취약점만 알린다(VS Code · 디스코드 — 공격·장애 알림과 같은 경로). 고쳐진 버전이 있으면 "올리기" 제안을 붙이고,
승인하면 그 프로젝트의 package.json(또는 requirements.txt)만 바꾼다 — 다시 배포는 사용자가 한다.

  · OSV 에는 패키지 이름·버전(purl)만 보낸다. 코드·파일 내용은 보내지 않는다.
  · 기록: 발견일·알림일·고친 날(다음 재검사에서 사라짐)·올리기 적용일 — 대응 이력(예: EU CRA)의 근거.
  · Core 가 켜져 있을 때만 돈다(PC 가 꺼져 있으면 다음에 켤 때). 네트워크가 안 되면 조용히 다음 번으로.
"""
from __future__ import annotations

import asyncio
import json
import threading
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
try:
    import proc_tools  # type: ignore
except ImportError:  # pragma: no cover
    from core import proc_tools  # type: ignore

logger = logging.getLogger(__name__)

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
#: purl 종류 → OSV 생태계. OS 패키지(apk·deb)는 배포판 정보가 필요해 이번 판은 보지 않는다.
ECOSYSTEMS = {"npm": "npm", "pypi": "PyPI", "maven": "Maven", "golang": "Go", "cargo": "crates.io", "gem": "RubyGems",
              "nuget": "NuGet", "composer": "Packagist"}
INTERVAL_S = 24 * 3600
FIRST_DELAY_S = 120
SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1}
SEVERITY_KO = {"CRITICAL": "치명", "HIGH": "높음", "MODERATE": "보통", "MEDIUM": "보통", "LOW": "낮음"}
_PURL = re.compile(r"^pkg:([a-z]+)/(.+)@([^?#]+)")


def enabled() -> bool:
    return (os.environ.get("RECODER_TEST_MODE") != "1" and os.environ.get("RECODER_VULN_RECHECK", "1") != "0"
            and os.environ.get("RECODER_OFFLINE") != "1")


def _home() -> Path:
    return Path(os.environ.get("RECODER_HOME") or (Path.home() / ".recoder"))


def _state_file() -> Path:
    return _home() / "vuln_recheck.json"


def _state_io():
    try:
        import state_io  # type: ignore
    except ImportError:  # pragma: no cover
        from core import state_io  # type: ignore
    return state_io


#: 재검사(하루 한 번 스레드)와 API(지금 검사·올리기)가 같은 상태를 읽고-고치고-쓴다 — 한 번에 하나만.
_STATE_LOCK = threading.RLock()


def _read_state() -> dict:
    """모양이 틀린 항목은 버린다(2.0.32 — 손상된 항목 하나 때문에 매일 검사가 조용히 멈추던 것)."""
    data = _state_io().read_json(_state_file(), {})
    if not isinstance(data, dict):
        return {}
    clean: dict = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        known = entry.get("known") if isinstance(entry.get("known"), dict) else {}
        entry["known"] = {k: v for k, v in known.items()
                          if isinstance(v, dict) and isinstance(v.get("id"), str) and isinstance(v.get("purl"), str)}
        clean[str(key)] = entry
    return clean


def _write_state(data: dict) -> None:
    _state_io().write_json(_state_file(), data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 입력 — 배포 기록과 SBOM
# ---------------------------------------------------------------------------


def deployments(records: Iterable[dict]) -> list[dict]:
    """서비스마다 가장 최근의 성공한 배포(SBOM 이 있는 것)."""
    latest: dict[str, dict] = {}
    for r in records:
        if not isinstance(r, dict) or str(r.get("status", "")).lower() not in ("succeeded", "ecsdeploystatus.succeeded"):
            continue
        sbom = r.get("sbom_path") or ((r.get("sbom") or {}).get("sbom_path") if isinstance(r.get("sbom"), dict) else None)
        if not sbom or not Path(sbom).is_file():
            continue
        key = f"{r.get('region')}/{r.get('cluster')}/{r.get('service')}"
        when = str(r.get("completed_at") or r.get("started_at") or "")
        if key not in latest or when > latest[key]["when"]:
            request = r.get("request") if isinstance(r.get("request"), dict) else {}
            latest[key] = {"key": key, "region": r.get("region"), "cluster": r.get("cluster"), "service": r.get("service"),
                           "sbom": sbom, "workspace": (request or {}).get("workspace_path") or "", "when": when,
                           "deployment_id": r.get("deployment_id", "")}
    return list(latest.values())


def purls(sbom_path: str) -> list[dict]:
    """CycloneDX JSON 의 언어 패키지 purl [{purl, type, name, version}] (중복 제거)."""
    try:
        data = json.loads(Path(sbom_path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    out: dict[str, dict] = {}

    def walk(components: list) -> None:
        for c in components or []:
            purl = str(c.get("purl") or "")
            m = _PURL.match(purl)
            if m and m.group(1) in ECOSYSTEMS:
                name = urllib.request.unquote(m.group(2))
                out[purl.split("?")[0]] = {"purl": purl.split("?")[0], "type": m.group(1), "name": name, "version": m.group(3)}
            walk(c.get("components") or [])
    walk(data.get("components") or [])
    return list(out.values())


# ---------------------------------------------------------------------------
# OSV
# ---------------------------------------------------------------------------


def _post_json(url: str, body: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 고정된 OSV 주소
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 고정된 OSV 주소
        return json.loads(resp.read().decode("utf-8"))


def query(packages: list[dict], *, post: Callable[[str, dict], dict] = _post_json) -> dict[str, list[str]]:
    """{purl: [취약점 ID]} — 1,000개씩 나눠 묻는다."""
    found: dict[str, list[str]] = {}
    for i in range(0, len(packages), 1000):
        batch = packages[i:i + 1000]
        res = post(OSV_BATCH, {"queries": [{"package": {"purl": p["purl"]}} for p in batch]})
        for p, r in zip(batch, res.get("results") or []):
            ids = [v["id"] for v in (r or {}).get("vulns") or [] if v.get("id")]
            if ids:
                found[p["purl"]] = ids
    return found


def _version_key(v: str) -> tuple:
    parts = re.findall(r"\d+|[A-Za-z]+", v or "")
    return tuple((0, int(p)) if p.isdigit() else (-1, p) for p in parts)


def fixed_version(vuln: dict, pkg: dict) -> str:
    """이 패키지·버전에서 벗어나는 가장 낮은 고쳐진 버전. 없으면 ''."""
    eco = ECOSYSTEMS.get(pkg["type"], "")
    current = _version_key(pkg["version"])
    best = ""
    for affected in vuln.get("affected") or []:
        p = affected.get("package") or {}
        if p.get("ecosystem") != eco or str(p.get("name", "")).lower() != pkg["name"].lower():
            continue
        for rng in affected.get("ranges") or []:
            #: GIT 범위의 fixed 는 커밋 해시다 — 버전이 아니다(2.0.32).
            if str(rng.get("type", "")).upper() not in ("SEMVER", "ECOSYSTEM"):
                continue
            for event in rng.get("events") or []:
                fixed = event.get("fixed")
                if fixed and _version_key(fixed) > current and (not best or _version_key(fixed) < _version_key(best)):
                    best = fixed
    return best


def severity(vuln: dict) -> str:
    level = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if level in SEVERITY_ORDER:
        return level
    for affected in vuln.get("affected") or []:
        level = str((affected.get("database_specific") or {}).get("severity") or "").upper()
        if level in SEVERITY_ORDER:
            return level
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# 재검사
# ---------------------------------------------------------------------------


def run(records: Iterable[dict], *, post: Callable[[str, dict], dict] = _post_json,
        get: Callable[[str], dict] = _get_json, store: Optional[Callable[[dict], Optional[dict]]] = None) -> list[dict]:
    """한 번 재검사 — 새로 생긴 취약점이 있는 배포마다 알림을 하나 만든다(만든 알림 목록)."""
    if store is None:
        try:
            import ops_alerts  # type: ignore
        except ImportError:  # pragma: no cover
            from core import ops_alerts  # type: ignore
        store = ops_alerts._store
    with _STATE_LOCK:
        return _run(records, post=post, get=get, store=store)


def _run(records: Iterable[dict], *, post: Callable[[str, dict], dict], get: Callable[[str], dict],
         store: Callable[[dict], Optional[dict]]) -> list[dict]:
    state = _read_state()
    raised: list[dict] = []
    for dep in deployments(records):
        packages = purls(dep["sbom"])
        if not packages:
            continue
        found = query(packages, post=post)
        first = dep["key"] not in state or not state[dep["key"]].get("checked_at")
        entry = state.setdefault(dep["key"], {"known": {}, "checked_at": ""})
        known: dict = entry.setdefault("known", {})
        now = _now()
        current_ids = {(vid, purl) for purl, ids in found.items() for vid in ids}
        # 사라진 것(다시 배포해 고쳐짐) — 고친 날
        for k, rec in known.items():
            if not rec.get("fixed_at") and (rec["id"], rec["purl"]) not in current_ids:
                rec["fixed_at"] = now
        fresh = []
        by_purl = {p["purl"]: p for p in packages}
        for vid, purl in sorted(current_ids):
            k = f"{vid}|{purl}"
            if k in known and not known[k].get("fixed_at"):
                #: 알림이 막혀(같은 서비스 알림 쿨다운) 아직 못 알린 것은 다음에 다시 알린다(2.0.32 — 영영 묻히던 것).
                if not known[k].get("notified_at"):
                    fresh.append(known[k])
                continue
            try:
                detail = get(OSV_VULN.format(urllib.request.quote(vid)))
            except Exception as exc:  # noqa: BLE001 - 상세를 못 받으면 ID 만으로
                logger.info("OSV 상세 조회 실패(%s): %s", vid, exc)
                detail = {"id": vid}
            pkg = by_purl[purl]
            item = {"id": vid, "purl": purl, "name": pkg["name"], "type": pkg["type"], "version": pkg["version"],
                    "summary": str(detail.get("summary") or "")[:200], "severity": severity(detail),
                    "aliases": [a for a in detail.get("aliases") or [] if str(a).startswith("CVE-")][:3],
                    "fixed": fixed_version(detail, pkg), "found_at": now, "notified_at": "", "fixed_at": "",
                    "fix_applied_at": "", "initial": first}
            known[k] = item
            fresh.append(item)
        entry["checked_at"] = now
        entry["deployment_id"] = dep["deployment_id"]
        if fresh:
            alert = store(_alert(dep, fresh, first=all(v.get("initial") for v in fresh)))
            if alert:
                for item in fresh:
                    item["notified_at"] = now
                raised.append(alert)
    _write_state(state)
    return raised


def _alert(dep: dict, fresh: list[dict], *, first: bool = False) -> dict:
    fresh = sorted(fresh, key=lambda v: -SEVERITY_ORDER.get(v["severity"], 0))
    top = fresh[0]["severity"]
    lines = [f"· {v['name']} {v['version']}" + (f" → {v['fixed']}" if v["fixed"] else " (고쳐진 버전 없음)")
             + f" — {SEVERITY_KO.get(v['severity'], '등급 미상')} {(v['aliases'] or [v['id']])[0]}"
             + (f": {v['summary']}" if v["summary"] else "") for v in fresh[:8]]
    upgrades: dict[tuple, dict] = {}
    for v in fresh:
        if v["fixed"] and v["type"] in ("npm", "pypi"):
            key = (v["type"], v["name"])
            if key not in upgrades or _version_key(v["fixed"]) > _version_key(upgrades[key]["to"]):
                upgrades[key] = {"ecosystem": v["type"], "name": v["name"], "from": v["version"], "to": v["fixed"]}
    suggestions = [{"action": "upgrade", "value": u, "label": f"{u['name']} {u['to']} 로 올리기",
                    "detail": f"{u['name']} 을(를) {u['from']} → {u['to']} 로 바꿉니다(package.json·requirements.txt). 다시 배포해야 반영됩니다."}
                   for u in list(upgrades.values())[:3]] if dep.get("workspace") else []
    return {"key": dep["key"], "kind": "vuln", "severity": "critical" if top in ("CRITICAL", "HIGH") else "warning",
            "title": (f"배포된 버전의 알려진 취약점 {len(fresh)}건" if first else f"새 취약점 {len(fresh)}건"),
            "detail": ("처음 검사에서 찾은, 지금 배포된 버전의 알려진 취약점입니다.\n" if first
                       else "배포 이후 새로 발표된 취약점입니다.\n") + "\n".join(lines),
            "region": dep["region"], "cluster": dep["cluster"], "service": dep["service"], "workspace": dep.get("workspace", ""),
            "vulns": [{k: v[k] for k in ("id", "name", "version", "fixed", "severity")} for v in fresh[:20]],
            "suggestions": suggestions}


# ---------------------------------------------------------------------------
# 올리기(승인 후)
# ---------------------------------------------------------------------------


_PKG_DIRS = ("", "backend", "server", "api", "frontend", "client", "web", "app")


def _read_text(path: Path) -> tuple[str, str]:
    """(내용, 줄바꿈). BOM 이 있어도 읽고(메모장·PowerShell 5.1), 원래 줄바꿈(CRLF/LF)을 기억한다(2.0.32)."""
    raw = path.read_bytes().decode("utf-8-sig")
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _write_text(path: Path, text: str, newline: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", newline) if newline != "\n" else text)


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def bump_requirement(text: str, name: str, to: str) -> str:
    """requirements 의 한 패키지를 `이름[extra]>=to` 로(상한·고정·환경 표시는 정리, 주석·마커는 그대로)."""
    want = _pep503(name)
    out: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*((?:[<>=!~]=?\s*[^;#,\s]+\s*,?\s*)*)(.*)$", line)
        if m and _pep503(m.group(2)) == want and not line.lstrip().startswith(("#", "-")):
            rest = m.group(5)
            out.append(f"{m.group(1)}{m.group(2)}{m.group(3) or ''}>={to}" + (" " + rest.strip() if rest.strip() else ""))
        else:
            out.append(line)
    return "\n".join(out)


def apply_fix(alert: dict, value: dict, *, npm_lock: bool = True) -> dict:
    """승인한 올리기 — 프로젝트 파일만 바꾼다(재배포는 사용자가)."""
    with _STATE_LOCK:
        return _apply_fix(alert, value, npm_lock=npm_lock)


def _apply_fix(alert: dict, value: dict, *, npm_lock: bool = True) -> dict:
    workspace = Path(str(alert.get("workspace") or ""))
    if not str(alert.get("workspace") or "") or not workspace.is_dir():
        raise ValueError("이 배포의 작업 폴더를 찾지 못했습니다 — 직접 버전을 올린 뒤 다시 배포하세요.")
    name, to = str(value.get("name") or ""), str(value.get("to") or "")
    if not re.fullmatch(r"[@\w./-]{1,214}", name) or not re.fullmatch(r"[\w.+-]{1,64}", to):
        raise ValueError("올릴 패키지 정보가 올바르지 않습니다.")
    changed: list[str] = []
    if value.get("ecosystem") == "npm":
        targets = [workspace / d / "package.json" for d in _PKG_DIRS if (workspace / d / "package.json").is_file()]
        for path in targets:
            text, nl = _read_text(path)
            data = json.loads(text)
            hit = False
            for section in ("dependencies", "devDependencies"):
                deps = data.get(section)
                if isinstance(deps, dict) and name in deps:
                    deps[name] = f"^{to}"
                    hit = True
            if hit:
                _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", nl)
                changed.append(path.relative_to(workspace).as_posix())
        if not changed and targets:
            #: 직접 쓰는 패키지가 아니다(다른 패키지가 끌어옴) — 잠금 파일이 있는 package.json 에 overrides 로 올린다.
            path = next((p for p in targets if (p.parent / "package-lock.json").is_file()), targets[0])
            text, nl = _read_text(path)
            data = json.loads(text)
            data.setdefault("overrides", {})[name] = f"^{to}"
            _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", nl)
            changed.append(f"{path.relative_to(workspace).as_posix()} (overrides)")
        lock_note = ""
        #: Windows 의 npm 은 npm.cmd 라 이름만으로는 실행되지 않는다 — 찾은 전체 경로로 부른다.
        npm = shutil.which("npm") if npm_lock else None
        if changed and npm:
            for rel in dict.fromkeys(c.split(" ")[0] for c in changed):
                folder = (workspace / rel).parent
                try:
                    done = proc_tools.run([npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
                                          cwd=folder, capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace")
                    lock_note = " 잠금 파일도 갱신했습니다." if done.returncode == 0 else " 잠금 파일은 갱신하지 못했습니다(npm install 로 갱신하세요)."
                except (OSError, subprocess.TimeoutExpired):
                    lock_note = " 잠금 파일은 갱신하지 못했습니다(npm install 로 갱신하세요)."
    elif value.get("ecosystem") == "pypi":
        lock_note = ""
        for path in workspace.rglob("requirements*.txt"):
            if "node_modules" in path.parts or ".venv" in path.parts:
                continue
            text, nl = _read_text(path)
            new = bump_requirement(text, name, to)
            if new != text:
                _write_text(path, new, nl)
                changed.append(path.relative_to(workspace).as_posix())
    else:
        raise ValueError("이 종류의 패키지는 자동으로 올리지 않습니다.")
    if not changed:
        raise ValueError(f"{name} 을(를) 쓰는 의존성 파일을 찾지 못했습니다 — 직접 올린 뒤 다시 배포하세요.")
    state = _read_state()
    entry = state.get(alert.get("key", ""), {})
    for rec in (entry.get("known") or {}).values():
        if rec.get("name") == name and not rec.get("fix_applied_at"):
            rec["fix_applied_at"] = _now()
    _write_state(state)
    return {"applied": True, "message": f"{name} 을(를) {to} 로 올렸습니다({', '.join(changed)}).{lock_note} 다시 배포하면 반영됩니다."}


def history() -> dict:
    """서비스별 발견·알림·고친 기록(대응 이력)."""
    return _read_state()


class Rechecker:
    """Core 가 켜져 있는 동안 하루에 한 번(시작 2분 뒤 첫 검사)."""

    def __init__(self, records: Callable[[], list[dict]]) -> None:
        self.records = records

    def tick(self) -> list[dict]:
        try:
            return run(self.records())
        except Exception as exc:  # noqa: BLE001 - 네트워크 없음 등 → 다음 번에
            logger.info("취약점 재검사 건너뜀: %s", exc)
            return []

    async def run_forever(self) -> None:
        await asyncio.sleep(FIRST_DELAY_S)
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(INTERVAL_S)


def records_from_store() -> list[dict]:
    try:
        try:
            from api.routes.ecs import _store_path  # type: ignore
        except ImportError:  # pragma: no cover
            from core.api.routes.ecs import _store_path  # type: ignore
        path = _store_path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        return [r for r in data if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001
        logger.info("배포 기록을 읽지 못했습니다: %s", exc)
        return []
