"""이미지 취약점(Trivy) → "어디서 왔고 어떻게 고치나".

보안 게이트가 CRITICAL 로 배포를 막을 때 화면에 "CRITICAL 1건" 만 보이면 사용자는
무엇을 고쳐야 할지 모른다. Trivy 결과의 패키지·설치 버전·수정 버전·위치를 읽어
출처(베이스 이미지 OS 패키지 / 베이스 이미지에 번들된 npm / 앱 의존성)를 가르고,
프로젝트의 package.json·requirements.txt 를 보고 구체적인 해결책을 만든다.

판정은 하지 않는다 — 차단 여부는 기존 게이트가 정한다. 여기서는 설명만 만든다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

#: 하위 의존성 취약점이 흔히 어느 직접 의존성에서 오는지. (하위 패키지 → [(직접 의존성, 안전한 버전 범위)])
#: 직접 의존성을 올리는 것이 유일한 정상 해결책인 경우만 적는다.
KNOWN_PARENTS: dict[str, list[tuple[str, str]]] = {
    "tar": [("sqlite3", "^6.0.1")],
    "cacache": [("sqlite3", "^6.0.1")],
    "make-fetch-happen": [("sqlite3", "^6.0.1")],
    "node-gyp": [("sqlite3", "^6.0.1")],
    "@tootallnate/once": [("sqlite3", "^6.0.1")],
}

_BASE_NPM = re.compile(r"(^|/)usr/local/lib/node_modules/(npm|corepack)/|(^|/)opt/yarn", re.I)


def _manifest(workspace: Optional[str]) -> tuple[dict, set[str], bool]:
    """(package.json dependencies, requirements 이름, lock 파일 존재)"""
    deps: dict = {}
    reqs: set[str] = set()
    has_lock = False
    if not workspace:
        return deps, reqs, has_lock
    root = Path(workspace)
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
        if isinstance(package, dict):
            for key in ("dependencies", "optionalDependencies"):
                if isinstance(package.get(key), dict):
                    deps.update(package[key])
    except (OSError, ValueError):
        pass
    has_lock = any((root / name).exists() for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
    try:
        for line in (root / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
            if m and not line.lstrip().startswith(("#", "-")):
                reqs.add(m.group(1).lower().replace("_", "-"))
    except OSError:
        pass
    return deps, reqs, has_lock


def _where(entry: dict) -> str:
    cls = str(entry.get("class") or "")
    pkg_path = str(entry.get("pkg_path") or "")
    if cls == "os-pkgs":
        return "base_os"
    if pkg_path and _BASE_NPM.search(pkg_path):
        return "base_npm"
    if cls == "lang-pkgs" or pkg_path:
        return "app"
    return "unknown"


def _version_major(spec: str) -> Optional[int]:
    m = re.search(r"(\d+)", str(spec or ""))
    return int(m.group(1)) if m else None


def advise(findings: list[dict], workspace: Optional[str] = None, *, severity: str = "CRITICAL") -> dict:
    """CRITICAL(기본) 항목별 출처·해결책과, 화면에 쓸 진단(diagnosis)을 만든다."""
    deps, reqs, has_lock = _manifest(workspace)
    items: list[dict] = []
    fixes: list[str] = []
    seen: set[tuple] = set()
    for f in findings or []:
        if str(f.get("severity") or "").upper() != severity:
            continue
        pkg = str(f.get("package") or "?")
        installed = str(f.get("installed") or "")
        fixed = str(f.get("fixed") or "")
        key = (f.get("id"), pkg, installed)
        if key in seen:
            continue
        seen.add(key)
        where = _where(f)
        if where == "base_os":
            fix = (f"베이스 이미지의 OS 패키지 {pkg} 입니다. 베이스 이미지를 최신으로 받아 다시 빌드하세요 "
                   "(`docker pull <베이스 이미지>` 또는 Dockerfile 런타임 단계에 `RUN apk upgrade --no-cache`)."
                   if fixed else
                   f"베이스 이미지의 OS 패키지 {pkg} 로, 아직 수정 버전이 없습니다. 더 최신이거나 다른 계열(-slim 등)의 베이스 이미지로 바꾸세요.")
        elif where == "base_npm":
            fix = ("베이스 이미지에 들어 있는 npm 이 가진 패키지입니다. 앱을 `node` 로 직접 실행한다면 런타임 단계에서 "
                   "`RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx` 로 npm 을 지우세요.")
        elif pkg in deps:
            fix = (f"package.json 의 {pkg} 를 {fixed} 이상으로 올리세요 (`npm install {pkg}@{fixed.split(',')[0].strip()}`)."
                   if fixed else f"{pkg} 는 아직 수정 버전이 없습니다. 대체 패키지를 검토하세요.")
        elif pkg.lower().replace("_", "-") in reqs:
            fix = (f"requirements.txt 의 {pkg} 를 {fixed} 이상으로 올리세요." if fixed
                   else f"{pkg} 는 아직 수정 버전이 없습니다. 대체 패키지를 검토하세요.")
        else:
            parent = next(((name, safe) for name, safe in KNOWN_PARENTS.get(pkg, [])
                           if name in deps and (_version_major(deps[name]) or 0) < (_version_major(safe) or 0)), None)
            if parent:
                name, safe = parent
                fix = (f"{pkg} 는 {name} {deps[name]} 이(가) 함께 설치하는 하위 패키지입니다. package.json 의 {name} 를 "
                       f"{safe} 로 올리세요" + (" — `npm install " + name + "@" + safe.lstrip('^~') + "` 로 lock 파일도 함께 갱신하세요."
                                              if has_lock else " (배포 준비 점검의 '자동 수정'으로 바로 고칠 수 있습니다)."))
            elif where == "app":
                fix = (f"{pkg} 는 다른 패키지가 함께 설치하는 하위 패키지입니다. `npm audit fix` 로 상위 패키지를 올리거나, "
                       f"package.json 의 overrides 에 \"{pkg}\": \"{fixed.split(',')[0].strip() or 'latest'}\" 를 지정하세요.")
            else:
                fix = f"{pkg} 를 {fixed or '수정된 버전'} 이상으로 올린 뒤 다시 빌드하세요."
        items.append({
            "id": f.get("id") or "", "package": pkg, "installed": installed, "fixed": fixed,
            "title": f.get("title") or "", "origin": where, "fix": fix,
        })
        if fix not in fixes:
            fixes.append(fix)

    origin_label = {"base_os": "베이스 이미지", "base_npm": "베이스 이미지 npm", "app": "앱 의존성", "unknown": "위치 미상"}
    lines = [
        f"{i['id'] or '(ID 없음)'}  {i['package']} {i['installed']} → {i['fixed'] or '수정 버전 없음'}  [{origin_label[i['origin']]}]"
        for i in items[:12]
    ]
    if len(items) > 12:
        lines.append(f"… 외 {len(items) - 12}건")
    n = len(items)
    diagnosis = {
        "code": "IMAGE_CRITICAL_CVE",
        "title": f"보안 검사에서 치명적(CRITICAL) 취약점 {n}건",
        "cause": ("Trivy 가 방금 빌드한 이미지에서 치명적 취약점을 찾아, 실행 중인 컨테이너를 바꾸기 전에 배포를 멈췄습니다. "
                  "기존 컨테이너는 그대로입니다."),
        "fix": " ".join(fixes[:3]) or "취약한 패키지를 수정 버전 이상으로 올린 뒤 다시 배포하세요.",
        "lines": lines,
        "step": "보안 확인 (Trivy 이미지 검사)",
    }
    return {"items": items, "diagnosis": diagnosis}


def _brought_by(pkg_path: str, package: str) -> str:
    """`app/node_modules/foo/node_modules/axios/package.json` → "foo" (그 하위 패키지를 함께 설치한 패키지)."""
    parts = [p for p in re.split(r"/+", str(pkg_path or "").replace("\\", "/")) if p]
    names: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "node_modules" and i + 1 < len(parts):
            name = parts[i + 1]
            if name.startswith("@") and i + 2 < len(parts):
                name = f"{name}/{parts[i + 2]}"
                i += 1
            names.append(name)
            i += 2
            continue
        i += 1
    if len(names) >= 2 and names[-1] == package:
        return names[-2]
    return ""


def trivy_headline(target: str, created: str, findings: list[dict], workspace: Optional[str] = None) -> str:
    """Trivy 결과 한 줄 요약 — **어느 이미지를**, 무엇이(패키지·버전·위치) 문제인지 그대로 적는다.

    예전에는 AI 요약 문장을 보여 줬다. 표본 5건만 보고 쓴 영어 문장이라 "어느 이미지인지"가 없었고
    (실기기: 지금 프로젝트에 없는 axios 를 이 프로젝트 문제로 읽었다), 위치도 알 수 없었다.
    """
    deps, _reqs, _lock = _manifest(workspace)
    crit = sum(1 for f in findings if str(f.get("severity") or "").upper() == "CRITICAL")
    high = sum(1 for f in findings if str(f.get("severity") or "").upper() == "HIGH")
    when = f" · {created} 빌드" if created else ""
    head = f"검사한 이미지 {target}{when} — " + ", ".join(
        part for part in (f"CRITICAL {crit}건" if crit else "", f"HIGH {high}건" if high else "") if part)
    groups: dict[tuple, dict] = {}
    for f in findings:
        key = (str(f.get("package") or "?"), str(f.get("installed") or ""), str(f.get("pkg_path") or ""))
        g = groups.setdefault(key, {"count": 0, "fixed": set(), "entry": f})
        g["count"] += 1
        for v in str(f.get("fixed") or "").split(","):
            if v.strip():
                g["fixed"].add(v.strip())
    lines = []
    for (pkg, installed, pkg_path), g in sorted(groups.items(), key=lambda kv: -kv[1]["count"])[:3]:
        where = _where(g["entry"])
        fixed = max(g["fixed"], key=lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)]) if g["fixed"] else ""
        if where == "base_os":
            origin = "베이스 이미지 OS 패키지"
        elif where == "base_npm":
            origin = "베이스 이미지에 든 npm"
        elif pkg in deps:
            origin = "package.json 의 직접 의존성"
        else:
            parent = _brought_by(pkg_path, pkg)
            origin = f"{parent} 이(가) 함께 설치한 하위 패키지" if parent else "다른 패키지가 함께 설치한 하위 패키지"
        loc = f", {pkg_path}" if pkg_path else ""
        lines.append(f"{pkg} {installed}{' → ' + fixed if fixed else ''} ({g['count']}건 · {origin}{loc})")
    if len(groups) > 3:
        lines.append(f"외 {len(groups) - 3}개 패키지")
    return head + (". " + "; ".join(lines) if lines else "")
