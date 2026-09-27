"""package.json 의 의존성 버전이 npm 에 실제로 있는지 확인한다.

왜 필요한가
    AI 가 만든 package.json 에 존재하지 않는 버전(예: ``jsonwebtoken@^9.1.2`` —
    9.x 의 마지막은 9.0.x)이 들어가면 Docker 빌드가 몇 분 뒤 ``npm error ETARGET``
    으로 멈춘다. 배포 계획 단계에서 레지스트리에 한 번 물어보면 빌드 전에 알 수 있다.

원칙
    - **실패하면 조용히 넘어간다(fail-open).** 오프라인·사내 레지스트리·시간 초과면
      판단하지 않는다. 잘못된 차단이 누락보다 나쁘다.
    - 판단할 수 있는 범위 문법(^ ~ = x 비교·||·하이픈)만 본다. 태그·URL·경로·
      workspace:·npm: 별칭은 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

_TIMEOUT = 6.0
_CACHE: dict[str, Optional[list[str]]] = {}
_LOCK = threading.Lock()

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_PARTIAL = re.compile(r"^v?(\d+|x|X|\*)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:-([0-9A-Za-z.-]+))?$")


def registry_url() -> str:
    url = (os.environ.get("NPM_CONFIG_REGISTRY") or os.environ.get("npm_config_registry") or "").strip()
    return (url or "https://registry.npmjs.org").rstrip("/")


def _parse(version: str) -> Optional[tuple[int, int, int, tuple]]:
    m = _VERSION.match(version.strip())
    if not m:
        return None
    pre = tuple(int(p) if p.isdigit() else p for p in (m.group(4) or "").split(".") if p)
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def _key(v: tuple[int, int, int, tuple]):
    # 정식 버전이 같은 숫자의 프리릴리스보다 크다.
    return (v[0], v[1], v[2], 1 if not v[3] else 0, tuple((0, p) if isinstance(p, int) else (1, p) for p in v[3]))


def _comparators(part: str) -> Optional[list[tuple[str, tuple]]]:
    """하나의 AND 묶음 → [(op, (maj, min, pat, pre))]. 모르는 문법이면 None."""
    part = part.strip()
    if part in {"", "*", "x", "X", "latest"}:
        return [] if part != "latest" else None
    hy = re.match(r"^(\S+)\s+-\s+(\S+)$", part)
    if hy:
        lo, hi = _comparators(">=" + hy.group(1)), _comparators("<=" + hy.group(2))
        return None if lo is None or hi is None else lo + hi
    out: list[tuple[str, tuple]] = []
    for token in re.split(r"\s+", re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", part)):
        m = re.match(r"^(>=|<=|>|<|=|\^|~>?|)v?(.+)$", token)
        if not m:
            return None
        op, rest = m.group(1), m.group(2)
        p = _PARTIAL.match(rest)
        if not p:
            return None
        nums = [None if g in (None, "x", "X", "*") else int(g) for g in p.groups()[:3]]
        pre = tuple(int(x) if x.isdigit() else x for x in (p.group(4) or "").split(".") if x)
        major, minor, patch = nums
        if major is None:
            continue
        if op in ("", "="):
            if minor is None:
                out += [(">=", (major, 0, 0, ())), ("<", (major + 1, 0, 0, ()))]
            elif patch is None:
                out += [(">=", (major, minor, 0, ())), ("<", (major, minor + 1, 0, ()))]
            else:
                out.append(("=", (major, minor, patch, pre)))
        elif op == "^":
            lo = (major, minor or 0, patch or 0, pre)
            if major > 0 or minor is None:
                hi = (major + 1, 0, 0, ())
            elif minor > 0 or patch is None:
                hi = (0, minor + 1, 0, ())
            else:
                hi = (0, 0, (patch or 0) + 1, ())
            out += [(">=", lo), ("<", hi)]
        elif op.startswith("~"):
            lo = (major, minor or 0, patch or 0, pre)
            hi = (major + 1, 0, 0, ()) if minor is None else (major, minor + 1, 0, ())
            out += [(">=", lo), ("<", hi)]
        else:
            out.append((op, (major, minor or 0, patch or 0, pre)))
    return out


def parse_range(spec: str) -> Optional[list[list[tuple[str, tuple]]]]:
    """``^1.2.3 || 2.x`` → OR 목록. 판단할 수 없는 스펙이면 None."""
    if not isinstance(spec, str):
        return None
    spec = spec.strip()
    if not spec or re.match(r"^(?:[a-z]+:|[./~]/|file:|link:|workspace:|npm:|git|http|github:)", spec) or "/" in spec:
        return None
    if re.fullmatch(r"[A-Za-z][\w.-]*", spec) and spec not in {"x", "X"}:
        return None  # dist-tag (latest, next …)
    groups = []
    for part in spec.split("||"):
        comps = _comparators(part)
        if comps is None:
            return None
        groups.append(comps)
    return groups


def _cmp_ok(v: tuple, op: str, target: tuple) -> bool:
    a, b = _key(v), _key(target)
    return {"=": a == b, ">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b}[op]


def satisfies(version: str, groups: list[list[tuple[str, tuple]]]) -> bool:
    v = _parse(version)
    if v is None:
        return False
    for comps in groups:
        if not all(_cmp_ok(v, op, t) for op, t in comps):
            continue
        if v[3]:
            # 프리릴리스는 같은 [major, minor, patch] 의 프리릴리스를 명시한 범위에서만 맞는다(npm 규칙).
            if not any(t[3] and t[:3] == v[:3] for _, t in comps):
                continue
        return True
    return False


def fetch_versions(name: str, timeout: float = _TIMEOUT) -> Optional[list[str]]:
    """레지스트리에 있는 버전 목록. 알 수 없으면 None(판단하지 않음), 패키지가 없으면 []."""
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
    url = f"{registry_url()}/{urllib.parse.quote(name, safe='@')}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8"})
    result: Optional[list[str]]
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        versions = data.get("versions") if isinstance(data, dict) else None
        result = sorted(versions) if isinstance(versions, dict) else None
    except urllib.error.HTTPError as exc:
        result = [] if exc.code == 404 else None
    except Exception:  # noqa: BLE001 - 오프라인·프록시·시간 초과: 판단하지 않는다
        result = None
    with _LOCK:
        _CACHE[name] = result
    return result


def suggest(spec: str, versions: list[str]) -> Optional[str]:
    """없는 범위를 대신할 실제 버전 범위 — 같은 major 의 최신, 없으면 전체 최신(정식 버전)."""
    parsed = [(v, _parse(v)) for v in versions]
    stable = [(v, p) for v, p in parsed if p and not p[3]]
    if not stable:
        return None
    m = re.search(r"(\d+)", spec or "")
    major = int(m.group(1)) if m else None
    same = [(v, p) for v, p in stable if major is not None and p[0] == major]
    best = max(same or stable, key=lambda item: _key(item[1]))
    return f"^{best[0]}"


def missing_versions(dependencies: dict, *, max_workers: int = 8) -> list[dict]:
    """[{name, spec, suggestion, exists}] — 범위를 만족하는 버전이 레지스트리에 없는 것만."""
    if os.environ.get("RECODER_OFFLINE") == "1":
        return []
    if os.environ.get("RECODER_TEST_MODE") == "1" and os.environ.get("RECODER_NPM_REGISTRY") != "1":
        return []  # 테스트는 레지스트리를 부르지 않는다(필요한 테스트만 켜고 fetch 를 바꿔 끼운다)
    checks = []
    for name, spec in (dependencies or {}).items():
        groups = parse_range(spec) if isinstance(name, str) and re.fullmatch(r"(@[\w.-]+/)?[\w.-]+", name) else None
        if groups is not None:
            checks.append((name, spec, groups))
    if not checks:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(checks))) as pool:
        fetched = list(pool.map(lambda item: fetch_versions(item[0]), checks))
    out = []
    for (name, spec, groups), versions in zip(checks, fetched):
        if versions is None:
            continue
        if not versions:
            out.append({"name": name, "spec": spec, "suggestion": None, "exists": False})
        elif not any(satisfies(v, groups) for v in versions):
            out.append({"name": name, "spec": spec, "suggestion": suggest(spec, versions), "exists": True})
    return out
