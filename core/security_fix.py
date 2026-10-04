"""보안 검사 결과 → 바로 적용할 수 있는 수정안.

보안 게이트(Trivy·Hadolint·gitleaks)는 문제를 "찾기만" 하고 고치는 것은 사용자에게 맡겼다.
여기서는 각 발견 항목을 **결정론적으로** 고칠 수 있는지 판단해 수정안을 만들고, 사용자가 고른
것만 적용한다(원본은 .recoder/backups). AI 는 쓰지 않는다 — 같은 입력이면 같은 수정이 나온다.

수정안 모양(화면): {id, tool, title, detail, files, diff, auto, risk, note, rebuild}
- auto=False 는 안내만 한다(사람이 판단해야 하는 경우).
- risk 가 있으면(메이저 버전 변경 등) 화면에서 기본으로 고르지 않는다.
- rebuild=True 는 이미지를 다시 빌드해야 Trivy 결과가 바뀐다는 뜻이다.

각 수정안은 "지금 디스크 상태 → 쓸 내용" 을 계산하는 함수(_make)를 들고 있다. 여러 개를
골라도 하나씩 **그 시점의 파일**에 다시 계산해 겹쳐 적용하므로, 앞의 수정이 줄 번호를
밀어도 엉뚱한 줄을 고치지 않는다. 게이트 통과 여부는 판정하지 않는다 — 수정 후 다시 검사한다.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:  # 패키지(core.*)·단독 실행 모두
    import build_readiness as br
    from vuln_advice import _where
except ImportError:  # pragma: no cover
    from core import build_readiness as br  # type: ignore
    from core.vuln_advice import _where  # type: ignore

Writes = dict[str, str]
Maker = Callable[[Path], Optional[Writes]]


@dataclass
class FixProposal:
    id: str
    tool: str
    title: str
    detail: str
    files: list[str] = field(default_factory=list)
    diff: str = ""
    auto: bool = True
    risk: str = ""
    note: str = ""
    rebuild: bool = False
    #: 적용할 때만 쓴다(화면에 보내지 않는다).
    make: Optional[Maker] = field(default=None, repr=False)
    post: list[tuple[str, tuple[str, ...]]] = field(default_factory=list, repr=False)
    #: 백업에도 남기면 안 되는 값(코드에서 .env 로 옮기는 키).
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "tool", "title", "detail", "files", "diff", "auto", "risk", "note", "rebuild")}


def _pid(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _read(root: Path, rel: str) -> Optional[str]:
    """줄바꿈을 \\n 으로 맞춰 읽는다(쓸 때 원래 줄바꿈으로 되돌린다)."""
    try:
        return br._read_raw(root / rel).replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def _diff(rel: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{rel}", f"b/{rel}", n=1))[:6000]


def _preview(root: Path, writes: Optional[Writes], mask: tuple[str, ...] = ()) -> str:
    """화면에 보일 diff. 시크릿 값은 가린다."""
    if not writes:
        return ""
    parts = []
    for rel, after in writes.items():
        before = _read(root, rel) or ""
        text = _diff(rel, before, after)
        for secret in mask:
            text = text.replace(secret, _masked(secret))
        parts.append(text)
    return "".join(parts)[:8000]


def _masked(secret: str) -> str:
    return secret[:4] + "…(가림)"


def _ver(v: str) -> tuple:
    out = []
    for part in re.split(r"[.\-+]", str(v).strip().lstrip("v"))[:4]:
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out + [0] * (4 - len(out)))


def _major(v: str) -> int:
    return _ver(v)[0] if v else 0


def _required_version(installed: str, fixed_lists: list[str]) -> Optional[str]:
    """CVE 마다의 고친 버전 목록 → 모두를 고치는 가장 낮은 버전. 같은 메이저 안에서 고를 수 있으면 그것을 고른다."""
    need: list[str] = []
    for fixed in fixed_lists:
        options = sorted({f.strip() for f in str(fixed or "").split(",") if f.strip() and _ver(f.strip()) > _ver(installed)}, key=_ver)
        if not options:
            return None
        same = [o for o in options if _major(o) == _major(installed)]
        need.append((same or options)[0])
    return max(need, key=_ver) if need else None


# ── Dockerfile (Trivy: 베이스 이미지) ───────────────────────────────────────

_EOL_BASES = (
    (re.compile(r"^(\s*FROM\s+(?:--platform=\S+\s+)?)node:(1[0-9]|20)((?:[.\-][^\s]*)?)(?=\s|$)", re.I | re.M), "node", "22"),
    (re.compile(r"^(\s*FROM\s+(?:--platform=\S+\s+)?)python:(3\.[0-9])((?:[.\-][^\s]*)?)(?=\s|$)", re.I | re.M), "python", "3.12"),
)


def _stage_starts(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if re.match(r"^\s*FROM\s", line, re.I)]


def _base_images(text: str) -> list[str]:
    """실제로 받아 오는 베이스 이미지(앞 단계 이름·scratch·변수 제외)."""
    stages: set[str] = set()
    images: list[str] = []
    for m in re.finditer(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", text, re.I | re.M):
        image = m.group(1)
        if image.lower() not in stages and image != "scratch" and "$" not in image and image not in images:
            images.append(image)
        if m.group(2):
            stages.add(m.group(2).lower())
    return images


def _final_family(text: str) -> str:
    froms = re.findall(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", text, re.I | re.M)
    last = froms[-1].lower() if froms else ""
    if "alpine" in last:
        return "alpine"
    if re.search(r"slim|bookworm|bullseye|trixie|debian|ubuntu|^python:|^node:", last):
        return "debian"
    return ""


def _cmd_index(lines: list[str]) -> Optional[int]:
    return max((i for i, l in enumerate(lines) if re.match(r"^\s*(?:CMD|ENTRYPOINT)\b", l, re.I)
                and not (i > 0 and lines[i - 1].rstrip().endswith("\\"))), default=None)


def _os_upgrade_edit(text: str) -> Optional[str]:
    lines = text.split("\n")
    starts = _stage_starts(lines)
    family = _final_family(text)
    if not starts or not family:
        return None
    final = starts[-1]
    if re.search(r"apk\s+upgrade|apt-get\s+(?:-\S+\s+)*(?:dist-)?upgrade", "\n".join(lines[final:])):
        return None
    step = ("RUN apk upgrade --no-cache" if family == "alpine"
            else "RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*")
    #: FROM 바로 다음(아직 root) — 뒤의 USER 보다 앞이어야 한다.
    lines[final + 1:final + 1] = ["# ReCoder: 베이스 이미지 OS 패키지의 알려진 취약점을 패치한다(보안 검사).", step]
    return "\n".join(lines)


def _eol_edit(text: str) -> Optional[str]:
    updated = text
    for pattern, name, version in _EOL_BASES:
        updated = pattern.sub(lambda m: f"{m.group(1)}{name}:{version}{m.group(3)}", updated)
    #: ARG NODE_VERSION=20 처럼 변수로 받는 경우
    updated = re.sub(r"^(\s*ARG\s+NODE_VERSION=)(1[0-9]|20)\b", r"\g<1>22", updated, flags=re.I | re.M)
    return updated if updated != text else None


def _npm_removal_edit(text: str) -> Optional[str]:
    if "node_modules/npm" in text:
        return None
    lines = text.split("\n")
    starts = _stage_starts(lines)
    if not starts:
        return None
    final = starts[-1]
    cmd_idx = _cmd_index(lines)
    if cmd_idx is not None and re.search(r"\b(?:npm|npx|yarn|pnpm)\b", lines[cmd_idx]):
        return None
    users = [i for i in range(final, len(lines)) if re.match(r"^\s*USER\b", lines[i], re.I)]
    at = users[0] if users else (cmd_idx if cmd_idx is not None and cmd_idx > final else len(lines))
    lines[at:at] = ["# ReCoder: 앱은 node 로 바로 뜬다 — 이미지에 들어 있는 npm(취약한 하위 패키지 동봉)을 지운다.", br._NPM_REMOVAL]
    return "\n".join(lines)


def _dockerfile_maker(edit: Callable[[str], Optional[str]]) -> Maker:
    def make(root: Path) -> Optional[Writes]:
        text = _read(root, "Dockerfile")
        out = edit(text) if text is not None else None
        return {"Dockerfile": out} if out else None
    return make


def _dockerfile_fixes(root: Path, items: list[dict]) -> list[FixProposal]:
    text = _read(root, "Dockerfile")
    if text is None:
        return []
    out: list[FixProposal] = []
    os_items = [i for i in items if _where(i) == "base_os"]
    npm_items = [i for i in items if _where(i) == "base_npm"]
    if os_items:
        pkgs = sorted({str(i.get("package")) for i in os_items})
        names = f"{', '.join(pkgs[:5])}{' 외' if len(pkgs) > 5 else ''}"
        make = _dockerfile_maker(_os_upgrade_edit)
        writes = make(root)
        if writes:
            out.append(FixProposal(
                id=_pid("trivy", "base_os", pkgs), tool="trivy", rebuild=True,
                title=f"베이스 이미지 OS 패키지 {len(pkgs)}개 패치",
                detail=f"{names} — 실행 단계에서 OS 패키지를 최신 보안 패치로 올립니다.",
                files=["Dockerfile"], diff=_preview(root, writes), make=make))
        images = _base_images(text)
        if images:
            #: 업그레이드 단계가 이미 있어도 Docker 는 그 RUN 결과를 캐시해 둔다 — 옛 베이스 이미지를
            #: 새로 받아야 다음 빌드에서 패치가 실제로 들어간다.
            out.append(FixProposal(
                id=_pid("trivy", "pull", images), tool="trivy", rebuild=True,
                title="베이스 이미지 새로 받기",
                detail=f"{', '.join(images)} 를 다시 받습니다(docker pull). PC 에 남은 옛 베이스 이미지와 캐시된 "
                       "패키지 설치 결과 때문에 이미 고쳐진 취약점이 계속 남는 경우가 많습니다.",
                files=[], make=lambda _root: {}, post=[("", ("docker", "pull", image)) for image in images],
                note="수정 버전이 아직 없는 OS 패키지는 베이스 이미지 계열(-slim·-alpine)을 바꿔야 할 수 있습니다."))
    if items:
        make = _dockerfile_maker(_eol_edit)
        writes = make(root)
        if writes:
            out.append(FixProposal(
                id=_pid("trivy", "eol"), tool="trivy", rebuild=True,
                title="지원이 끝난 런타임 버전 올리기",
                detail="Node 20 이하·Python 3.9 이하 베이스 이미지는 보안 패치가 끝났습니다. Node 22 / Python 3.12 로 바꿉니다.",
                files=["Dockerfile"], diff=_preview(root, writes), make=make,
                risk="런타임 메이저 버전이 바뀝니다. 네이티브 모듈·오래된 문법을 쓰면 빌드나 실행이 달라질 수 있습니다."))
    if npm_items:
        pkgs = sorted({str(i.get("package")) for i in npm_items})
        make = _dockerfile_maker(_npm_removal_edit)
        writes = make(root)
        lines = text.split("\n")
        cmd_idx = _cmd_index(lines)
        if writes:
            out.append(FixProposal(
                id=_pid("trivy", "base_npm", pkgs), tool="trivy", rebuild=True,
                title=f"이미지에 들어 있는 npm 제거 ({len(pkgs)}개 패키지 취약점)",
                detail=f"{', '.join(pkgs[:5])} 는 베이스 이미지에 함께 들어 있는 npm 의 하위 패키지입니다. "
                       "앱은 node 로 바로 뜨므로 실행 이미지에서 npm 을 지웁니다.",
                files=["Dockerfile"], diff=_preview(root, writes), make=make))
        elif cmd_idx is not None and re.search(r"\b(?:npm|npx|yarn|pnpm)\b", lines[cmd_idx]):
            out.append(FixProposal(
                id=_pid("trivy", "base_npm_manual"), tool="trivy", auto=False,
                title="이미지에 들어 있는 npm 의 취약점",
                detail=f"{', '.join(pkgs[:5])} 는 npm 에 함께 들어 있는 패키지입니다. 실행 명령(CMD)이 npm 으로 앱을 "
                       "띄워서 npm 을 지울 수 없습니다 — CMD 를 node 로 바로 실행하게 바꾸면 지울 수 있습니다.",
                files=["Dockerfile"]))
    return out


# ── npm 의존성 ──────────────────────────────────────────────────────────────

def _manifest_for(pkg_path: str, root: Path) -> str:
    """Trivy PkgPath(app/backend/node_modules/x/package.json) → 그 의존성을 설치한 package.json."""
    p = str(pkg_path or "").replace("\\", "/").lstrip("/")
    p = re.sub(r"^(?:app|usr/src/app|srv|workspace|home/node/app|opt/app)/", "", p)
    folder = p.split("/node_modules/")[0] if "/node_modules/" in p else ""
    folder = folder.strip("/")
    rel = f"{folder}/package.json" if folder else "package.json"
    return rel if (root / rel).is_file() else "package.json"


def _json_dump_like(before: str, data: dict) -> str:
    m = re.search(r'\n([ \t]+)"', before)
    indent = m.group(1) if m else "  "
    return json.dumps(data, ensure_ascii=False, indent=indent) + "\n"


def _npm_maker(manifest: str, pkg: str, target: str) -> Maker:
    def make(root: Path) -> Optional[Writes]:
        raw = _read(root, manifest)
        try:
            data = json.loads(raw or "")
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        section = next((k for k in ("dependencies", "devDependencies", "optionalDependencies")
                        if isinstance(data.get(k), dict) and pkg in data[k]), None)
        spec = f"^{target}"
        if section:
            current = str(data[section][pkg])
            low = re.search(r"\d+(?:\.\d+){0,3}", current)
            if low and _ver(low.group(0)) >= _ver(target) and not current.startswith("<"):
                return None  # 이미 고친 범위
            data[section][pkg] = spec
        else:
            overrides = data.setdefault("overrides", {})
            if not isinstance(overrides, dict) or overrides.get(pkg) == spec:
                return None
            overrides[pkg] = spec
        return {manifest: _json_dump_like(raw or "", data)}
    return make


def _npm_fixes(root: Path, items: list[dict]) -> list[FixProposal]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for item in items:
        if _where(item) != "app" or str(item.get("class") or "") == "os-pkgs":
            continue
        if str(item.get("type") or "") not in ("", "node-pkg", "npm", "yarn", "pnpm"):
            continue
        manifest = _manifest_for(str(item.get("pkg_path") or ""), root)
        if not (root / manifest).is_file():
            continue
        groups.setdefault((manifest, str(item.get("package")), str(item.get("installed"))), []).append(item)
    out: list[FixProposal] = []
    for (manifest, pkg, installed), group in sorted(groups.items()):
        target = _required_version(installed, [str(g.get("fixed") or "") for g in group])
        if not target:
            out.append(FixProposal(id=_pid("trivy", "npm-none", manifest, pkg, installed), tool="trivy", auto=False,
                                   title=f"{pkg} {installed} — 고친 버전이 아직 없음",
                                   detail=f"취약점 {len(group)}건에 수정 버전이 없습니다. 이 패키지를 쓰는 곳을 줄이거나 대체 패키지를 검토하세요.",
                                   files=[manifest]))
            continue
        try:
            data = json.loads(_read(root, manifest) or "")
        except ValueError:
            continue
        direct = any(isinstance(data.get(k), dict) and pkg in data[k] for k in ("dependencies", "devDependencies", "optionalDependencies"))
        folder = manifest.rsplit("/", 1)[0] if "/" in manifest else ""
        lock = next((n for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml") if (root / folder / n).is_file()), None)
        if lock in ("yarn.lock", "pnpm-lock.yaml") and not direct:
            out.append(FixProposal(id=_pid("trivy", "npm-manual", manifest, pkg, installed), tool="trivy", auto=False,
                                   title=f"{pkg} {installed} → {target} ({len(group)}건)",
                                   detail=f"{pkg} 는 다른 패키지가 설치하는 하위 패키지입니다. {lock} 를 쓰는 프로젝트라 "
                                          f"{'resolutions' if lock == 'yarn.lock' else 'pnpm.overrides'} 에 {pkg}@^{target} 를 지정한 뒤 설치하세요.",
                                   files=[manifest, f"{folder}/{lock}".lstrip("/")]))
            continue
        make = _npm_maker(manifest, pkg, target)
        writes = make(root)
        if not writes:
            continue
        how = (f"package.json 의 {pkg} 를 ^{target} 로 올립니다" if direct else
               f"{pkg} 는 다른 패키지가 함께 설치하는 하위 패키지라 package.json 의 overrides 로 ^{target} 를 지정합니다")
        post: list[tuple[str, tuple[str, ...]]] = []
        note = ""
        if lock == "package-lock.json":
            post = [(folder, ("npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"))]
            note = "package-lock.json 도 함께 갱신합니다(npm install --package-lock-only)."
        elif lock:
            note = f"{lock} 는 자동으로 갱신하지 못합니다 — 적용 후 그 폴더에서 설치 명령을 한 번 실행하세요."
        risk = "" if _major(target) == _major(installed) else f"메이저 버전이 바뀝니다({installed} → {target}). 이 패키지를 쓰는 코드가 깨질 수 있습니다."
        out.append(FixProposal(
            id=_pid("trivy", "npm", manifest, pkg, installed, target), tool="trivy", rebuild=True,
            title=f"{pkg} {installed} → {target} (취약점 {len(group)}건)", detail=f"{how}.",
            files=[manifest] + ([f"{folder}/{lock}".lstrip("/")] if lock else []),
            diff=_preview(root, writes), risk=risk, note=note, make=make, post=post))
    return out


# ── Python 의존성 ───────────────────────────────────────────────────────────

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(?:(==|>=|~=|<=|>|<|!=)\s*([^\s;#,]*))?")


def _pip_maker(pkg: str, target: str) -> Maker:
    norm = pkg.lower().replace("_", "-")

    def make(root: Path) -> Optional[Writes]:
        req = _read(root, "requirements.txt")
        if req is None:
            return None
        lines = req.split("\n")
        for i, line in enumerate(lines):
            m = _REQ_LINE.match(line)
            if not m or m.group(1).lower().replace("_", "-") != norm:
                continue
            if m.group(4) and m.group(3) in ("==", ">=", "~=") and _ver(m.group(4)) >= _ver(target):
                return None
            lines[i] = f"{m.group(1)}{m.group(2) or ''}=={target}{line[m.end():]}"
            return {"requirements.txt": "\n".join(lines)}
        body = req.rstrip("\n")
        return {"requirements.txt": (body + "\n" if body else "") + f"{pkg}>={target}  # ReCoder: 보안 검사 — 하위 의존성 수정 버전\n"}
    return make


def _python_fixes(root: Path, items: list[dict]) -> list[FixProposal]:
    if _read(root, "requirements.txt") is None:
        return []
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        if str(item.get("type") or "") in ("python-pkg", "pip", "poetry", "pipenv"):
            groups.setdefault((str(item.get("package")), str(item.get("installed"))), []).append(item)
    out: list[FixProposal] = []
    for (pkg, installed), group in sorted(groups.items()):
        target = _required_version(installed, [str(g.get("fixed") or "") for g in group])
        if not target:
            continue
        make = _pip_maker(pkg, target)
        writes = make(root)
        if not writes:
            continue
        risk = "" if _major(target) == _major(installed) else f"메이저 버전이 바뀝니다({installed} → {target})."
        out.append(FixProposal(id=_pid("trivy", "pip", pkg, installed, target), tool="trivy", rebuild=True,
                               title=f"{pkg} {installed} → {target} (취약점 {len(group)}건)",
                               detail=f"requirements.txt 에 {pkg} {target} 이상을 지정합니다.",
                               files=["requirements.txt"], diff=_preview(root, writes), risk=risk, make=make))
    return out


# ── Hadolint ───────────────────────────────────────────────────────────────

def _hadolint_line_fix(code: str, line: str) -> tuple[str, str]:
    """(고친 줄, 제목). 고칠 수 없으면 (원래 줄, "")."""
    if code == "DL3020" and re.match(r"^\s*ADD\s", line, re.I) and not re.search(r"https?://|\.(?:tar|tgz|gz|bz2|xz|zip)\b", line):
        return re.sub(r"^(\s*)ADD\b", r"\1COPY", line, count=1, flags=re.I), "파일 복사는 ADD 대신 COPY"
    if code == "DL3025":
        m = re.match(r"^(\s*(?:CMD|ENTRYPOINT)\s+)(?!\[)(.+?)\s*$", line, re.I)
        if m and not re.search(r"[|&;<>$`\\'\"*?]", m.group(2)):
            return m.group(1) + json.dumps(m.group(2).split(), ensure_ascii=False).replace('","', '", "'), "실행 명령을 JSON 형식으로(종료 신호가 앱에 바로 전달)"
    if code == "DL3015" and "apt-get install" in line and "--no-install-recommends" not in line:
        return line.replace("apt-get install", "apt-get install --no-install-recommends", 1), "apt-get 추천 패키지 설치하지 않기"
    if code == "DL3042" and re.search(r"\bpip3?\s+install\b", line) and "--no-cache-dir" not in line:
        return re.sub(r"\b(pip3?\s+install)\b", r"\1 --no-cache-dir", line, count=1), "pip 캐시를 이미지에 남기지 않기"
    if code == "DL3019" and re.search(r"\bapk\s+add\b", line) and "--no-cache" not in line:
        return re.sub(r"\bapk\s+add\b", "apk add --no-cache", line, count=1), "apk 캐시를 이미지에 남기지 않기"
    if code == "DL3000":
        m = re.match(r"^(\s*WORKDIR\s+)(?![/$])(\S+)", line, re.I)
        if m:
            return m.group(1) + "/" + m.group(2) + line[m.end():], "WORKDIR 를 절대 경로로"
    if code == "DL3004" and re.search(r"\bsudo\s+", line):
        return re.sub(r"\bsudo\s+", "", line), "RUN 에서 sudo 빼기(빌드는 이미 root)"
    return line, ""


def _shell_to_exec(prefix: str, command: str) -> str:
    """셸 형식 명령을 같은 동작의 JSON 형식으로. Docker 의 셸 형식은 `/bin/sh -c "<명령>"` 과 같다."""
    return prefix + json.dumps(["/bin/sh", "-c", command], ensure_ascii=False).replace('","', '", "')


def _hadolint_block_fix(code: str, lines: list[str], n: int) -> Optional[tuple[list[str], str]]:
    """여러 줄을 바꾸는 수정. (새 줄 목록, 제목) 또는 None."""
    if code == "DL3003":
        m = re.match(r"^(\s*)RUN\s+cd\s+([\w.\-/]+)\s*&&\s*(.+?)\s*$", lines[n])
        if not m or lines[n].rstrip().endswith("\\") or ".." in m.group(2).split("/") or re.search(r"\bcd\s", m.group(3)):
            return None
        workdir = br._stage_workdir(lines, n)
        if not workdir:
            return None
        folder = m.group(2).strip("/")
        if m.group(2).startswith("/"):
            new = [f"{m.group(1)}WORKDIR {m.group(2)}", f"{m.group(1)}RUN {m.group(3)}", f"{m.group(1)}WORKDIR {workdir}"]
        else:
            new = [m.group(1) + l for l in br._in_folder(folder, m.group(3), workdir)]
        return lines[:n] + new + lines[n + 1:], "RUN 안의 cd 대신 WORKDIR"
    if code == "DL3025":
        if any(re.match(r"^\s*SHELL\s", l, re.I) for l in lines):
            return None  # SHELL 을 바꾼 Dockerfile 은 셸 형식의 뜻이 달라진다 — 손대지 않는다.
        end = n
        while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
            end += 1
        if not re.match(r"^\s*(?:CMD|ENTRYPOINT|HEALTHCHECK)\b", lines[n], re.I):
            return None
        for i in range(end, n - 1, -1):
            m = re.match(r"^(.*?\b(?:CMD|ENTRYPOINT)\s+)(?!\[)(.+?)\s*$", lines[i], re.I)
            if m and i == end:
                if re.match(r"^\s*(?:CMD|ENTRYPOINT)\b", lines[n], re.I) and n != end:
                    return None  # 여러 줄에 걸친 실행 명령은 직접 고친다
                if not re.search(r"[|&;<>$`\\'\"*?(){}~]", m.group(2)) and not lines[n].lstrip().upper().startswith("HEALTHCHECK"):
                    return None  # 단순한 명령은 한 줄 수정(_hadolint_line_fix)이 맡는다
                new = list(lines)
                new[i] = _shell_to_exec(m.group(1), m.group(2))
                return new, "실행 명령을 JSON 형식으로(셸 동작은 그대로)"
        return None
    return None


def _nearest(lines: list[str], wanted: str, hint: int) -> Optional[int]:
    hits = [i for i, l in enumerate(lines) if l == wanted]
    return min(hits, key=lambda i: abs(i - hint)) if hits else None


def _hadolint_maker(code: str, original: str, hint: int) -> Maker:
    def make(root: Path) -> Optional[Writes]:
        text = _read(root, "Dockerfile")
        if text is None:
            return None
        lines = text.split("\n")
        n = _nearest(lines, original, hint)
        if n is None:
            return None
        if code == "DL3009":
            end = n
            while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
                end += 1
            if "rm -rf /var/lib/apt/lists" in "\n".join(lines[n:end + 1]):
                return None
            lines[end] = lines[end].rstrip() + " && rm -rf /var/lib/apt/lists/*"
        else:
            block = _hadolint_block_fix(code, lines, n)
            if block:
                lines = block[0]
            else:
                new, title = _hadolint_line_fix(code, lines[n])
                if not title or new == lines[n]:
                    return None
                lines[n] = new
        return {"Dockerfile": "\n".join(lines)}
    return make


def _hadolint_fixes(root: Path, violations: list[dict]) -> list[FixProposal]:
    text = _read(root, "Dockerfile")
    if text is None:
        return []
    lines = text.split("\n")
    out: list[FixProposal] = []
    manual_lint: dict[str, list[tuple[int, str]]] = {}
    for v in violations:
        code = str(v.get("code") or "")
        try:
            n = int(v.get("line") or 0) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(lines):
            continue
        message = str(v.get("message") or "").strip()[:160]
        if code == "DL3002":
            out.append(FixProposal(id=_pid("hadolint", code, n), tool="hadolint", auto=False,
                                   title="컨테이너가 root 로 실행됨 (DL3002)",
                                   detail="마지막 USER 가 root 입니다. 앱 전용 사용자를 만들고 USER 로 바꾸세요 — 앱이 쓰는 폴더 권한도 함께 확인해야 합니다.",
                                   files=["Dockerfile"]))
            continue
        block = _hadolint_block_fix(code, lines, n)
        title = "apt 목록 캐시 지우기" if code == "DL3009" else (block[1] if block else _hadolint_line_fix(code, lines[n])[1])
        if not title:
            manual_lint.setdefault(code, []).append((n, message))
            continue
        make = _hadolint_maker(code, lines[n], n)
        writes = make(root)
        if writes:
            out.append(FixProposal(id=_pid("hadolint", code, n, lines[n]), tool="hadolint", title=f"{title} ({code})",
                                   detail=f"Dockerfile {n + 1}번째 줄",
                                   files=["Dockerfile"], diff=_preview(root, writes), make=make))
    for code, hits in sorted(manual_lint.items()):
        where = ", ".join(str(n + 1) for n, _ in hits[:6])
        out.append(FixProposal(id=_pid("hadolint", "manual", code, where), tool="hadolint", auto=False,
                               title=f"{code} — 직접 확인 ({len(hits)}곳)",
                               detail=f"Dockerfile {where}번째 줄: {hits[0][1] or code}"
                                      + (" 버전 고정은 재현 가능한 빌드를 위한 권고이며, 그 자체로 취약점은 아닙니다." if code in ("DL3008", "DL3018", "DL3013", "DL3016") else ""),
                               files=["Dockerfile"]))
    return out


# ── gitleaks ───────────────────────────────────────────────────────────────

_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_BROWSER_HINT = re.compile(r"(^|/)(client|frontend|web|public|static|src/components|src/pages|src/app)/|\.(jsx|tsx|vue|svelte)$")
_ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]+)?$")


def _env_name(line: str, rule: str) -> str:
    m = re.search(r"\b(?:const|let|var|final)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=", line) \
        or re.search(r"""['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?\s*[:=]\s*['"`]""", line) \
        or re.search(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    raw = m.group(1) if m else rule
    name = re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)).strip("_").upper()
    return name if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) else "SECRET_KEY"


def _secret_literal(line: str) -> Optional[tuple[str, str]]:
    """그 줄에서 키처럼 보이는 가장 긴 따옴표 문자열 → (따옴표 포함 원문, 값)."""
    best: Optional[tuple[str, str]] = None
    for m in re.finditer(r"""(['"`])([^'"`\s]{12,})\1""", line):
        value = m.group(2)
        if "${" in value or value.startswith(("http://localhost", "./", "../", "/")):
            continue
        if best is None or len(value) > len(best[1]):
            best = (m.group(0), value)
    return best


def _ignores(text: str, rel: str) -> bool:
    """.gitignore·.dockerignore 가 이 파일(.env 류)을 빼는지 — 흔한 패턴만 본다."""
    base = rel.rsplit("/", 1)[-1]
    rules = [l.strip() for l in (text or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    hit = False
    for rule in rules:
        neg = rule.startswith("!")
        pat = rule[1:] if neg else rule
        pat = pat.lstrip("/").removeprefix("**/")
        folder = pat.rstrip("/")
        if (pat in (base, rel) or (pat.endswith("*") and base.startswith(pat[:-1])) or pat in (".env*", "*.env")
                or (folder and rel.startswith(folder + "/"))):
            hit = not neg
    return hit


def local_env_only(root: Path, rel: str) -> bool:
    """.env 류(또는 ReCoder 의 .recoder 폴더)가 커밋에서도 이미지에서도 빠지는지.
    그렇다면 PC 에만 있는 파일이다 — 키를 보관하는 올바른 곳이고 유출이 아니다."""
    rel = rel.lstrip("/")
    env_like = _ENV_FILE.search(rel) and not rel.endswith((".example", ".sample", ".template"))
    if not env_like and not rel.startswith(".recoder/"):
        return False
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    git_ok = any(_ignores(_read(root, f"{d}/.gitignore".lstrip("/")) or "", rel if not d else rel[len(d) + 1:])
                 for d in {"", folder})
    docker_ok = (not (root / "Dockerfile").is_file()) or _ignores(_read(root, ".dockerignore") or "", rel)
    return git_ok and docker_ok



def _ignore_writes(root: Path) -> Writes:
    """.gitignore·.dockerignore 에 .env 와 .recoder(백업)가 빠져 있으면 그 줄만 덧붙인다."""
    writes: Writes = {}
    git = _read(root, ".gitignore") or ""
    add = []
    if not _ignores(git, ".env"):
        add += [".env", ".env.*", "!.env.example"]
    if not _ignores(git, ".recoder/x"):
        add.append(".recoder/")
    if add:
        writes[".gitignore"] = _append(git, "# ReCoder: 키가 든 .env·ReCoder 백업은 커밋하지 않는다\n" + "\n".join(add) + "\n")
    if (root / "Dockerfile").is_file():
        docker = _read(root, ".dockerignore") or ""
        add = ([".env", ".env.*"] if not _ignores(docker, ".env") else []) + ([".recoder"] if not _ignores(docker, ".recoder/x") else [])
        if add:
            writes[".dockerignore"] = _append(docker, "# ReCoder: 키는 이미지에 넣지 않는다(실행할 때 환경변수로 넘긴다)\n" + "\n".join(add) + "\n")
    return writes


def _append(text: str, block: str) -> str:
    body = (text or "").rstrip("\n")
    return (body + "\n" if body else "") + block


def _env_value(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s#'\"]", value) else value


def _move_secret_maker(rel: str, literals: list[tuple[str, str, str]], is_py: bool) -> Maker:
    """literals: (따옴표 포함 원문, 값, 환경변수 이름)."""
    def make(root: Path) -> Optional[Writes]:
        text = _read(root, rel)
        if text is None:
            return None
        env = _read(root, ".env") or ""
        existing = {}
        for line in env.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
        after = text
        add: list[str] = []
        used: set[str] = set()
        for quoted, value, name in literals:
            if quoted not in after:
                continue
            final = name
            n = 2
            while (final in existing and existing[final] != value) or final in used:
                final, n = f"{name}_{n}", n + 1
            used.add(final)
            ref = f'os.environ.get("{final}", "")' if is_py else f"process.env.{final}"
            after = after.replace(quoted, ref)
            if final not in existing:
                add.append(f"{final}={_env_value(value)}")
                existing[final] = value
        if after == text:
            return None
        if is_py and not re.search(r"^\s*import\s+os\b|^\s*from\s+os\s+import", after, re.M):
            lines = after.split("\n")
            at = 0
            while at < len(lines) and (lines[at].startswith("#!") or re.match(r"^\s*#.*coding[:=]", lines[at]) or lines[at].startswith("from __future__")):
                at += 1
            lines.insert(at, "import os")
            after = "\n".join(lines)
        writes: Writes = {rel: after}
        if add:
            writes[".env"] = _append(env, "\n".join(add) + "\n")
        example = _read(root, ".env.example")
        if example is not None or not (root / ".env.example").exists():
            ex_keys = {l.split("=", 1)[0].strip() for l in (example or "").splitlines() if "=" in l}
            new_keys = [k for k in used if k not in ex_keys]
            if new_keys:
                writes[".env.example"] = _append(example or "", "\n".join(f"{k}=" for k in sorted(new_keys)) + "\n")
        writes.update(_ignore_writes(root))
        return writes
    return make


def _gitignore_maker(root_: Path) -> Maker:
    def make(root: Path) -> Optional[Writes]:
        return _ignore_writes(root) or None
    return make


def _reads_dotenv(root: Path, rel: str, is_py: bool) -> bool:
    if is_py:
        req = (_read(root, "requirements.txt") or "") + (_read(root, "pyproject.toml") or "")
        return "dotenv" in req.lower()
    folder = rel.split("/")[0] if "/" in rel else ""
    pkgs = (_read(root, "package.json") or "") + (_read(root, f"{folder}/package.json") or "" if folder else "")
    return "dotenv" in pkgs or bool(re.search(r"--env-file", pkgs))


def gitleaks_rel(file: str) -> str:
    return re.sub(r"^/?repo/", "", str(file or "").replace("\\", "/")).lstrip("/")


def _gitleaks_fixes(root: Path, findings: list[dict]) -> list[FixProposal]:
    out: list[FixProposal] = []
    by_file: dict[str, list[dict]] = {}
    for f in findings:
        rel = gitleaks_rel(str(f.get("file") or ""))
        if not rel or ".." in rel.split("/"):
            continue
        by_file.setdefault(rel, []).append(f)
    for rel, items in sorted(by_file.items()):
        if _ENV_FILE.search(rel) and not rel.endswith((".example", ".sample", ".template")):
            if local_env_only(root, rel):
                out.append(FixProposal(id=_pid("gitleaks", "env-local", rel), tool="gitleaks", auto=False,
                                       title=f"{rel} — PC 에만 있는 키(유출 아님)",
                                       detail=".gitignore 가 커밋에서, .dockerignore 가 이미지에서 이 파일을 뺍니다. 키를 보관하는 올바른 위치라 "
                                              "게이트에서 세지 않습니다. 이 파일을 예전에 커밋한 적이 있다면 키를 교체(rotate)하세요."))
                continue
            make = _gitignore_maker(root)
            writes = make(root)
            if writes:
                out.append(FixProposal(id=_pid("gitleaks", "ignore-env", rel), tool="gitleaks",
                                       title=f"{rel} 를 커밋·이미지에서 빼기",
                                       detail=f"{rel} 에 키가 있는데 .gitignore·.dockerignore 가 이 파일을 빼지 않습니다.",
                                       files=sorted(writes), diff=_preview(root, writes), make=make,
                                       note="이미 커밋했거나 이미지에 들어간 적이 있다면 그 키는 교체(rotate)하세요 — 기록에 남아 있습니다."))
            continue
        text = _read(root, rel)
        if text is None:
            continue
        lines = text.split("\n")
        is_js, is_py = rel.endswith(_JS), rel.endswith(".py")
        literals: list[tuple[str, str, str]] = []
        manual: list[str] = []
        for f in items:
            try:
                n = int(f.get("line") or 0) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= n < len(lines):
                continue
            lit = _secret_literal(lines[n])
            if lit is None or not (is_js or is_py):
                manual.append(f"{n + 1}번째 줄")
            elif is_js and _BROWSER_HINT.search(rel):
                manual.append(f"{n + 1}번째 줄(브라우저에서 실행되는 코드)")
            elif all(lit[0] != q for q, _, _ in literals):
                literals.append((lit[0], lit[1], _env_name(lines[n], str(f.get("rule_id") or "SECRET"))))
        if literals:
            make = _move_secret_maker(rel, literals, is_py)
            writes = make(root)
            if writes:
                names = [name for _, _, name in literals]
                risk = "" if _reads_dotenv(root, rel, is_py) else (
                    "이 앱은 .env 를 직접 읽지 않습니다(dotenv 없음). ReCoder 로컬 Docker 배포는 .env 값을 넘기지만, "
                    "그 밖에서 실행하면 값이 비게 됩니다.")
                out.append(FixProposal(
                    id=_pid("gitleaks", "move", rel, [q for q, _, _ in literals]), tool="gitleaks",
                    title=f"{rel} 의 키 {len(literals)}개를 .env 로 옮기기",
                    detail=f"코드에 적힌 값을 환경변수({', '.join(names)})로 읽게 바꾸고, 값은 커밋·이미지에서 빠지는 .env 에 둡니다.",
                    files=sorted(writes), diff=_preview(root, writes, tuple(v for _, v, _ in literals)), make=make, risk=risk,
                    secrets=tuple(v for _, v, _ in literals),
                    note="이 키가 이미 저장소나 다른 사람에게 갔다면 발급처에서 키를 교체(rotate)하세요."))
        if manual:
            out.append(FixProposal(id=_pid("gitleaks", "manual", rel, manual), tool="gitleaks", auto=False,
                                   title=f"{rel} 의 키 — 직접 옮겨야 함",
                                   detail=f"{', '.join(manual[:5])}: 자동으로 바꿀 수 없는 위치입니다. 브라우저 코드의 키는 서버 쪽으로 옮기고, "
                                          "그 밖에는 값을 .env 로 옮긴 뒤 환경변수로 읽게 하세요.", files=[rel]))
    return out


def _unhandled_trivy(root: Path, items: list[dict]) -> list[FixProposal]:
    """위 수정기가 다루지 않는 항목(Go 바이너리·jar·다른 매니페스트 등)도 조용히 버리지 않는다."""
    has_dockerfile = (root / "Dockerfile").is_file()
    has_req = (root / "requirements.txt").is_file()
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for item in items:
        where, typ = _where(item), str(item.get("type") or "")
        if where in ("base_os", "base_npm") and has_dockerfile:
            continue
        if where == "app" and typ in ("", "node-pkg", "npm", "yarn", "pnpm") and (root / _manifest_for(str(item.get("pkg_path") or ""), root)).is_file():
            continue
        if typ in ("python-pkg", "pip", "poetry", "pipenv") and has_req:
            continue
        groups.setdefault((typ or where, str(item.get("package")), str(item.get("installed"))), []).append(item)
    out = []
    for (typ, pkg, installed), group in sorted(groups.items()):
        fixed = _required_version(installed, [str(g.get("fixed") or "") for g in group])
        out.append(FixProposal(
            id=_pid("trivy", "manual", typ, pkg, installed), tool="trivy", auto=False,
            title=f"{pkg} {installed}" + (f" → {fixed}" if fixed else " — 고친 버전 없음") + f" (취약점 {len(group)}건)",
            detail=(f"{typ} 패키지라 자동으로 고치지 못합니다. " if typ else "")
                   + (f"{pkg} 를 {fixed} 이상으로 올린 뒤 이미지를 다시 빌드하세요." if fixed else "대체 패키지나 베이스 이미지 변경을 검토하세요."),
            files=[]))
    return out


# ── 공개 API ───────────────────────────────────────────────────────────────

def _findings(reports: dict, kind: str) -> list[dict]:
    rep = (reports or {}).get(kind) or {}
    if not isinstance(rep, dict):
        return []
    items = rep.get("findings") or rep.get("violations") or []
    return [f for f in items if isinstance(f, dict)] if isinstance(items, list) else []


def plan(workspace: str, reports: dict[str, Any]) -> list[FixProposal]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("유효한 프로젝트 폴더가 아닙니다.")
    proposals: list[FixProposal] = []
    items = _findings(reports, "trivy")
    if items:
        proposals += _dockerfile_fixes(root, items)
        proposals += _npm_fixes(root, items)
        proposals += _python_fixes(root, items)
        proposals += _unhandled_trivy(root, items)
    viol = _findings(reports, "hadolint")
    if viol:
        proposals += _hadolint_fixes(root, viol)
    secrets = _findings(reports, "gitleaks")
    if secrets:
        proposals += _gitleaks_fixes(root, secrets)
    seen: set[str] = set()
    unique = []
    for p in proposals:
        if p.id not in seen:
            seen.add(p.id)
            unique.append(p)
    return unique


def _run_post(root: Path, cwd: str, cmd: tuple[str, ...]) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        return f"{' '.join(cmd)} 를 실행하지 못했습니다({cmd[0]} 없음) — {cwd or '프로젝트'} 폴더에서 직접 실행하세요."
    try:
        r = subprocess.run([exe, *cmd[1:]], cwd=str(root / cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{' '.join(cmd)} 실패: {exc}"
    if r.returncode != 0:
        tail = " / ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])[:300]
        if cmd[:2] == ("docker", "pull"):
            return (f"베이스 이미지 {cmd[2]} 를 새로 받지 못했습니다 — 인터넷·Docker Hub 접속을 확인하세요. "
                    f"다음 빌드는 PC 에 있는 이미지로 진행합니다. ({tail})")
        return f"{' '.join(cmd)} 실패: {tail}"
    return ""


def apply(workspace: str, reports: dict[str, Any], ids: list[str]) -> dict:
    """고른 수정안만 적용한다. 각 수정은 그 시점의 파일에 다시 계산해 겹쳐 적용한다."""
    root = Path(workspace).expanduser().resolve()
    wanted = {i for i in ids if isinstance(i, str)}
    proposals = [p for p in plan(str(root), reports) if p.id in wanted]
    found = {p.id for p in proposals}
    applied: list[str] = []
    skipped: list[dict] = [{"id": i, "reason": "이미 고쳐졌거나 더는 해당하지 않습니다."} for i in sorted(wanted - found)]
    changed: list[str] = []
    backups: list[str] = []
    notes: list[str] = []
    post: list[tuple[str, tuple[str, ...]]] = []
    rebuild = False
    backed_up: set[str] = set()
    for p in proposals:
        if not p.auto or p.make is None:
            skipped.append({"id": p.id, "reason": "자동으로 고칠 수 없는 항목입니다. 안내를 따라 직접 고치세요."})
            continue
        writes = p.make(root)
        if writes is None:
            skipped.append({"id": p.id, "reason": "앞의 수정으로 이미 고쳐졌습니다."})
            continue
        bad = False
        for rel in writes:
            try:
                (root / rel).resolve().relative_to(root)
            except ValueError:
                bad = True
        if bad:
            skipped.append({"id": p.id, "reason": "프로젝트 밖 파일은 고치지 않습니다."})
            continue
        for rel, content in writes.items():
            target = root / rel
            newline = "\n"
            if target.is_file():
                raw = br._read_raw(target)
                newline = "\r\n" if "\r\n" in raw else "\n"
                #: 파일마다 이번 적용 전 원본 한 번만. 키 값은 백업에도 남기지 않는다(.env 는 통째로 제외).
                if rel != ".env" and rel not in backed_up:
                    backed_up.add(rel)
                    for secret in p.secrets:
                        raw = raw.replace(secret, "[.env 로 옮김]")
                    backups.append(br._backup(root, rel, raw))
            target.parent.mkdir(parents=True, exist_ok=True)
            br._write_raw(target, content.replace("\n", newline) if newline != "\n" else content)
            changed.append(rel)
        for item in p.post:
            if item not in post:
                post.append(item)
        applied.append(p.id)
        rebuild = rebuild or p.rebuild
        if p.note:
            notes.append(p.note)
    for cwd, cmd in post:
        msg = _run_post(root, cwd, cmd)
        if msg:
            notes.append(msg)
    return {"applied": applied, "skipped": skipped, "changed": sorted(set(changed)), "backups": backups,
            "notes": list(dict.fromkeys(n for n in notes if n)), "rebuild": rebuild}
