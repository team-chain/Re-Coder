"""AI 가 만든 Node 프로젝트에서 자주 깨지는 빌드·실행 문제를 찾고, 고칠 내용(파일 → 새 내용)을 만든다.

실기기(2026-10-10 TEMP 쇼핑몰, AI 자유 생성)에서 실제 Docker 빌드를 돌려 나온 실패를 순서대로 막는다.
  1) workspaces 폴더(backend/·frontend/)에 package.json 이 없다 → npm 설치·빌드가 시작도 못 한다.
  2) Dockerfile 이 lock 파일 없이 `npm ci` → "npm ci can only install packages when … package-lock.json".
  3) tsconfig 의 references 가 없는 파일(tsconfig.node.json)을 가리킨다 → vite 빌드가 ENOENT 로 멈춘다.
  4) vite.config 의 minify: 'terser' 인데 terser 가 없다 → vite 5 빌드 실패.
  5) 서버가 화면 폴더를 `new URL('../../../frontend/dist', import.meta.url)` 처럼 프로젝트 밖으로 가리킨다
     → 컨테이너에서 화면이 안 나온다(ENOENT index.html).
  6) 컨테이너 빌드 로그(tsc·vite)의 오류를 **파일별 문제**로 나눈다 → 대규모 생성의 부분 교정이 그 파일을 고친다
     (예전에는 Dockerfile 하나를 지목해 소스 오류를 영영 고치지 못했다).

모든 함수는 파일을 직접 쓰지 않는다 — {경로: 새 내용} 을 돌려주고, 쓰는 쪽(생성 직후 자동 교정·배포 준비 점검의
자동 수정)이 백업과 함께 적용한다.
"""
from __future__ import annotations

import json
import posixpath
import re
from typing import Iterable, Mapping, Optional

import node_manifests

_CODE = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue")
_SEPARATORS = re.compile(r"&&|\|\||;|\|")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _norm(path: str) -> str:
    out = posixpath.normpath(str(path or "").replace("\\", "/").strip())
    return "" if out in (".", "") else out.lstrip("/")


def strip_jsonc(text: str) -> str:
    """tsconfig 같은 JSONC 를 JSON 으로 — 문자열 밖의 주석·끝 쉼표만 걷는다."""
    out, i, n = [], 0, len(text or "")
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_jsonc(text: Optional[str]) -> Optional[dict]:
    try:
        data = json.loads(strip_jsonc(text or ""))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _dump_like(original: str, data: dict) -> str:
    newline = "\r\n" if "\r\n" in (original or "") else "\n"
    return json.dumps(data, ensure_ascii=False, indent=2).replace("\n", newline) + newline


# ── 1) 스크립트가 들어가는 폴더 · 빠진 package.json ─────────────────────────

def script_folders(scripts: Mapping) -> list[str]:
    """`cd X && …` 로 들어가는 폴더(루트 기준)."""
    out: list[str] = []
    for value in (scripts or {}).values():
        cwd = ""
        for segment in _SEPARATORS.split(str(value or "")):
            words = segment.strip().split()
            while words and _ENV_ASSIGN.match(words[0]):
                words = words[1:]
            if words[:1] == ["cd"] and len(words) > 1:
                cwd = _norm(posixpath.join(cwd, words[1].strip("'\"")))
                if cwd and not cwd.startswith("..") and cwd not in out:
                    out.append(cwd)
    return out


def missing_manifests(files, package: dict) -> dict[str, str]:
    """{폴더/package.json: 새 내용} — workspaces·스크립트가 가리키는데 package.json 이 없는 코드 폴더."""
    listing = files.files()
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    folders = node_manifests.workspace_folders(package, listing) + script_folders(scripts)
    out: dict[str, str] = {}
    for folder in dict.fromkeys(folders):
        if not folder or files.exists(f"{folder}/package.json"):
            continue
        code = {p: files.read(p) or "" for p in listing
                if p.startswith(folder + "/") and p.lower().endswith(_CODE) and "/node_modules/" not in f"/{p}"}
        if not code:
            continue  # 코드가 없는 폴더(문서·정적 파일)는 Node 프로젝트가 아니다
        #: 설정 파일도 함께 본다(tsconfig 의 outDir, vite.config 의 terser)
        extra = {p: files.read(p) or "" for p in listing
                 if p == f"{folder}/tsconfig.json" or re.match(rf"{re.escape(folder)}/vite\.config\.[cm]?[jt]s$", p)}
        text, _unknown = node_manifests.infer_package_json(folder, {**code, **extra}, package)
        out[f"{folder}/package.json"] = text
    return out


# ── 2) lock 파일 없는 npm ci ───────────────────────────────────────────────

def npm_ci_without_lock(dockerfile: str, files) -> Optional[str]:
    """lock 파일이 없는 폴더에서 `npm ci` 를 부르면 `npm install` 로 바꾼 Dockerfile(바꿀 게 없으면 None).

    여러 줄(\\ 로 이은) RUN 도 한 명령으로 본다. lock 파일이 있을 때만 npm ci 를 부르는 조건문은 건드리지 않는다.
    """
    lines = (dockerfile or "").split("\n")
    folder = ""
    changed = False
    i = 0
    while i < len(lines):
        end = i
        while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
            end += 1
        logical = " ".join(l.rstrip().rstrip("\\") for l in lines[i:end + 1])
        stripped = logical.strip()
        op = stripped.split(" ", 1)[0].upper() if stripped else ""
        if op == "FROM":
            folder = ""
        elif op == "COPY" and not re.search(r"--from", stripped):
            m = re.search(r"(?:^|\s)\.?/?([\w./-]*?)package(?:\*|-lock)?\.json\*?(?=\s)", stripped + " ")
            if m:
                folder = _norm(m.group(1).rstrip("/"))
            elif re.match(r"COPY\s+(?:--[\w-]+=\S+\s+)*\.\s", stripped + " ", re.I):
                folder = ""
        if op == "RUN" and re.search(r"\bnpm\s+ci\b", logical) and not re.search(
                r"-[fe]\s+\S*package-lock\.json|\bnpm\s+ci\b[^;&|]*\|\|\s*npm\s+(?:install|i)\b", logical):
            here = folder
            cd = re.search(r"\bcd\s+(?:/app/|\./)?([\w./-]+)\s*&&[^&]*\bnpm\s+ci\b", logical)
            if cd:
                here = _norm(cd.group(1))
            lock = any(files.exists(f"{here}/{n}" if here else n) for n in ("package-lock.json", "npm-shrinkwrap.json"))
            if not lock:
                for k in range(i, end + 1):
                    new = re.sub(r"\bnpm\s+ci\b", "npm install --no-audit --no-fund", lines[k])
                    new = re.sub(r"\s--only[= ]prod(?:uction)?\b|\s--production\b", " --omit=dev", new)
                    if new != lines[k]:
                        lines[k] = new
                        changed = True
        i = end + 1
    return "\n".join(lines) if changed else None


# ── 3) tsconfig references 가 없는 파일을 가리킨다 ─────────────────────────

def tsconfig_missing_refs(files) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in files.files():
        if posixpath.basename(rel) != "tsconfig.json" or "/node_modules/" in f"/{rel}":
            continue
        raw = files.read(rel) or ""
        data = load_jsonc(raw)
        refs = data.get("references") if data else None
        if not isinstance(refs, list) or not refs:
            continue
        base = posixpath.dirname(rel)
        keep = []
        for ref in refs:
            target = _norm(posixpath.join(base, str((ref or {}).get("path") or ""))) if isinstance(ref, dict) else ""
            if target and (files.exists(target) or files.exists(f"{target}/tsconfig.json")):
                keep.append(ref)
        if len(keep) == len(refs):
            continue
        if keep:
            data["references"] = keep
        else:
            data.pop("references", None)
        out[rel] = _dump_like(raw, data)
    return out


# ── 4) vite minify: 'terser' 인데 terser 없음 ──────────────────────────────

TERSER_VERSION = "^5.36.0"


def vite_terser_missing(files) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in files.files():
        if not re.search(r"(^|/)vite\.config\.[cm]?[jt]s$", rel) or "/node_modules/" in f"/{rel}":
            continue
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", files.read(rel) or "", flags=re.S)
        if not re.search(r"""\bminify\s*:\s*['"]terser['"]""", text):
            continue
        folder = posixpath.dirname(rel)
        manifest = f"{folder}/package.json" if folder else "package.json"
        raw = files.read(manifest)
        try:
            pkg = json.loads(raw or "")
        except ValueError:
            continue
        if not isinstance(pkg, dict):
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "terser" in deps:
            continue
        dev = dict(pkg.get("devDependencies") or {})
        dev["terser"] = TERSER_VERSION
        pkg["devDependencies"] = dict(sorted(dev.items()))
        out[manifest] = _dump_like(raw or "", pkg)
    return out


# ── 5) 프로젝트 밖을 가리키는 정적 폴더 경로 ───────────────────────────────

_URL_LIT = re.compile(r"""new\s+URL\(\s*(['"])((?:\.\./)+[^'"]*)\1\s*,\s*import\.meta\.url\s*\)""")
_JOIN_LIT = re.compile(r"""path\.(?:join|resolve)\(\s*__dirname\s*,\s*((?:['"][^'"]*['"]\s*,?\s*)+)\)""")


def _runtime_dir(files, rel: str) -> str:
    """이 소스가 실제로 실행되는 폴더 — TypeScript 면 tsconfig 의 rootDir → outDir 로 옮긴 곳."""
    if not rel.endswith((".ts", ".mts", ".cts")):
        return posixpath.dirname(rel)
    folder = posixpath.dirname(rel)
    while True:
        cfg_rel = f"{folder}/tsconfig.json" if folder else "tsconfig.json"
        if files.exists(cfg_rel):
            cfg = (load_jsonc(files.read(cfg_rel)) or {}).get("compilerOptions") or {}
            out = cfg.get("outDir")
            if not out or cfg.get("noEmit"):
                return posixpath.dirname(rel)
            root = _norm(posixpath.join(folder, cfg.get("rootDir") or "src"))
            out_dir = _norm(posixpath.join(folder, out))
            src_dir = posixpath.dirname(rel)
            if src_dir == root or src_dir.startswith(root + "/"):
                return _norm(out_dir + src_dir[len(root):])
            return posixpath.dirname(rel)
        if not folder:
            return posixpath.dirname(rel)
        folder = posixpath.dirname(folder)


def static_paths_outside(files, candidates: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """([문제 설명], {파일: 고친 내용}) — 실행 위치에서 프로젝트 밖으로 나가는 상대 경로(화면 폴더 등).

    candidates: 그 경로가 가리켜야 할 폴더 후보(빌드 결과 폴더·실제 있는 폴더). `../` 를 줄여 후보와 맞으면 고친다.
    """
    wanted = {_norm(c) for c in candidates if c}
    problems: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")) or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        if "import.meta.url" not in text and "__dirname" not in text:
            continue
        here = _runtime_dir(files, rel)
        updated = text

        def fix_path(spec: str) -> Optional[str]:
            resolved = posixpath.normpath(posixpath.join(here or ".", spec))
            if not resolved.startswith(".."):
                return None
            ups = len(re.match(r"^(?:\.\./)*", spec).group(0)) // 3
            rest = spec[ups * 3:]
            for drop in range(1, ups + 1):
                cand = "../" * (ups - drop) + rest
                target = _norm(posixpath.join(here or ".", cand))
                if target and not target.startswith("..") and (target in wanted or files.has_dir(target)):
                    return cand or "."
            return ""

        for m in list(_URL_LIT.finditer(text)):
            spec = m.group(2)
            fixed = fix_path(spec)
            if fixed is None:
                continue
            problems.append(f"{rel}: '{spec}' 는 실행 위치({here}/)에서 프로젝트 밖을 가리킵니다")
            if fixed:
                updated = updated.replace(m.group(0), m.group(0).replace(spec, fixed if fixed.startswith(".") else "./" + fixed), 1)
        for m in list(_JOIN_LIT.finditer(text)):
            parts = re.findall(r"""['"]([^'"]*)['"]""", m.group(1))
            spec = posixpath.join(*parts) if parts else ""
            fixed = fix_path(spec)
            if fixed is None:
                continue
            problems.append(f"{rel}: '{spec}' 는 실행 위치({here}/)에서 프로젝트 밖을 가리킵니다")
            if fixed:
                quote = "'" if "'" in m.group(1) else '"'
                call = m.group(0)
                new_call = call.replace(m.group(1), f"{quote}{fixed}{quote}")
                updated = updated.replace(call, new_call, 1)
        if updated != text:
            writes[rel] = updated
    return problems, writes


# ── 6) 생성 코드의 tsconfig — strict 는 두고, 빌드를 막는 린트성 옵션만 끈다 ─────

#: strict 밖의 추가 검사 — 코드 동작과 무관한데(미사용 변수·인덱스 접근 undefined) AI 코드의 tsc 빌드를 대부분 깨뜨렸다
#: (실기기 TEMP 쇼핑몰: tsc 오류 40건 중 30건). 타입 안전성(strict)은 그대로 둔다.
LINT_ONLY_TS_OPTIONS = ("noUnusedLocals", "noUnusedParameters", "noUncheckedIndexedAccess",
                        "exactOptionalPropertyTypes", "noPropertyAccessFromIndexSignature")


def relax_generated_tsconfig(text: str) -> Optional[str]:
    data = load_jsonc(text)
    if not data or not isinstance(data.get("compilerOptions"), dict):
        return None
    opts = data["compilerOptions"]
    hit = [k for k in LINT_ONLY_TS_OPTIONS if opts.get(k) is True]
    if not hit:
        return None
    for k in hit:
        opts[k] = False
    return _dump_like(text, data)


# ── 7) 빌드 로그 → 파일별 문제 ──────────────────────────────────────────────

_TSC_LINE = re.compile(r"(?m)^(?:#\d+\s+[\d.]+\s+)?(?:\d+\.\d+\s+)?([\w./@-]+\.(?:ts|tsx|mts|cts|js|jsx))\((\d+),(\d+)\):\s+error\s+(TS\d+):\s*(.+)$")
_VITE_FILE = re.compile(r"(?m)file:\s+(/[\w./@-]+\.(?:tsx|ts|jsx|js|vue|scss|css|mjs))(?![\w])(?::(\d+):(\d+))?")
_VITE_MSG = re.compile(r"(?m)^(?:#\d+\s+[\d.]+\s+)?(?:\d+\.\d+\s+)?(?:error during build:\s*)?(?:\[[\w:-]+\]\s*)?(.+?)$")
_STAGE = re.compile(r"\[([\w.-]+)\s+\d+/\d+\]\s+RUN\b")


def _stage_workdirs(dockerfile: str) -> dict[str, str]:
    out: dict[str, str] = {}
    stage = ""
    for line in (dockerfile or "").splitlines():
        m = re.match(r"\s*FROM\s+\S+(?:\s+AS\s+([\w.-]+))?", line, re.I)
        if m:
            stage = (m.group(1) or "").lower()
            continue
        w = re.match(r"\s*WORKDIR\s+(\S+)", line, re.I)
        if w and stage:
            out[stage] = w.group(1)
    return out


def _match_op(paths: list[str], reported: str, prefer: str) -> Optional[str]:
    reported = _norm(re.sub(r"^/app/", "", reported))
    exact = [p for p in paths if _norm(p) == reported]
    if exact:
        return exact[0]
    tail = [p for p in paths if _norm(p).endswith("/" + reported)]
    if prefer:
        pref = [p for p in tail if _norm(p).startswith(prefer + "/")]
        if len(pref) == 1:
            return pref[0]
    return tail[0] if len(tail) == 1 else None


def build_log_issues(log: str, op_paths: list[str], dockerfile: str = "") -> list[dict]:
    """컨테이너 빌드 로그에서 소스 파일 오류를 뽑아 파일별 문제로 만든다(파일당 한 건, 오류 줄은 모아서)."""
    if not log:
        return []
    workdirs = _stage_workdirs(dockerfile)
    failed_stage = next((m.group(1).lower() for m in _STAGE.finditer(log)), "")
    workdir = workdirs.get(failed_stage, "")
    prefer = _norm(re.sub(r"^/app/?", "", workdir)) if workdir else ""
    grouped: dict[str, list[str]] = {}
    for m in _TSC_LINE.finditer(log):
        rel = m.group(1)
        candidates = [rel] + ([posixpath.join(prefer, rel)] if prefer else [])
        target = next((t for t in (_match_op(op_paths, c, prefer) for c in candidates) if t), None)
        if not target:
            continue
        line = f"{m.group(2)}행 {m.group(4)}: {m.group(5).strip()[:220]}"
        if line not in grouped.setdefault(target, []):
            grouped[target].append(line)
    if not grouped:
        for m in _VITE_FILE.finditer(log):
            target = _match_op(op_paths, m.group(1), prefer)
            if not target:
                continue
            before = log[:m.start()].splitlines()[-6:]
            msg = next((re.sub(r"^(?:#\d+\s+[\d.]+\s+)?(?:\d+\.\d+\s+)?", "", l).strip() for l in reversed(before)
                        if re.search(r"error|not exported|failed|Could not|Unexpected|Expected", l, re.I)
                        and "error during build" not in l), "")
            text = f"{m.group(2)}행: {msg[:300]}" if m.group(2) else msg[:300]
            if text and text not in grouped.setdefault(target, []):
                grouped[target].append(text)
    issues = []
    for target, lines in grouped.items():
        issues.append({
            "code": "GENERATED_BUILD_FAILED", "severity": "error", "file": target,
            "message": f"컨테이너 빌드에서 {target} 의 오류 {len(lines)}건: " + " / ".join(lines[:8]),
            "fix": "오류 줄의 타입·이름·import 를 실제 코드에 맞게 고치세요. 타입 검사를 끄거나(any 남발·ts-ignore) 기능을 지우지 마세요.",
        })
    return issues


# ── 8) tsc 빌드인데 tsconfig.json 이 없다 ────────────────────────────────────

def node_tsconfig(files, folder: str) -> Optional[str]:
    """Node 서버 폴더의 tsconfig(없을 때 만들 내용). TypeScript 파일이 없으면 None."""
    prefix = f"{folder}/" if folder else ""
    ts = [p for p in files.files() if p.startswith(prefix) and p.endswith((".ts", ".mts", ".cts"))
          and not p.endswith(".d.ts") and "/node_modules/" not in f"/{p}"]
    if not ts:
        return None
    rels = [p[len(prefix):] for p in ts]
    root = "src" if all(r.startswith("src/") for r in rels) else "."
    try:
        pkg = json.loads(files.read(f"{prefix}package.json") or "{}")
    except ValueError:
        pkg = {}
    esm = isinstance(pkg, dict) and pkg.get("type") == "module"
    options = {"target": "ES2022", "module": "NodeNext" if esm else "CommonJS",
               "moduleResolution": "NodeNext" if esm else "Node", "outDir": "dist", "rootDir": root,
               "strict": True, "esModuleInterop": True, "skipLibCheck": True, "resolveJsonModule": True,
               "forceConsistentCasingInFileNames": True}
    data = {"compilerOptions": options,
            "include": [f"{root}/**/*.ts" if root != "." else "**/*.ts"],
            "exclude": ["node_modules", "dist"]}
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ── 9) 매 렌더마다 새 함수를 돌려주는 훅 + useEffect 의존성 → 요청 무한 반복 ─────────

_HOOK_DEF = re.compile(r"\bexport\s+(?:default\s+)?function\s+(use[A-Z]\w*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)[^{]*\{|"
                       r"\bexport\s+const\s+(use[A-Z]\w*)\s*=\s*(?:<[^>]*>)?\s*\(([^)]*)\)[^=]*=>\s*\{")


def _match_brace(text: str, open_at: int) -> int:
    """text[open_at] == '{' 의 짝 '}' 위치(-1: 못 찾음). 문자열·주석·템플릿은 건너뛴다."""
    depth, i, n = 0, open_at, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _top_mask(body: str) -> list[bool]:
    """본문의 각 글자가 훅 최상위(중첩 블록 밖)인가. 구조 분해 `const { a } =` 의 중괄호는 최상위 문장의 일부다."""
    mask = [True] * len(body)
    i = 0
    while i < len(body):
        if body[i] == "{":
            end = _match_brace(body, i)
            if end < 0:
                break
            line_start = body.rfind("\n", 0, i) + 1
            if re.match(r"\s*(?:const|let|var)\s*$", body[line_start:i]):
                i = end + 1  # 구조 분해 패턴 — 최상위 선언
                continue
            for k in range(i + 1, end):
                mask[k] = False
            i = end + 1
            continue
        i += 1
    return mask


def _param_names(params: str) -> list[str]:
    names = []
    depth = 0
    cleaned = ""
    for c in params:
        if c in "<({[":
            depth += 1
        elif c in ">)}]":
            depth -= 1
        cleaned += c if depth == 0 else " "
    for part in cleaned.split(","):
        m = re.match(r"\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)", part)
        if m:
            names.append(m.group(1))
    return names


def _unstable_hooks(text: str) -> list[dict]:
    """[{name, start, end, members, deps}] — 객체 리터럴로 새 함수를 돌려주는(메모하지 않은) 커스텀 훅."""
    found = []
    for m in _HOOK_DEF.finditer(text):
        name = m.group(1) or m.group(3)
        params = m.group(2) if m.group(1) else m.group(4)
        open_at = m.end() - 1
        close_at = _match_brace(text, open_at)
        if close_at < 0:
            continue
        body = text[open_at + 1:close_at]
        mask = _top_mask(body)

        def top(pattern: str):
            return [m for m in re.finditer(pattern, body, re.M) if m.start() < len(mask) and mask[m.start()]]

        inner = {m.group(1) for m in top(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")}
        inner |= {m.group(1) for m in top(r"^\s*const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)")}
        rets = top(r"^(\s*)return\s*(\{)")
        if not inner or not rets:
            continue
        last = rets[-1]
        obj_open = last.start(2)
        obj_close = _match_brace(body, obj_open)
        if obj_close < 0:
            continue
        literal = body[obj_open:obj_close + 1]
        members = set(re.findall(r"(?:^|[,{]\s*)([A-Za-z_$][\w$]*)\s*(?=[,}\n])", literal)) | \
            set(re.findall(r"[,{]\s*[A-Za-z_$][\w$]*\s*:\s*([A-Za-z_$][\w$]*)\s*(?=[,}\n])", literal))
        if not members & inner:
            continue
        if all(re.search(rf"\bconst\s+{re.escape(x)}\s*=\s*useCallback\(", body) for x in members & inner):
            continue
        bound: list[str] = []
        for decl in top(r"^\s*(?:const|let)\s+(\{[^}]*\}|\[[^\]]*\]|[A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?="):
            target = decl.group(1)
            if target.startswith(("{", "[")):
                for part in target.strip("{}[]").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    alias = part.split(":")[-1].strip().split("=")[0].strip().lstrip(".")
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
                        bound.append(alias)
            else:
                bound.append(target)
        deps = [d for d in dict.fromkeys(_param_names(params) + bound) if d not in inner]
        tail = body[obj_close + 1:]
        semi = re.match(r"\s*;?", tail).end()
        found.append({"name": name, "start": open_at + 1 + last.start(), "end": open_at + 1 + obj_close + 1 + semi,
                      "indent": last.group(1), "literal": literal, "deps": deps})
    return found


def _loop_users(files, hook: str, code_files: list[str]) -> list[str]:
    users = []
    for rel in code_files:
        text = files.read(rel) or ""
        if not re.search(rf"\b{hook}\s*\(", text):
            continue
        names = set()
        for m in re.finditer(rf"\b(?:const|let)\s+(\{{[^}}]*\}}|[A-Za-z_$][\w$]*)\s*=\s*{hook}\s*\(", text):
            target = m.group(1)
            if target.startswith("{"):
                names |= {p.split(":")[-1].strip() for p in target.strip("{}").split(",") if p.strip()}
            else:
                names.add(target)
        for deps in re.findall(r"\b(?:useEffect|useLayoutEffect|useCallback|useMemo)\s*\((?:.|\n)*?\}\s*,\s*\[([^\]]*)\]\s*\)", text):
            if names & {d.strip().split(".")[0] for d in deps.split(",")}:
                users.append(rel)
                break
    return users


def react_effect_loops(files) -> tuple[list[str], dict[str, str]]:
    """([문제], {훅 파일: 고친 내용}) — 훅이 매 렌더 새 함수를 돌려주고, 그 함수를 useEffect 의존성에 넣은 화면.

    그 화면은 그려질 때마다 요청 → 상태 변경 → 다시 그림 → 새 함수 → 또 요청을 끝없이 되풀이한다
    (실기기 TEMP 쇼핑몰: 첫 화면 6초에 /api/products 2,258번 → 서버 요청 한도 429 로 모든 화면이 멈춤).
    고침: 훅이 돌려주는 객체를 useMemo 로 감싸 훅의 값(토큰 등)이 바뀔 때만 새로 만든다 — 동작은 같다.
    """
    code_files = [p for p in files.files() if p.endswith((".ts", ".tsx", ".js", ".jsx"))
                  and "/node_modules/" not in f"/{p}" and not re.search(r"(^|/)(dist|build)/", p)]
    problems: list[str] = []
    writes: dict[str, str] = {}
    for rel in code_files:
        text = files.read(rel) or ""
        if "use" not in text or not re.search(r"\bexport\b", text):
            continue
        hooks = _unstable_hooks(text)
        if not hooks:
            continue
        updated = text
        for hook in sorted(hooks, key=lambda h: -h["start"]):
            users = _loop_users(files, hook["name"], code_files)
            if not users:
                continue
            problems.append(f"{hook['name']}({rel}) 를 쓰는 {', '.join(users[:3])} 가 요청을 끝없이 되풀이합니다")
            deps = ", ".join(hook["deps"])
            new = f"{hook['indent']}return useMemo(() => ({hook['literal']}), [{deps}]);"
            updated = updated[:hook["start"]] + new + updated[hook["end"]:]
        if updated == text:
            continue
        if not re.search(r"\buseMemo\b[^;]*from\s+['\"]react['\"]", updated):
            named = re.search(r"import\s+(?:([A-Za-z_$][\w$]*)\s*,\s*)?\{([^}]*)\}\s*from\s*(['\"])react\3", updated)
            if named:
                inner = named.group(2).rstrip().rstrip(",")
                updated = updated[:named.start(2)] + f"{inner}, useMemo " + updated[named.end(2):]
            else:
                updated = "import { useMemo } from 'react';\n" + updated
        writes[rel] = updated
    return problems, writes


# ── 10) 빌드하는 단계에서 개발 의존성을 빼고 설치 ─────────────────────────────

_BUILD_CMD = re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|\b(?:npx\s+)?(?:tsc|vite\s+build|next\s+build|react-scripts\s+build|webpack|ng\s+build|nuxt\s+build)\b")
_INSTALL = re.compile(r"\b(?:npm\s+(?:ci|install|i)|pnpm\s+install|yarn\s+install)\b")
_OMIT_FLAGS = re.compile(r"\s+(?:--omit[= ]dev|--only[= ]prod(?:uction)?|--production(?:=true)?|--prod)\b")


def build_stage_omits_dev(dockerfile: str) -> Optional[str]:
    """빌드(npm run build·tsc·vite build)를 하는 단계가 개발 의존성 없이 설치하면 고친 Dockerfile, 아니면 None.

    실기기(TEMP 2.0.6): 빌드 단계가 `npm install --omit=dev` 라 typescript·vite 가 없어 `sh: tsc: not found`.
    · 그 단계의 설치에서 --omit=dev·--only=production·--production 을 뺀다(실행 단계는 그대로 — 운영 이미지는 가볍게).
    · 그 단계에 ENV NODE_ENV=production 이 먼저 있으면 npm 이 개발 의존성을 건너뛰므로 설치에 --include=dev 를 붙인다.
    """
    lines = (dockerfile or "").split("\n")
    stages: list[tuple[int, int]] = []
    start = None
    for i, line in enumerate(lines):
        if re.match(r"\s*FROM\s", line, re.I):
            if start is not None:
                stages.append((start, i))
            start = i
    if start is None:
        return None
    stages.append((start, len(lines)))
    changed = False
    for a, b in stages:
        body = "\n".join(lines[a:b])
        logical = re.sub(r"\\\r?\n", " ", body)
        runs = [l for l in logical.split("\n") if re.match(r"\s*RUN\s", l, re.I)]
        if not any(_BUILD_CMD.search(r) for r in runs):
            continue
        prod_env = False
        for i in range(a, b):
            line = lines[i]
            if re.match(r"\s*ENV\s", line, re.I) and re.search(r"\bNODE_ENV[= ]\s*['\"]?production", line):
                prod_env = True
            if not _INSTALL.search(line) or line.lstrip().startswith("#"):
                continue
            new = _OMIT_FLAGS.sub("", line)
            if prod_env and "--include=dev" not in new and re.search(r"\bnpm\s+(?:ci|install|i)\b", new):
                new = re.sub(r"\bnpm\s+(ci|install|i)\b", r"npm \1 --include=dev", new, count=1)
            if new != line:
                lines[i] = new
                changed = True
    return "\n".join(lines) if changed else None


# ── 11) 쓰는데 import 하지 않은 이름 ─────────────────────────────────────────

_SRC = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_EXPORTED = re.compile(r"^\s*export\s+(?:declare\s+)?(?:default\s+)?(?:async\s+)?(?:abstract\s+)?"
                       r"(?:const|let|var|function\*?|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)


def _exports_of(text: str) -> set[str]:
    names = set(_EXPORTED.findall(text))
    for group in re.findall(r"\bexport\s*\{([^}]*)\}(?!\s*from)", text):
        names |= {p.split(" as ")[-1].strip() for p in group.split(",") if p.strip()}
    return {n for n in names if n != "default"}


def _code_only(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return re.sub(r"""(['"])(?:\\.|(?!\1)[^\\\n])*\1""", "''", text)


def _declared_here(code: str, name: str) -> bool:
    n = re.escape(name)
    return bool(re.search(rf"\b(?:const|let|var|function\*?|class|interface|type|enum|import)\s+(?:type\s+)?{n}\b", code)
                or re.search(rf"\bimport\b[^;]*\b{n}\b[^;]*\bfrom\b", code)
                or re.search(rf"\bexport\s*(?:type\s*)?\{{[^}}]*\b{n}\b[^}}]*\}}\s*from\b", code)  # 다시 내보내기
                or re.search(rf"[{{,]\s*(?:[\w$]+\s*:\s*)?{n}\s*(?:=[^,}}]+)?\s*[,}}]\s*[^=]*=", code)  # 구조 분해
                or re.search(rf"[(,]\s*(?:\.\.\.)?{n}\s*[:,)=?]", code)  # 함수 인자
                or re.search(rf"\bcatch\s*\(\s*{n}\b|\bfor\s*\(\s*(?:const|let|var)\s+{n}\b", code))


def missing_imports(files) -> tuple[list[str], dict[str, str]]:
    """([문제], {파일: import 를 더한 내용}) — 프로젝트의 한 파일만 내보내는 이름을 쓰면서 import 하지 않은 파일.

    실기기(TEMP 2.0.6): AdminOrdersPage.tsx 가 apiClient·OrderDetail 을 쓰면서 import 하지 않아 tsc TS2304.
    확실할 때만 — 내보내는 파일이 하나뿐이고, 그 파일 안에서 지역 선언·인자·구조 분해로 쓰이지 않을 때.
    """
    sources = [p for p in files.files() if p.endswith(_SRC) and "/node_modules/" not in f"/{p}"
               and not re.search(r"(^|/)(dist|build)/|\.d\.ts$", p)]
    texts = {p: files.read(p) or "" for p in sources}
    owners: dict[str, list[str]] = {}
    for p, t in texts.items():
        for name in _exports_of(t):
            owners.setdefault(name, []).append(p)
    problems: list[str] = []
    writes: dict[str, str] = {}
    for rel, text in texts.items():
        code = _code_only(text)
        side = rel.split("/", 1)[0] if "/" in rel else ""
        adds: list[tuple[str, str]] = []
        for name, where in owners.items():
            if len(name) < 4 or rel in where:
                continue
            same_side = [w for w in where if (w.split("/", 1)[0] if "/" in w else "") == side]
            if len(same_side) != 1:
                continue
            if not re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", code) or _declared_here(code, name):
                continue
            #: 실제로 이름으로 쓰는가 — 속성 접근·호출·타입 자리·JSX 태그
            if not re.search(rf"(?<![\w$.]){re.escape(name)}\s*(?:[.(<\[]|\s*\)|>|\s*\||\s*;|\s*,|\s*\]|\s*=>)|[:<|,]\s*{re.escape(name)}\b|<{re.escape(name)}[\s/>]", code):
                continue
            adds.append((name, same_side[0]))
        if not adds:
            continue
        rel_imports = re.findall(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""", text)
        with_js = bool(rel_imports) and all(re.search(r"\.(?:js|mjs|cjs)$", s) for s in rel_imports)
        new_lines = []
        for target, names in sorted({t: sorted(n for n, w in adds if w == t) for _n, t in adds}.items()):
            spec = posixpath.relpath(target, posixpath.dirname(rel) or ".")
            spec = spec if spec.startswith(".") else "./" + spec
            spec = re.sub(r"\.(tsx?|jsx?|mjs)$", ".js" if with_js else "", spec)
            types_only = all(re.search(rf"^\s*export\s+(?:declare\s+)?(?:interface|type)\s+{re.escape(n)}\b", texts[target], re.M)
                             for n in names)
            kw = "import type" if types_only and rel.endswith((".ts", ".tsx")) else "import"
            new_lines.append(f"{kw} {{ {', '.join(names)} }} from '{spec}';")
            problems.append(f"{rel}: {', '.join(names)} 을(를) 쓰지만 {target} 에서 불러오지 않습니다")
        lines = text.split("\n")
        last_import = max((i for i, l in enumerate(lines) if re.match(r"\s*import\b", l)), default=-1)
        #: 여러 줄 import 의 끝까지
        while 0 <= last_import < len(lines) - 1 and not re.search(r"""from\s+['"][^'"]+['"]\s*;?\s*$|^\s*import\s+['"]""", lines[last_import]):
            last_import += 1
        lines[last_import + 1:last_import + 1] = new_lines
        writes[rel] = "\n".join(lines)
    return problems, writes


# ── 12) TypeScript 인데 타입 선언(@types)이 없는 패키지 ──────────────────────

def missing_type_packages(files) -> dict[str, str]:
    """{package.json: @types 를 더한 내용} — TS 코드가 불러오는 패키지에 타입이 없어 tsc 가 TS7016 으로 멈추는 경우.
    검증된 버전을 아는 @types 만 넣는다(node_manifests.DEV_VERSIONS)."""
    out: dict[str, str] = {}
    listing = files.files()
    manifests = [p for p in listing if posixpath.basename(p) == "package.json" and "/node_modules/" not in f"/{p}"]
    for manifest in manifests:
        folder = posixpath.dirname(manifest)
        prefix = f"{folder}/" if folder else ""
        raw = files.read(manifest)
        try:
            pkg = json.loads(raw or "")
        except ValueError:
            continue
        if not isinstance(pkg, dict):
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        if "typescript" not in deps:
            continue
        nested = {posixpath.dirname(m) for m in manifests if m != manifest and m.startswith(prefix)}
        ts = {p: files.read(p) or "" for p in listing if p.startswith(prefix) and p.endswith((".ts", ".tsx"))
              and not p.endswith(".d.ts") and "/node_modules/" not in f"/{p}" and not any(p.startswith(n + "/") for n in nested)}
        if not ts:
            continue
        wanted = []
        for name in sorted(node_manifests.imported_packages(ts)):
            types = f"@types/{name.replace('@', '').replace('/', '__')}" if name.startswith("@") else f"@types/{name}"
            if name in node_manifests._TYPED or types in deps or types not in node_manifests.DEV_VERSIONS:
                continue
            wanted.append(types)
        if not wanted:
            continue
        dev = dict(pkg.get("devDependencies") or {})
        for t in wanted:
            dev[t] = node_manifests.DEV_VERSIONS[t]
        pkg["devDependencies"] = dict(sorted(dev.items()))
        out[manifest] = _dump_like(raw or "", pkg)
    return out


# ── 13) 라우터를 두 번 감쌈(main 과 App 둘 다 <BrowserRouter>) ─────────────────

_ROUTER_TAGS = ("BrowserRouter", "HashRouter", "MemoryRouter", "Router")


def _router_names(text: str) -> set[str]:
    """react-router(-dom)에서 불러온 라우터 컴포넌트의 이 파일 안 이름(별칭 포함)."""
    names: set[str] = set()
    for group in re.findall(r"""import\s*\{([^}]*)\}\s*from\s*['"]react-router(?:-dom)?['"]""", text):
        for part in group.split(","):
            bits = [b.strip() for b in part.split(" as ")]
            if bits and bits[0] in _ROUTER_TAGS:
                names.add(bits[-1])
    return names


def nested_routers(files) -> tuple[list[str], dict[str, str]]:
    """([문제], {진입 파일: 바깥 라우터를 뺀 내용}) — 진입 파일과 App 이 둘 다 라우터로 감싸면 React Router 가
    "You cannot render a <Router> inside another <Router>" 로 화면 전체를 멈춘다(실기기 TEMP 2.0.6: 빈 화면)."""
    problems: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not re.search(r"(^|/)(main|index)\.(jsx|tsx|js|ts)$", rel) or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        if "render" not in text:
            continue
        outer = [n for n in _router_names(text) if re.search(rf"<{n}[\s>]", text)]
        if len(outer) != 1:
            continue
        app = None
        for local, spec in re.findall(r"""import\s+([A-Za-z_$][\w$]*)\s+from\s+['"](\.{1,2}/[^'"]+)['"]""", text):
            if re.search(rf"<{local}[\s/>]", text):
                base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
                app = next((c for c in (base, *(base + e for e in (".tsx", ".jsx", ".ts", ".js"))) if files.exists(c)), None)
                if app:
                    break
        if not app:
            continue
        inner_text = files.read(app) or ""
        if not any(re.search(rf"<{n}[\s>]", inner_text) for n in _router_names(inner_text)):
            continue
        name = outer[0]
        updated = _unwrap(text, name)
        if updated is None:
            continue
        if not re.search(rf"<{name}[\s>]", updated):
            updated = _drop_named_import(updated, name)
        #: App 이 같은 공급자(AuthProvider 등)도 다시 감싸면 바깥 것도 뺀다 — 바깥 공급자는 라우터 밖이 되어
        #: useNavigate 를 쓰면 멈추고, 안쪽과 상태가 둘로 갈린다.
        for wrapper in sorted(set(re.findall(r"^\s*<([A-Z][\w$]*)(?:\s[^>]*)?>\s*$", updated, re.M))):
            if wrapper in ("App",) or not re.search(rf"<{wrapper}[\s>]", inner_text):
                continue
            again = _unwrap(updated, wrapper)
            if again is not None:
                updated = again
                if not re.search(rf"<{wrapper}[\s>/]", updated):
                    updated = re.sub(rf"""^import\s*\{{\s*{wrapper}\s*\}}\s*from\s*['"][^'"]+['"];?[ \t]*\n""", "", updated, flags=re.M)
        problems.append(f"{rel} 와 {app} 가 둘 다 라우터(<{name}>)로 감쌉니다")
        writes[rel] = updated
    return problems, writes


def _unwrap(text: str, name: str) -> Optional[str]:
    """한 줄짜리 <name …> … </name> 감싸기 하나를 벗긴다(안쪽 들여쓰기 한 단계 줄임). 확실하지 않으면 None."""
    lines = text.split("\n")
    opens = [i for i, l in enumerate(lines) if re.fullmatch(rf"\s*<{name}(?:\s[^>]*)?>\s*", l)]
    closes = [i for i, l in enumerate(lines) if re.fullmatch(rf"\s*</{name}>\s*", l)]
    if len(opens) != 1 or len(closes) != 1 or closes[0] < opens[0]:
        return None
    o, c = opens[0], closes[0]
    return "\n".join(lines[:o] + [re.sub(r"^  ", "", l) for l in lines[o + 1:c]] + lines[c + 1:])


def _drop_named_import(text: str, name: str) -> str:
    def fix(m: re.Match) -> str:
        parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        keep = [p for p in parts if p.split(" as ")[-1].strip() != name]
        if not keep:
            return ""
        return m.group(0).replace(m.group(1), " " + ", ".join(keep) + " ")
    out = re.sub(r"""import\s*\{([^}]*)\}\s*from\s*['"]react-router(?:-dom)?['"];?[ \t]*\n?""", fix, text, count=1)
    return out
