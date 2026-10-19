"""npm 패키지가 믿을 만한지 공개 정보로 판정한다 (슬롭스쿼팅·타이포스쿼팅 방어).

왜 필요한가
    AI 는 그럴듯한 패키지 이름을 지어낸다. 공격자는 그 이름을 미리 npm 에 올려 둔다
    (slopsquatting). 유명 패키지와 한두 글자 다른 이름도 같은 수법이다(typosquatting).
    설치되는 순간 설치 스크립트가 PC·CI 에서 실행되므로, `npm install` 전에 막아야 한다.

판정 (AI 호출 없음)
    - 존재하지 않음(404)은 기존 NODE_DEPENDENCY_VERSION_NOT_FOUND 가 이미 잡는다 — 여기서는 보지 않는다.
    - 짝퉁 의심(block): 유명 패키지 목록에 없는 이름이 유명 패키지와 편집 거리 1~2 이고,
      주간 다운로드가 그 유명 패키지의 1% 미만.
    - 매우 의심(block): 처음 올라온 지 30일 미만 그리고 주간 다운로드 1,000 미만.
    - 주의(warn): 유명 목록 밖이면서 설치 스크립트(preinstall/install/postinstall) 있음,
      저장소 정보 없음, 최신 버전이 사용 중단(deprecated).
    - 확인 못 함(unknown): 네트워크 실패 — 판단하지 않는다(fail-open, 기존 npm_registry 와 같은 원칙).

설정
    RECODER_TRUSTED_SCOPES=@mycompany,@team  사내 스코프는 검사하지 않는다.
    사내 레지스트리(NPM_CONFIG_REGISTRY 가 npmjs 가 아님)면 공개 정보가 없으므로 검사하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

try:
    from resources import resource as _resource
except ImportError:  # pragma: no cover
    from core.resources import resource as _resource  # type: ignore

YOUNG_DAYS = 30
LOW_WEEKLY = 1_000
TYPO_RATIO = 0.01
#: 한 글자 차이여도 주간 이만큼 쓰이면 자기 사용자층이 있는 별개 패키지로 본다.
TYPO_MAX_WEEKLY = 5_000
CACHE_TTL = 24 * 3600
_TIMEOUT = 6.0
_NAME = re.compile(r"^(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.I)


@dataclass
class Verdict:
    name: str
    level: str                      # ok | warn | block | unknown
    reasons: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)   # typo | young | install_script | no_repo | deprecated
    replacement: Optional[str] = None
    weekly: Optional[int] = None
    created: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 유명 패키지 목록 (core/data/popular_npm.json — scripts/update_popular_npm.py 로 갱신)
# ---------------------------------------------------------------------------

_POPULAR: Optional[dict[str, int]] = None
_POP_LOCK = threading.Lock()


def popular() -> dict[str, int]:
    global _POPULAR
    with _POP_LOCK:
        if _POPULAR is None:
            try:
                data = json.loads(Path(_resource("data", "popular_npm.json")).read_text(encoding="utf-8"))
                _POPULAR = {str(k): int(v) for k, v in (data.get("weekly") or {}).items()}
            except Exception:  # noqa: BLE001 - 목록이 없으면 짝퉁 판정만 건너뛴다
                _POPULAR = {}
        return _POPULAR


def _norm(name: str) -> str:
    """구분자 차이(express_validator · expressvalidator)도 가깝게 본다."""
    return re.sub(r"[-_.]", "", name.lower())


def distance(a: str, b: str, limit: int = 3) -> int:
    """Damerau-Levenshtein(인접 교환 1회) — limit 을 넘으면 limit 을 돌려 빨리 끝낸다."""
    if abs(len(a) - len(b)) >= limit:
        return limit
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            best = min(best, cur[j])
        if best >= limit:
            return limit
        prev2, prev = prev, cur
    return min(prev[-1], limit)


def look_alike(name: str, pop: Optional[dict[str, int]] = None) -> Optional[str]:
    """유명 목록에 없는 이름이 유명 패키지 하나와 한 글자만 다르면(또는 구분자만 다르면) 그 이름.

    두 글자 차이·짧은 이름까지 보면 정상 패키지를 너무 많이 잡는다(실측: npm 검색 표본 896개 중
    15개 오탐 → 한 글자 기준으로 줄임). 타이포스쿼팅은 대부분 한 글자 차이다.
    """
    pop = popular() if pop is None else pop
    if not pop or name in pop:
        return None
    scope, _, base = name.rpartition("/")
    if len(base) < 5:
        return None
    n_base = _norm(base)
    best: tuple[int, str] | None = None
    for cand, weekly in pop.items():
        c_scope, _, c_base = cand.rpartition("/")
        if c_scope != scope or abs(len(c_base) - len(base)) > 1 or len(c_base) < 5:
            continue
        if _norm(c_base) != n_base and distance(base.lower(), c_base.lower(), 2) > 1:
            continue
        if best is None or weekly > best[0]:
            best = (weekly, cand)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 공개 정보 조회 (캐시 24시간)
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    home = Path(os.environ.get("RECODER_HOME") or Path.home() / ".recoder")
    return home / "cache" / "package_trust.json"


_CACHE_LOCK = threading.Lock()


def _cache_load() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cache_save(data: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - 캐시는 없어도 된다
        pass


def _registry() -> str:
    url = (os.environ.get("NPM_CONFIG_REGISTRY") or os.environ.get("npm_config_registry") or "").strip()
    return (url or "https://registry.npmjs.org").rstrip("/")


def _public_registry() -> bool:
    return re.match(r"^https?://registry\.(npmjs\.org|npmjs\.com)$", _registry()) is not None


def _get_json(url: str, timeout: float = _TIMEOUT):
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "recoder-package-trust"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_info(name: str) -> Optional[dict]:
    """{created, weekly, install_script, repository, deprecated, exists} — 알 수 없으면 None."""
    quoted = urllib.parse.quote(name, safe="@")
    try:
        doc = _get_json(f"{_registry()}/{quoted}")
    except urllib.error.HTTPError as exc:
        return {"exists": False} if exc.code == 404 else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(doc, dict):
        return None
    latest = (doc.get("dist-tags") or {}).get("latest")
    manifest = (doc.get("versions") or {}).get(latest) or {}
    scripts = manifest.get("scripts") or {}
    info = {
        "exists": True,
        "created": (doc.get("time") or {}).get("created"),
        "install_script": any(k in scripts for k in ("preinstall", "install", "postinstall")),
        "repository": bool(doc.get("repository") or manifest.get("repository")),
        "deprecated": bool(manifest.get("deprecated")),
        "weekly": None,
    }
    info["weekly"] = fetch_weekly(name)
    return info


def fetch_weekly(name: str) -> Optional[int]:
    """지난주 다운로드 수. downloads API 가 막혀 있으면 검색 API 의 주간 수로 대신한다."""
    try:
        data = _get_json(f"https://api.npmjs.org/downloads/point/last-week/{urllib.parse.quote(name, safe='@')}")
        if isinstance(data, dict) and isinstance(data.get("downloads"), int):
            return data["downloads"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0
    except Exception:  # noqa: BLE001
        pass
    try:
        data = _get_json(f"{_registry()}/-/v1/search?" + urllib.parse.urlencode({"text": name, "size": 20}))
        for obj in (data or {}).get("objects", []):
            if (obj.get("package") or {}).get("name") == name:
                weekly = (obj.get("downloads") or {}).get("weekly")
                return int(weekly) if isinstance(weekly, (int, float)) else None
        return 0  # 검색은 됐는데 결과에 없다 — 아주 드문 패키지
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------

def trusted_scopes() -> set[str]:
    raw = os.environ.get("RECODER_TRUSTED_SCOPES", "")
    return {s.strip().rstrip("/") for s in raw.split(",") if s.strip().startswith("@")}


def _age_days(created: Optional[str], now: Optional[float] = None) -> Optional[float]:
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ((now or time.time()) - dt.timestamp()) / 86400


def judge(name: str, info: Optional[dict], *, pop: Optional[dict[str, int]] = None,
          now: Optional[float] = None) -> Verdict:
    pop = popular() if pop is None else pop
    if name in pop:
        return Verdict(name, "ok", weekly=pop[name])
    if info is None:
        return Verdict(name, "unknown", ["npm 공개 정보를 확인하지 못했습니다."])
    if not info.get("exists", True):
        return Verdict(name, "ok")  # 404 는 기존 검사(NODE_DEPENDENCY_VERSION_NOT_FOUND)가 낸다
    v = Verdict(name, "ok", weekly=info.get("weekly"), created=info.get("created"))
    weekly = info.get("weekly")
    similar = look_alike(name, pop)
    if similar and (weekly is None or weekly < min(TYPO_MAX_WEEKLY, max(LOW_WEEKLY, pop[similar] * TYPO_RATIO))):
        v.level = "block"
        v.kinds.append("typo")
        v.replacement = similar
        v.reasons.append(f"유명 패키지 `{similar}` 와 이름이 거의 같은데 사용량이 매우 적습니다(짝퉁 의심)")
    age = _age_days(info.get("created"), now)
    if age is not None and age < YOUNG_DAYS and weekly is not None and weekly < LOW_WEEKLY:
        v.level = "block"
        v.kinds.append("young")
        v.reasons.append(f"처음 올라온 지 {max(0, round(age))}일, 지난주 다운로드 {weekly:,}회 — 갓 생긴 무명 패키지")
    warn = []
    if info.get("install_script"):
        v.kinds.append("install_script")
        warn.append("설치할 때 실행되는 스크립트가 있습니다")
    if not info.get("repository"):
        v.kinds.append("no_repo")
        warn.append("소스 저장소 정보가 없습니다")
    if info.get("deprecated"):
        v.kinds.append("deprecated")
        warn.append("최신 버전이 사용 중단(deprecated)으로 표시돼 있습니다")
    if warn:
        v.reasons += warn
        if v.level == "ok":
            v.level = "warn"
    return v


def assess(names: list[str], *, fetch: Optional[Callable[[str], Optional[dict]]] = None,
           max_workers: int = 8, now: Optional[float] = None) -> dict[str, Verdict]:
    """패키지 이름들 → 판정. 유명 패키지·신뢰 스코프·사내 레지스트리는 조회하지 않는다."""
    out: dict[str, Verdict] = {}
    pop = popular()
    scopes = trusted_scopes()
    todo = []
    for name in dict.fromkeys(n for n in names if isinstance(n, str)):
        if not _NAME.match(name):
            continue
        if name in pop:
            out[name] = Verdict(name, "ok", weekly=pop[name])
        elif name.startswith("@") and name.split("/", 1)[0] in scopes:
            out[name] = Verdict(name, "ok", ["신뢰 스코프 — 검사 생략"])
        else:
            todo.append(name)
    if not todo:
        return out
    if fetch is None:
        if os.environ.get("RECODER_OFFLINE") == "1" or not _public_registry():
            return out
        if os.environ.get("RECODER_TEST_MODE") == "1" and os.environ.get("RECODER_NPM_REGISTRY") != "1":
            return out
        fetch = _cached_fetch
    with ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
        infos = list(pool.map(fetch, todo))
    for name, info in zip(todo, infos):
        out[name] = judge(name, info, pop=pop, now=now)
    return out


def _cached_fetch(name: str) -> Optional[dict]:
    with _CACHE_LOCK:
        cache = _cache_load()
        hit = cache.get(name)
    if hit and time.time() - hit.get("at", 0) < CACHE_TTL:
        return hit.get("info")
    info = fetch_info(name)
    if info is not None:
        with _CACHE_LOCK:
            cache = _cache_load()
            cache[name] = {"at": time.time(), "info": info}
            # 오래된 항목은 버린다(파일이 끝없이 커지지 않게)
            cache = {k: v for k, v in cache.items() if time.time() - v.get("at", 0) < 7 * CACHE_TTL}
            _cache_save(cache)
    return info


def rename_dependency(text: str, section: str, old: str, new: str, spec: str) -> Optional[str]:
    """package.json 원문의 서식을 지키며 한 의존성의 이름과 범위를 바꾼다."""
    start = text.find(f'"{section}"')
    pattern = re.compile(r'"' + re.escape(old) + r'"(\s*:\s*)"[^"]*"')
    m = pattern.search(text, start if start >= 0 else 0)
    if not m:
        return None
    updated = text[:m.start()] + f'"{new}"{m.group(1)}"{spec}"' + text[m.end():]
    try:
        data = json.loads(updated)
    except ValueError:
        return None
    deps = data.get(section) if isinstance(data, dict) else None
    if not isinstance(deps, dict) or new not in deps:
        return None
    return updated
