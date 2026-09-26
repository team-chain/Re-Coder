"""배포 전 "이 프로젝트가 컨테이너에서 실제로 빌드·실행될까" 정적 판정.

왜 필요한가
    생성된 Dockerfile 은 프로젝트 설정(package.json·requirements)을 그대로
    따른다. 설정이 코드와 어긋나 있으면(예: ``build`` 가 ``react-scripts build``
    인데 ``src/index.js`` 가 없음) Docker 는 몇 분 동안 의존성을 설치한 뒤
    마지막에 실패하고, 화면에는 Dockerfile 줄 번호만 남는다. 실기기에서 실제로
    그렇게 막혔다(test temp 게시판).

원칙
    - **파일만 읽는다.** 네트워크·Docker·LLM 을 부르지 않는다. 빠르고 결정적이다.
    - **확실한 것만 error 로 낸다.** 파일이 없음, 포트가 다름처럼 실패가 확정적인
      경우만 error 이고, 추정이 섞이면 warning 이다. 잘못된 차단은 사용자가 할 수
      있는 일을 없앤다.
    - 코드 생성 결과 검증에도 쓰도록 디스크 위에 **가상 파일(overlay)** 을 얹어
      볼 수 있다. overlay 의 ``None`` 은 삭제를 뜻한다.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

# ---------------------------------------------------------------------------
# 공용 타입
# ---------------------------------------------------------------------------

ERROR = "error"
WARNING = "warning"

#: 사용자가 버튼 한 번으로 적용할 수 있는(백업을 남기는) 수정 종류.
AUTO_FIXABLE = {"DOCKERIGNORE_MISSING", "DOCKERFILE_PORT_MISMATCH", "DOCKERFILE_HEALTH_PATH_UNKNOWN",
                "NODE_UNUSED_BUILD_SCRIPT", "DOCKERFILE_WORKDIR_NOT_WRITABLE"}


@dataclass(frozen=True)
class ReadinessIssue:
    code: str
    severity: str
    message: str
    fix: str
    file: str = ""
    auto_fix: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Readiness:
    runtime: str = "unknown"              # node | python | static | unknown
    app_port: Optional[int] = None
    port_from_env: bool = False           # 앱이 PORT 환경변수를 읽는가
    health_path: Optional[str] = None     # 코드에서 확인한 헬스 라우트
    serves_root: bool = False             # "/" 가 200 을 줄 근거가 있는가
    docs_path: Optional[str] = None       # FastAPI 자동 문서처럼 항상 200 인 경로
    local_data_files: list[str] = field(default_factory=list)  # 작업 폴더에 만드는 SQLite 파일
    issues: list[ReadinessIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ReadinessIssue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[ReadinessIssue]:
        return [i for i in self.issues if i.severity == WARNING]

    def probe_path(self) -> Optional[str]:
        """컨테이너가 떴는지 HTTP 로 확인할 경로. 근거가 없으면 None."""
        if self.health_path:
            return self.health_path
        if self.serves_root:
            return "/"
        return self.docs_path

    def to_dict(self) -> dict:
        return {
            "runtime": self.runtime,
            "app_port": self.app_port,
            "port_from_env": self.port_from_env,
            "health_path": self.health_path,
            "probe_path": self.probe_path(),
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# 파일 보기 (디스크 + overlay)
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build",
    ".next", "out", "coverage", ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    ".recoder", ".turbo", ".cache", "site-packages",
}
_MAX_FILES = 2000
_MAX_BYTES = 512_000


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/").strip()
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    return "/".join(parts)


class ProjectFiles:
    """프로젝트 루트 기준 상대 경로로 파일을 읽는다. overlay 가 디스크보다 우선."""

    def __init__(self, root: Path, overlay: Optional[Mapping[str, Optional[str]]] = None):
        self.root = Path(root)
        self.overlay = {_norm(k): v for k, v in (overlay or {}).items() if _norm(k)}
        self._listing: Optional[list[str]] = None

    def exists(self, rel: str) -> bool:
        rel = _norm(rel)
        if rel in self.overlay:
            return self.overlay[rel] is not None
        try:
            return (self.root / rel).is_file()
        except OSError:
            return False

    def dir_has_files(self, rel: str) -> bool:
        prefix = _norm(rel) + "/"
        return any(p.startswith(prefix) for p in self.files())

    def read(self, rel: str) -> Optional[str]:
        rel = _norm(rel)
        if rel in self.overlay:
            return self.overlay[rel]
        path = self.root / rel
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                return None
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None

    def files(self) -> list[str]:
        if self._listing is None:
            found: list[str] = []
            if self.root.is_dir():
                for directory, dirs, names in os.walk(self.root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
                    for name in sorted(names):
                        rel = Path(directory, name).relative_to(self.root).as_posix()
                        found.append(rel)
                        if len(found) >= _MAX_FILES:
                            break
                    if len(found) >= _MAX_FILES:
                        break
            merged = set(found)
            for rel, content in self.overlay.items():
                if content is None:
                    merged.discard(rel)
                elif not any(part in _SKIP_DIRS for part in rel.split("/")[:-1]):
                    merged.add(rel)
            self._listing = sorted(merged)
        return self._listing

    def has_dir(self, rel: str) -> bool:
        rel = _norm(rel)
        if self.dir_has_files(rel):
            return True
        try:
            return (self.root / rel).is_dir()
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

_NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2",
    "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode",
    "querystring", "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib", "test",
}

#: 스크립트 명령 → (그 명령을 제공하는 패키지, 사람이 읽을 이름)
_NODE_TOOLS: dict[str, tuple[str, str]] = {
    "react-scripts": ("react-scripts", "Create React App"),
    "vite": ("vite", "Vite"),
    "next": ("next", "Next.js"),
    "tsc": ("typescript", "TypeScript"),
    "webpack": ("webpack", "webpack"),
    "nest": ("@nestjs/cli", "NestJS CLI"),
    "ng": ("@angular/cli", "Angular CLI"),
    "vue-cli-service": ("@vue/cli-service", "Vue CLI"),
    "parcel": ("parcel", "Parcel"),
    "esbuild": ("esbuild", "esbuild"),
    "rollup": ("rollup", "Rollup"),
    "babel": ("@babel/cli", "Babel CLI"),
    "ts-node": ("ts-node", "ts-node"),
    "tsx": ("tsx", "tsx"),
    "nodemon": ("nodemon", "nodemon"),
    "astro": ("astro", "Astro"),
    "nuxt": ("nuxt", "Nuxt"),
    "svelte-kit": ("@sveltejs/kit", "SvelteKit"),
    "remix": ("@remix-run/dev", "Remix"),
    "gatsby": ("gatsby", "Gatsby"),
}
_SCRIPT_SEPARATORS = re.compile(r"&&|\|\||;|\|")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_JS_SUFFIXES = (".js", ".cjs", ".mjs", ".jsx", ".ts", ".tsx", ".cts", ".mts")
_BROWSER_DIRS = ("public/", "static/", "assets/", "www/", "wwwroot/")


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _script_commands(script: str) -> list[list[str]]:
    """``a && b | c`` → [["a"...], ["b"...]] — 환경변수 대입·래퍼 제거."""
    out = []
    for segment in _SCRIPT_SEPARATORS.split(script or ""):
        words = segment.strip().split()
        while words and (_ENV_ASSIGN.match(words[0]) or words[0] in {"cross-env", "npx", "env", "dotenv", "--"}):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _package_of(specifier: str) -> Optional[str]:
    if not specifier or specifier.startswith((".", "/", "~", "#", "@/", "node:", "http:", "https:", "data:", "virtual:")):
        return None
    if specifier.startswith("@"):
        parts = specifier.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 and parts[1] else None
    return specifier.split("/")[0]


_IMPORT_PATTERNS = (
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\s+(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]"""),
    re.compile(r"""\bexport\s+[\w*{}\s,]+\s+from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
)


def _detect_js_port(text: str) -> tuple[Optional[int], bool]:
    """(포트, PORT 환경변수 사용 여부)."""
    active = _strip_js_comments(text)
    uses_env = bool(re.search(r"process\.env\.PORT\b|process\.env\[['\"]PORT['\"]\]", active))
    patterns = (
        r"process\.env\.PORT\s*(?:\|\||\?\?)\s*['\"]?(\d{2,5})\b",
        r"\.listen\(\s*(\d{2,5})\b",
        r"\b(?:const|let|var)\s+(?:PORT|port|APP_PORT|SERVER_PORT)\s*=\s*(?:Number\(|parseInt\()?\s*['\"]?(\d{2,5})\b",
        r"\bPORT\s*(?:\|\||\?\?)\s*['\"]?(\d{2,5})\b",
    )
    for pat in patterns:
        m = re.search(pat, active)
        if m:
            port = int(m.group(1))
            if 1 <= port <= 65535:
                return port, uses_env
    return None, uses_env


_HEALTH_NAMES = ("/health", "/healthz", "/api/health", "/status", "/ping", "/readyz", "/livez")

#: 작업 폴더 기준 상대 경로로 SQLite 파일을 여는 코드(컨테이너 안에 데이터가 생긴다).
_SQLITE_PATTERNS = (
    re.compile(r"""new\s+(?:sqlite3\.)?Database\(\s*['"](\.?/?[\w./-]+\.(?:db|sqlite3?))['"]"""),
    re.compile(r"""(?:sqlite3?|better-sqlite3)['"]\)\s*\(\s*['"](\.?/?[\w./-]+\.(?:db|sqlite3?))['"]"""),
    re.compile(r"""sqlite3\.connect\(\s*['"](\.?/?[\w./-]+\.(?:db|sqlite3?))['"]"""),
    re.compile(r"""sqlite(?:\+\w+)?:///(\.?/?[\w./-]+\.(?:db|sqlite3?))"""),
)


def _local_sqlite_files(text: str) -> list[str]:
    found = []
    for pattern in _SQLITE_PATTERNS:
        for path in pattern.findall(text):
            if not path.startswith("/"):
                found.append(path)
    return found


def _express_routes(text: str) -> tuple[set[str], set[str]]:
    """(GET 라우트, express.static 로 제공하는 폴더)."""
    active = _strip_js_comments(text)
    apps = set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:express|fastify|Fastify|koa|Koa)\(", active))
    apps |= set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+Koa\(", active))
    routes: set[str] = set()
    for app in apps or {"app"}:
        routes.update(re.findall(rf"\b{re.escape(app)}\.(?:get|all)\s*\(\s*['\"]([^'\"]+)['\"]", active))
    statics = set()
    for m in re.finditer(r"express\.static\(\s*(?:path\.(?:join|resolve)\(\s*__dirname\s*,\s*)?['\"]([^'\"]+)['\"]", active):
        statics.add(_norm(m.group(1)))
    return routes, statics


def _start_entry(scripts: dict) -> Optional[str]:
    for name in ("start", "start:prod", "serve"):
        for words in _script_commands(str(scripts.get(name, ""))):
            if words[0] in {"node", "nodemon"}:
                args = [w for w in words[1:] if not w.startswith("-")]
                if args:
                    return _norm(args[0].strip("'\""))
    return None


def _analyze_node(files: ProjectFiles, result: Readiness) -> None:
    raw = files.read("package.json")
    try:
        package = json.loads(raw or "")
        if not isinstance(package, dict):
            raise ValueError
    except ValueError:
        result.issues.append(ReadinessIssue(
            "NODE_PACKAGE_JSON_INVALID", ERROR,
            "package.json 을 JSON 으로 읽을 수 없습니다. 컨테이너 안의 npm 설치가 바로 실패합니다.",
            "package.json 의 문법 오류(쉼표·따옴표)를 고치세요.", "package.json"))
        return

    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(package.get(key), dict):
            deps.update(package[key])
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    workspaces = bool(package.get("workspaces"))

    # 1) 빌드 스크립트 — Docker 이미지가 `npm run build` 로 실제 실행한다.
    def check_script(name: str, depth: int = 0) -> None:
        script = str(scripts.get(name, ""))
        for words in _script_commands(script):
            tool = words[0]
            if tool in {"npm", "yarn", "pnpm"} and depth < 3:
                rest = [w for w in words[1:] if not w.startswith("-")]
                target = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else (rest[0] if rest and rest[0] != "run" else "")
                if target and target in scripts and target != name:
                    check_script(target, depth + 1)
                continue
            if tool not in _NODE_TOOLS:
                continue
            pkg, label = _NODE_TOOLS[tool]
            if pkg not in deps and not workspaces:
                result.issues.append(ReadinessIssue(
                    "NODE_BUILD_TOOL_MISSING", ERROR,
                    f"`{name}` 스크립트가 {label}(`{tool}`)를 실행하지만 package.json 의존성에 `{pkg}` 가 없습니다.",
                    f"실제로 쓰는 도구라면 `npm install --save-dev {pkg}`, 쓰지 않는다면 `{name}` 스크립트를 지우세요.",
                    "package.json"))
                continue
            if tool == "react-scripts" and len(words) > 1 and words[1] in {"build", "start"}:
                missing = []
                if not any(files.exists(f"src/index{ext}") for ext in (".js", ".jsx", ".ts", ".tsx")):
                    missing.append("src/index.js")
                if not files.exists("public/index.html"):
                    missing.append("public/index.html")
                if missing:
                    # 판정은 서버 구조(정적 폴더)를 읽은 뒤에 한다 — 아래 3) 참고.
                    cra_broken.append((name, words[1], missing))
            elif tool == "vite" and (len(words) == 1 or words[1] == "build"):
                has_config_root = any(
                    re.search(r"\broot\s*:", files.read(c) or "")
                    for c in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs")
                )
                if not has_config_root and not files.exists("index.html"):
                    result.issues.append(ReadinessIssue(
                        "NODE_BUILD_ENTRY_MISSING", ERROR,
                        f"`{name}` 스크립트가 Vite 빌드인데 프로젝트 루트에 index.html 이 없습니다.",
                        "Vite 진입 파일(index.html)을 루트에 두거나 vite.config 의 root 를 지정하세요.",
                        "package.json"))
            elif tool == "next" and len(words) > 1 and words[1] == "build":
                if not any(files.has_dir(d) for d in ("pages", "app", "src/pages", "src/app")):
                    result.issues.append(ReadinessIssue(
                        "NODE_BUILD_ENTRY_MISSING", ERROR,
                        f"`{name}` 스크립트가 `next build` 인데 pages/ 또는 app/ 폴더가 없습니다.",
                        "Next.js 페이지 폴더(app/ 또는 pages/)를 만들거나 build 스크립트를 고치세요.",
                        "package.json"))
            elif tool == "tsc":
                project = None
                for flag in ("-p", "--project"):
                    if flag in words[1:-1]:
                        project = words[words.index(flag) + 1]
                config = _norm(project) if project else "tsconfig.json"
                if config.endswith(".json") and not files.exists(config):
                    result.issues.append(ReadinessIssue(
                        "NODE_BUILD_ENTRY_MISSING", ERROR,
                        f"`{name}` 스크립트가 TypeScript 컴파일(`tsc`)인데 {config} 가 없습니다.",
                        f"{config} 를 추가하거나 build 스크립트를 고치세요.", "package.json"))

    cra_broken: list[tuple[str, str, list[str]]] = []
    if "build" in scripts:
        check_script("build")

    # 2) 시작 진입점 — 컨테이너 CMD 가 이 파일을 실행한다.
    entry = _start_entry(scripts)
    if entry and not files.exists(entry) and not re.match(r"^(dist|build|out|lib)/", entry):
        result.issues.append(ReadinessIssue(
            "NODE_START_ENTRY_MISSING", ERROR,
            f"`start` 스크립트가 실행하는 {entry} 파일이 없습니다. 컨테이너가 시작 직후 종료됩니다.",
            f"{entry} 를 만들거나 start 스크립트를 실제 서버 파일로 고치세요.", "package.json"))
    main = package.get("main")
    if not entry and isinstance(main, str) and main.endswith((".js", ".cjs", ".mjs")) and not files.exists(main) \
            and not re.match(r"^(\./)?(dist|build|out|lib)/", main):
        result.issues.append(ReadinessIssue(
            "NODE_START_ENTRY_MISSING", WARNING,
            f"package.json 의 main 이 가리키는 {main} 이 없습니다.",
            "main 을 실제 서버 파일로 고치거나 start 스크립트를 추가하세요.", "package.json"))

    # 3) 소스가 쓰는 패키지가 선언돼 있는가 + 포트·라우트
    entry_candidates = [e for e in (entry, _norm(main) if isinstance(main, str) else None,
                                    "server.js", "index.js", "app.js", "src/server.js", "src/index.js",
                                    "src/app.js", "src/main.ts", "src/index.ts", "server.ts", "index.ts") if e]
    undeclared: dict[str, str] = {}
    routes: set[str] = set()
    statics: set[str] = set()
    for rel in files.files():
        if not rel.endswith(_JS_SUFFIXES) or rel.startswith(_BROWSER_DIRS) or "/node_modules/" in rel:
            continue
        if re.search(r"(^|/)(tests?|__tests__|__mocks__)/|\.(test|spec)\.[cm]?[jt]sx?$", rel):
            continue
        text = files.read(rel)
        if text is None:
            continue
        active = _strip_js_comments(text)
        for pattern in _IMPORT_PATTERNS:
            for spec in pattern.findall(active):
                pkg = _package_of(spec)
                if pkg and pkg not in deps and pkg not in _NODE_BUILTINS and pkg != package.get("name"):
                    undeclared.setdefault(pkg, rel)
        r, s = _express_routes(text)
        routes |= r
        statics |= s
        result.local_data_files += [f for f in _local_sqlite_files(active) if f not in result.local_data_files]
    if undeclared and not workspaces:
        names = sorted(undeclared)
        shown = ", ".join(f"`{n}`({undeclared[n]})" for n in names[:6])
        result.issues.append(ReadinessIssue(
            "NODE_UNDECLARED_DEPENDENCY", ERROR,
            f"코드가 불러오는 패키지가 package.json 에 없습니다: {shown}. 컨테이너에는 선언된 패키지만 설치됩니다.",
            f"`npm install {' '.join(names[:6])}` 로 의존성에 추가하세요.", undeclared[names[0]]))

    for rel in entry_candidates:
        text = files.read(rel)
        if text is None:
            continue
        port, uses_env = _detect_js_port(text)
        if port or uses_env:
            result.app_port, result.port_from_env = port, uses_env
            break

    for folder in sorted(statics):
        if not files.has_dir(folder):
            result.issues.append(ReadinessIssue(
                "NODE_STATIC_DIR_MISSING", WARNING,
                f"서버가 정적 폴더 `{folder}` 를 제공하도록 돼 있지만 그 폴더가 없습니다.",
                f"`{folder}` 폴더에 index.html 등을 두거나 express.static 경로를 고치세요.", ""))
    result.health_path = next((p for p in _HEALTH_NAMES if p in routes), None)
    result.serves_root = "/" in routes or "*" in routes or any(
        files.exists(f"{folder}/index.html") for folder in statics)

    # CRA 빌드가 깨졌을 때: 서버가 정적 폴더를 그대로 제공하고 src/ 가 없으면
    # 빌드 자체가 필요 없는 구조다(생성 코드가 CRA 설정만 남긴 경우). 그때만
    # "빌드 스크립트 제거"를 자동 수정으로 제안한다.
    for name, command, missing in cra_broken:
        static_site = any(files.exists(f"{folder}/index.html") for folder in statics)
        unused = static_site and not files.has_dir("src") and bool(routes or statics)
        if unused:
            result.issues.append(ReadinessIssue(
                "NODE_UNUSED_BUILD_SCRIPT", ERROR,
                f"`{name}` 스크립트가 `react-scripts {command}` 인데 이 프로젝트에는 src/ 가 없고, 서버가 "
                f"{', '.join(sorted(statics))}/ 의 파일을 그대로 제공합니다. 쓰지 않는 빌드가 컨테이너에서 실패합니다.",
                f"package.json 에서 react-scripts 를 실행하는 `{name}` 스크립트를 지우세요(자동 수정 가능, "
                "원본은 .recoder/backups 에 보관). 쓰지 않는 react-scripts 의존성도 지우면 이미지가 작아집니다.",
                "package.json", True))
        else:
            result.issues.append(ReadinessIssue(
                "NODE_BUILD_ENTRY_MISSING", ERROR,
                f"`{name}` 스크립트가 `react-scripts {command}` 인데 Create React App 이 요구하는 "
                f"{', '.join(missing)} 이(가) 없습니다. 컨테이너 빌드가 이 단계에서 실패합니다.",
                "React 앱을 빌드하려면 src/index.js(와 public/index.html)를 만드세요. 빌드가 필요 없는 "
                f"프로젝트라면 `{name}` 스크립트를 지우세요.", "package.json"))


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

#: import 이름 ≠ 배포 패키지 이름인 흔한 경우.
_PY_DIST_FOR_MODULE = {
    "cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "jwt": "pyjwt", "jose": "python-jose",
    "dateutil": "python-dateutil", "multipart": "python-multipart", "google": "",
    "attr": "attrs", "Crypto": "pycryptodome", "OpenSSL": "pyopenssl", "magic": "python-magic",
    "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "serial": "pyserial",
    "telegram": "python-telegram-bot", "discord": "discord.py", "docx": "python-docx",
    "pptx": "python-pptx", "slugify": "python-slugify", "socketio": "python-socketio",
    "engineio": "python-engineio", "websocket": "websocket-client", "zmq": "pyzmq",
    "win32api": "", "pkg_resources": "setuptools", "setuptools": "", "_pytest": "",
}


def _requirement_names(files: ProjectFiles) -> Optional[set[str]]:
    names: set[str] = set()
    found = False
    for rel in ("requirements.txt", "requirements/base.txt", "requirements/prod.txt"):
        text = files.read(rel)
        if text is None:
            continue
        found = True
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    text = files.read("pyproject.toml")
    if text is not None:
        found = True
        for m in re.finditer(r"""['"]([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;]|['"])""", text):
            names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
        for m in re.finditer(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*['\"{]", text, re.MULTILINE):
            names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return names if found else None


def _analyze_python(files: ProjectFiles, result: Readiness) -> None:
    py_files = [rel for rel in files.files() if rel.endswith(".py")
                and not re.search(r"(^|/)(tests?|migrations)/|(^|/)(test_[^/]*|[^/]*_test|conftest|setup)\.py$", rel)]
    local = {PurePosixPath(rel).parts[0].removesuffix(".py") for rel in files.files()}
    local |= {PurePosixPath(rel).stem for rel in py_files}
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    requirements = _requirement_names(files)
    imported: dict[str, str] = {}
    routes: set[str] = set()
    fastapi_docs = False
    for rel in py_files:
        text = files.read(rel)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            result.issues.append(ReadinessIssue(
                "PY_SYNTAX_ERROR", ERROR,
                f"{rel} 에 문법 오류가 있습니다(줄 {exc.lineno}). 컨테이너가 시작 직후 종료됩니다.",
                "해당 줄의 문법 오류를 고치세요.", rel))
            continue
        result.local_data_files += [f for f in _local_sqlite_files(text) if f not in result.local_data_files]
        apps: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.setdefault(alias.name.split(".")[0], rel)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.setdefault(node.module.split(".")[0], rel)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name in {"FastAPI", "Flask", "APIRouter"}:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            apps[target.id] = name
                    if name == "FastAPI" and not any(
                            kw.arg == "docs_url" and isinstance(kw.value, ast.Constant) and kw.value.value is None
                            for kw in node.value.keywords):
                        fastapi_docs = True
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and isinstance(deco.func.value, ast.Name) and apps.get(deco.func.value.id) in {"FastAPI", "Flask"}
                        and deco.func.attr in {"get", "route", "api_route"} and deco.args
                        and isinstance(deco.args[0], ast.Constant) and isinstance(deco.args[0].value, str)):
                    routes.add(deco.args[0].value)
    if requirements is None and any(m not in stdlib and m not in local for m in imported):
        result.issues.append(ReadinessIssue(
            "PY_REQUIREMENTS_MISSING", ERROR,
            "외부 패키지를 쓰는데 requirements.txt(또는 pyproject.toml)가 없습니다. 컨테이너에 설치할 목록이 없습니다.",
            "`pip freeze > requirements.txt` 대신 실제로 쓰는 패키지만 requirements.txt 에 적으세요.", ""))
    elif requirements is not None:
        missing = []
        for module, rel in sorted(imported.items()):
            if module in stdlib or module in local or module.startswith("_"):
                continue
            dist = _PY_DIST_FOR_MODULE.get(module, module)
            if not dist:
                continue
            if re.sub(r"[-_.]+", "-", dist).lower() not in requirements \
                    and re.sub(r"[-_.]+", "-", module).lower() not in requirements:
                missing.append((dist, rel))
        if missing:
            shown = ", ".join(f"`{d}`({r})" for d, r in missing[:6])
            result.issues.append(ReadinessIssue(
                "PY_UNDECLARED_DEPENDENCY", WARNING,
                f"코드가 불러오지만 requirements 에 없는 패키지가 있습니다: {shown}. 컨테이너에서 ImportError 로 종료될 수 있습니다.",
                "requirements.txt 에 해당 패키지를 추가하세요(이름이 다른 패키지가 제공한다면 무시해도 됩니다).",
                missing[0][1]))
    result.health_path = next((p for p in _HEALTH_NAMES if p in routes), None)
    result.serves_root = "/" in routes
    result.docs_path = "/docs" if fastapi_docs else None


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

def _dockerfile_facts(text: str) -> dict:
    joined = re.sub(r"\\\r?\n", " ", text)
    facts: dict = {"expose": [], "health_port": None, "health_path": None, "env_port": None,
                   "runs_build": False, "cmd_entry": None, "user": None, "workdir": None, "workdir_owned": False}
    for line in joined.splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        op = words[0].upper()
        if op == "FROM":
            facts["expose"] = []
            facts.update(user=None, workdir=None, workdir_owned=False)
        elif op == "USER" and len(words) > 1:
            facts["user"] = words[1]
        elif op == "WORKDIR" and len(words) > 1:
            facts["workdir"], facts["workdir_owned"] = words[1].rstrip("/") or "/", False
        elif op == "RUN" and facts["workdir"] and re.search(
                rf"\bchown\b[^&;]*\s(?:{re.escape(facts['workdir'])}|\.)(?:\s|$|/?\s*[;&])", line + " "):
            facts["workdir_owned"] = True
        elif op == "EXPOSE":
            facts["expose"] += [int(w.split("/")[0]) for w in words[1:] if re.fullmatch(r"\d{1,5}(/tcp)?", w, re.I)]
        elif op == "ENV":
            m = re.search(r"\bPORT[= ]\s*(\d{2,5})\b", line)
            if m:
                facts["env_port"] = int(m.group(1))
        elif op == "HEALTHCHECK":
            m = re.search(r"(?:localhost|127\.0\.0\.1):(\d{2,5})(/[^\s'\"|)]*)?", line)
            if m:
                facts["health_port"] = int(m.group(1))
                facts["health_path"] = m.group(2) or "/"
        elif op == "RUN" and re.search(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?build\b", line):
            facts["runs_build"] = True
        elif op == "CMD":
            m = re.search(r"""\bnode["']?\s*,?\s*["']?([\w./-]+\.[cm]?js)""", line)
            facts["cmd_entry"] = _norm(m.group(1)) if m else None
    return facts


_DOCKERIGNORE_TRIGGERS = ("node_modules", ".env", ".venv", "venv", ".git")


def _analyze_dockerfile(files: ProjectFiles, result: Readiness, dockerfile: str) -> None:
    text = files.read(dockerfile)
    if text is None:
        return
    facts = _dockerfile_facts(text)
    expose = facts["expose"][0] if len(facts["expose"]) == 1 else None
    effective = facts["env_port"] if (result.port_from_env and facts["env_port"]) else result.app_port
    if result.port_from_env and not facts["env_port"] and not result.app_port:
        effective = None  # PORT 를 읽지만 기본값이 없음 — 판단 불가
    if expose and effective and expose != effective:
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_PORT_MISMATCH", ERROR,
            f"앱은 {effective} 포트에서 요청을 받는데 Dockerfile 은 {expose} 포트를 엽니다(EXPOSE"
            f"{'·HEALTHCHECK' if facts['health_port'] == expose else ''}). 컨테이너가 떠도 접속·헬스 확인이 실패합니다.",
            f"Dockerfile 의 {expose} 를 {effective} 로 맞추세요(자동 수정 가능).", dockerfile, True))
    if facts["cmd_entry"] and not files.exists(facts["cmd_entry"]) \
            and not re.match(r"^(dist|build|out|lib)/", facts["cmd_entry"]):
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_ENTRY_MISSING", ERROR,
            f"Dockerfile 의 CMD 가 실행하는 {facts['cmd_entry']} 가 프로젝트에 없습니다.",
            "CMD 를 실제 서버 파일로 고치거나 Dockerfile 을 다시 생성하세요.", dockerfile))
    non_root = facts["user"] and facts["user"].split(":")[0] not in {"root", "0"}
    if result.local_data_files and non_root and facts["workdir"] and not facts["workdir_owned"]:
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_WORKDIR_NOT_WRITABLE", ERROR,
            f"앱이 {facts['workdir']} 에 SQLite 파일({result.local_data_files[0]})을 만들지만, 컨테이너는 "
            f"{facts['user']} 사용자로 실행되고 그 폴더는 root 소유입니다. DB 를 열지 못해(SQLITE_CANTOPEN) API 가 실패합니다.",
            f"Dockerfile 의 USER 앞에 `RUN chown {facts['user']} {facts['workdir']}` 를 추가하세요(자동 수정 가능).",
            dockerfile, True))
    probe = result.probe_path()
    if facts["health_path"] and probe and facts["health_path"] not in {probe, "/"} \
            and facts["health_path"] != result.health_path:
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_HEALTH_PATH_UNKNOWN", WARNING,
            f"HEALTHCHECK 가 {facts['health_path']} 를 확인하지만 코드에서 그 경로를 찾지 못했습니다"
            f"(확인된 경로: {probe}). 컨테이너가 unhealthy 로 표시될 수 있습니다.",
            f"HEALTHCHECK 경로를 {probe} 로 바꾸거나(자동 수정 가능) {facts['health_path']} 라우트를 추가하세요.",
            dockerfile, True))
    if not files.exists(".dockerignore") and any(
            files.has_dir(d) or files.exists(d) for d in _DOCKERIGNORE_TRIGGERS):
        result.issues.append(ReadinessIssue(
            "DOCKERIGNORE_MISSING", WARNING,
            ".dockerignore 가 없어 node_modules·.git·.env 같은 파일까지 빌드에 들어갑니다. "
            "PC 의 node_modules 가 컨테이너 설치본을 덮어 빌드가 깨지거나 자격증명이 이미지에 남을 수 있습니다.",
            ".dockerignore 를 만드세요(자동 수정 가능).", ".dockerignore", True))


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def detect_runtime(files: ProjectFiles) -> str:
    if files.exists("package.json"):
        return "node"
    if any(files.exists(f) for f in ("requirements.txt", "pyproject.toml", "main.py", "app.py", "manage.py")):
        return "python"
    if files.exists("index.html"):
        return "static"
    return "unknown"


def analyze(workspace: str | Path, overlay: Optional[Mapping[str, Optional[str]]] = None,
            *, dockerfile: Optional[str] = "Dockerfile") -> Readiness:
    """프로젝트(와 Dockerfile)를 읽어 빌드·실행 가능성을 판정한다."""
    files = ProjectFiles(Path(workspace), overlay)
    result = Readiness(runtime=detect_runtime(files))
    if result.runtime == "node":
        _analyze_node(files, result)
    elif result.runtime == "python":
        _analyze_python(files, result)
    if result.local_data_files:
        shown = ", ".join(result.local_data_files[:3])
        result.issues.append(ReadinessIssue(
            "DATA_IN_CONTAINER", WARNING,
            f"앱이 SQLite 파일({shown})을 컨테이너 안의 작업 폴더에 만듭니다. 컨테이너를 다시 배포하면 데이터가 초기화됩니다.",
            "실제 운영이라면 데이터 폴더를 볼륨으로 연결하거나(docker run -v) 외부 DB 를 쓰세요. 시연·개발용이면 그대로 둬도 됩니다.", ""))
    if dockerfile and files.exists(dockerfile):
        _analyze_dockerfile(files, result, dockerfile)
    return result


def issues_as_risk_reasons(readiness: Readiness) -> list[str]:
    """배포 승인 카드에 넣을 문장. error 는 BLOCKER 표시."""
    out = []
    for issue in readiness.issues:
        prefix = "BLOCKER: 빌드·실행 실패 예상 — " if issue.severity == ERROR else "확인 필요 — "
        out.append(f"{prefix}{issue.message} 해결: {issue.fix}")
    return out


# ---------------------------------------------------------------------------
# 자동 수정 (사용자가 누른 경우만)
# ---------------------------------------------------------------------------

NODE_DOCKERIGNORE = """# ReCoder: Docker 빌드에서 제외할 파일
**/node_modules
.git
.recoder
.vscode
coverage
*.log
npm-debug.log*
.env
.env.*
!.env.example
Dockerfile*
.dockerignore
"""

PYTHON_DOCKERIGNORE = """# ReCoder: Docker 빌드에서 제외할 파일
.git
.recoder
.vscode
**/__pycache__
**/*.pyc
.venv
venv
env
.pytest_cache
.mypy_cache
*.log
.env
.env.*
!.env.example
Dockerfile*
.dockerignore
"""


def dockerignore_for(workspace: str | Path) -> str:
    return NODE_DOCKERIGNORE if (Path(workspace) / "package.json").is_file() else PYTHON_DOCKERIGNORE


def write_dockerignore_if_missing(workspace: str | Path) -> Optional[str]:
    """없을 때만 만든다. 만든 경로 또는 None(이미 있음)."""
    target = Path(workspace) / ".dockerignore"
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(dockerignore_for(workspace))
    except FileExistsError:
        return None
    return str(target)


def _read_raw(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:  # 줄바꿈(CRLF) 그대로
        return stream.read()


def _write_raw(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _backup(root: Path, rel: str, text: str) -> str:
    """원본을 .recoder/backups 에 남긴다(빌드 컨텍스트·Git 에 섞이지 않게)."""
    import time
    folder = root / ".recoder" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{rel.replace('/', '_')}.{time.strftime('%Y%m%d-%H%M%S')}"
    target, n = folder / stem, 1
    while target.exists():  # 같은 초에 여러 번 고쳐도 원본을 덮지 않는다
        target, n = folder / f"{stem}-{n}", n + 1
    _write_raw(target, text)
    return target.relative_to(root).as_posix()


def apply_fix(workspace: str | Path, code: str) -> dict:
    """AUTO_FIXABLE 만 적용한다. 다시 판정한 결과를 돌려준다."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("유효한 프로젝트 폴더가 아닙니다.")
    if code not in AUTO_FIXABLE:
        raise ValueError("자동으로 고칠 수 없는 항목입니다. 안내에 따라 직접 수정하세요.")
    before = analyze(root)
    issue = next((i for i in before.issues if i.code == code), None)
    if issue is None:
        return {"applied": False, "message": "이미 해결된 항목입니다.", "readiness": before.to_dict()}
    changed: list[str] = []
    if code == "DOCKERIGNORE_MISSING":
        path = write_dockerignore_if_missing(root)
        if path:
            changed.append(".dockerignore")
    elif code == "NODE_UNUSED_BUILD_SCRIPT":
        manifest = root / "package.json"
        text = _read_raw(manifest)
        package = json.loads(text)
        scripts = package.get("scripts") or {}
        removed = [name for name, value in list(scripts.items())
                   if isinstance(value, str) and re.search(r"\breact-scripts\s+(?:build|start)\b", value)]
        if not removed:
            raise ValueError("지울 react-scripts 스크립트를 찾지 못했습니다.")
        backup = _backup(root, "package.json", text)
        for name in removed:
            scripts.pop(name, None)
        newline = "\r\n" if "\r\n" in text else "\n"
        _write_raw(manifest, json.dumps(package, ensure_ascii=False, indent=2).replace("\n", newline) + newline)
        changed += ["package.json", backup]
    elif code == "DOCKERFILE_HEALTH_PATH_UNKNOWN":
        dockerfile = root / "Dockerfile"
        text = _read_raw(dockerfile)
        facts = _dockerfile_facts(text)
        new_path = before.probe_path()
        if not facts["health_path"] or not new_path:
            raise ValueError("바꿀 헬스 경로를 확정하지 못했습니다.")
        backup = _backup(root, "Dockerfile", text)
        old_path = re.escape(facts["health_path"])
        updated = re.sub(rf"((?:localhost|127\.0\.0\.1):\d{{2,5}}){old_path}(?=[\s'\"|)])", rf"\g<1>{new_path}", text)
        _write_raw(dockerfile, updated)
        changed += ["Dockerfile", backup]
    elif code == "DOCKERFILE_WORKDIR_NOT_WRITABLE":
        dockerfile = root / "Dockerfile"
        text = _read_raw(dockerfile)
        facts = _dockerfile_facts(text)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        user_line = max((i for i, l in enumerate(lines) if re.match(r"^\s*USER\s+\S", l, re.I)), default=None)
        if user_line is None or not facts["workdir"] or not facts["user"]:
            raise ValueError("USER·WORKDIR 를 확인하지 못했습니다. Dockerfile 을 직접 수정하세요.")
        owner = facts["user"] if ":" in facts["user"] else facts["user"]
        backup = _backup(root, "Dockerfile", text)
        lines[user_line:user_line] = [
            "# ReCoder: 앱이 작업 폴더에 데이터 파일을 만들 수 있게 한다(비root 실행).",
            f"RUN chown {owner} {facts['workdir']}", ""]
        _write_raw(dockerfile, newline.join(lines))
        changed += ["Dockerfile", backup]
    elif code == "DOCKERFILE_PORT_MISMATCH":
        dockerfile = root / "Dockerfile"
        text = _read_raw(dockerfile)
        facts = _dockerfile_facts(text)
        old = facts["expose"][0]
        new = (facts["env_port"] if before.port_from_env and facts["env_port"] else before.app_port)
        if not new or old == new:
            raise ValueError("맞출 포트를 확정하지 못했습니다. Dockerfile 을 직접 확인하세요.")
        backup = _backup(root, "Dockerfile", text)
        updated = re.sub(rf"(^\s*EXPOSE\s+){old}\b", rf"\g<1>{new}", text, flags=re.IGNORECASE | re.MULTILINE)
        updated = re.sub(rf"((?:localhost|127\.0\.0\.1):){old}\b", rf"\g<1>{new}", updated)
        _write_raw(dockerfile, updated)
        changed += ["Dockerfile", backup]
    after = analyze(root)
    return {"applied": bool(changed), "changed": changed,
            "message": "수정했습니다. 배포 내용을 다시 확인하세요." if changed else "변경할 내용이 없었습니다.",
            "readiness": after.to_dict()}
