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
    for k in hit:
        opts[k] = False
    #: module 과 moduleResolution 이 맞지 않으면 tsc 가 설정 오류(TS5110·TS5095)로 실패한다 — 생성 검증이 이것 하나로
    #: "확인 필요" 가 되고 AI 교정 회차를 썼다(실기기 2.0.16 쇼핑몰 cdf7: module ES2020 + moduleResolution nodeNext).
    res = str(opts.get("moduleResolution") or "").lower()
    mod = str(opts.get("module") or "").lower()
    changed = bool(hit)
    if res in ("node16", "nodenext") and mod and mod not in ("node16", "nodenext", "node18", "node20"):
        opts["module"] = "NodeNext" if res == "nodenext" else "Node16"
        changed = True
    elif res == "bundler" and mod in ("commonjs", "amd", "umd", "system", "none"):
        opts["module"] = "ESNext"
        changed = True
    if not changed:
        return None
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


def build_log_issues(log: str, op_paths: list[str], dockerfile: str = "", contents: Optional[Mapping[str, str]] = None) -> list[dict]:
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
        src = ((contents or {}).get(target) or "").split("\n")
        n = int(m.group(2)) - 1
        if 0 <= n < len(src) and src[n].strip():
            line += f" ← `{src[n].strip()[:160]}`"  # 그 줄의 실제 코드(교정이 정확히 찾게)
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
            "message": f"컨테이너 빌드에서 {target} 의 오류 {len(lines)}건: " + " / ".join(lines[:20]),
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


def runtime_workdir_not_writable(dockerfile: str) -> Optional[str]:
    """실행 단계가 root 로 만든 WORKDIR 에 `COPY --chown=<uid>` 로 내용만 넣고 `USER <uid>` 로 뜨면, 작업 폴더 자체는
    root 소유라 앱이 그 안에 폴더를 만들지 못한다(uploads·logs 등) → 고친 Dockerfile, 아니면 None.

    실기기 2.0.20 TEMP(ECS): `mkdir '/app/backend/uploads'` EACCES 로 앱이 기동 중 종료 → 태스크가 계속 교체돼 주소 무응답.
    USER 줄 바로 앞에 작업 폴더만(재귀 없이) 그 사용자에게 넘기는 `RUN chown` 을 넣는다. 셸이 있는 node 이미지에서만.
    """
    lines = (dockerfile or "").split("\n")
    starts = [i for i, line in enumerate(lines) if re.match(r"\s*FROM\s", line, re.I)]
    if not starts:
        return None
    a = starts[-1]
    base = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", lines[a], re.I)
    if not base or not re.search(r"(?:^|/)node(?::|$)", base.group(1).split("@")[0]) or "distroless" in base.group(1):
        return None
    workdir = owner = None
    copied: set[str] = set()
    user_at = None
    for i in range(a + 1, len(lines)):
        line = lines[i]
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*WORKDIR\s+(\S+)", line, re.I)
        if m and user_at is None:
            workdir = m.group(1).rstrip("/") or "/"
        m = re.match(r"\s*COPY\s+(?:--from=\S+\s+)?--chown=(\S+)\s+(?:--from=\S+\s+)?.*\s(\S+)\s*$", line, re.I)
        if m and user_at is None:
            owner = m.group(1)
            copied.add(m.group(2).rstrip("/") or "/")
        if re.match(r"\s*RUN\s.*\bchown\b", line, re.I):
            return None
        m = re.match(r"\s*USER\s+(\S+)", line, re.I)
        if m and user_at is None:
            user_at = i
            user = m.group(1)
    if user_at is None or not workdir or workdir == "/" or not owner or user in ("0", "root"):
        return None
    if user.split(":")[0] != owner.split(":")[0] or workdir not in copied:
        return None
    dirs = [workdir]
    parent = workdir.rsplit("/", 1)[0]
    if parent and parent != "/" and parent not in dirs:
        dirs.insert(0, parent)
    if user_at > a + 1 and lines[user_at - 1].lstrip().startswith("#"):
        user_at -= 1
    lines.insert(user_at, f"# 앱이 작업 폴더 안에 업로드·로그 폴더를 만들 수 있게(내용은 COPY --chown 이 이미 넘겼다)\nRUN chown {owner} {' '.join(dirs)}")
    return "\n".join(lines)


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
        # Scan identifiers once. Searching the entire file for every export in
        # a large workspace made full checks take minutes before any progress.
        used = set(re.findall(r"(?<![\w$.])[A-Za-z_$][\w$]*(?![\w$])", code))
        for name in sorted(used.intersection(owners)):
            where = owners[name]
            if len(name) < 4 or rel in where:
                continue
            same_side = [w for w in where if (w.split("/", 1)[0] if "/" in w else "") == side]
            if len(same_side) != 1:
                continue
            if _declared_here(code, name):
                continue
            #: 실제로 이름으로 쓰는가 — 속성 접근·호출·타입 자리·JSX 태그
            if not re.search(rf"(?<![\w$.]){re.escape(name)}\s*(?:[.(<\[)>|;,\]]|=>)|[:<|,]\s*{re.escape(name)}\b|<{re.escape(name)}[\s/>]", code):
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


# ── 14) Vite + TypeScript 인데 import.meta.env 타입 선언이 없다 ─────────────────

VITE_ENV_DTS = '/// <reference types="vite/client" />\n'


def vite_env_types(files) -> dict[str, str]:
    """{폴더/src/vite-env.d.ts: 내용} — tsc 가 `Property 'env' does not exist on type 'ImportMeta'`(TS2339)로 멈추는 경우.
    Vite 공식 템플릿이 넣는 한 줄 선언이다(실기기 2.0.7: frontend/src/api/client.ts)."""
    out: dict[str, str] = {}
    listing = files.files()
    for manifest in [p for p in listing if posixpath.basename(p) == "package.json" and "/node_modules/" not in f"/{p}"]:
        folder = posixpath.dirname(manifest)
        prefix = f"{folder}/" if folder else ""
        try:
            pkg = json.loads(files.read(manifest) or "")
        except ValueError:
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else {}
        if "vite" not in deps:
            continue
        mine = [p for p in listing if p.startswith(prefix) and "/node_modules/" not in f"/{p}"]
        if not any(p.endswith((".ts", ".tsx")) and "import.meta.env" in (files.read(p) or "") for p in mine):
            continue
        if any(p.endswith(".d.ts") and "vite/client" in (files.read(p) or "") for p in mine):
            continue
        cfg = load_jsonc(files.read(f"{prefix}tsconfig.json")) or {}
        if "vite/client" in ((cfg.get("compilerOptions") or {}).get("types") or []):
            continue
        src = f"{prefix}src"
        out[f"{src}/vite-env.d.ts"] = VITE_ENV_DTS
    return out


# ── 15) Prisma 를 쓰는데 prisma CLI(개발 의존성)가 없다 ───────────────────────

def prisma_cli_missing(files) -> dict[str, str]:
    """{package.json: prisma 를 개발 의존성으로 더한 내용} — `prisma generate` 가 없으면 @prisma/client 의 타입·엔진이
    만들어지지 않아 tsc 가 수십 건(Order·Payment 없음, tx 가 any)으로 멈춘다. 버전은 @prisma/client 와 같게."""
    out: dict[str, str] = {}
    for manifest in files.files():
        if posixpath.basename(manifest) != "package.json" or "/node_modules/" in f"/{manifest}":
            continue
        raw = files.read(manifest) or ""
        try:
            pkg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(pkg, dict):
            continue
        deps = pkg.get("dependencies") if isinstance(pkg.get("dependencies"), dict) else {}
        dev = pkg.get("devDependencies") if isinstance(pkg.get("devDependencies"), dict) else {}
        spec = deps.get("@prisma/client") or dev.get("@prisma/client")
        if not isinstance(spec, str) or "prisma" in deps or "prisma" in dev:
            continue
        pkg["devDependencies"] = dict(dev, prisma=spec)
        out[manifest] = _dump_like(raw, pkg)
    return out


# ── 16) 함수마다 만들어 쓰는 클라이언트를 어떤 함수는 만들지 않고 쓴다 ─────────────

def _mask_code(text: str) -> str:
    """주석·문자열 안을 공백으로(길이·줄 위치는 그대로) — 위치 계산용."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
            i = j
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c and not (c != "`" and text[j] == "\n"):
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


def _brace_pairs(masked: str) -> list[tuple[int, int]]:
    pairs, stack = [], []
    for i, c in enumerate(masked):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            pairs.append((stack.pop(), i))
    return pairs


_LOCAL_CLIENT = re.compile(r"(?m)^([ \t]+)(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\(\s*\)\s*;?[ \t]*$")
_FN_OPEN = re.compile(r"(?:\)|=>)\s*(?::\s*[^{;=]+?)?\s*$")


def _is_function_body(masked: str, open_at: int) -> bool:
    """masked[open_at] == '{' 가 함수 본문의 시작인가(`) {`·`): 타입 {`·`=> {` — if·for·while·switch·catch 블록은 아니다).
    반환 타입에 중괄호·제네릭이 있어도(`): Promise<{ id: string }> {`) 찾는다."""
    head = masked[max(0, open_at - 600):open_at]
    if re.search(r"=>\s*$", head):
        return True
    depth = 0
    pairs = {"}": "{", ">": "<", "]": "["}
    k = len(head) - 1
    while k >= 0:
        c = head[k]
        if c in pairs and not (c == ">" and k > 0 and head[k - 1] == "="):
            depth += 1
        elif c in "{<[":
            if depth == 0:
                return False
            depth -= 1
        elif c == ";" and depth == 0:
            return False
        elif c == ")" and depth == 0:
            between = head[k + 1:].strip()
            if between and not between.startswith(":"):
                return False
            pd = 0
            for j in range(k, -1, -1):
                if head[j] == ")":
                    pd += 1
                elif head[j] == "(":
                    pd -= 1
                    if pd == 0:
                        word = re.search(r"([A-Za-z_$][\w$]*)\s*(?:<[^()]*>)?\s*$", head[:j])
                        return not (word and word.group(1) in ("if", "for", "while", "switch", "catch", "with"))
            return False
        k -= 1
    return False


def hoisted_clients(files) -> tuple[list[str], dict[str, str]]:
    """([문제], {파일: 고친 내용}) — `const prisma = getPrismaClient();` 를 함수마다 쓰다가 어떤 함수에서는 빠뜨려
    `Cannot find name 'prisma'`(TS2304)로 빌드가 멈춘 경우(실기기 TEMP 2.0.8 점검: 32건). 빠진 함수의 맨 앞에
    같은 줄을 넣는다(다른 함수와 똑같이 — 모듈 최상위로 올리면 초기화 순서가 바뀔 수 있다)."""
    problems: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith(_SRC) or rel.endswith(".d.ts") or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build)/", rel):
            continue
        text = files.read(rel) or ""
        if not _LOCAL_CLIENT.search(text):
            continue
        masked = _mask_code(text)
        imported = set()
        for m in re.finditer(r"\bimport\s+(?:type\s+)?([^;]*?)\s+from\s+['\"]", masked):
            imported |= set(re.findall(r"[A-Za-z_$][\w$]*", m.group(1)))
        # 위치 계산은 원문 줄로(문자열은 가려 둔 masked 기준) — 지역 선언은 원문에서 찾는다
        decls: dict[str, tuple[str, list[int]]] = {}
        for m in _LOCAL_CLIENT.finditer(text):
            name, fn = m.group(2), m.group(3)
            if fn not in imported or masked[m.start(2):m.end(2)] != name:
                continue
            prev = decls.get(name)
            if prev and prev[0] != fn:
                decls[name] = ("", [])  # 같은 이름을 다른 함수로 만든다 — 추측하지 않는다
                continue
            decls.setdefault(name, (fn, []))[1].append(m.start(2))
        if not decls:
            continue
        pairs = _brace_pairs(masked)

        def enclosing(pos: int) -> list[tuple[int, int]]:
            return sorted((p for p in pairs if p[0] < pos < p[1]), key=lambda p: p[0], reverse=True)

        inserts: dict[int, str] = {}
        fixed_names = []
        for name, (fn, positions) in decls.items():
            if not fn:
                continue
            if re.search(rf"(?m)^(?:export\s+)?(?:const|let|var|function|class)\s+{re.escape(name)}\b", masked):
                continue  # 모듈 최상위에 이미 있다
            scopes = [enclosing(p)[0] for p in positions if enclosing(p)]
            for use in re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*\.", masked):
                at = use.start()
                if any(a < at < b for a, b in scopes):
                    continue
                chain = enclosing(at)
                body = next((p for p in chain if _is_function_body(masked, p[0])), None)
                if body is None:
                    continue  # 모듈 최상위 사용 — 건드리지 않는다
                # 그 함수 안에 같은 이름의 다른 선언(인자·구조 분해 등)이 있으면 건드리지 않는다
                head = masked[max(0, body[0] - 200):body[0]]
                if re.search(rf"[(,]\s*{re.escape(name)}\s*[:,)=?]", head) or _declared_here(masked[body[0]:body[1]], name):
                    continue
                if body[0] in inserts:
                    continue
                nl = text.find("\n", body[0])
                inner = text[nl + 1:body[1]] if nl >= 0 else ""
                ind = re.match(r"[ \t]*", inner).group(0) if inner.strip() else "  "
                inserts[body[0]] = f"\n{ind}const {name} = {fn}();"
                if name not in fixed_names:
                    fixed_names.append(name)
        if not inserts:
            continue
        new = text
        for pos in sorted(inserts, reverse=True):
            new = new[:pos + 1] + inserts[pos] + new[pos + 1:]
        writes[rel] = new
        problems.append(f"{rel}: {', '.join(fixed_names)} 을(를) 만들지 않고 쓰는 함수 {len(inserts)}곳")
    return problems, writes


# ── 17) 모듈 이름으로 쓰는데(orderService.getOrderById) 그 모듈을 불러오지 않았다 ─────

_GLOBALS = {"console", "process", "Math", "JSON", "Object", "Array", "Promise", "Number", "String", "Date", "Error",
            "window", "document", "globalThis", "Buffer", "Symbol", "Reflect", "Intl", "React", "module", "exports", "require"}


def namespace_imports(files) -> tuple[list[str], dict[str, str]]:
    """([문제], {파일: import 를 더한 내용}) — 같은 쪽(서버·화면)에 `orderService.ts` 가 있고 그 모듈이 내보내는 함수를
    `orderService.x()` 로 쓰는데 불러오지 않은 파일(실기기 TEMP 2.0.8 점검: TS2304 'orderService')."""
    sources = [p for p in files.files() if p.endswith(_SRC) and not p.endswith(".d.ts") and "/node_modules/" not in f"/{p}"
               and not re.search(r"(^|/)(dist|build)/", p)]
    by_base: dict[tuple[str, str], list[str]] = {}
    for p in sources:
        side = p.split("/", 1)[0] if "/" in p else ""
        base = re.sub(r"\.(?:tsx?|jsx?|mjs)$", "", posixpath.basename(p))
        if base != "index":
            by_base.setdefault((side, base), []).append(p)
    problems: list[str] = []
    writes: dict[str, str] = {}
    texts: dict[str, str] = {}
    for rel in sources:
        text = files.read(rel) or ""
        code = _code_only(text)
        side = rel.split("/", 1)[0] if "/" in rel else ""
        adds = []
        for name in sorted(set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]{3,})\s*\.\s*[A-Za-z_$]", code))):
            if name in _GLOBALS or _declared_here(code, name):
                continue
            targets = [t for t in by_base.get((side, name), []) if t != rel]
            if len(targets) != 1:
                continue
            target = targets[0]
            body = texts.setdefault(target, files.read(target) or "")
            members = set(re.findall(rf"(?<![\w$.]){re.escape(name)}\s*\.\s*([A-Za-z_$][\w$]*)", code))
            if not members or not members <= _exports_of(body):
                continue
            adds.append((name, target))
        if not adds:
            continue
        rel_imports = re.findall(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""", text)
        with_js = bool(rel_imports) and all(re.search(r"\.(?:js|mjs|cjs)$", s) for s in rel_imports)
        new_lines = []
        for name, target in adds:
            spec = posixpath.relpath(target, posixpath.dirname(rel) or ".")
            spec = spec if spec.startswith(".") else "./" + spec
            spec = re.sub(r"\.(tsx?|jsx?|mjs)$", ".js" if with_js else "", spec)
            new_lines.append(f"import * as {name} from '{spec}';")
            problems.append(f"{rel}: {name}.… 을 쓰지만 {target} 을(를) 불러오지 않습니다")
        lines = text.split("\n")
        last_import = max((i for i, l in enumerate(lines) if re.match(r"\s*import\b", l)), default=-1)
        while 0 <= last_import < len(lines) - 1 and not re.search(r"""from\s+['"][^'"]+['"]\s*;?\s*$|^\s*import\s+['"]""", lines[last_import]):
            last_import += 1
        lines[last_import + 1:last_import + 1] = new_lines
        writes[rel] = "\n".join(lines)
    return problems, writes


# ── 18) TypeScript 가 좁히지 못하는 확실한 모양(동작은 그대로) ─────────────────────

_ENV_CONST = re.compile(r"(?m)^(export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=\s*process\.env\.([A-Za-z_][A-Za-z0-9_]*)\s*;")
_SPREAD_AND = re.compile(r"\.\.\.\(\s*([A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*)\s*&&\s*(\{[^{}()]*\})\s*\)")


def ts_safe_rewrites(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 실행 동작은 그대로 두고 tsc 만 막히는 AI 의 흔한 모양을 바로잡는다.

    · `const JWT_SECRET = process.env.JWT_SECRET;` 다음 최상위에 `if (!JWT_SECRET) throw …` 가 있으면 함수 안에서는
      여전히 string | undefined 라 jwt.sign·createHmac 이 TS2769/TS2345 로 막힌다 → `process.env.X as string`
      (바로 아래 확인이 없으면 손대지 않는다 — 그때는 정말 없을 수 있다).
    · `...(data && { data })` 는 data 가 unknown·문자열이면 TS2698 → `...(data ? { data } : {})` (같은 동작).
    실기기(카페 2.0.8): 서버 빌드 오류 15건 중 10건이 이 두 가지였다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".tsx", ".mts", ".cts")) or rel.endswith(".d.ts") or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        new = text
        for m in list(_ENV_CONST.finditer(new)):
            name = m.group(2)
            guard = re.search(rf"(?m)^if\s*\(\s*!\s*{re.escape(name)}\s*(?:\|\|[^)]*)?\)\s*\{{?\s*\n?\s*throw\b", new[m.end():])
            if guard:
                notes.append(f"{rel}: {name} 은 시작할 때 확인하므로 문자열로")
        new = _ENV_CONST.sub(lambda m: (m.group(0) if not re.search(
            rf"(?m)^if\s*\(\s*!\s*{re.escape(m.group(2))}\s*(?:\|\|[^)]*)?\)\s*\{{?\s*\n?\s*throw\b", text[m.end():])
            else f"{m.group(1) or ''}const {m.group(2)} = process.env.{m.group(3)} as string;"), new)
        jwt_name = re.search(r"""import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s+['"]jsonwebtoken['"]""", new)
        if jwt_name:
            #: jsonwebtoken 9 의 타입은 expiresIn 에 아무 문자열이나 받지 않는다('7d' 같은 값만) — 환경 변수 값은 TS2769
            j = jwt_name.group(1)
            fixed_exp = re.sub(r"(\bexpiresIn\s*:\s*)([A-Za-z_$][\w$.]*)(\s*[,}\n])",
                               lambda m: m.group(0) if m.group(2) in ("undefined", "null") or " as " in m.group(0)
                               else f"{m.group(1)}{m.group(2)} as {j}.SignOptions['expiresIn']{m.group(3)}", new)
            #: 줄임 표기 `{ expiresIn }` 도 같은 문제(실기기 2.0.10 Haiku)
            fixed_exp = re.sub(r"([{,]\s*)expiresIn(\s*(?:[,}]))",
                               lambda m: f"{m.group(1)}expiresIn: expiresIn as {j}.SignOptions['expiresIn']{m.group(2)}", fixed_exp)
            if fixed_exp != new:
                notes.append(f"{rel}: 토큰 만료 시간(expiresIn)의 타입을 jsonwebtoken 에 맞게")
                new = fixed_exp
        spread = _SPREAD_AND.sub(lambda m: f"...({m.group(1)} ? {m.group(2)} : {{}})", new)
        if spread != new:
            notes.append(f"{rel}: 조건부 펼치기(...(x && {{…}}))를 같은 동작의 삼항식으로")
            new = spread
        if new != text:
            writes[rel] = new
    return notes, writes


# ── 19) Tailwind 유틸리티를 같은 이름으로 다시 정의(@apply 자기 자신) ───────────────

_SELF_APPLY = re.compile(r"(?m)^([ \t]*)\.([\w-]+)\s*\{\s*@apply\s+([^;{}]+);\s*\}[ \t]*\n?")


def tailwind_self_apply(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {css: 내용}) — `.text-center { @apply text-center; }` 는 Tailwind 가 "circular dependency" 로
    화면 빌드를 멈춘다(실기기 카페: tsc 를 고치자 드러남). 같은 이름의 유틸리티는 Tailwind 가 이미 만들어 주므로
    그 이름만 @apply 에서 빼고, 남는 것이 없으면 규칙을 지운다(화면 모양은 같다)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".css", ".scss", ".pcss")) or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        if "@apply" not in text:
            continue
        names: list[str] = []

        def fix(m: re.Match) -> str:
            utils = m.group(3).split()
            if m.group(2) not in utils:
                return m.group(0)
            names.append(m.group(2))
            rest = [u for u in utils if u != m.group(2)]
            return f"{m.group(1)}.{m.group(2)} {{\n{m.group(1)}  @apply {' '.join(rest)};\n{m.group(1)}}}\n" if rest else ""
        new = _SELF_APPLY.sub(fix, text)
        if new != text:
            writes[rel] = new
            notes.append(f"{rel}: Tailwind 유틸리티를 자기 자신으로 다시 정의({', '.join(names[:4])})")
    return notes, writes


# ── 20) ESM(NodeNext) 서버의 확장자 없는 상대 import ───────────────────────────────

_REL_SPEC = re.compile(r"""(\bfrom\s*|\bimport\s*\(\s*|^\s*import\s*)(['"])(\.{1,2}/[^'"]+)\2""", re.MULTILINE)
_HAS_EXT = re.compile(r"\.(m?js|cjs|jsx|json|css|scss|svg|png|jpe?g|gif|webp|wasm|node|txt|html)$", re.I)


def esm_import_extensions(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — Node 가 ESM 으로 실행하는 서버("type": "module")에서 `import app from './app'`
    처럼 확장자가 없으면 Node 가 ERR_MODULE_NOT_FOUND 로 시작하지 못한다(번들러가 없는 서버는 확장자가 필요).
    tsc 는 moduleResolution 이 node 면 통과시키므로 빌드 검증으로는 드러나지 않는다(실기기 2.0.11: 빌드 통과, 실행 즉시 실패).
    원본이 x.ts/x.js 면 './x.js', 폴더(x/index.ts)면 './x/index.js'. tsconfig 가 CommonJS 로 내보내면(require 가 돼
    ESM 에서 죽는다) module·moduleResolution 을 NodeNext 로 바꾼다. Vite·Next 같은 번들러 폴더는 건드리지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    all_files = list(files.files())
    for pkg_path in all_files:
        if posixpath.basename(pkg_path) != "package.json" or "/node_modules/" in f"/{pkg_path}":
            continue
        folder = posixpath.dirname(pkg_path)
        pkg = load_jsonc(files.read(pkg_path)) or {}
        if pkg.get("type") != "module":
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg.get("dependencies") or {}, dict) else {}
        build = str((pkg.get("scripts") or {}).get("build") or "") if isinstance(pkg.get("scripts"), dict) else ""
        if any(b in deps for b in ("vite", "react-scripts", "next", "webpack", "parcel", "@sveltejs/kit", "nuxt")) \
                or re.search(r"\b(vite|next|webpack|parcel|react-scripts)\b", build):
            continue  # 번들러가 확장자를 처리한다
        if not folder and any(posixpath.basename(p) == "package.json" and "/" in p and "/node_modules/" not in f"/{p}" for p in all_files) \
                and not any(p.endswith((".ts", ".js", ".mjs")) and "/" not in p for p in all_files):
            continue  # 루트는 하위 폴더만 묶는다
        prefix = f"{folder}/" if folder else ""
        cfg_path = f"{prefix}tsconfig.json"
        cfg_text = files.read(cfg_path)
        cfg = load_jsonc(cfg_text) if cfg_text else None
        if cfg is not None:
            opts = cfg.get("compilerOptions") if isinstance(cfg.get("compilerOptions"), dict) else {}
            if str(opts.get("module") or "commonjs").lower() in ("commonjs", "umd", "amd", "system", "none"):
                opts = dict(opts, module="NodeNext", moduleResolution="NodeNext")
                cfg = dict(cfg, compilerOptions=opts)
                writes[cfg_path] = _dump_like(cfg_text, cfg)
                notes.append(f"{cfg_path}: \"type\": \"module\" 서버가 CommonJS 로 컴파일되던 것을 NodeNext(ESM)로")
            elif str(opts.get("moduleResolution") or "").lower() in ("bundler",):
                opts = dict(opts, module="NodeNext", moduleResolution="NodeNext")
                cfg = dict(cfg, compilerOptions=opts)
                writes[cfg_path] = _dump_like(cfg_text, cfg)
                notes.append(f"{cfg_path}: 번들러가 없는 서버의 moduleResolution 을 NodeNext 로")
        exts = (".ts", ".mts") if cfg is not None else (".js", ".mjs")
        for rel in all_files:
            if not rel.startswith(prefix) or not rel.endswith(exts) or rel.endswith(".d.ts") \
                    or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build|public)/", rel[len(prefix):]):
                continue
            if any(posixpath.basename(p) == "package.json" and p != pkg_path and rel.startswith(posixpath.dirname(p) + "/")
                   and len(posixpath.dirname(p)) > len(folder) for p in all_files):
                continue  # 더 안쪽 package.json 이 맡는다
            text = files.read(rel) or ""
            here = posixpath.dirname(rel)

            def fix(m: re.Match) -> str:
                spec = m.group(3)
                if _HAS_EXT.search(spec.split("?")[0]):
                    return m.group(0)
                base = posixpath.normpath(posixpath.join(here, spec))
                if any(files.exists(base + e) for e in (".ts", ".tsx", ".js")):
                    new = spec + ".js"
                elif files.exists(base + ".mts") or files.exists(base + ".mjs"):
                    new = spec + ".mjs"
                elif any(files.exists(f"{base}/index{e}") for e in (".ts", ".tsx", ".js")):
                    new = spec.rstrip("/") + "/index.js"
                else:
                    return m.group(0)  # 없는 파일 — 다른 점검(NODE_LOCAL_IMPORT_MISSING)이 다룬다
                return f"{m.group(1)}{m.group(2)}{new}{m.group(2)}"

            new_text = _REL_SPEC.sub(fix, text)
            if new_text != text:
                writes[rel] = new_text
                notes.append(f"{rel}: 상대 import 에 .js 확장자(Node ESM)")
    return notes, writes


# ── 21) Prisma 스키마가 코드와 다른 관례로 써진 경우 ─────────────────────────────────

_PRISMA_ENUM = re.compile(r"(?ms)^(enum\s+\w+\s*\{)(.*?)^\}")
_ENUM_VALUE = re.compile(r"(?m)^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s+)@map\(\s*\"([a-z][a-z0-9_]*)\"\s*\)([ \t]*(?://[^\n]*)?)$")
_UNSUPPORTED_PRISMA = re.compile(r"(?m)^[ \t]*@@(?:validate|check)\s*\(.*\)[ \t]*\n?")


def prisma_schema_alignment(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {schema.prisma: 내용}) — 스키마를 코드가 실제로 쓰는 모양에 맞춘다(동작은 같다).

    실기기(2.0.10 쇼핑몰, Haiku): 스키마는 `PAID @map("paid")`·금액 BigInt 로, 코드는 'paid' 문자열·number 로 써서
    서버 tsc 오류 78건 중 40여 건이 이것이었고, 없는 속성 `@@validate(…)` 때문에 prisma generate 부터 멈췄다.
    · enum 값: 코드가 대문자 이름(OrderStatus.PAID·'PAID')을 쓰지 않고 @map 의 소문자 값을 쓰면 값 이름을 그 소문자로
      (DB 에 저장되는 값은 @map 과 같아 그대로다).
    · BigInt: 코드에 bigint 를 다루는 흔적(BigInt(…)·123n·bigint)이 없으면 Int 로(금액 최소 단위는 Int 범위).
    · Prisma 에 없는 @@validate / @@check 줄은 지운다(값 검사는 코드가 한다)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    all_files = list(files.files())
    for schema in all_files:
        if not schema.endswith(".prisma") or "/node_modules/" in f"/{schema}":
            continue
        text = files.read(schema) or ""
        side = schema.split("/", 1)[0] + "/" if "/" in schema and not schema.startswith("prisma/") else ""
        code = "\n".join(files.read(p) or "" for p in all_files
                         if p.startswith(side) and p.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs"))
                         and "/node_modules/" not in f"/{p}" and not p.endswith(".d.ts"))
        new = _UNSUPPORTED_PRISMA.sub("", text)
        if new != text:
            notes.append(f"{schema}: Prisma 에 없는 @@validate/@@check 제거")

        renamed: dict[str, str] = {}

        def fix_enum(m: re.Match) -> str:
            head, body = m.group(1), m.group(2)
            name = re.search(r"enum\s+(\w+)", head).group(1)
            values = _ENUM_VALUE.findall(body)
            if not values:
                return m.group(0)
            upper_used = any(re.search(rf"\b{re.escape(name)}\.{re.escape(v[1])}\b|['\"]{re.escape(v[1])}['\"]", code)
                             for v in values if v[1] != v[3])
            lower_used = any(re.search(rf"['\"]{re.escape(v[3])}['\"]", code) for v in values)
            if upper_used or not lower_used:
                return m.group(0)
            notes.append(f"{schema}: enum {name} 값을 코드가 쓰는 소문자({', '.join(v[3] for v in values[:4])}…)로")
            renamed.update({v[1]: v[3] for v in values})
            return head + _ENUM_VALUE.sub(lambda v: f"{v.group(1)}{v.group(4)}{v.group(5)}", body) + "}"

        new = _PRISMA_ENUM.sub(fix_enum, new)
        for before, after in renamed.items():
            #: 기본값(@default(CUSTOMER))도 바뀐 이름으로 — 안 바꾸면 prisma generate 가 P1012 로 멈춘다
            new = re.sub(rf"@default\(\s*{re.escape(before)}\s*\)", f"@default({after})", new)
        if re.search(r"\bBigInt\b", new) and not re.search(r"\bBigInt\s*\(|\b\d+n\b|\bbigint\b", code):
            replaced = re.sub(r"(?m)^(\s*\w+\s+)BigInt(\??)(?=[\s\[]|$)", r"\1Int\2", new)
            if replaced != new:
                notes.append(f"{schema}: 코드가 number 로 다루는 BigInt 열을 Int 로")
                new = replaced
        if new != text:
            writes[schema] = new
    return notes, writes


# ── 22) express Request 를 넓히지 않은 요청 타입 ──────────────────────────────────

_REQ_IFACE = re.compile(r"(?m)^(export\s+)?interface\s+(\w*Request)\s*(<[^>{]*>)?\s*\{")


def express_request_interfaces(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — `interface AuthenticatedRequest { user?: … }` 처럼 express 의 Request 를 넓히지 않은
    요청 타입을 라우터 핸들러 인자로 쓰면 tsc 가 TS2769(No overload)·TS2339(req.body/params/query 없음)를 줄줄이 낸다
    (실기기 2.0.10: 한 정의 때문에 서버 오류 25건). 그 타입을 express 핸들러에서 req 로 쓰는 곳이 있으면 `extends Request`."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    all_files = [p for p in files.files() if p.endswith((".ts", ".tsx")) and "/node_modules/" not in f"/{p}"]
    texts = {p: files.read(p) or "" for p in all_files}
    for rel, text in texts.items():
        new = text
        for m in list(_REQ_IFACE.finditer(text)):
            name = m.group(2)
            used = any(re.search(rf"\(\s*req\s*:\s*{re.escape(name)}\b", t) and re.search(r"from\s+['\"]express['\"]", t)
                       for t in texts.values())
            if not used:
                continue
            new = new.replace(m.group(0), m.group(0)[:-1].rstrip() + " extends Request {", 1)
            notes.append(f"{rel}: {name} 가 express Request 를 넓히게(extends Request)")
        if new != text:
            imp = re.search(r"""(?m)^import\s+(type\s+)?\{([^}]*)\}\s+from\s+['"]express['"];?""", new)
            if imp and re.search(r"\bRequest\b", imp.group(2)):
                pass
            elif imp:
                new = new.replace(imp.group(0), imp.group(0).replace("{", "{ Request,", 1), 1)
            else:
                new = "import type { Request } from 'express';\n" + new
            writes[rel] = new
    return notes, writes


# ── 23) Prisma 관계 필드 이름이 코드와 다름(빌드 로그로 확인) ──────────────────────────

_INCLUDE_MISS = re.compile(r"'(\w+)' does not exist in type '(\w+?)(?:Include|Select)(?:<|')")


def _flat(name: str) -> str:
    return re.sub(r"[_\-]", "", name).lower()


def prisma_relation_renames(log: str, contents: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {schema.prisma: 내용}) — tsc 가 `'order_items' does not exist in type 'OrderInclude'` 라고 하는데
    스키마의 Order 에는 같은 모델(OrderItem[])을 가리키는 관계가 다른 이름(items)으로 하나뿐이면, 스키마의 관계 필드
    이름을 코드가 쓰는 이름으로 바꾼다(관계 필드 이름은 DB 열이 아니라 Prisma 클라이언트의 이름 — DB 는 그대로).
    실기기(2.0.10 Haiku): 서버 tsc 남은 오류 9건 중 6건이 이것. 옛 이름을 include/select 에 쓰는 코드가 있으면 손대지 않는다."""
    misses = sorted(set(_INCLUDE_MISS.findall(log or "")))
    schemas = [p for p in contents if p.endswith(".prisma")]
    if not misses or len(schemas) != 1:
        return [], {}
    schema = schemas[0]
    text = contents[schema] or ""
    models = set(re.findall(r"(?m)^model\s+(\w+)\s*\{", text))
    side = schema.split("/", 1)[0] + "/" if "/" in schema and not schema.startswith("prisma/") else ""
    code = "\n".join(c or "" for p, c in contents.items() if p.startswith(side) and p.endswith((".ts", ".js", ".mjs", ".cjs")))
    notes: list[str] = []
    new = text
    for key, model in misses:
        block = re.search(rf"(?ms)^model\s+{re.escape(model)}\s*\{{(.*?)^\}}", new)
        if not block or model not in models:
            continue
        fields = re.findall(r"(?m)^[ \t]*(\w+)[ \t]+(\w+)(\[\])?\??", block.group(1))
        cands = [f for f, t, _l in fields if t in models and _flat(key) in {
            _flat(t), _flat(t) + "s", _flat(re.sub(r"(?<!^)(?=[A-Z])", "_", t)) + "s"}]
        if len(cands) != 1 or cands[0] == key or any(f == key for f, _t, _l in fields):
            continue
        old = cands[0]
        if re.search(rf"(?:include|select)\s*:\s*\{{[^}}]*\b{re.escape(old)}\s*:", code):
            continue  # 옛 이름도 쓰는 코드가 있다 — 어느 쪽이 맞는지 모른다
        body = re.sub(rf"(?m)^([ \t]*){re.escape(old)}(?=[ \t]+\w)", rf"\g<1>{key}", block.group(1), count=1)
        new = new[:block.start(1)] + body + new[block.end(1):]
        notes.append(f"{schema}: {model}.{old} → {key}(코드가 쓰는 관계 이름)")
    return notes, ({schema: new} if new != text else {})


# ── 24) ESM 서버에서 CommonJS 전역(require·__dirname) ───────────────────────────────

_REQUIRE_LINE = re.compile(r"""(?m)^([ \t]*)(?:const|let|var)\s+(\w+|\{[^}]*\})\s*(?::\s*[\w.<>]+\s*)?=\s*require\(\s*['"]([^'"]+)['"]\s*\)\s*;?[ \t]*\n""")


def _module_mode(files, rel: str) -> str:
    """이 파일이 Node 에서 ESM 으로 실행되는가 — 가장 가까운 package.json 의 type, .mjs/.mts."""
    if rel.endswith((".mjs", ".mts")):
        return "esm"
    if rel.endswith((".cjs", ".cts")):
        return "cjs"
    folder = posixpath.dirname(rel)
    while True:
        pkg = load_jsonc(files.read(posixpath.join(folder, "package.json") if folder else "package.json"))
        if pkg is not None:
            return "esm" if pkg.get("type") == "module" else "cjs"
        if not folder:
            return "cjs"
        folder = posixpath.dirname(folder)


def esm_commonjs_globals(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — "type": "module" 서버에서 `require(…)`·`__dirname` 을 쓰면 tsc 는 통과해도
    Node 가 시작하자마자 ReferenceError(require is not defined in ES module scope)로 죽는다.
    실기기(2.0.11 Haiku 쇼핑몰): 운영 모드에서만 도는 화면 제공 코드가 `const path = require('path')`·`__dirname` 을 써서
    빌드 검증은 통과했지만 컨테이너가 뜨자마자 죽을 상태였다.
    · `const x = require('m')` / `const { a } = require('m')` → 맨 위 `import x from 'm'` / `import { a } from 'm'`
      (이미 같은 이름을 불러왔으면 그 줄만 지운다)
    · `__dirname`·`__filename` → `fileURLToPath(import.meta.url)` 로 만든 같은 이름의 값을 맨 위에 둔다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".mts", ".js", ".mjs")) or rel.endswith(".d.ts") or "/node_modules/" in f"/{rel}" \
                or re.search(r"(^|/)(dist|build|public)/", rel):
            continue
        text = files.read(rel) or ""
        if "require(" not in text and "__dirname" not in text and "__filename" not in text:
            continue
        if _module_mode(files, rel) != "esm" or not re.search(r"(?m)^\s*(import|export)\s", text):
            continue
        if re.search(r"\bcreateRequire\s*\(", text):
            continue  # 이미 ESM 방식으로 require 를 만들어 쓴다
        new = text
        imports: list[str] = []
        for m in list(_REQUIRE_LINE.finditer(text)):
            binding, spec = m.group(2).strip(), m.group(3)
            if binding.startswith("{"):
                names = [n.strip().replace(":", " as ") for n in binding[1:-1].split(",") if n.strip()]
                stmt = f"import {{ {', '.join(names)} }} from '{spec}';"
                bound = [n.split(" as ")[-1].strip() for n in names]
            else:
                stmt = f"import {binding} from '{spec}';"
                bound = [binding]
            already = all(re.search(rf"(?m)^import\s[^;]*\b{re.escape(b)}\b[^;]*from", new) for b in bound)
            new = new.replace(m.group(0), "", 1)
            if not already:
                imports.append(stmt)
        uses_dir = re.search(r"(?<![\w.$])__dirname\b", new) and not re.search(r"\b(?:const|let|var)\s+__dirname\b", new)
        uses_file = re.search(r"(?<![\w.$])__filename\b", new) and not re.search(r"\b(?:const|let|var)\s+__filename\b", new)
        if uses_dir or uses_file:
            imports.append("import { fileURLToPath as recoderFileURLToPath } from 'url';")
            if uses_dir:
                imports.append("import { dirname as recoderDirname } from 'path';")
        #: 식 안에 남은 require(…)(예: `id: require('uuid').v4()`) — Node 의 공식 방법(createRequire)으로 같은 require 를 만든다.
        #: 실기기 0d40: 서버 app.ts·server.ts 에 이것이 남아 시작하자마자(또는 그 경로에서) ReferenceError 로 죽을 코드였다.
        inline = bool(re.search(r"(?<![\w.$])require\s*\(", new)) and not re.search(r"\b(?:const|let|var)\s+require\b", new)
        if inline:
            imports.append("import { createRequire as recoderCreateRequire } from 'module';")
        if new == text and not imports:
            continue
        lines = []
        if imports:
            lines += imports
        if inline:
            lines.append("const require = recoderCreateRequire(import.meta.url);")
        if uses_file or uses_dir:
            lines.append("const __filename = recoderFileURLToPath(import.meta.url);")
        if uses_dir:
            lines.append("const __dirname = recoderDirname(__filename);")
        newline = "\r\n" if "\r\n" in new else "\n"
        last_import = list(re.finditer(r"(?m)^import\s[^;]*;[ \t]*$", new))
        at = last_import[-1].end() if last_import else 0
        block = newline.join(lines)
        new = new[:at] + (newline if at else "") + block + ("" if at else newline) + new[at:]
        writes[rel] = new
        notes.append(f"{rel}: ESM 에서 쓸 수 없는 require/__dirname 을 import·import.meta.url 로")
    return notes, writes


_PROD_GATE = re.compile(
    r"""if\s*\(\s*process\.env\.NODE_ENV\s*===?\s*(['"])production\1\s*\)\s*\{""")


def _block_end(text: str, open_at: int) -> int:
    """`{` 위치에서 짝이 맞는 `}` 의 위치(문자열·주석 안 괄호는 대략 건너뛴다). 못 찾으면 -1."""
    depth, i, quote = 0, open_at, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def frontend_production_gate(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 화면(express.static·index.html)을 `NODE_ENV === 'production'` 일 때만 제공하는 서버.

    결제를 끈 데모(로컬 Docker·ECS staging)는 결제 계약상 NODE_ENV=test 로 뜬다 — 그러면 서버가 화면을 제공하지 않아
    주소를 열면 Cannot GET / 이 된다(실기기 2.0.11 쇼핑몰 0ce6). 조건을 `!== 'development'` 로 바꾸면 개발 서버(vite)를 쓰는
    개발 모드만 빠지고, 운영·데모 컨테이너는 같은 화면을 제공한다. API·인증 동작은 바뀌지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".mts", ".js", ".mjs", ".cjs")) or rel.endswith(".d.ts") \
                or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build|public)/", rel):
            continue
        text = files.read(rel) or ""
        if "NODE_ENV" not in text or ".static(" not in text:
            continue
        new, changed, pos = text, False, 0
        while True:
            m = _PROD_GATE.search(new, pos)
            if not m:
                break
            end = _block_end(new, m.end() - 1)
            body = new[m.end():end] if end > 0 else ""
            if end > 0 and re.search(r"\.static\s*\(", body) and re.search(r"index\.html|sendFile", body):
                cond = f"if (process.env.NODE_ENV !== {m.group(1)}development{m.group(1)}) {{"
                new = new[:m.start()] + cond + new[m.end():]
                pos = m.start() + len(cond)
                changed = True
            else:
                pos = m.end()
        if changed:
            writes[rel] = new
            notes.append(f"{rel}: 화면 제공 조건을 운영 전용에서 개발 모드 제외로")
    return notes, writes


_OPERAND = r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*"
_CURRENCY_CMP = re.compile(
    rf"(?<![\w$.)\]])({_OPERAND})\s*(!==|===|!=|==)\s*({_OPERAND})(?![\w$(\[.])")


def currency_case_compare(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 주문 통화('KRW')와 결제 이벤트 통화를 글자 그대로 비교하는 서버.

    Stripe(와 ReCoder 결제 모듈)는 통화를 소문자('krw')로 돌려준다. 주문을 'KRW' 로 저장한 앱은 결제가 끝나도
    "Payment currency does not match" 로 주문이 영영 결제 대기에 남는다(실기기 2.0.11 쇼핑몰 0ce6, 모의 결제 웹훅까지 확인).
    통화 코드(ISO 4217)는 대소문자 구분이 없으므로 두 쪽을 소문자로 맞춰 비교한다. 다른 비교는 건드리지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".mts", ".js", ".mjs", ".cjs")) or rel.endswith(".d.ts") \
                or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build|public)/", rel):
            continue
        text = files.read(rel) or ""
        if "currency" not in text.lower():
            continue

        def fix(m: re.Match) -> str:
            left, op, right = m.group(1), m.group(2), m.group(3)
            last = lambda e: e.split(".")[-1].lower()
            if "currency" not in last(left) or "currency" not in last(right):
                return m.group(0)
            return f"String({left}).toLowerCase() {op} String({right}).toLowerCase()"

        new = _CURRENCY_CMP.sub(fix, text)
        if new != text:
            writes[rel] = new
            notes.append(f"{rel}: 통화 비교를 대소문자 무시로")
    return notes, writes


# ── Tailwind 연결 ────────────────────────────────────────────────────────────

#: className 에 흔히 쓰는 Tailwind 유틸리티 — 이게 많으면 화면이 Tailwind 를 전제로 쓰였다.
_TW_TOKEN = re.compile(
    r"^(?:[a-z]+:)*(?:flex|grid|hidden|block|inline-flex|inline-block|items-(?:center|start|end)|"
    r"justify-(?:between|center|end|start|around)|(?:p|m)[xytrbl]?-(?:\d+(?:\.5)?|auto)|space-[xy]-\d+|gap-\d+|"
    r"w-(?:full|screen|\d+(?:/\d+)?)|h-(?:full|screen|\d+)|min-h-screen|max-w-\w+|"
    r"text-(?:xs|sm|base|lg|[2-9]?xl|center|left|right|white|black|[a-z]+-\d00)|bg-(?:white|black|[a-z]+-\d00)|"
    r"font-(?:bold|semibold|medium|light)|rounded(?:-\w+)?|shadow(?:-\w+)?|border(?:-[a-z]+-\d00|-\d)?|"
    r"container|mx-auto|transition(?:-\w+)?|hover:[\w-]+|cursor-pointer|overflow-\w+|truncate)$")
_CLASS_ATTR = re.compile(r"""\bclass(?:Name)?\s*=\s*(?:\{\s*)?(?:"([^"]*)"|'([^']*)'|`([^`]*)`)""")
_TW_CONFIGS = ("tailwind.config.js", "tailwind.config.cjs", "tailwind.config.mjs", "tailwind.config.ts")
_POSTCSS_CONFIGS = ("postcss.config.js", "postcss.config.cjs", "postcss.config.mjs", "postcss.config.ts",
                    ".postcssrc", ".postcssrc.json", ".postcssrc.js", ".postcssrc.cjs")
_ENTRY_CANDIDATES = ("src/main.tsx", "src/main.jsx", "src/main.ts", "src/main.js",
                     "src/index.tsx", "src/index.jsx", "src/index.ts", "src/index.js")
_STYLE_PLACEHOLDER = re.compile(r"^/\*\s*ReCoder:[^*]*스타일[^*]*\*/\s*", re.M)
#: 검증한 v3 조합(PostCSS 플러그인 방식 — Vite·CRA 모두).
_TW3_DEPS = {"tailwindcss": "^3.4.17", "postcss": "^8.4.49", "autoprefixer": "^10.4.20"}
_TW4_DEPS = {"tailwindcss": "^4.0.0", "@tailwindcss/postcss": "^4.0.0", "postcss": "^8.4.49"}


def tailwind_token_count(texts: Iterable[str]) -> int:
    count = 0
    for text in texts:
        for m in _CLASS_ATTR.finditer(text or ""):
            value = m.group(1) or m.group(2) or m.group(3) or ""
            count += sum(1 for token in value.split() if _TW_TOKEN.match(token))
    return count


def _tw_major(pkg: dict) -> int:
    for section in ("dependencies", "devDependencies"):
        spec = str((pkg.get(section) or {}).get("tailwindcss") or "")
        if not spec:
            continue
        m = re.search(r"(\d+)", spec)
        if m:
            return 4 if int(m.group(1)) >= 4 else 3
        return 4  # latest·* 는 지금 설치되는 v4
    return 3


#: Tailwind 에만 있는 모양(Bootstrap 과 겹치지 않는 것) — mt-4·p-3·text-center·rounded 같은 공통 이름만으로는 Tailwind 로 보지 않는다.
_TW_STRONG = re.compile(r"^(?:[a-z]+:)*(?:(?:bg|text|border|from|to|ring)-[a-z]+-\d00|[pm][xy]-\d|space-[xy]-\d|gap-\d|"
                        r"min-h-screen|max-w-(?:\d?xl|screen-\w+)|font-(?:semibold|medium)|items-center|justify-(?:between|center)|"
                        r"rounded-(?:md|lg|xl|full)|shadow-(?:md|lg|xl)|w-\d+|h-\d+|grid-cols-\d+|hover:[\w-]+|md:[\w-]+|sm:[\w-]+)$")


def _tailwind_strong_count(texts) -> int:
    count = 0
    for text in texts:
        for m in _CLASS_ATTR.finditer(text or ""):
            value = m.group(1) or m.group(2) or m.group(3) or ""
            count += sum(1 for token in value.split() if _TW_STRONG.match(token))
    return count


def tailwind_directives(major: int) -> str:
    return ('@import "tailwindcss";\n' if major >= 4 else "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n")


def _has_directives(css: str, major: int) -> bool:
    if major >= 4:
        return bool(re.search(r"""@import\s+(?:url\()?["']tailwindcss["']""", css))
    return bool(re.search(r"@tailwind\s+utilities", css)) or bool(re.search(r"""@import\s+["']tailwindcss/utilities""", css))


def _with_directives(css: str, major: int) -> str:
    """지시문을 앞쪽(@charset·@import 다음)에 넣는다. ReCoder 가 넣었던 빈 자리 주석은 지운다."""
    body = _STYLE_PLACEHOLDER.sub("", css or "", count=1)
    lines = body.splitlines(keepends=True)
    at = 0
    while at < len(lines) and (not lines[at].strip() or re.match(r"\s*@(?:charset|import)\b", lines[at])):
        at += 1
    head = "".join(lines[:at])
    if head and not head.endswith("\n"):
        head += "\n"
    rest = "".join(lines[at:])
    return head + tailwind_directives(major) + ("\n" + rest if rest.strip() else "")


def tailwind_setup(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 화면이 Tailwind 클래스로 쓰였는데 빌드에 Tailwind 가 연결돼 있지 않은 Vite·CRA 앱.

    실기기 2.0.12 쇼핑몰(ECS 배포까지 성공): 화면 코드는 Tailwind 클래스 359개를 썼는데 tailwind.config·postcss.config 가
    없고 스타일 파일은 빈 자리 주석뿐이라, 배포한 화면이 스타일 없는 HTML 로 보였다. 빌드는 통과하므로 어떤 점검에도
    걸리지 않았다. 설정 파일 두 개와 지시문(@tailwind …)을 넣고, 빠진 개발 의존성을 선언한다(기존 설정은 바꾸지 않는다)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    listing = files.files()
    for manifest in [p for p in listing if posixpath.basename(p) == "package.json" and "/node_modules/" not in f"/{p}"]:
        folder = posixpath.dirname(manifest)
        prefix = f"{folder}/" if folder else ""
        pkg = load_jsonc(files.read(manifest)) or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        bundler = "vite" if "vite" in deps else "cra" if "react-scripts" in deps else ""
        if not bundler:
            continue
        sources = [p for p in listing if p.startswith(prefix + "src/") and "/node_modules/" not in f"/{p}"
                   and p.endswith((".tsx", ".jsx", ".ts", ".js", ".html", ".vue", ".svelte"))]
        tokens = tailwind_token_count(files.read(p) or "" for p in sources + [prefix + "index.html"])
        if tokens < 15 or _tailwind_strong_count(files.read(p) or "" for p in sources) < 8:
            continue
        html = files.read(prefix + "index.html") or ""
        if re.search(r"cdn\.tailwindcss\.com|@tailwindcss/browser|bootstrap", html):
            continue  # 브라우저에서 바로 쓰는 Tailwind(CDN)·Bootstrap 화면 — 건드리지 않는다
        if deps.keys() & {"bootstrap", "react-bootstrap", "bulma", "@tailwindcss/vite", "@tailwindcss/postcss", "windicss", "unocss"}:
            continue  # 다른 CSS 프레임워크이거나 이미 v4 플러그인으로 연결됨
        vite_conf = "".join(files.read(prefix + n) or "" for n in ("vite.config.ts", "vite.config.js", "vite.config.mjs"))
        if "tailwind" in vite_conf:
            continue
        locked = any(files.exists(prefix + n) for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
        major = _tw_major(pkg)
        if locked and any(k not in deps for k in (_TW4_DEPS if major >= 4 else _TW3_DEPS)):
            continue  # 잠금 파일이 있으면 의존성을 더할 수 없다(npm ci 실패) — 설치 안 된 플러그인을 쓰는 설정도 쓰지 않는다
        esm = pkg.get("type") == "module"
        changed: list[str] = []
        # 1) 설정 파일 (v4 는 tailwind.config 가 필요 없다)
        if major < 4 and not any(files.exists(prefix + name) for name in _TW_CONFIGS):
            content = (["./src/**/*.{js,jsx,ts,tsx}", "./public/index.html"] if bundler == "cra"
                       else ["./index.html", "./src/**/*.{js,jsx,ts,tsx,vue,svelte}"])
            body = ("/** @type {import('tailwindcss').Config} */\n"
                    + ("export default " if esm else "module.exports = ")
                    + "{\n  content: " + json.dumps(content) + ",\n  theme: { extend: {} },\n  plugins: [],\n};\n")
            writes[prefix + "tailwind.config.js"] = body
            changed.append("tailwind.config.js")
        has_postcss = any(files.exists(prefix + name) for name in _POSTCSS_CONFIGS) or "postcss" in pkg
        if bundler == "vite" and not has_postcss:
            plugins = "{ '@tailwindcss/postcss': {} }" if major >= 4 else "{ tailwindcss: {}, autoprefixer: {} }"
            writes[prefix + "postcss.config.js"] = (("export default " if esm else "module.exports = ")
                                                    + "{\n  plugins: " + plugins + ",\n};\n")
            changed.append("postcss.config.js")
        # 2) 지시문 — 진입 파일이 불러오는 스타일 파일(없으면 src/index.css 를 만들고 불러온다)
        entry = None
        html = files.read(prefix + "index.html") or ""
        m = re.search(r"""<script[^>]+src=["']/?([^"']+\.(?:tsx|jsx|ts|js))["']""", html)
        if m and files.exists(prefix + m.group(1)):
            entry = prefix + m.group(1)
        entry = entry or next((prefix + c for c in _ENTRY_CANDIDATES if files.exists(prefix + c)), None)
        css_files = [p for p in listing if p.startswith(prefix) and p.endswith(".css") and "/node_modules/" not in f"/{p}"
                     and not re.search(r"(^|/)(dist|build)/", p[len(prefix):])]
        if any(_has_directives(writes.get(p) or files.read(p) or "", major) for p in css_files):
            pass
        elif entry:
            entry_text = files.read(entry) or ""
            imported = [posixpath.normpath(posixpath.join(posixpath.dirname(entry), spec))
                        for spec in re.findall(r"""^\s*import\s+['"](\.{1,2}/[^'"]+\.css)['"]""", entry_text, re.M)]
            target = next((p for p in imported if files.exists(p) or p in writes), None)
            if target is None:
                target = imported[0] if imported else posixpath.join(posixpath.dirname(entry), "index.css")
                if not imported:
                    lines = entry_text.splitlines(keepends=True)
                    last = max((i for i, line in enumerate(lines) if re.match(r"\s*import\b", line)), default=-1)
                    lines.insert(last + 1, "import './index.css';\n")
                    writes[entry] = "".join(lines)
                    changed.append(f"{entry[len(prefix):]} (스타일 불러오기)")
            writes[target] = _with_directives(files.read(target) or "", major)
            changed.append(f"{target[len(prefix):]} (@tailwind 지시문)")
        # 3) 개발 의존성
        #: 설정을 이미 넣었더라도(앞 회차 — 같은 package.json 을 다른 교정이 먼저 고쳤을 수 있다) 의존성은 늘 맞춘다.
        #: 실기기 0d40: 설정만 들어가고 tailwindcss 가 선언되지 않아 화면 빌드가 "Cannot find module 'tailwindcss'" 로 멈췄다.
        need = {k: v for k, v in (_TW4_DEPS if major >= 4 else _TW3_DEPS).items() if k not in deps}
        if need and not locked:
            raw = files.read(manifest) or "{}"
            data = json.loads(raw)
            data.setdefault("devDependencies", {}).update(need)
            newline = "\r\n" if "\r\n" in raw else "\n"
            writes[manifest] = json.dumps(data, ensure_ascii=False, indent=2).replace("\n", newline) + newline
            changed.append("package.json (" + ", ".join(need) + ")")
        if changed:
            notes.append(f"{folder or '.'}: Tailwind 클래스 {tokens}개를 쓰는 화면에 Tailwind 연결 — " + ", ".join(changed))
    return notes, writes


# ── 샘플 데이터 스크립트를 빌드에 넣기 ───────────────────────────────────────

SEED_SCRIPT_NAMES = ("db:seed", "seed", "seed:db", "db:seed:demo", "init-db", "db:init", "init:db", "setup-db", "db:setup")
_SEED_RUNNER = re.compile(r"(?:tsx|ts-node(?:-esm)?|node\s+--loader\s+\S+)\s+(?:watch\s+)?\.?/?([\w./-]+\.ts)\b")
_REL_IMPORT = re.compile(r"""((?:\bfrom|\bimport)\s*\(?\s*)(['"])(\.{1,2}/[^'"]+)\2""")
_DIRNAME_PATH = re.compile(r"""(path\.(?:join|resolve)\(\s*__dirname\s*,\s*)(['"])([^'"]+)\2(\s*\))""")


def _rel(target: str, start: str) -> str:
    out = posixpath.relpath(target, start or ".")
    return out if out.startswith(".") else "./" + out


def seed_script_into_build(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 샘플 데이터 스크립트가 서버 빌드(tsconfig rootDir) 밖에 있어 배포한 앱에서 돌릴 수 없는 경우.

    실기기 2.0.12 쇼핑몰: 데모 상품을 넣는 `backend/scripts/init-db.ts` 가 rootDir(src) 밖이라 dist 에 없고,
    package.json 은 있지도 않은 `src/scripts/init-db.ts` 를 가리켰다 → 로컬·ECS 어디서도 상품이 0개.
    스크립트를 rootDir 안(src/scripts/)으로 옮기고(상대 import·__dirname 경로를 새 위치 기준으로), package.json 경로를 맞춘다.
    옛 위치에는 새 파일을 부르는 한 줄만 남긴다. 옮긴 파일은 타입 검사에서 뺀다 — 예전엔 빌드에 들지 않던 파일이
    서버 빌드를 깨면 안 된다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    listing = files.files()
    for manifest in [p for p in listing if posixpath.basename(p) == "package.json" and "/node_modules/" not in f"/{p}"]:
        folder = posixpath.dirname(manifest)
        prefix = f"{folder}/" if folder else ""
        pkg = load_jsonc(files.read(manifest)) or {}
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        conf = load_jsonc(files.read(prefix + "tsconfig.json"))
        if not scripts or not isinstance(conf, dict):
            continue
        options = conf.get("compilerOptions") if isinstance(conf.get("compilerOptions"), dict) else {}
        if not options.get("rootDir"):
            continue  # rootDir 를 정하지 않은 프로젝트에 파일을 옮기면 tsc 가 출력 폴더 모양을 바꾼다(dist/src/…)
        root = str(options.get("rootDir")).strip().lstrip("./").rstrip("/")
        out = str(options.get("outDir") or "dist").strip().lstrip("./").rstrip("/")
        if not root or root == ".":
            continue
        new_scripts = dict(scripts)
        for name in SEED_SCRIPT_NAMES:
            cmd = scripts.get(name)
            m = _SEED_RUNNER.search(cmd) if isinstance(cmd, str) else None
            if not m:
                continue
            spec = posixpath.normpath(m.group(1))
            actual = prefix + spec if files.exists(prefix + spec) else None
            if actual is None:
                found = [p for p in listing if p.startswith(prefix) and posixpath.basename(p) == posixpath.basename(spec)
                         and "/node_modules/" not in f"/{p}" and not re.search(r"(^|/)(dist|build)/", p[len(prefix):])]
                actual = found[0] if len(found) == 1 else None
            if actual is None:
                continue
            in_pkg = actual[len(prefix):]
            if in_pkg.startswith(root + "/"):
                target = in_pkg
            else:
                target = f"{root}/scripts/{posixpath.basename(in_pkg)}"
                if files.exists(prefix + target) or prefix + target in writes:
                    continue
                old_dir, new_dir = posixpath.dirname(in_pkg), posixpath.dirname(target)
                runtime_dir = posixpath.join(out, posixpath.relpath(new_dir, root))
                text = files.read(actual) or ""
                moved = _REL_IMPORT.sub(lambda r: r.group(1) + r.group(2) + _rel(
                    posixpath.normpath(posixpath.join(old_dir, r.group(3))), new_dir) + r.group(2), text)

                def asset(r: re.Match) -> str:
                    resolved = posixpath.normpath(posixpath.join(old_dir, r.group(3)))
                    if not files.exists(prefix + resolved):
                        return r.group(0)
                    #: 빌드한 파일(dist/scripts)에서 원본 자원(src/db/schema.sql 등)을 찾는다 — 실행 이미지에 서버 폴더가 통째로 있다.
                    return r.group(1) + r.group(2) + posixpath.relpath(resolved, runtime_dir) + r.group(2) + r.group(4)

                moved = _DIRNAME_PATH.sub(asset, moved)
                outside = [r.group(3) for r in _REL_IMPORT.finditer(moved)
                           if not posixpath.normpath(posixpath.join(new_dir, r.group(3))).startswith(root + "/")]
                if outside:
                    continue  # rootDir 밖(../data/…)을 import 하면 tsc 가 rootDir 오류를 낸다 — 옮기지 않는다
                writes[prefix + target] = ("// @ts-nocheck — ReCoder: 배포한 앱에서도 돌 수 있게 빌드 폴더 안으로 옮긴 샘플 데이터 "
                                           "스크립트(타입 검사 제외)\n" + moved)
                writes[actual] = (f"// ReCoder: 이 스크립트는 {target} 로 옮겼습니다(서버 빌드에 포함되도록).\n"
                                  f"import '{_rel(posixpath.splitext(target)[0] + '.js', old_dir)}';\n")
                notes.append(f"{in_pkg} → {target}")
            if spec != target:
                new_scripts[name] = cmd.replace(m.group(1), target, 1)
                notes.append(f"package.json {name}: {target}")
        if new_scripts != scripts:
            raw = files.read(manifest) or "{}"
            data = json.loads(raw)
            data["scripts"] = new_scripts
            newline = "\r\n" if "\r\n" in raw else "\n"
            writes[manifest] = json.dumps(data, ensure_ascii=False, indent=2).replace("\n", newline) + newline
    return notes, writes


#: 코드가 불러오는데 AI 가 만들지 않은 스타일 파일에 넣는 기본 스타일. 예전엔 빈 주석만 넣어 화면이 맨 HTML 로 보였다
#: (실기기 2.0.12). Tailwind 앱은 tailwind_setup 이 이 내용을 지시문으로 바꾼다(클래스 선택자보다 약한 요소 선택자만 쓴다).
BASE_STYLESHEET = """/* ReCoder 기본 스타일 — 코드가 불러오지만 생성되지 않은 스타일 파일이라 기본 모양을 넣었습니다. 자유롭게 바꾸세요. */
:root { color-scheme: light; --rc-text: #1f2937; --rc-muted: #6b7280; --rc-line: #e5e7eb; --rc-accent: #2563eb; --rc-bg: #f9fafb; }
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans KR", sans-serif; line-height: 1.6;
       color: var(--rc-text); background: var(--rc-bg); }
#root, main { max-width: 1120px; margin: 0 auto; padding: 0 20px 48px; }
header, nav { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; padding: 14px 0; border-bottom: 1px solid var(--rc-line); }
h1, h2, h3 { line-height: 1.25; margin: 1.2em 0 .5em; }
a { color: var(--rc-accent); text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; border-radius: 8px; }
button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--rc-accent); background: var(--rc-accent);
         color: #fff; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
input, select, textarea { font: inherit; padding: 8px 10px; border: 1px solid var(--rc-line); border-radius: 8px; background: #fff;
                          max-width: 100%; }
form { display: grid; gap: 12px; max-width: 420px; }
table { width: 100%; border-collapse: collapse; background: #fff; }
th, td { padding: 10px; border-bottom: 1px solid var(--rc-line); text-align: left; }
ul { padding-left: 1.2em; }
"""


def missing_style_content(files, rel: str) -> str:
    """없는 스타일 파일에 넣을 내용 — 그 화면이 Tailwind 로 쓰였으면 지시문, 아니면 기본 스타일."""
    parts = rel.split("/")
    for depth in range(len(parts) - 1, -1, -1):
        prefix = "/".join(parts[:depth])
        manifest = f"{prefix}/package.json" if prefix else "package.json"
        if not files.exists(manifest):
            continue
        pkg = load_jsonc(files.read(manifest)) or {}
        sources = [p for p in files.files() if p.startswith((prefix + "/src/") if prefix else "src/")
                   and p.endswith((".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte"))]
        if tailwind_token_count(files.read(p) or "" for p in sources) >= 15:
            return tailwind_directives(_tw_major(pkg))
        break
    return BASE_STYLESHEET


# ── 화면을 다시 그리지 않는 상태 훅 ───────────────────────────────────────────

def _statement_end(text: str, start: int) -> int:
    """start 에서 시작한 문장이 끝나는 `;`(괄호 밖) 바로 뒤 위치. 못 찾으면 줄 끝."""
    depth, i, quote = 0, start, ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return i + 1
        elif ch == "\n" and depth <= 0 and text[start:i].strip().endswith(")"):
            return i
        i += 1
    return len(text)


def ref_state_hooks(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 받아 온 값을 useRef 에만 넣고 화면을 다시 그리지 않는 사용자 훅.

    실기기 2.0.12 쇼핑몰: `useApi` 가 응답을 `stateRef.current.data = …` 로만 넣고 `{ data: stateRef.current.data }` 를
    돌려줘서, 요청이 성공해도 컴포넌트가 다시 그려지지 않았다 → 상품 상세는 로딩이 끝나지 않고 목록은 늘 비어 보였다.
    빌드·타입 검사로는 잡히지 않는다. 값을 바꾼 뒤 다시 그리게(useReducer) 하고, 매번 새로 만들어지는 훅 인자(options)가
    콜백 의존성에 있으면 다시 그릴 때마다 요청이 반복되므로 ref 로 최신 값을 읽게 바꾼다. 반환 모양·호출 방법은 같다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".tsx", ".js", ".jsx")) or rel.endswith(".d.ts") or "/node_modules/" in f"/{rel}" \
                or re.search(r"(^|/)(dist|build)/", rel):
            continue
        text = files.read(rel) or ""
        if "useRef" not in text or re.search(r"\b(?:useState|useReducer|useSyncExternalStore)\s*[(<]", text):
            continue
        hook = re.search(r"(?m)^(\s*)(?:export\s+)?(?:default\s+)?function\s+(use[A-Z][\w$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)", text)
        if not hook:
            continue
        ref = re.search(r"(?m)^(\s*)const\s+([\w$]+)\s*=\s*useRef\b", text[hook.end():])
        if not ref:
            continue
        name, indent = ref.group(2), ref.group(1)
        returns_ref = re.search(rf"\breturn\s*\{{[^}}]*:\s*{re.escape(name)}\.current\.[\w$]+", text)
        assigns = re.search(rf"(?m)^\s*{re.escape(name)}\.current\.[\w$]+\s*=", text)
        if not (returns_ref and assigns):
            continue
        ts = rel.endswith((".ts", ".tsx"))
        lines = text.splitlines(keepends=True)
        # 1) 값을 바꾼 묶음 뒤에서 다시 그린다 — 훅 본문 바로 아래(렌더 중) 대입은 건드리지 않는다(무한 렌더).
        #    대입이 여러 줄이어도(객체·삼항) 문장이 끝난 뒤에 넣는다.
        assign = re.compile(rf"(?m)^([ \t]*){re.escape(name)}\.current\.[\w$]+\s*=(?!=)")
        spans = []
        broken = False
        for m in assign.finditer(text):
            if len(m.group(1)) > len(indent):
                e = stmt_end(text, m.end())
                if e < 0:
                    broken = True
                    break
                spans.append((m.start(), e, m.group(1)))
        if broken:
            continue  # 식 안의 대입(화살표 함수 몸통 등)은 안전하게 다시 그릴 자리가 없다 — 건드리지 않는다
        groups: list[tuple[int, str]] = []
        for k, (a, b, ind) in enumerate(spans):
            nxt = spans[k + 1][0] if k + 1 < len(spans) else None
            if nxt is not None and not text[b:nxt].strip(" \t\r\n;"):
                continue
            groups.append((b, ind))
        new = text
        nl = "\r\n" if "\r\n" in text else "\n"
        for b, ind in reversed(groups):
            new = new[:b] + f"{nl}{ind}recoderRerender();" + new[b:]
        # 2) useRef 문장 뒤에 다시 그리는 장치와(필요하면) 최신 인자 ref
        start = hook.end() + ref.start(0)
        end = _statement_end(new, new.index("useRef", start))
        params = [re.split(r"[?:=\s]", p.strip(), maxsplit=1)[0] for p in hook.group(3).split(",") if p.strip()]
        params = [p for p in params if re.fullmatch(r"[A-Za-z_$][\w$]*", p)]
        raw_params = {re.split(r"[?:=\s]", x.strip(), maxsplit=1)[0]: x for x in hook.group(3).split(",") if x.strip()}

        def _object_like(p: str) -> bool:
            #: 렌더마다 새로 만들어지는 객체 인자(options = {}, config, params …)는 ref 로 읽는다 — 의존성에 남기면
            #: 다시 그릴 때마다 요청이 반복된다. url 같은 값 인자는 의존성에 남긴다(빼면 주소가 바뀌어도 첫 값으로만 요청).
            body = re.sub(r"\[[^\]\n]*\]\s*\)", "", new[end:])
            only_props = not re.search(rf"(?<![\w$.]){re.escape(p)}(?![\w$]|\s*\??\.)", body)
            decl = raw_params.get(p, "")
            return only_props or bool(re.search(r"=\s*[{\[]", decl)) or bool(
                re.fullmatch(r"(?:options?|opts|config|cfg|params|settings|init|requestOptions|fetchOptions)", p))
        stale = [p for p in params if _object_like(p)
                 and re.search(rf"\[\s*(?:[\w$]+\s*,\s*)*{re.escape(p)}\s*(?:,\s*[\w$]+\s*)*\]\s*\)", new[end:])]
        if any(re.search(rf"[{{,]\s*{re.escape(p)}\s*[,}}]", new[end:]) for p in stale):
            continue  # 단축 속성({ options })은 ref 로 바꿀 수 없다 — 건드리지 않는다
        block = f"\n{indent}const [, recoderRerender] = useReducer({'(n: number)' if ts else '(n)'} => n + 1, 0);"
        for p in stale:
            block += f"\n{indent}const recoder_{p} = useRef({p});\n{indent}recoder_{p}.current = {p};"
        new = new[:end] + block + new[end:]
        tail_from = end + len(block)
        tail = new[tail_from:]
        for p in stale:
            #: 먼저 의존성 배열에서 빼고(`[url, options]` → `[url]`), 그다음 나머지 읽기를 ref 로.
            def _drop(m: re.Match, p=p) -> str:
                items = [x.strip() for x in m.group(1).split(",") if x.strip() and x.strip() != p]
                return "[" + ", ".join(items) + "]" + m.group(2)
            tail = re.sub(r"\[([^\[\]\n]*)\](\s*\))", lambda m, p=p: _drop(m) if re.search(rf"(?<![\w$.]){re.escape(p)}(?![\w$])", m.group(1)) else m.group(0), tail)
            tail = re.sub(rf"(?<![\w$.]){re.escape(p)}(?![\w$])", f"recoder_{p}.current", tail)
        new = new[:tail_from] + tail
        # 3) import
        new = add_react_named(new, "useReducer")
        if new != text:
            writes[rel] = new
            notes.append(f"{rel}: {hook.group(2)} 가 값을 바꾼 뒤 화면을 다시 그리게")
    return notes, writes


# ── 화면이 부르는 상태 확인 주소 · 원화 금액 표시 ─────────────────────────────

def health_alias(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 서버는 /health 만 두었는데 화면은 /api/health 로 부르는 앱.

    실기기 2.0.12 쇼핑몰: 결제 결과 화면이 api 클라이언트(baseURL '/api')로 '/health' 를 불러 /api/health → 404.
    테스트 결제 모드를 모른 채 결제 완료를 기다리지 않아, 결제는 끝났는데 화면이 비어 있었다(0d40 결과물도 fetch('/api/health')).
    결제 약속은 /health 또는 /api/health 둘 다 허용하므로 서버의 상태 경로가 두 주소에 같은 응답을 주게 한다
    (`app.get(['/health', '/api/health'], …)`). 앱이 따로 띄우는 모의 서버(mock 등)는 건드리지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    listing = [p for p in files.files() if p.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs"))
               and "/node_modules/" not in f"/{p}" and not re.search(r"(^|/)(dist|build|tests?|__tests__)/", p)]
    texts = {p: files.read(p) or "" for p in listing}
    ui = {p for p, t in texts.items() if p.endswith((".tsx", ".jsx")) or re.search(r"(^|/)(frontend|client|web)/", p)}
    server = {p: t for p, t in texts.items() if p not in ui and not re.search(r"mock|stub|fake", posixpath.basename(p), re.I)}
    api_base = any(re.search(r"""baseURL\s*:[^,\n}]*['"`][^'"`]*/api/?['"`]""", texts[p]) or
                   re.search(r"""(?:API_BASE_URL|API_URL)\s*=[^;\n]*['"`]/api/?['"`]""", texts[p]) for p in ui)
    wants = any(re.search(r"""['"`]/api/health['"`]""", texts[p]) for p in ui) or (
        api_base and any(re.search(r"""\(\s*['"`]/health['"`]""", texts[p]) for p in ui))
    if not wants or any(re.search(r"""['"`]/api/health['"`]""", t) for t in server.values()):
        return notes, writes
    route = re.compile(r"""\b(app|router|[\w$]*[Rr]outer)\.get\(\s*(['"])/health\2\s*,""")
    for rel, text in sorted(server.items(), key=lambda kv: "/routes/" not in kv[0]):
        m = route.search(text)
        if not m:
            continue
        if not re.search(r"""from\s+['"]express['"]|require\(\s*['"]express['"]\s*\)""", text):
            continue  # 배열 경로는 Express 만 받는다(Fastify 는 시작 실패, Hono 는 404)
        if m.group(1) != "app":
            #: 라우터면 어디에 붙었는지 본다 — '/api' 아래면 이미 /api/health 다.
            stem = posixpath.splitext(posixpath.basename(rel))[0]
            mounts = [re.search(rf"""\.use\(\s*(['"])([^'"]*)\1\s*,\s*([\w$]+)""", line)
                      for t in server.values() for line in t.splitlines() if stem in t and ".use(" in line]
            names = {name for t in server.values()
                     for name in re.findall(rf"""import\s+([\w$]+)\s+from\s+['"][^'"]*/{re.escape(stem)}(?:\.[jt]s)?['"]""", t)}
            prefixes = [mm.group(2) for mm in mounts if mm and mm.group(3) in names]
            if any(pfx.rstrip("/") == "/api" for pfx in prefixes):
                return notes, writes
            if prefixes and not any(pfx.rstrip("/") == "" for pfx in prefixes):
                continue
        writes[rel] = route.sub(lambda mm: f"{mm.group(1)}.get(['/health', '/api/health'],", text, count=1)
        notes.append(f"{rel}: /api/health 도 같은 상태 응답")
        break
    return notes, writes


def krw_minor_units(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 원화 금액을 100 으로 나눠 표시하는 화면.

    원화는 소수 단위가 없어 결제(Stripe·ReCoder 결제 모듈)는 저장된 정수를 그대로 '원' 으로 청구한다. 그런데 AI 가
    `price_cents / 100` 으로 표시해 실기기 2.0.12 쇼핑몰은 4,900원짜리를 ₩49 로 보여 주고 ₩4,900 을 결제했다.
    원화(KRW)로 표시하는 파일의 단위 변환 함수만 고친다(나누기·곱하기 100 → 그대로)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".tsx", ".js", ".jsx")) or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build)/", rel):
            continue
        text = files.read(rel) or ""
        if not re.search(r"""currency\s*:\s*['"]KRW['"]""", text) or not re.search(r"\b100\b", text):
            continue
        if re.search(r"""currency\s*:\s*['"](?!KRW)[A-Z]{3}['"]""", text):
            continue  # 여러 통화를 다루는 파일은 건드리지 않는다

        def body_fix(m: re.Match) -> str:
            body = m.group(0)
            body = re.sub(r"return\s+([\w$.]+)\s*/\s*100\s*;", r"return \1;", body)
            body = re.sub(r"Math\.round\(\s*([\w$.]+)\s*\*\s*100\s*\)", r"Math.round(\1)", body)
            return body

        fn = re.compile(r"(?:export\s+)?function\s+[\w$]*(?:[Cc]ents|[Ww]on|KRW|[Kk]rw|[Mm]inor)[\w$]*\s*\([^)]*\)[^{]*\{[^{}]*\}")
        new = fn.sub(body_fix, text)
        if new != text:
            writes[rel] = new
            notes.append(f"{rel}: 원화 금액을 100 으로 나누지 않게")
    return notes, writes


# ── 새로고침하면 로그인이 풀리는 화면 ─────────────────────────────────────────

def auth_user_persist(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 토큰은 localStorage 에 두면서 사용자 정보는 메모리에만 두는 로그인 상태.

    실기기 2.0.13 카페: 가입·로그인은 되는데 새로고침하거나 주소로 이동하면 user 가 null 이 되어 헤더가 다시
    '로그인/회원가입' 이 되고, 주문 내역(로그인 필요)은 로그인 화면으로 튕겼다. 토큰은 남아 있으므로 사용자 정보도
    같은 방식으로 보관·복원한다(로그아웃하면 지운다). 사용자 정보를 서버(/auth/me)로 되살리는 앱은 건드리지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".tsx", ".jsx", ".ts", ".js")) or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build)/", rel):
            continue
        text = files.read(rel) or ""
        if "createContext" not in text or "localStorage" not in text:
            continue
        m = re.search(r"const\s*\[\s*user\s*,\s*setUser\s*\]\s*=\s*useState(<([^>]+)>)?\(\s*null\s*\)\s*;", text)
        if not m or not re.search(r"localStorage\.getItem\(\s*['\"][\w-]*token[\w-]*['\"]", text, re.I):
            continue
        if re.search(r"localStorage\.(?:get|set)Item\(\s*['\"][\w-]*user", text, re.I) or re.search(r"/auth/me|/me['\"`]|/profile['\"`]", text):
            continue
        typ = m.group(2)
        t = typ or ""
        init = ("() => {\n    try {\n      const saved = localStorage.getItem('recoder_auth_user');\n"
                f"      return saved ? {'(JSON.parse(saved) as ' + t + ')' if t else 'JSON.parse(saved)'} : null;\n"
                "    } catch {\n      return null;\n    }\n  }")
        param = f"value: {t} | ((prev: {t}) => {t})" if t else "value"
        call = f"(value as (prev: {t}) => {t})(prev)" if t else "value(prev)"
        block = (f"const [user, recoderSetUser] = useState{m.group(1) or ''}({init});\n"
                 "  //: ReCoder: 사용자 정보도 토큰처럼 보관해 새로고침 뒤에도 로그인 상태가 유지되게 한다(로그아웃하면 지운다).\n"
                 f"  const setUser = useCallback(({param}) => {{\n"
                 "    recoderSetUser((prev) => {\n"
                 f"      const next = typeof value === 'function' ? {call} : value;\n"
                 "      try {\n        if (next) localStorage.setItem('recoder_auth_user', JSON.stringify(next));\n"
                 "        else localStorage.removeItem('recoder_auth_user');\n      } catch {\n        /* 저장소를 못 써도 로그인은 동작 */\n      }\n"
                 "      return next;\n    });\n  }, []);")
        new = text[:m.start()] + block + text[m.end():]
        new = add_react_named(new, "useCallback")
        writes[rel] = new
        notes.append(f"{rel}: 새로고침 뒤에도 로그인 사용자 유지")
    return notes, writes


# ── 서버 응답 봉투({status, data}) 와 화면이 기대하는 모양 ────────────────────

_ENVELOPE_SERVER = re.compile(r"""\.json\(\s*\{\s*(?:status\s*:\s*['"]success['"]|success\s*:\s*true)\s*,[^)]*\bdata\b""", re.S)
_UNWRAP_HELPER = """
//: ReCoder: 서버가 모든 응답을 { status, message, data } 로 감싸므로, data 안의 값도 바로 읽을 수 있게 한다.
//: 원래 있던 키(status·message·data)는 그대로 두고 빠진 키만 채운다 — 기존 코드의 `.data` 접근은 그대로 동작한다.
//: 목록(data 가 배열)은 그 배열에 status·message·data 를 붙여 돌려준다 — `.map` 도, `.data.map` 도 동작한다.
function recoderUnwrap<T>(body: T): T {
  const b = body as unknown as Record<string, unknown>;
  if (!b || typeof b !== 'object' || Array.isArray(b) || !('status' in b || 'success' in b) || !b.data || typeof b.data !== 'object') {
    return body;
  }
  if (Array.isArray(b.data)) {
    return Object.assign([...(b.data as unknown[])], b) as unknown as T;
  }
  return { ...(b.data as Record<string, unknown>), ...b } as unknown as T;
}
"""


def api_envelope_unwrap(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 서버는 성공 응답을 `{ status: 'success', message, data }` 로 감싸는데, 화면의 API
    클라이언트는 본문을 그대로 돌려줘 `const { token, id } = await api.post('/auth/signup')` 가 undefined 를 받는 앱.

    실기기 2.0.13 카페: 가입은 성공(200)했는데 토큰이 'undefined' 로 저장되어 주문 내역이 401(Invalid token)이었다.
    클라이언트가 돌려주는 값에 data 안의 키를 더한다(있던 키는 그대로 — 지금 `.data` 로 읽는 코드도 계속 동작)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    listing = files.files()
    server = [p for p in listing if p.endswith((".ts", ".js", ".mjs", ".cjs")) and "/node_modules/" not in f"/{p}"
              and not re.match(r"(frontend|client|web)/", p) and not re.search(r"(^|/)(dist|build)/", p)]
    if not any(_ENVELOPE_SERVER.search(files.read(p) or "") for p in server):
        return notes, writes
    for rel in listing:
        if not re.match(r"(frontend|client|web)/src/.*(api|client|service|http|request)[\w/.-]*\.(ts|tsx|js|jsx)$", rel, re.I) \
                or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        if "recoderUnwrap" in text:
            continue
        ts = rel.endswith((".ts", ".tsx"))
        new = re.sub(r"(const\s+([\w$]+)\s*=\s*await\s+[\w$]+\.json\(\)\s*;\s*return\s+)\2\s*;",
                     lambda m: f"{m.group(1)}recoderUnwrap({m.group(2)});", text)
        #: await 가 없던 곳(async 가 아닌 함수·then 콜백)에 await 를 넣지 않는다 — 약속에 이어 붙인다.
        new = re.sub(r"\breturn\s+await\s+([\w$]+)\.json\(\)\s*;", lambda m: f"return recoderUnwrap(await {m.group(1)}.json());", new)
        new = re.sub(r"\breturn\s+([\w$]+)\.json\(\)\s*;", lambda m: f"return {m.group(1)}.json().then(recoderUnwrap);", new)
        if new == text:
            continue
        helper = _UNWRAP_HELPER if ts else re.sub(r"<T>|: T\b|\s*as unknown as T|\s*as unknown as Record<string, unknown>|\s*as Record<string, unknown>|\s*as unknown\[\]", "", _UNWRAP_HELPER)
        at = imports_end(new)
        writes[rel] = new[:at] + helper + new[at:]
        notes.append(f"{rel}: 서버 응답 봉투의 data 값을 바로 읽게")
    return notes, writes


def cart_persist(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 장바구니를 화면 메모리에만 두는 앱(서버 장바구니 API 가 없음).

    실기기 2.0.13 카페: 메뉴를 담은 뒤 새로고침하거나 주소로 장바구니를 열면 비어 있었다. 브라우저에 보관하고 복원한다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".tsx", ".jsx")) or "/node_modules/" in f"/{rel}" or not re.search(r"cart|basket", rel, re.I):
            continue
        text = files.read(rel) or ""
        if "createContext" not in text or "localStorage" in text or re.search(r"""['"`]/(?:api/)?cart""", text):
            continue
        m = re.search(r"const\s*\[\s*([\w$]+)\s*,\s*(set[\w$]+)\s*\]\s*=\s*useState(<[^>]+>)?\(\s*\[\s*\]\s*\)\s*;", text)
        if not m:
            continue
        name, typ = m.group(1), m.group(3) or ""
        cast = f" as {typ[1:-1]}" if typ else ""
        block = (f"const [{name}, {m.group(2)}] = useState{typ}(() => {{\n"
                 "    //: ReCoder: 장바구니를 브라우저에 보관해 새로고침·주소 이동 뒤에도 그대로 둔다.\n"
                 "    try {\n      const saved = localStorage.getItem('recoder_cart');\n"
                 f"      const parsed = saved ? JSON.parse(saved) : [];\n      return (Array.isArray(parsed) ? parsed : []){cast};\n"
                 "    } catch {\n      return [];\n    }\n  });\n"
                 "  useEffect(() => {\n    try {\n"
                 f"      localStorage.setItem('recoder_cart', JSON.stringify({name}));\n"
                 "    } catch {\n      /* 저장소를 못 써도 장바구니는 동작 */\n    }\n"
                 f"  }}, [{name}]);")
        new = text[:m.start()] + block + text[m.end():]
        new = add_react_named(new, "useEffect")
        writes[rel] = new
        notes.append(f"{rel}: 장바구니를 새로고침 뒤에도 유지")
    return notes, writes


_MOCK_MODULE = re.compile(r"(?:^|/)(?:mock[-_]?(?:payment|stripe|pay)|(?:payment|stripe)[-_]?mock|fake[-_]?(?:payment|stripe))[\w-]*\.(?:ts|js|mjs)$", re.I)


def drop_ai_mock_server_imports(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 서버가 AI 가 따로 만든 모의 결제 서버 파일(mockPaymentServer.ts 등)을 불러와 띄우는 코드.

    결제 약속상 모의 결제 서버는 배포가 함께 띄운다(로컬·ECS). AI 가 만든 그 파일은 내보내는 것이 없어서 서버가
    `import { startMockPaymentServer } …` 에서 시작하자마자 SyntaxError 로 죽었다(실기기 0d40). 그 import 와 호출 문장을
    뺀다. 호출이 문장으로 떼어 낼 수 없는 곳(대입·조건식 안)에 있으면 건드리지 않는다(빼면 정의되지 않은 이름이 남는다)."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".js", ".mjs")) or "/node_modules/" in f"/{rel}" or re.search(r"(^|/)(dist|build)/", rel) \
                or re.match(r"(frontend|client|web)/", rel) or _MOCK_MODULE.search(rel):
            continue
        text = files.read(rel) or ""
        new, ok, touched = text, True, False
        for m in list(re.finditer(r"""(?m)^[ \t]*import\s*\{([^}]*)\}\s*from\s*['"](\.{1,2}/[^'"]+)['"];?[ \t]*\r?\n""", text)):
            target = re.sub(r"\.js$", "", posixpath.normpath(posixpath.join(posixpath.dirname(rel), m.group(2))))
            found = next((target + ext for ext in (".ts", ".js", ".mjs") if files.exists(target + ext)), None)
            if not found or not _MOCK_MODULE.search(found):
                continue
            body = files.read(found) or ""
            names = [n.strip().split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()]
            if any(re.search(rf"\bexport\b[^\n]*\b{re.escape(n)}\b", body) for n in names):
                continue  # 실제로 내보내는 모듈이면 그대로 쓴다
            candidate = new.replace(m.group(0), "", 1)
            for n in names:
                while True:
                    call = re.search(rf"(?m)^([ \t]*)(?:await\s+|void\s+)?{re.escape(n)}\s*\(", candidate)
                    if not call:
                        break
                    end = stmt_end(candidate, call.start() + len(call.group(1)))
                    if end < 0:
                        ok = False
                        break
                    #: 빈 문장(;)으로 남긴다 — 중괄호 없는 if 의 몸통이었으면 다음 문장을 빨아들이지 않게.
                    candidate = (candidate[:call.start()] + call.group(1)
                                 + ";// ReCoder: 모의 결제 서버는 배포가 함께 띄웁니다(로컬·ECS) — 앱 안에서 따로 띄우지 않습니다."
                                 + candidate[end:])
                if re.search(rf"(?<![\w$.]){re.escape(n)}(?![\w$])", re.sub(r"//[^\n]*", "", candidate)):
                    ok = False  # 문장이 아닌 곳에 남았다
            if ok:
                new, touched = candidate, True
        if touched and ok and new != text:
            writes[rel] = new
            notes.append(f"{rel}: AI 가 만든 모의 결제 서버를 앱 안에서 띄우지 않게")
    return notes, writes


# ── 공용: 문장 끝 · React import · import 블록 끝 ─────────────────────────────

_CONT_END = re.compile(r"(?:[=+\-*/%&|^!?:,(\[{<>]|\b(?:return|typeof|await|new|in|of|instanceof))\s*$")
_CONT_START = re.compile(r"^\s*(?:[.?:+\-*/%&|^,)\]}]|=>|\bas\b|\bsatisfies\b)")


def stmt_end(text: str, start: int) -> int:
    """start 에서 시작한 JS/TS 문장이 끝나는 위치(그 뒤 첫 글자). 괄호·문자열·템플릿·주석을 건너뛰고,
    `;` 또는 (세미콜론 없는 코드) 이어지지 않는 줄바꿈에서 끝난다."""
    depth, i, n = 0, start, len(text)
    stack: list[str] = []
    while i < n:
        ch = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith("${", j):
                    k = stmt_end_brace(text, j + 1)
                    j = k
                    continue
                j += 1
            i = j + 1
            continue
        if ch == "/" and _regex_context(text, i):
            #: 정규식 리터럴(/[{]/g) 안의 괄호는 세지 않는다.
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if stack:
                stack.pop()
            elif ch == "}":
                return i  # 바깥 블록이 닫힘 — 문장도 여기서 끝
            else:
                return -1  # 식 안(화살표 함수 몸통 등)이다 — 문장으로 다룰 수 없다
        elif ch == ";" and not stack:
            return i + 1
        elif ch == "\n" and not stack:
            head = text[start:i].rstrip()
            rest = text[i + 1:]
            nxt = rest.lstrip("\r\n\t ")
            if head and not _CONT_END.search(head) and not _CONT_START.match(nxt):
                return i
        i += 1
    return n


def _regex_context(text: str, i: int) -> bool:
    """i 의 `/` 가 나눗셈이 아니라 정규식 리터럴의 시작인가(앞의 의미 있는 글자로 판단)."""
    if text.startswith("//", i) or text.startswith("/*", i):
        return False
    k = i - 1
    while k >= 0 and text[k] in " \t":
        k -= 1
    if k < 0 or text[k] in "(,=:!&|?{};[\n":
        return True
    return bool(re.search(r"\b(?:return|typeof|case|in|of)\s*$", text[max(0, i - 12):i]))


def stmt_end_brace(text: str, open_at: int) -> int:
    """`{` 위치에서 짝 맞는 `}` 다음 위치."""
    depth, i, n = 0, open_at, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def add_react_named(text: str, name: str) -> str:
    """react 의 이름 가져오기에 name 을 더한다(이미 있으면 그대로, 여러 줄·끝 쉼표 모양도 깨지지 않게)."""
    m = re.search(r"""import\s+(?:React\s*,\s*)?\{([^}]*)\}\s*from\s*['"]react['"]""", text)
    if m:
        if re.search(rf"\b{re.escape(name)}\b", m.group(1)):
            return text
        inner = m.group(1)
        if "\n" in inner:
            body = inner.rstrip()
            trailing = body.endswith(",")
            body = body.rstrip(",")
            ind = re.search(r"\n([ \t]*)\S", inner)
            ind = ind.group(1) if ind else "  "
            new_inner = f"{body},\n{ind}{name}" + ("," if trailing else "") + "\n"
        else:
            body = inner.strip().rstrip(",").strip()
            new_inner = f" {body}, {name} " if body else f" {name} "
        return text[:m.start(1)] + new_inner + text[m.end(1):]
    d = re.search(r"""import\s+React\s+from\s*(['"])react\1""", text)
    if d:
        return text[:d.start()] + f"import React, {{ {name} }} from {d.group(1)}react{d.group(1)}" + text[d.end():]
    return f"import {{ {name} }} from 'react';\n" + text


def imports_end(text: str) -> int:
    """맨 위 import 문들이 끝나는 위치(여러 줄 import·세미콜론 없는 코드 포함). import 가 없으면 0."""
    end, pos = 0, 0
    for m in re.finditer(r"(?m)^[ \t]*import\b", text):
        if m.start() < end:
            continue
        stop = re.compile(r"""(?:\bfrom\s*['"][^'"]+['"]|^[ \t]*import\s*['"][^'"]+['"])[ \t]*;?""", re.M)
        found = stop.search(text, m.start())
        if not found:
            break
        e = found.end()
        nl = text.find("\n", e)
        end = len(text) if nl < 0 else nl + 1
    return end


_CREATE_CTX = re.compile(r"(?m)^\s*(?:export\s+)?const\s+([A-Z][\w$]*)\s*(?::[^=\n]+)?=\s*(?:React\.)?createContext\s*(?:<\s*([A-Za-z_$][\w$]*)[^>]*>)?\s*\(")
_IDENT = r"[A-Za-z_$][\w$]*"


def _top_level_parts(body: str) -> Optional[list[str]]:
    """객체 리터럴 본문을 최상위 쉼표로 나눈다(문자열·괄호 안 쉼표·주석은 무시). 판단할 수 없으면 None."""
    parts, depth, cur, i, n = [], 0, [], 0, len(body)
    while i < n:
        c = body[i]
        if c in "'\"`":
            j = i + 1
            while j < n and body[j] != c:
                j += 2 if body[j] == "\\" else 1
            cur.append(body[i:j + 1])
            i = j + 1
            continue
        if body.startswith("//", i):
            j = body.find("\n", i)
            i = n if j < 0 else j
            continue
        if body.startswith("/*", i):
            j = body.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return None
        if c == "," and depth == 0:
            part = "".join(cur).strip()
            if not part:
                return None  # 빈 항목(,,) — 문법이 깨진 객체
            parts.append(part)
            cur = []
        else:
            cur.append(c)
        i += 1
    if depth:
        return None
    last = "".join(cur).strip()
    if last:
        parts.append(last)
    return parts


def _object_entries(body: str) -> Optional[dict[str, bool]]:
    """{키: 단축형인가}. 펼침(...)·계산된 키처럼 판단할 수 없으면 None."""
    parts = _top_level_parts(body)
    if parts is None:
        return None
    out: dict[str, bool] = {}
    for part in parts:
        if part.startswith("..."):
            return None  # 펼쳐 넣은 값이 있으면 무엇이 들어 있는지 모른다
        m = re.match(rf"(?:async\s+)?(?:get\s+|set\s+)?\[?['\"]?({_IDENT})['\"]?\]?\s*(?:[:(,]|$)", part)
        if not m:
            return None
        out[m.group(1)] = bool(re.fullmatch(_IDENT, part))
    return out


def _object_keys(body: str) -> Optional[set[str]]:
    entries = _object_entries(body)
    return None if entries is None else set(entries)


def _fn_bodies(masked: str) -> list[tuple[int, int]]:
    return [(o, c) for o, c in _brace_pairs(masked) if _is_function_body(masked, o)]


def _enclosing_fn(bodies: list[tuple[int, int]], pos: int) -> Optional[tuple[int, int]]:
    inside = [(o, c) for o, c in bodies if o < pos < c]
    return max(inside, key=lambda oc: oc[0]) if inside else None


def _depths(masked: str, start: int, end: int) -> list[int]:
    """masked[start:end] 각 글자의 괄호 깊이(start 기준 0)."""
    out, d = [], 0
    for k in range(start, end):
        c = masked[k]
        if c in ")]}":
            d -= 1
        out.append(d)
        if c in "([{":
            d += 1
    return out


def _top_decls(masked: str, body: tuple[int, int]) -> set[str]:
    """함수 본문 최상위(중첩 블록·콜백 밖)에서 선언한 이름들."""
    o, c = body
    depth = _depths(masked, o + 1, c)
    names: set[str] = set()
    seg = masked[o + 1:c]
    for m in re.finditer(rf"\b(?:const|let|var)\s+(?:\[([^\]]*)\]|\{{([^{{}}]*)\}}|({_IDENT}))|\bfunction\s+({_IDENT})", seg):
        if depth[m.start()] != 0:
            continue
        if m.group(1) is not None:
            names |= set(re.findall(rf"(?:^|,)\s*({_IDENT})", m.group(1)))
        elif m.group(2) is not None:
            for part in m.group(2).split(","):
                bits = part.split(":")
                name = re.match(rf"\s*({_IDENT})", bits[-1])
                if name and not part.strip().startswith("..."):
                    names.add(name.group(1))
        else:
            names.add(m.group(3) or m.group(4))
    return names


def _provider_value(text: str, ctx: str):
    """Provider 의 value 객체 본문 (시작, 끝), useMemo 의존성 배열 (시작, 끝), 감싼 함수 본문 (열기, 닫기)."""
    m = re.search(rf"<\s*{re.escape(ctx)}\.Provider\s+value\s*=\s*\{{", text)
    if not m:
        return None
    masked = _mask_code(text)
    fn = _enclosing_fn(_fn_bodies(masked), m.start())
    if not fn:
        return None
    rest = text[m.end():]
    inline = re.match(r"\s*\{", rest)
    if inline:
        open_at = m.end() + inline.end() - 1
        close = _match_brace(text, open_at)
        return (open_at + 1, close, None, fn) if close > 0 else None
    ident = re.match(rf"\s*({_IDENT})\s*\}}", rest)
    if not ident:
        return None
    var = ident.group(1)
    #: 같은 함수(Provider 컴포넌트) 최상위의, JSX 에서 가장 가까운 선언만 — 다른 Provider·핸들러의 `const value` 를 잡지 않게.
    depth = _depths(masked, fn[0] + 1, fn[1])
    decl = None
    pat = re.compile(rf"\bconst\s+{re.escape(var)}\s*(?::[^=]+)?=\s*(?:(?:React\.)?useMemo\s*\(\s*\(\s*\)\s*=>\s*\(\s*)?\{{")
    for d in pat.finditer(masked, fn[0] + 1, m.start()):
        if depth[d.start() - fn[0] - 1] == 0:
            decl = d
    if decl is None:
        return None
    open_at = decl.end() - 1
    close = _match_brace(text, open_at)
    if close < 0:
        return None
    deps = None
    if "useMemo" in decl.group(0):
        tail = re.match(r"\s*\)\s*,\s*\[([^\]]*)\]", text[close + 1:])
        if tail:
            deps = (close + 1 + tail.start(1), close + 1 + tail.end(1))
    return open_at + 1, close, deps, fn


def _hook_returns(text: str) -> list[tuple[str, int, int, Optional[str], tuple[int, int]]]:
    """(훅 이름, return 객체 본문 시작, 끝, 반환 타입 이름, 훅 본문) — 훅 본문 최상위의 **마지막** return 이 객체일 때만
    (앞의 `if (!ctx) return {…}` 같은 대비용 return 은 훅이 주는 값이 아니다)."""
    out = []
    masked = _mask_code(text)
    for m in re.finditer(rf"\bexport\s+(?:default\s+)?function\s+(use[A-Z][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*({_IDENT})[^{{]*)?\{{|"
                         rf"\bexport\s+const\s+(use[A-Z][\w$]*)\s*=\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*({_IDENT})[^=]*)?=>\s*\{{", masked):
        name = m.group(1) or m.group(3)
        type_name = m.group(2) or m.group(4)
        open_at = m.end() - 1
        close = _match_brace(text, open_at)
        if close < 0:
            continue
        depth = _depths(masked, open_at + 1, close)
        tops = [r for r in re.finditer(r"\breturn\b", masked[open_at + 1:close]) if depth[r.start()] == 0]
        if not tops:
            continue
        last = re.match(r"return\s*\{", masked[open_at + 1 + tops[-1].start():close])
        if not last:
            continue
        at = open_at + 1 + tops[-1].start() + last.end() - 1
        end = _match_brace(text, at)
        if end > 0:
            out.append((name, at + 1, end, type_name, (open_at, close)))
    return out


def _span_of(prov: dict, text: str):
    """(시작, 끝, 의존성 배열, 감싼 함수 본문)."""
    if prov["kind"] == "context":
        return _provider_value(text, prov["name"])
    for name, start, end, _t, body in _hook_returns(text):
        if name == prov["name"]:
            return start, end, None, body
    return None


def _code_files(files) -> list[str]:
    return [rel for rel in files.files() if rel.endswith((".tsx", ".jsx", ".ts", ".js")) and "/node_modules/" not in f"/{rel}"
            and not re.search(r"(^|/)(dist|build)/", rel) and not rel.endswith(".d.ts")]


def _providers(files) -> tuple[list[dict], list[str]]:
    """값을 주는 쪽(Context Provider 의 value · 훅의 return 객체) 목록과 코드 파일들."""
    code = _code_files(files)
    found: dict[str, list[dict]] = {}
    for rel in code:
        text = files.read(rel) or ""
        if "createContext" in text:
            for m in _CREATE_CTX.finditer(text):
                found.setdefault(m.group(1), []).append({"kind": "context", "name": m.group(1), "rel": rel, "type": m.group(2)})
        for name in set(re.findall(r"\bexport\s+(?:default\s+)?(?:function\s+|const\s+)(use[A-Z][\w$]*)", text)):
            found.setdefault(name, []).append({"kind": "hook", "name": name, "rel": rel, "type": None})
    provs = []
    for items in found.values():
        if len(items) != 1:
            continue  # 같은 이름이 여러 파일에 있으면 어느 것인지 이름만으로 정하지 않는다
        prov = items[0]
        if prov["kind"] == "hook":
            text = files.read(prov["rel"]) or ""
            hit = next((h for h in _hook_returns(text) if h[0] == prov["name"]), None)
            if not hit:
                continue
            prov["type"] = hit[3]
        provs.append(prov)
    return provs, code


def _imports_from(files, rel: str, text: str, name: str, target: str) -> bool:
    """rel 이 name 을 target 파일에서 가져오는가(같은 파일이면 참)."""
    if rel == target:
        return True
    for m in re.finditer(r"""\bimport\s+([^;]*?)\s+from\s*['"]([^'"]+)['"]""", text):
        clause, spec = m.group(1), m.group(2)
        if not re.search(rf"(?:^|[\s{{,]){re.escape(name)}(?:[\s}},]|$)", clause):
            continue
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        elif spec[:2] in ("@/", "~/"):
            base = None
            tail = spec[2:]
        else:
            continue
        stem = re.sub(r"\.(?:m?js|jsx|tsx?)$", "", base if base is not None else tail)
        tgt = re.sub(r"\.(?:m?js|jsx|tsx?)$", "", target)
        if base is not None and (tgt == stem or tgt == stem + "/index"):
            return True
        if base is None and (tgt.endswith("/" + stem) or tgt.endswith("/" + stem + "/index")):
            return True
    return False


def _consumed_names(text: str, ctx: str) -> Optional[set[str]]:
    """useContext(Ctx) 에서 구조 분해로 꺼내는 이름들(판단 불가면 None). 담아 둔 변수는 같은 함수 안에서만 본다."""
    masked = _mask_code(text)
    bodies = _fn_bodies(masked)
    names: set[str] = set()
    sources: list[str] = []
    for m in re.finditer(rf"\b(?:const|let)\s*\{{([^{{}}]*)\}}\s*(?::[^=]+)?=\s*(?:React\.)?useContext\s*\(\s*{re.escape(ctx)}\s*\)", masked):
        sources.append(text[m.start(1):m.end(1)])
    for h in re.finditer(rf"\b(?:const|let)\s+({_IDENT})\s*(?::[^=]+)?=\s*(?:React\.)?useContext\s*\(\s*{re.escape(ctx)}\s*\)", masked):
        scope = _enclosing_fn(bodies, h.start())
        for d in re.finditer(rf"\b(?:const|let)\s*\{{([^{{}}]*)\}}\s*=\s*{re.escape(h.group(1))}\s*[;\n]", masked):
            if _enclosing_fn(bodies, d.start()) == scope:
                sources.append(text[d.start(1):d.end(1)])
    for group in sources:
        for part in group.split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith("...") or "{" in part:
                return None
            name = re.match(rf"({_IDENT})", part)
            if not name:
                return None
            names.add(name.group(1))
    return names


def _hook_consumed(text: str, hook: str) -> Optional[set[str]]:
    names: set[str] = set()
    masked = _mask_code(text)
    for m in re.finditer(rf"\b(?:const|let)\s*\{{([^{{}}]*)\}}\s*(?::[^=]+)?=\s*{re.escape(hook)}\s*\(", masked):
        for part in text[m.start(1):m.end(1)].split(","):
            part = part.strip()
            if not part:
                continue
            if part.startswith("...") or "{" in part:
                return None
            n = re.match(rf"({_IDENT})", part)
            if not n:
                return None
            names.add(n.group(1))
    return names


def _context_gaps(files) -> list[dict]:
    providers, code = _providers(files)
    gaps = []
    for prov in providers:
        ptext = files.read(prov["rel"]) or ""
        span = _span_of(prov, ptext)
        if not span:
            continue
        entries = _object_entries(ptext[span[0]:span[1]])
        if entries is None:
            continue
        pmasked = _mask_code(ptext)
        scope = _top_decls(pmasked, span[3])
        for rel in code:
            text = files.read(rel) or ""
            if not re.search(rf"\b{re.escape(prov['name'])}\b", text):
                continue
            if prov["kind"] == "context":
                used = _consumed_names(text, prov["name"]) if "useContext" in text else set()
            else:
                used = _hook_consumed(text, prov["name"])
            if not used or not _imports_from(files, rel, text, prov["name"], prov["rel"]):
                continue
            missing = sorted(used - set(entries))
            if missing:
                gaps.append({"rel": rel, "ctx": prov["name"], "ctx_rel": prov["rel"], "type": prov["type"],
                             "kind": prov["kind"], "missing": missing, "entries": entries, "scope": scope})
    return gaps


#: 나눠 만든 파일끼리 가장 흔한 이름 어긋남(장바구니·로그인). 쓰는 쪽 이름 → 주는 쪽에서 찾을 이름(앞이 우선).
_ALIASES: dict[str, tuple[str, ...]] = {
    "total": ("totalPrice", "totalAmount", "cartTotal", "subtotal"),
    "totalPrice": ("total", "totalAmount", "cartTotal", "subtotal"),
    "totalAmount": ("totalPrice", "total", "cartTotal", "subtotal"),
    "cartTotal": ("totalPrice", "total", "totalAmount"),
    "subtotal": ("totalPrice", "total", "totalAmount"),
    "totalItems": ("itemCount", "cartCount", "count", "totalQuantity"),
    "itemCount": ("totalItems", "cartCount", "count", "totalQuantity"),
    "cartCount": ("totalItems", "itemCount", "count", "totalQuantity"),
    "totalQuantity": ("totalItems", "itemCount", "cartCount"),
    "items": ("cartItems", "cart"),
    "cartItems": ("items", "cart"),
    "cart": ("items", "cartItems"),
    "addToCart": ("addItem", "addCartItem"),
    "addItem": ("addToCart", "addCartItem"),
    "removeFromCart": ("removeItem", "removeCartItem"),
    "removeItem": ("removeFromCart", "removeCartItem"),
    "updateQuantity": ("updateItemQuantity", "updateCartItem", "updateCartQuantity"),
    "updateItemQuantity": ("updateQuantity", "updateCartItem"),
    "clearCart": ("clear", "resetCart", "emptyCart"),
    "loading": ("isLoading",),
    "isLoading": ("loading",),
    "user": ("currentUser",),
    "currentUser": ("user",),
    "logout": ("signOut",),
    "signOut": ("logout",),
    "login": ("signIn",),
    "signIn": ("login",),
    "register": ("signup", "signUp"),
    "signup": ("register", "signUp"),
    "signUp": ("register", "signup"),
}
#: 값으로 쓰는 이름인데 주는 쪽에는 계산 함수만 있다 → 호출 결과를 넣는다.
_CALL_ALIASES: dict[str, tuple[str, ...]] = {
    "total": ("getTotalPrice", "getTotal", "getCartTotal", "getTotalAmount"),
    "totalPrice": ("getTotalPrice", "getTotal", "getCartTotal", "getTotalAmount"),
    "totalAmount": ("getTotalAmount", "getTotalPrice", "getTotal"),
    "cartTotal": ("getCartTotal", "getTotalPrice", "getTotal"),
    "subtotal": ("getSubtotal", "getTotalPrice", "getTotal"),
    "totalItems": ("getTotalItems", "getItemCount", "getCartCount"),
    "itemCount": ("getItemCount", "getTotalItems", "getCartCount"),
    "cartCount": ("getCartCount", "getTotalItems", "getItemCount"),
}


def _resolve_gap(gap: dict, name: str) -> Optional[str]:
    """빠진 이름을 채울 식(이름 그대로면 단축형). 못 채우면 None.
    식에 쓰는 이름은 주는 쪽 함수 최상위에 선언됐거나 객체에 단축형으로 들어 있는(= 그 자리에서 보이는) 변수만 —
    `totalPrice: items.reduce(…)` 같은 키·getter·메서드는 변수가 아니라서 쓰면 ReferenceError 다."""
    entries, scope = gap["entries"], gap["scope"]

    def visible(ident: str) -> bool:
        return ident in scope or entries.get(ident) is True

    if name in scope:
        return name
    for alias in _ALIASES.get(name, ()):
        if visible(alias):
            return alias
    for fn in _CALL_ALIASES.get(name, ()):
        if visible(fn):
            return f"{fn}()"
    if name == "isAuthenticated":
        for who in ("user", "currentUser", "token"):
            if visible(who):
                return f"!!{who}"
    return None


def context_value_gaps(files) -> list[tuple[str, str]]:
    """[(설명, 쓰는 파일)] — Provider·훅이 주지 않는 값을 꺼내 쓰는 곳(자동으로 고칠 수 없는 것만)."""
    out = []
    for gap in _context_gaps(files):
        bad = [n for n in gap["missing"] if _resolve_gap(gap, n) is None]
        if bad:
            out.append((f"{', '.join(bad)}({gap['rel']} ← {gap['ctx_rel']})", gap["rel"]))
    return out


def _append_entries(body: str, parts: list[str]) -> Optional[str]:
    """객체 본문 끝에 항목을 더한다 — 끝 주석·끝 쉼표가 있어도 문법이 깨지지 않게. 결과를 다시 읽어 확인한다."""
    masked = _mask_code(body)
    last = len(masked.rstrip()) - 1
    if last >= 0 and masked[last] != ",":
        body = body[:last + 1] + "," + body[last + 1:]
    stripped = body.rstrip()
    if "\n" in body:
        ind = re.search(r"\n([ \t]+)\S", body)
        ind = ind.group(1) if ind else "  "
        new_body = stripped + "".join(f"\n{ind}{p}," for p in parts) + body[len(stripped):]
    else:
        new_body = (stripped + " " if stripped.strip() else " ") + ", ".join(parts) + " "
    keys = _object_keys(new_body)
    if keys is None or not all(re.match(_IDENT, p).group(0) in keys for p in parts):
        return None
    return new_body


def context_value_missing(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 훅·화면이 Context·훅에서 꺼내는 값을 주는 쪽이 넣지 않았는데, 같은 값이 주는 쪽에
    이미 있다(useState 의 setItems, 다른 이름의 totalPrice·addItem 등). 주는 쪽 객체에 그 이름으로 넣어 준다.

    실기기 c395 카페: hooks/useCart.ts 가 `const { items, setItems } = useContext(CartContext)` 인데 CartProvider 의 value 에
    setItems 가 없어 [장바구니에 추가] 를 누르면 "a is not a function" 으로 멈췄고, 장바구니 화면은 useCart() 의 `total`
    (훅은 totalPrice 를 준다)을 읽어 합계가 ₩0 이었다. tsc 는 TS2339 로 알려 주지만 배포 빌드는 타입 오류만으로 멈추지 않는다."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    plan: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for gap in _context_gaps(files):
        resolved = {n: _resolve_gap(gap, n) for n in gap["missing"]}
        if any(v is None for v in resolved.values()):
            continue  # 하나라도 못 채우면 그 쓰는 쪽은 손대지 않는다(경고로 남는다)
        plan.setdefault((gap["ctx_rel"], gap["kind"], gap["ctx"], gap["type"] or ""), {}).update(resolved)
    for (ctx_rel, kind, name, type_name), entries in sorted(plan.items()):
        text = writes.get(ctx_rel, files.read(ctx_rel) or "")
        span = _span_of({"kind": kind, "name": name}, text)
        if not span:
            continue
        start, end, deps, _fn = span
        keys = _object_keys(text[start:end]) or set()
        todo = [(n, e) for n, e in sorted(entries.items()) if n not in keys]
        if not todo:
            continue
        parts = [n if e == n else f"{n}: {e}" for n, e in todo]
        new_body = _append_entries(text[start:end], parts)
        if new_body is None:
            continue
        new_deps = None
        if deps:
            dep_text = text[deps[0]:deps[1]]
            have = {d.strip() for d in dep_text.split(",") if d.strip()}
            used_ids = []
            for _n, e in todo:
                ident = re.match(rf"!*({_IDENT})", e)
                if ident and ident.group(1) not in have and ident.group(1) not in used_ids:
                    used_ids.append(ident.group(1))
            if used_ids:
                d_core = dep_text.rstrip().rstrip(",")
                new_deps = d_core + (", " if d_core.strip() else "") + ", ".join(used_ids)
        if new_deps is not None:
            text = text[:deps[0]] + new_deps + text[deps[1]:]
        text = text[:start] + new_body + text[end:]
        if type_name:
            t = re.search(rf"\b(?:interface\s+{re.escape(type_name)}\b[^{{]*|type\s+{re.escape(type_name)}\s*=\s*)\{{", text)
            if t:
                close = _match_brace(text, t.end() - 1)
                if close > 0:
                    inner = text[t.end():close]
                    missing_t = [n for n, _e in todo if not re.search(rf"(?m)^\s*(?:readonly\s+)?{re.escape(n)}\??\s*[:(]", inner)]
                    if missing_t:
                        tind = re.search(r"\n([ \t]+)\S", inner)
                        tind = tind.group(1) if tind else "  "
                        lines = "".join(f"{tind}{n}: any; // ReCoder: 쓰는 쪽이 찾는 이름\n" for n in missing_t)
                        pre = inner.rstrip(" \t")
                        if not pre.endswith("\n"):
                            pre += "\n"
                        text = text[:t.end()] + pre + lines + text[close:]
        writes[ctx_rel] = text
        notes.append(f"{ctx_rel}: {name} 에 {', '.join(parts)} 추가")
    return notes, writes


_OPTIONAL_METHOD = re.compile(r"(?m)^([ \t]*(?:async[ \t]+)?\*?[ \t]*)([A-Za-z_$][\w$]*)\?([ \t]*(?:<[^>\n]*>)?[ \t]*\()")


def optional_object_methods(files) -> tuple[list[str], dict[str, str]]:
    """([고친 것], {파일: 내용}) — 객체 리터럴의 메서드에 인터페이스처럼 `?` 를 붙였다(`async onPaymentFailed?(event) {`).
    tsc 가 TS1162(문법 오류)로 실패해 배포 빌드가 멈춘다(실기기 2.0.16 쇼핑몰 cdf7). 클래스·인터페이스·타입 안은 그대로."""
    notes: list[str] = []
    writes: dict[str, str] = {}
    for rel in files.files():
        if not rel.endswith((".ts", ".tsx", ".mts")) or rel.endswith(".d.ts") or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel) or ""
        if "?(" not in text and "? (" not in text and "?<" not in text:
            continue
        masked = _mask_code(text)
        pairs = _brace_pairs(masked)
        cuts = []
        for m in _OPTIONAL_METHOD.finditer(masked):
            #: 본문이 있는 메서드만(`) {` / `): T {`) — 인터페이스·타입의 선언(`;`)은 아니다
            close = masked.find(")", m.end())
            after = masked[close + 1:close + 200] if close > 0 else ""
            if not re.match(r"[ \t]*(?::[ \t]*[^;{=\n]+?)?[ \t]*\{", after):
                continue
            q = m.end(2)
            inside = [(o, c) for o, c in pairs if o < q < c]
            if not inside:
                continue
            o = max(inside)[0]
            head = masked[max(0, o - 300):o]
            if re.search(r"\b(?:class|interface)\b[^{};]*$", head) or re.search(r"\btype\s+[\w$<>, ]+=\s*$", head):
                continue
            cuts.append(q)
        if cuts:
            new = text
            for q in sorted(cuts, reverse=True):
                new = new[:q] + new[q + 1:]
            writes[rel] = new
            notes.append(f"{rel}: 객체 메서드의 ? {len(cuts)}곳")
    return notes, writes
