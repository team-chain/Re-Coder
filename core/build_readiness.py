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
                "NODE_UNUSED_BUILD_SCRIPT", "DOCKERFILE_WORKDIR_NOT_WRITABLE", "NODE_VULNERABLE_DEPENDENCY",
                "NODE_DEPENDENCY_VERSION_NOT_FOUND", "DOCKERFILE_SUBPROJECT_DEPS_MISSING", "NODE_FRONTEND_NOT_SERVED",
                "NODE_LOCAL_IMPORT_MISSING", "NODE_VITE_JSX_IN_JS", "NODE_VITE_PROCESS_ENV", "NODE_IMPORT_PACKAGE_TYPO",
                "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING", "NODE_PG_NUMERIC_STRINGS", "NODE_CLIENT_HARDCODED_LOCALHOST",
                "NODE_ROUTER_ANCHOR_LINK", "NODE_MODULE_FORMAT_MISMATCH", "NODE_FRONTEND_NOT_BUILT",
                "NODE_IMPORT_NAME_MISSING", "NODE_CLIENT_API_DOUBLE_PREFIX"}

#: 이 버전 아래를 쓰면 이미지 보안 검사(Trivy)에서 CRITICAL 이 나와 배포가 막히는 직접 의존성.
#: (패키지 → (안전한 최소 major, 권장 범위, 이유)). 버전만 올리면 되는 경우만 적는다.
KNOWN_VULNERABLE_NODE: dict[str, tuple[int, str, str]] = {
    "sqlite3": (6, "^6.0.1", "sqlite3 5.x 는 치명적(CRITICAL) 취약점이 있는 tar(7.5.20 이하)를 함께 설치합니다"),
}


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
    #: 루트 build 스크립트가 들어가서 빌드하는 하위 프로젝트(client/ 등) — 각자 package.json 이 있다.
    subprojects: list[str] = field(default_factory=list)
    server_entry: Optional[str] = None     # 서버 진입 파일(정적 제공 자동 수정 대상)
    #: 서버 진입 파일이 자기 package.json 을 가진 하위 폴더(server/ 등)에 있으면 그 폴더.
    runtime_subproject: Optional[str] = None
    #: 로컬 Docker 배포가 함께 띄울 수 있는 서비스(postgres·mongodb·redis)와 서버 코드가 읽는 환경변수.
    services: list[str] = field(default_factory=list)
    env_names: list[str] = field(default_factory=list)
    #: 자동 수정에 쓰는 세부 정보(JSX 가 든 .js 파일, 오타 패키지 등).
    fix_data: dict = field(default_factory=dict)
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


def _spec_major(spec) -> Optional[int]:
    """"^5.1.6" → 5. 태그·경로·URL·와일드카드는 판단하지 않는다(None)."""
    if not isinstance(spec, str):
        return None
    m = re.match(r"^\s*(?:[\^~]|=)?\s*v?(\d+)(?:\.|\s*$|\.x)", spec)
    return int(m.group(1)) if m else None


def _start_entry(scripts: dict) -> Optional[str]:
    for name in ("start", "start:prod", "serve"):
        for words in _script_commands(str(scripts.get(name, ""))):
            if words[0] in {"node", "nodemon"}:
                #: `node -r dotenv/config server.js` — -r·--require·--import·--loader 다음 값은 진입 파일이 아니다.
                args, skip = [], False
                for w in words[1:]:
                    if skip:
                        skip = False
                        continue
                    if w in (_NODE_VALUE_FLAGS if words[0] == "node" else _NODEMON_VALUE_FLAGS):
                        skip = True
                        continue
                    if not w.startswith("-"):
                        args.append(w)
                if args:
                    return _norm(args[0].strip("'\""))
    return None


_NODE_VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-e", "--eval", "-p", "--print",
                     "--env-file", "--watch-path", "--inspect-port"}
_NODEMON_VALUE_FLAGS = {"-w", "--watch", "-e", "--ext", "-x", "--exec", "--signal", "-d", "--delay", "-i", "--ignore", "--config",
                        "-r", "--require"}



_FRONTEND_BUILDS = (
    (re.compile(r"\breact-scripts\s+build\b"), "build"),
    (re.compile(r"\bvite(?:\s+build\b|\s*$)"), "dist"),
    (re.compile(r"\bvue-cli-service\s+build\b"), "dist"),
    (re.compile(r"\bparcel\s+build\b"), "dist"),
    (re.compile(r"\bastro\s+build\b"), "dist"),
)
_DIR_FLAGS = ("--prefix", "-C", "--cwd", "--dir")
_EXTERNAL_SERVICES = (
    ("pg", "PostgreSQL", r"localhost:5432|127\.0\.0\.1:5432|postgres(?:ql)?://[^'\"\s]*@(?:localhost|127\.0\.0\.1)"),
    ("mysql2", "MySQL", r"localhost:3306|127\.0\.0\.1:3306|host\s*:\s*['\"](?:localhost|127\.0\.0\.1)['\"]"),
    ("mysql", "MySQL", r"localhost:3306|127\.0\.0\.1:3306|host\s*:\s*['\"](?:localhost|127\.0\.0\.1)['\"]"),
    ("mongoose", "MongoDB", r"mongodb://(?:[^'\"\s@]*@)?(?:localhost|127\.0\.0\.1)"),
    ("mongodb", "MongoDB", r"mongodb://(?:[^'\"\s@]*@)?(?:localhost|127\.0\.0\.1)"),
    ("redis", "Redis", r"redis://(?:localhost|127\.0\.0\.1)|localhost:6379"),
    ("ioredis", "Redis", r"redis://(?:localhost|127\.0\.0\.1)|localhost:6379"),
)


def _npm_dir_and_args(words: list[str]) -> tuple[str, list[str]]:
    """``npm --prefix client run build`` → ("client", ["run", "build"])."""
    target, rest, skip = "", [], False
    for i, word in enumerate(words[1:], start=1):
        if skip:
            skip = False
            continue
        if word in _DIR_FLAGS:
            if i + 1 < len(words):
                target = _norm(words[i + 1].strip("'\""))
            skip = True
            continue
        m = re.match(r"^(--prefix|--cwd|--dir)=(.+)$", word)
        if m:
            target = _norm(m.group(2).strip("'\""))
            continue
        if not word.startswith("-"):
            rest.append(word)
    return target, rest


def _subproject_scripts(scripts: dict, files: "ProjectFiles") -> tuple[list[str], set[str]]:
    """(루트 build 가 들어가서 실행하는 하위 프로젝트, 스크립트가 스스로 의존성을 설치하는 하위 프로젝트)."""
    runs: list[str] = []
    installs: set[str] = set()

    def visit(name: str, depth: int) -> None:
        cwd = ""
        for words in _script_commands(str(scripts.get(name, ""))):
            if words[0] == "cd" and len(words) > 1:
                cwd = _norm(words[1].strip("'\""))
                continue
            if words[0] not in {"npm", "yarn", "pnpm"}:
                continue
            flag_dir, rest = _npm_dir_and_args(words)
            target = flag_dir or cwd
            installing = (rest[:1] in (["install"], ["i"], ["ci"])) or (words[0] == "yarn" and not rest)
            if target and target not in {".", ""} and files.exists(f"{target}/package.json"):
                if installing:
                    installs.add(target)
                elif target not in runs:
                    runs.append(target)
            elif not target and depth < 3 and not installing:
                script = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else (rest[0] if rest else "")
                if script in scripts and script != name:
                    visit(script, depth + 1)

    for lifecycle in ("preinstall", "install", "postinstall", "prebuild"):
        if lifecycle in scripts:
            visit(lifecycle, 0)
    if "build" in scripts:
        visit("build", 0)
    return runs, installs


def _frontend_out_dir(files: "ProjectFiles", folder: str) -> Optional[str]:
    try:
        sub = json.loads(files.read(f"{folder}/package.json") or "")
    except ValueError:
        return None
    build = str(((sub or {}).get("scripts") or {}).get("build", "")) if isinstance(sub, dict) else ""
    for pattern, out in _FRONTEND_BUILDS:
        if pattern.search(build):
            return f"{folder}/{out}"
    return None


# ---------------------------------------------------------------------------
# 소스 교차 점검 — AI 가 파일을 나눠 만들 때 흔히 어긋나는 것들
# ---------------------------------------------------------------------------

_JS_RESOLVE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".vue", ".svelte")
_STYLE_EXTS = (".css", ".scss", ".sass", ".less")
_JSX_LINE = re.compile(r"(?:\(|return|=>)\s*\n?\s*<[A-Z][\w.]*[\s/>]|\breturn\s*\(\s*\n\s*<[a-z][\w-]*[\s>]", re.MULTILINE)
_SERVER_MARKERS = re.compile(r"""require\(\s*['"](?:express|http|https|fs|path|pg|mongoose|mongodb|redis|ioredis|koa|fastify)['"]\s*\)|"""
                             r"""from\s+['"](?:express|node:\w+|http|fs|path|pg|mongoose|koa|fastify)['"]|\.listen\s*\(""")


def _is_server_file(text: str) -> bool:
    return bool(_SERVER_MARKERS.search(text))
_ESM_NAMED_IMPORT = re.compile(r"""\bimport\s+(?:[\w$]+\s*,\s*)?\{([^}]*)\}\s*from\s*['"](\.{1,2}/[^'"]+)['"]""")
_CJS_REQUIRE_BIND = re.compile(r"""\b(?:const|let|var)\s+([\w$]+)\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
_CJS_DESTRUCTURE = re.compile(r"""\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
_RELATIVE_SPEC = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\(\s*)['"](\.{1,2}/[^'"]+)['"]""")
_BARE_IMPORT = re.compile(r"""^\s*import\s+['"](\.{1,2}/[^'"]+)['"]""", re.MULTILINE)


def _resolve_local(files: "ProjectFiles", importer: str, spec: str) -> Optional[str]:
    import posixpath
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec.split("?")[0]))
    if base.startswith(".."):
        return base  # 프로젝트 밖 — 판단하지 않는다
    if files.exists(base):
        return base
    for ext in _JS_RESOLVE_EXTS + _STYLE_EXTS + (".d.ts",):
        if files.exists(base + ext):
            return base + ext
    #: TypeScript(NodeNext)는 './x.js' 로 적고 실제 파일은 x.ts 다.
    stem, dot, ext = base.rpartition(".")
    if dot and ext in ("js", "mjs", "cjs", "jsx"):
        for ts_ext in (".ts", ".tsx", ".mts", ".cts", ".d.ts"):
            if files.exists(stem + ts_ext):
                return stem + ts_ext
    for ext in _JS_RESOLVE_EXTS:
        if files.exists(f"{base}/index{ext}"):
            return f"{base}/index{ext}"
    return None


def _esm_exports(text: str) -> Optional[set[str]]:
    #: 판단할 수 없는 형태(재수출·타입 수출·구조 분해 수출)는 None — 잘못 막느니 넘어간다.
    if re.search(r"\bexport\s*\*|\bexport\s+(?:declare\s+)?(?:interface|type|enum|namespace|abstract)\b|\bexport\s+(?:const|let|var)\s*[\[{]", text):
        return None
    names = set(re.findall(r"\bexport\s+(?:async\s+)?(?:const|let|var|function\*?|class)\s+([\w$]+)", text))
    for group in re.findall(r"\bexport\s*\{([^}]*)\}", text):
        for part in group.split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    if re.search(r"\bexport\s+default\b", text):
        names.add("default")
    return names if names else None


def _cjs_exports(text: str) -> Optional[set[str]]:
    literal = re.findall(r"\bmodule\.exports\s*=\s*\{([^{}]*)\}", text)
    if len(literal) != 1 or re.search(r"\bmodule\.exports\s*=\s*(?![\s{])", text) or "..." in literal[0]:
        return None  # 함수·클래스·다른 값을 내보내거나 여러 번 대입 — 판단하지 않는다
    names: set[str] = set()
    for part in literal[0].split(","):
        part = part.strip()
        m = re.match(r"^(?:async\s+)?([\w$]+)", part)
        if m:
            names.add(m.group(1))
    names |= set(re.findall(r"\b(?:module\.)?exports\.([\w$]+)\s*=", text))
    return names


def _vite_projects(files: "ProjectFiles", manifests: dict) -> set[str]:
    out = set()
    for folder in manifests:
        try:
            pkg = json.loads(files.read(f"{folder}/package.json" if folder else "package.json") or "")
        except ValueError:
            continue
        deps = {}
        for key in ("dependencies", "devDependencies"):
            if isinstance(pkg, dict) and isinstance(pkg.get(key), dict):
                deps.update(pkg[key])
        if "vite" in deps:
            out.add(folder)
    return out


_GENERIC_PACKAGE_WORDS = {"js", "ts", "node", "client", "server", "core", "sdk", "api", "web", "browser", "dom", "lib"}


def _closest_declared(name: str, declared: set[str], imported: set[str] = frozenset()) -> Optional[str]:
    """`@stripe/js` → `@stripe/stripe-js` 처럼 같은 범위의 선언된 패키지 중 가장 가까운 것.

    선언만 하고 어디서도 import 하지 않는 패키지를 먼저 본다 — AI 가 선언은 맞게 하고
    import 에서 이름을 틀리는 경우가 대부분이다.
    """
    import difflib
    scope = name.split("/")[0] if name.startswith("@") else ""
    tail = name.split("/")[-1]

    def close(d: str) -> bool:
        dtail = d.split("/")[-1]
        #: 후보가 이 이름을 그대로 품고 앞뒤에 덧붙인 꼴(passport→passport-jwt, redis→ioredis, uuid→uuidv4,
        #: @mui/material→@mui/icons-material)은 다른 패키지다 — 이름을 바꾸면 없는 내보내기를 부르게 된다.
        #: (글자 하나가 빠진 expres→express 는 오타로 본다 — 덧붙인 부분이 한 글자이고 구분자가 아닐 때)
        if dtail.startswith(tail):
            extra = dtail[len(tail):]
            if len(extra) > 1 or extra in "-_.":
                return False
        elif dtail.endswith(tail):
            extra = dtail[:-len(tail)]
            if len(extra) > 1 or extra in "-_.":
                # 예외: `@stripe/js` → `@stripe/stripe-js` 처럼 범위 안에서 접미 낱말 하나만 적은 경우
                return bool(scope) and tail in _GENERIC_PACKAGE_WORDS and dtail[-len(tail) - 1] in "-_."
        # 범위가 있으면 범위 뒤 이름만 비교한다 — `@aws-sdk/` 같은 공통 접두어가 비율을 부풀리지 않도록
        return difflib.SequenceMatcher(None, tail, dtail).ratio() >= 0.75

    pool = [d for d in declared if d and d != name and d not in imported and (not scope or d.startswith(scope + "/"))
            and close(d)]
    if not pool:
        return None
    return max(pool, key=lambda d: difflib.SequenceMatcher(None, name, d).ratio())


def api_prefix_rewrite(text: str, names) -> str:
    """names(baseURL 이 /api 인 axios 인스턴스)의 호출 경로에서 앞의 /api 를 뺀다: client.get('/api/x') → client.get('/x')."""
    for name in names:
        text = re.sub(rf"""(\b{re.escape(name)}\.(?:get|post|put|patch|delete|head|options|request)\s*\(\s*)(['"`])/api(/[^'"`]*)?\2""",
                      lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or '/'}{m.group(2)}", text)
        text = re.sub(rf"""(\b{re.escape(name)}\.(?:get|post|put|patch|delete|head|options|request)\s*\(\s*`)/api(/)""",
                      lambda m: f"{m.group(1)}{m.group(2)}", text)
    return text


def add_missing_export(text: str, name: str, kind: str) -> Optional[str]:
    """파일 최상위에 선언된 name 을 내보낸다. 선언이 없거나 확실하지 않으면 None."""
    newline_ = "\r\n" if "\r\n" in text else "\n"
    if kind == "esm" and name.startswith("default:"):
        #: `import Header from './Header'` 인데 파일은 `export function Header` 만 있다 — 기본 내보내기를 보탠다.
        local = name.split(":", 1)[1]
        declared = re.search(rf"^(?:export\s+)?(?:(?:async\s+)?function\*?|class|const|let|var)\s+{re.escape(local)}\b", text, re.MULTILINE)
        if not declared:
            components = re.findall(r"^export\s+(?:(?:async\s+)?function|class|const|let)\s+([A-Z][\w$]*)", text, re.MULTILINE)
            if len(components) != 1:
                return None
            local = components[0]
        return text.rstrip() + newline_ + newline_ + f"export default {local};" + newline_
    decl = re.compile(rf"^(?:(?:async\s+)?function\*?|class|const|let|var)\s+{re.escape(name)}\b", re.MULTILINE)
    if kind == "esm":
        m = decl.search(text)
        if m:
            return text[:m.start()] + "export " + text[m.start():]
        if re.search(rf"^export\s+default\s+(?:(?:async\s+)?function\*?|class)\s+{re.escape(name)}\b", text, re.MULTILINE):
            #: `export default function Header` 인데 쓰는 쪽은 `import { Header }` — 이름으로도 내보낸다.
            return text.rstrip() + newline_ + newline_ + f"export {{ {name} }};" + newline_
        #: `export const api = { getProducts: () => client.get(...) }` 인데 쓰는 쪽은 `import { getProducts }` —
        #: 그 객체의 함수를 이름으로 내보낸다. axios 응답이면 쓰는 쪽이 기대하는 데이터(res.data)를 돌려준다.
        owners = [obj for obj, body in re.findall(r"^export\s+const\s+([\w$]+)\s*=\s*\{(.*?)^\};?", text, re.MULTILINE | re.DOTALL)
                  if re.search(rf"^\s*{re.escape(name)}\s*[:(]", body, re.MULTILINE)]
        if len(owners) != 1:
            return None
        newline = "\r\n" if "\r\n" in text else "\n"
        axios_like = bool(re.search(r"""from\s+['"]axios['"]|require\(\s*['"]axios['"]\s*\)""", text))
        call = f"{owners[0]}.{name}(...args)"
        body = (f"Promise.resolve({call}).then((res) => (res && typeof res === 'object' && 'data' in res && 'status' in res ? res.data : res))"
                if axios_like else call)
        return (text.rstrip() + newline + newline + "// ReCoder: 다른 파일이 이 이름으로 불러와 함께 내보낸다." + newline
                + f"export const {name} = (...args) => {body};" + newline)
    #: CommonJS: module.exports = { … } 한 곳에 이름을 보탠다
    if not decl.search(text):
        return None
    literal = list(re.finditer(r"\bmodule\.exports\s*=\s*\{([^{}]*)\}", text))
    if len(literal) != 1 or re.search(r"\bmodule\.exports\s*=\s*(?![\s{])", text):
        return None
    m = literal[0]
    body = m.group(1).rstrip()
    sep = "" if not body.strip() else ("" if body.endswith(",") else ",")
    inner = f"{body}{sep} {name} " if "\n" not in body else f"{body}{sep}\n  {name}\n"
    return text[:m.start(1)] + inner + text[m.end(1):]


def _analyze_js_sources(files: "ProjectFiles", result: "Readiness", manifests: dict, owner, undeclared: dict) -> None:
    deps = manifests.get("", set())
    vite = _vite_projects(files, manifests)
    missing_files: list[tuple[str, str]] = []
    missing_names: list[str] = []
    missing_exports: list[tuple[str, str, str]] = []  # (내보내야 할 파일, 이름, esm|cjs)
    jsx_in_js: list[str] = []
    process_env: list[str] = []
    for rel in files.files():
        if not rel.endswith(_JS_SUFFIXES) or "/node_modules/" in f"/{rel}":
            continue
        text = files.read(rel)
        if text is None:
            continue
        active = _strip_js_comments(text)
        project = owner(rel)
        # 1) 없는 파일을 불러온다
        for spec in set(_RELATIVE_SPEC.findall(active)) | set(_BARE_IMPORT.findall(active)):
            if _resolve_local(files, rel, spec) is None:
                missing_files.append((rel, spec))
        # 2) 가져오는 이름이 그 파일에 없다(나눠 만든 파일끼리 자주 어긋난다)
        for names, spec in _ESM_NAMED_IMPORT.findall(active):
            target = _resolve_local(files, rel, spec)
            if not target or not target.endswith(_JS_SUFFIXES):
                continue
            exported = _esm_exports(_strip_js_comments(files.read(target) or ""))
            if exported is None:
                continue
            for name in (re.sub(r"^type\s+", "", n.strip()).split(" as ")[0].strip() for n in names.split(",")):
                if name and name not in exported:
                    missing_names.append(f"`{name}`({rel} → {target})")
                    missing_exports.append((target, name, "esm"))
        #: 기본 가져오기(import Header from './Header')인데 그 파일에 기본 내보내기가 없다 — Vite 빌드가 멈춘다.
        for local, spec in re.findall(r"""^\s*import\s+([\w$]+)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"](\.{1,2}/[^'"]+)['"]""", active, re.MULTILINE):
            target = _resolve_local(files, rel, spec)
            if not target or not target.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs")):
                continue
            target_text = _strip_js_comments(files.read(target) or "")
            if re.search(r"\bmodule\.exports\b|\bexports\.[\w$]+\s*=", target_text):
                continue  # CommonJS 파일의 기본 가져오기는 module.exports 다
            exported = _esm_exports(target_text)
            if exported is None or "default" in exported:
                continue
            missing_names.append(f"`default`({rel} → {target})")
            missing_exports.append((target, f"default:{local}", "esm"))
        cjs_bindings = []
        for var, spec in _CJS_REQUIRE_BIND.findall(active):
            cjs_bindings.append((var, spec, None))
        for names, spec in _CJS_DESTRUCTURE.findall(active):
            cjs_bindings.append((None, spec, [n.strip().split(":")[0].strip() for n in names.split(",") if n.strip()]))
        for var, spec, names in cjs_bindings:
            target = _resolve_local(files, rel, spec)
            if not target or not target.endswith(_JS_SUFFIXES):
                continue
            exported = _cjs_exports(_strip_js_comments(files.read(target) or ""))
            if exported is None:
                continue
            #: 문자열 안('./cfg.cjs')의 "cfg.cjs" 를 속성 접근으로 보지 않는다
            code_only = _QUOTED_STRING.sub("''", active)
            used = names if names is not None else set(re.findall(rf"\b{re.escape(var)}\.([\w$]+)", code_only))
            for name in sorted(set(used)):
                if name not in exported:
                    missing_names.append(f"`{name}`({rel} → {target})")
                    missing_exports.append((target, name, "cjs"))
        # 3) Vite 는 .js 안의 JSX 를 해석하지 않는다 · 브라우저 코드에 process.env 가 없다
        #    (서버 파일·설정 파일은 제외 — 단일 패키지 Vite+Express 앱의 server.js 를 건드리면 안 된다)
        if project in vite and not _is_server_file(active) and not re.search(r"(^|/)(?:vite|vitest|playwright|cypress|jest|tailwind|postcss|eslint)\.config\.|(^|/)(?:cypress|e2e|tests?|__tests__)/", rel):
            if rel.endswith(".js") and _JSX_LINE.search(active) and re.search(r"""\bfrom\s+['"]react['"]|\bReact\b|\.jsx?['"]""", active):
                jsx_in_js.append(rel)
            if re.search(r"\bprocess\.env\.[A-Za-z_]", active):
                process_env.append(rel)
    if missing_files:
        shown = ", ".join(f"`{spec}`({rel})" for rel, spec in missing_files[:6])
        styles_only = all(spec.endswith(_STYLE_EXTS) for _, spec in missing_files)
        result.fix_data["missing_styles"] = [(rel, spec) for rel, spec in missing_files if spec.endswith(_STYLE_EXTS)]
        result.issues.append(ReadinessIssue(
            "NODE_LOCAL_IMPORT_MISSING", ERROR,
            f"코드가 불러오는 프로젝트 파일이 없습니다: {shown}. 빌드나 실행이 이 import 에서 멈춥니다.",
            ("없는 스타일 파일을 빈 파일로 만드세요(자동 수정 가능)." if styles_only else
             "빠진 파일을 만들거나 import 경로를 실제 파일로 고치세요."),
            missing_files[0][0], styles_only))
    if missing_names:
        #: 그 파일 안에 같은 이름이 선언만 되고 내보내지지 않았으면(const CartContext = createContext()) 내보내기만
        #: 붙이면 된다 — 나눠 만든 파일끼리 가장 흔한 어긋남(실기기 생성 쇼핑몰). 모두 그런 경우에만 자동 수정.
        export_fixes = sorted({(t, n, k) for t, n, k in missing_exports})
        fixable = bool(export_fixes) and len(export_fixes) == len({(t, n) for t, n, _ in missing_exports}) and all(
            add_missing_export(files.read(t) or "", n, k) is not None for t, n, k in export_fixes)
        if fixable:
            result.fix_data["missing_exports"] = export_fixes
        result.issues.append(ReadinessIssue(
            "NODE_IMPORT_NAME_MISSING", ERROR,
            "불러오는 이름을 그 파일이 내보내지 않습니다: " + ", ".join(list(dict.fromkeys(missing_names))[:6])
            + ". 빌드가 실패하거나 실행 중 undefined 오류(… is not a function)로 멈춥니다.",
            ("그 파일에 선언된 이름에 내보내기(export)를 붙이세요(자동 수정 가능)." if fixable
             else "내보내는 쪽 이름과 쓰는 쪽 이름을 맞추세요."),
            missing_names[0].split("(")[1].split(" ")[0], fixable))
    if jsx_in_js:
        result.fix_data["jsx_in_js"] = jsx_in_js
        result.issues.append(ReadinessIssue(
            "NODE_VITE_JSX_IN_JS", ERROR,
            f"Vite 는 .js 파일 안의 JSX 를 해석하지 않습니다: {', '.join(jsx_in_js[:5])}. `vite build` 가 "
            "\"Failed to parse source for import analysis\" 로 실패합니다.",
            "파일 확장자를 .jsx 로 바꾸세요(자동 수정 가능 — index.html 과 import 경로도 함께 고칩니다).",
            jsx_in_js[0], True))
    if process_env:
        result.fix_data["process_env"] = process_env
        result.issues.append(ReadinessIssue(
            "NODE_VITE_PROCESS_ENV", ERROR,
            f"Vite 화면 코드가 process.env 를 씁니다: {', '.join(process_env[:5])}. 브라우저에는 process 가 없어 "
            "화면이 \"process is not defined\" 로 멈춥니다(Create React App 방식).",
            "import.meta.env.VITE_… 로 바꾸세요(자동 수정 가능 — REACT_APP_X 는 VITE_X 로 바꿉니다).",
            process_env[0], True))
    # 4) pg 는 NUMERIC/DECIMAL 을 문자열로 돌려준다 — 화면이 price.toFixed() 를 부르면 "toFixed is not a function"
    pg_files = [rel for rel in files.files() if rel.endswith(_JS_SUFFIXES) and "/node_modules/" not in f"/{rel}"
                and re.search(r"""require\(\s*['"]pg['"]\s*\)|from\s+['"]pg['"]""", files.read(rel) or "")]
    if pg_files:
        all_js = {rel: (files.read(rel) or "") for rel in files.files()
                  if rel.endswith(_JS_SUFFIXES) and "/node_modules/" not in f"/{rel}"}
        declares_numeric = any(re.search(r"\b(?:DECIMAL|NUMERIC)\s*\(", t, re.I) for t in all_js.values()) \
            or any(re.search(r"\b(?:DECIMAL|NUMERIC)\s*\(", files.read(rel) or "", re.I) for rel in files.files() if rel.endswith(".sql"))
        uses_number_api = any(re.search(r"\.(?:toFixed|toLocaleString)\(", t) for t in all_js.values())
        parser_set = any("setTypeParser" in t for t in all_js.values())
        if declares_numeric and uses_number_api and not parser_set:
            result.fix_data["pg_numeric_files"] = pg_files
            result.issues.append(ReadinessIssue(
                "NODE_PG_NUMERIC_STRINGS", WARNING,
                f"PostgreSQL(pg)은 DECIMAL/NUMERIC 값을 문자열로 돌려주는데 코드가 숫자 메서드(toFixed 등)를 씁니다"
                f"({pg_files[0]}). 화면에서 \"toFixed is not a function\" 오류가 납니다.",
                "pg 의 NUMERIC 타입 파서를 숫자로 설정하세요(자동 수정 가능).", pg_files[0], True))

    # 5) 화면 코드가 http://localhost:포트 를 직접 부른다 — 배포 포트가 다르거나 다른 PC 에서 열면 Network Error
    hardcoded: list[str] = []
    for rel in files.files():
        if not rel.endswith((".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")) or "/node_modules/" in f"/{rel}":
            continue
        project = owner(rel)
        if not project or not _frontend_out_dir(files, project):
            continue  # 프런트엔드(빌드 도구가 있는 하위 프로젝트)만 본다
        if re.search(r"(^|/)[\w.-]*\.config\.[cm]?[jt]s$|(^|/)(?:cypress|e2e|tests?|__tests__|mocks?)/|setupProxy\.[jt]s$", rel):
            continue  # 개발 프록시·테스트 설정의 localhost 는 정상이다
        if re.search(r"""(['"`])https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/[^'"`\s]*)?\1""", _strip_js_comments(files.read(rel) or "")):
            hardcoded.append(rel)
    if hardcoded:
        result.fix_data["hardcoded_api"] = hardcoded
        result.issues.append(ReadinessIssue(
            "NODE_CLIENT_HARDCODED_LOCALHOST", ERROR,
            f"화면 코드가 API 를 http://localhost:포트 로 직접 부릅니다: {', '.join(hardcoded[:4])}. 배포 포트가 다르거나 "
            "다른 PC·서버에서 열면 요청이 실패합니다(Network Error).",
            "서버가 화면을 함께 제공하므로 상대 경로(/api/…)로 부르게 바꾸세요(자동 수정 가능).", hardcoded[0], True))

    # 5-2) axios 인스턴스의 baseURL 이 이미 /api 인데 쓰는 쪽이 '/api/…' 를 또 붙인다 → /api/api/… 404
    double: dict[str, list[str]] = {}
    for rel in files.files():
        if not rel.endswith((".js", ".jsx", ".ts", ".tsx")) or "/node_modules/" in f"/{rel}":
            continue
        text = _strip_js_comments(files.read(rel) or "")
        instances = []
        for var, value in re.findall(r"""\b(?:const|let|var)\s+([\w$]+)\s*=\s*axios\.create\(\s*\{[^}]*?\bbaseURL\s*:\s*([^,\n}]+)""", text, re.DOTALL):
            value = value.strip()
            ident = re.fullmatch(r"[\w$]+", value)
            if ident:  # baseURL: API_BASE_URL — 같은 파일의 상수를 따라간다
                m = re.search(rf"""\b(?:const|let|var)\s+{re.escape(value)}\s*=\s*([^;\n]+)""", text)
                value = m.group(1) if m else ""
            #: 문자열 그대로인 baseURL 만 본다 — `import.meta.env.VITE_API || '/api'` 처럼 배포에서 다른 주소가 될 수
            #: 있는 값은 '/api/…' 호출이 맞는 코드일 수 있다.
            if re.fullmatch(r"""(['"`])(?:https?://[^'"`$\s/]+)?[^'"`$]*?/api/?\1\s*;?""", value.strip()):
                instances.append(var)
        for var in instances:
            names_by_file: dict[str, set[str]] = {rel: {var}}
            default_export = re.search(rf"\bexport\s+default\s+{re.escape(var)}\b", text)
            for other in files.files():
                if other == rel or not other.endswith((".js", ".jsx", ".ts", ".tsx")) or "/node_modules/" in f"/{other}":
                    continue
                body = _strip_js_comments(files.read(other) or "")
                for local, spec in re.findall(r"""\bimport\s+([\w$]+)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"](\.{1,2}/[^'"]+)['"]""", body):
                    if default_export and _resolve_local(files, other, spec) == rel:
                        names_by_file.setdefault(other, set()).add(local)
            for target, names in names_by_file.items():
                body = files.read(target) or ""
                if api_prefix_rewrite(body, names) != body:
                    double.setdefault(target, [])
                    double[target] = sorted(set(double[target]) | names)
    if double:
        result.fix_data["api_double_prefix"] = double
        first = next(iter(double))
        result.issues.append(ReadinessIssue(
            "NODE_CLIENT_API_DOUBLE_PREFIX", ERROR,
            f"axios 인스턴스의 baseURL 이 이미 /api 인데 화면 코드가 '/api/…' 를 한 번 더 붙입니다: {', '.join(list(double)[:4])}. "
            "요청이 /api/api/… 로 가서 404 가 나고 화면에 데이터가 나오지 않습니다.",
            "그 호출들의 경로에서 앞의 /api 를 빼세요(자동 수정 가능).", first, True))

    # 6) React Context 의 value 에 없는 이름을 useContext 로 꺼낸다 → "x is not a function"(나눠 만든 파일끼리 어긋남)
    context_keys: dict[str, set[str]] = {}
    for rel in files.files():
        if not rel.endswith((".jsx", ".tsx", ".js", ".ts")) or "/node_modules/" in f"/{rel}":
            continue
        text = _strip_js_comments(files.read(rel) or "")
        for name in re.findall(r"\b(?:export\s+)?(?:const|let)\s+([A-Z][\w$]*)\s*=\s*createContext\(", text):
            keys: set[str] = set()
            bodies = list(re.findall(rf"<{re.escape(name)}\.Provider\s+value=\{{\s*\{{([^}}]*)\}}\s*\}}", text))
            for var in re.findall(rf"<{re.escape(name)}\.Provider\s+value=\{{\s*([\w$]+)\s*\}}", text):
                m = re.search(rf"\b(?:const|let)\s+{re.escape(var)}\s*=\s*\{{([^}}]*)\}}", text)
                if m:
                    bodies.append(m.group(1))
            if not bodies or any("..." in b for b in bodies):
                continue  # 펼침(...state)이 있으면 키를 알 수 없다 — 판단하지 않는다
            for body in bodies:
                keys |= {k.strip().split(":")[0].split("=")[0].strip() for k in body.split(",") if k.strip()}
            if keys:
                context_keys[name] = keys
    missing_members: list[str] = []
    if context_keys:
        for rel in files.files():
            if not rel.endswith((".jsx", ".tsx", ".js", ".ts")) or "/node_modules/" in f"/{rel}":
                continue
            text = _strip_js_comments(files.read(rel) or "")
            for names, ctx in re.findall(r"\b(?:const|let)\s*\{([^}]*)\}\s*=\s*useContext\(\s*([A-Z][\w$]*)\s*\)", text):
                keys = context_keys.get(ctx)
                if not keys:
                    continue
                for name in (n.strip().split(":")[0].split("=")[0].strip() for n in names.split(",") if n.strip()):
                    if name and not name.startswith("...") and name not in keys:
                        missing_members.append(f"`{name}`({rel} ← {ctx})")
    if missing_members:
        result.issues.append(ReadinessIssue(
            "NODE_CONTEXT_MEMBER_MISSING", ERROR,
            "useContext 로 꺼내는 이름이 그 Context 의 value 에 없습니다: " + ", ".join(missing_members[:6])
            + ". 화면이 \"… is not a function\" 으로 멈춥니다.",
            "Context 의 value 에 그 이름을 추가하거나 쓰는 쪽 이름을 맞추세요.",
            missing_members[0].split("(")[1].split(" ")[0]))

    # 7) react-router 앱에서 <a href="/..."> 로 이동하면 전체를 다시 불러와 장바구니 같은 상태가 사라진다
    anchor_files: list[str] = []
    for rel in files.files():
        if not rel.endswith((".jsx", ".tsx")) or "/node_modules/" in f"/{rel}":
            continue
        declared = manifests.get(owner(rel), set())
        if "react-router-dom" not in declared and "react-router-dom" not in deps:
            continue
        if router_link_rewrite(files.read(rel) or "") != (files.read(rel) or ""):
            anchor_files.append(rel)
    if anchor_files:
        result.fix_data["router_anchor_files"] = anchor_files
        result.issues.append(ReadinessIssue(
            "NODE_ROUTER_ANCHOR_LINK", WARNING,
            f"React Router 앱에서 <a href=\"/…\"> 로 이동합니다: {', '.join(anchor_files[:4])}. 페이지 전체가 다시 로드돼 "
            "장바구니 같은 화면 상태가 사라집니다.",
            "<Link to=\"/…\"> 로 바꾸세요(자동 수정 가능).", anchor_files[0], True))

    typos = {}
    imported_everywhere: set[str] = set()
    for rel in files.files():
        if rel.endswith(_JS_SUFFIXES) and "/node_modules/" not in f"/{rel}":
            body = _strip_js_comments(files.read(rel) or "")
            for pattern in _IMPORT_PATTERNS:
                imported_everywhere |= {p for p in (_package_of(x) for x in pattern.findall(body)) if p}
    for project, pkgs in undeclared.items():
        declared = manifests.get(project, set())
        for pkg, rel in pkgs.items():
            guess = _closest_declared(pkg, declared, imported_everywhere)
            if guess:
                typos[pkg] = (guess, rel)
    if typos:
        result.fix_data["package_typos"] = {k: v[0] for k, v in typos.items()}
        result.issues.append(ReadinessIssue(
            "NODE_IMPORT_PACKAGE_TYPO", ERROR,
            "import 한 패키지 이름이 선언된 패키지와 다릅니다: "
            + ", ".join(f"`{k}` → `{v[0]}`?({v[1]})" for k, v in list(typos.items())[:5])
            + ". 선언 안 된 이름은 설치되지 않아 빌드가 실패합니다.",
            "import 를 package.json 에 선언된 이름으로 고치세요(자동 수정 가능).", next(iter(typos.values()))[1], True))


# ---------------------------------------------------------------------------
# Node 모듈 형식(ESM·CommonJS) — AI 가 파일을 나눠 만들면 서로 다른 형식이 섞인다
# ---------------------------------------------------------------------------
#
# 실기기(TEMP 쇼핑몰): package.json 이 "type": "module" 이고 server.js 는 import 로 쓰였는데
# src/ 의 라우트·모델은 require/module.exports 로 만들어져 컨테이너가
# "does not provide an export named 'default'" 로 시작 직후 죽었다. develop·1.1.25 모두 같았다.
# 서버 진입 파일에서 실제로 불러오는 파일만 따라가 판정하므로 화면 코드(Vite)는 건드리지 않는다.

_ESM_STATEMENT = re.compile(
    r"""^[ \t]*(?:import\s+(?:[\w$*{][^;'"]*?\s+from\s+)?['"][^'"]+['"]|import\s*\{[^}]*\}\s*from\s*['"]|export\s+(?:default\b|async\s+function\b|function\b|class\b|const\b|let\b|var\b|\{|\*))""",
    re.MULTILINE)
_CJS_MARKER = re.compile(r"""\brequire\s*\(\s*['"][^'"]+['"]\s*\)|\bmodule\.exports\b|^[ \t]*exports\.[\w$]+\s*=""", re.MULTILINE)
_QUOTED_STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"")
_CREATE_REQUIRE = re.compile(r"[cC]reateRequire\s*\(")
_REL_SPEC_ANY = re.compile(
    r"""(\bfrom\s*|^[ \t]*import\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)(['"])(\.{1,2}/[^'"\n]*)\2""", re.MULTILINE)
#: require() 로 부를 수 없는 ESM 전용 패키지(이 major 이상). CommonJS 로 바꾸면 안 된다.
_ESM_ONLY_PACKAGES = {"node-fetch": 3, "chalk": 5, "nanoid": 4, "got": 12, "p-limit": 4, "ora": 6,
                      "execa": 6, "boxen": 6, "inquirer": 9, "log-symbols": 5, "strip-ansi": 7,
                      "string-width": 6, "camelcase": 7, "globby": 12, "del": 7, "open": 9, "lowdb": 2,
                      "query-string": 8, "file-type": 17, "pretty-bytes": 6, "mime": 4}


def _js_syntax(text: str) -> str:
    """'esm' | 'cjs' | 'mixed'(import 와 require 를 함께, createRequire 없이) | 'none'."""
    active = _strip_js_comments(text)
    active = re.sub(r"`(?:\\.|[^`\\])*`", "``", active)  # 템플릿 문자열 안의 코드 예시는 보지 않는다
    esm = bool(_ESM_STATEMENT.search(active))
    cjs = bool(_CJS_MARKER.search(active))
    if esm and cjs:
        return "esm" if _CREATE_REQUIRE.search(active) else "mixed"
    return "esm" if esm else ("cjs" if cjs else "none")


def _package_type_raw(files: "ProjectFiles", folder: str) -> str:
    """package.json 의 type 그대로("module" | "commonjs" | "" = 적지 않음)."""
    try:
        pkg = json.loads(files.read(f"{folder}/package.json" if folder else "package.json") or "")
    except ValueError:
        return ""
    value = pkg.get("type") if isinstance(pkg, dict) else None
    return value if value in ("module", "commonjs") else ""


def _dockerfile_node_is_old(files: "ProjectFiles") -> bool:
    """루트 Dockerfile 이 Node 22 미만 이미지를 쓰는가(없으면 ReCoder 기본 node:22 → False)."""
    text = files.read("Dockerfile") or ""
    majors = [int(m) for m in re.findall(r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?node:(\d+)", text)]
    return bool(majors) and majors[-1] < 22


def _package_type(files: "ProjectFiles", folder: str) -> str:
    try:
        pkg = json.loads(files.read(f"{folder}/package.json" if folder else "package.json") or "")
    except ValueError:
        return "commonjs"
    return "module" if isinstance(pkg, dict) and pkg.get("type") == "module" else "commonjs"


def _module_graph(files: "ProjectFiles", entry: str) -> list[str]:
    """진입 파일에서 상대 경로 import/require 로 닿는 JS 파일(진입 포함, 방문 순서)."""
    seen: list[str] = []
    queue = [entry]
    while queue and len(seen) < 400:
        rel = queue.pop(0)
        if rel in seen or not rel.endswith((".js", ".cjs", ".mjs")):
            continue
        text = files.read(rel)
        if text is None:
            continue
        seen.append(rel)
        for _kw, _q, spec in _REL_SPEC_ANY.findall(_strip_js_comments(text)):
            target = _resolve_local(files, rel, spec)
            if target and not target.startswith("..") and target not in seen:
                queue.append(target)
    return seen


def _relative_spec(importer: str, target: str) -> str:
    import posixpath
    spec = posixpath.relpath(target, posixpath.dirname(importer) or ".")
    return spec if spec.startswith("../") else "./" + spec


def module_spec_rewrite(text: str, importer: str, mapping: Mapping[str, str], files: "ProjectFiles",
                        esm_importer: bool) -> str:
    """importer 안의 상대 경로 import/require 중 mapping(옛 경로→새 경로)에 걸리는 것을 새 경로로.

    esm_importer=True 면 확장자 없는 경로(ESM 에서 ERR_MODULE_NOT_FOUND)도 실제 파일 이름으로 채운다.
    """
    def repl(m):
        kw, quote, spec = m.group(1), m.group(2), m.group(3)
        target = _resolve_local(files, importer, spec)
        if not target or target.startswith(".."):
            return m.group(0)
        new_target = mapping.get(target)
        if new_target is None:
            if not esm_importer or not target.endswith((".js", ".mjs", ".cjs", ".json")):
                return m.group(0)
            import posixpath
            if posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec)) == target:
                return m.group(0)  # 이미 정확한 파일 이름
            new_target = target
        return f"{kw}{quote}{_relative_spec(importer, new_target)}{quote}"

    return _REL_SPEC_ANY.sub(repl, text)


def esm_named_cjs_rewrite(text: str, cjs_targets: set[str], importer: str, files: "ProjectFiles") -> str:
    """ESM 이 CommonJS(.cjs) 파일에서 이름을 골라 가져오면 Node 가 이름을 못 찾을 수 있다 —
    기본 가져오기 + 구조 분해로 바꾼다(항상 module.exports 를 그대로 받는다)."""
    pattern = re.compile(r"""^([ \t]*)import\s+(?:([\w$]+)\s*,\s*)?\{([^}]*)\}\s*from\s*(['"])(\.{1,2}/[^'"]+)\4\s*;?""", re.MULTILINE)
    used: set[str] = set(re.findall(r"\b__recoder_[\w$]*", text))

    def repl(m):
        indent, default, names, quote, spec = m.groups()
        target = _resolve_local(files, importer, spec)
        if target not in cjs_targets:
            return m.group(0)
        parts = []
        sources = []
        for raw in names.split(","):
            raw = raw.strip()
            if not raw or raw.startswith("type "):
                continue
            src, _, alias = raw.partition(" as ")
            sources.append(src.strip())
            parts.append(f"{src.strip()}: {alias.strip()}" if alias.strip() else src.strip())
        try:
            target_text = files.read(target) or ""
        except Exception:  # noqa: BLE001
            target_text = ""
        if target_text and sources and set(sources) <= _lexer_exports(target_text):
            return m.group(0)  # Node 가 이름을 찾을 수 있다 — 그대로 둔다
        stem = "__recoder_" + re.sub(r"\W", "_", spec.rsplit("/", 1)[-1].split(".")[0])
        base, n = default or stem, 2
        while not default and base in used:
            base, n = f"{stem}_{n}", n + 1
        used.add(base)
        line = f"{indent}import {base} from {quote}{spec}{quote};"
        if parts:
            line += f"\n{indent}const {{ {', '.join(parts)} }} = {base};"
        return line

    return pattern.sub(repl, text)


def _js_code_mask(text: str) -> str:
    """주석·문자열·템플릿 리터럴을 공백으로 바꾼 같은 길이의 텍스트(구문 위치 판정용, 보수적)."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif c != "`" and text[j] == "\n":
                    break
                j += 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def _has_top_level_await(code: str) -> bool:
    """마스크된 코드에서 함수 밖 await(최상위 await)가 있는지. 확실하지 않으면 True 쪽으로 본다."""
    stack: list[bool] = []  # True = 함수 본문
    last_boundary = 0
    for m in re.finditer(r"[{};]|\bawait\b", code):
        tok = m.group(0)
        if tok == "{":
            prefix = code[last_boundary:m.start()]
            stack.append(bool(re.search(r"\bfunction\b|=>\s*$|\)\s*$", prefix)))
            last_boundary = m.end()
        elif tok == "}":
            if stack:
                stack.pop()
            last_boundary = m.end()
        elif tok == ";":
            last_boundary = m.end()
        else:
            if any(stack):
                continue
            prefix = code[last_boundary:m.start()]
            if re.search(r"\basync\b[^;]*=>", prefix):
                continue  # async () => await x (중괄호 없는 화살표 함수)
            return True
    return False


def _unsafe_for_cjs(text: str) -> bool:
    """esm_to_cjs 가 확실히 바꿀 수 없는 형태 — 최상위 await, import 속성, 여러 이름 export, 템플릿 속 import 줄."""
    code = _js_code_mask(text)
    if _has_top_level_await(code):
        return True
    if re.search(r"""\bfrom\s*['"][^'"]+['"]\s*(?:with|assert)\s*\{""", text):
        return True
    for m in re.finditer(r"^[ \t]*export\s+(?:const|let|var)\s+[^\n]*", code, re.MULTILINE):
        depth = 0
        for ch in m.group(0):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                return True
    #: import/export 로 시작하는 줄이 템플릿·문자열 안에 있으면(마스크에서 사라짐) 정규식 변환이 그 줄을 바꾼다
    for m in re.finditer(r"^[ \t]*(?:import|export)\b", text, re.MULTILINE):
        if not code[m.start():m.end()].strip():
            return True
    return False


def esm_to_cjs(text: str) -> Optional[str]:
    """단순한 ESM 파일을 CommonJS 로 바꾼다. 확실히 바꿀 수 없는 형태면 None.

    바꾸는 것: import(기본·이름·네임스페이스·부수효과), export default/const/function/class/{…},
    import.meta.url·dirname·filename. 못 바꾸는 것: 최상위 await, export * / export … from.
    """
    if re.search(r"^export\s*\*|^export\s*\{[^}]*\}\s*from\b|^await\s|^(?:const|let|var)\s[^\n]*=\s*await\s", text, re.MULTILINE):
        return None
    if _unsafe_for_cjs(text):
        return None
    for pkg, major in _ESM_ONLY_PACKAGES.items():
        if re.search(rf"""from\s*['"]{re.escape(pkg)}['"]""", text):
            return None  # ESM 전용 패키지 — require 로 부를 수 없다
    newline = "\r\n" if "\r\n" in text else "\n"
    exported: list[tuple[str, str]] = []  # (지역 이름, 내보내는 이름)
    default_tail: list[str] = []

    def imp(m):
        indent, clause, quote, spec = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        req = f"require({quote}{spec}{quote})"
        default, named, ns = None, None, None
        mm = re.match(r"^([\w$]+)?\s*,?\s*(?:\{([^}]*)\}|\*\s+as\s+([\w$]+))?$", clause, re.DOTALL)
        if not mm:
            raise ValueError(clause)
        default, named, ns = mm.group(1), mm.group(2), mm.group(3)
        out = []
        if default:
            out.append(f"{indent}const {default} = {req};")
        if ns:
            out.append(f"{indent}const {ns} = {req};")
        if named is not None:
            parts = []
            for raw in named.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                if raw.startswith("type "):
                    continue
                src, _, alias = raw.partition(" as ")
                parts.append(f"{src.strip()}: {alias.strip()}" if alias.strip() else src.strip())
            if parts:
                out.append(f"{indent}const {{ {', '.join(parts)} }} = {default or req};")
        return newline.join(out)

    try:
        text = re.sub(r"""^([ \t]*)import\s+((?:[\w$]+\s*,?\s*)?(?:\{[^}]*\}|\*\s+as\s+[\w$]+)?)\s*from\s*(['"])([^'"]+)\3\s*;?""",
                      imp, text, flags=re.MULTILINE)
    except ValueError:
        return None
    text = re.sub(r"""^([ \t]*)import\s*(['"])([^'"]+)\2\s*;?""", r"\1require(\2\3\2);", text, flags=re.MULTILINE)
    if re.search(r"^[ \t]*import\s+[\w${*]", text, re.MULTILINE):
        return None  # 해석하지 못한 import 가 남았다

    def named_decl(m):
        kind, name = m.group(2), m.group(3)
        exported.append((name, name))
        return f"{m.group(1)}{kind} {name}"

    text = re.sub(r"^([ \t]*)export\s+(const|let|var)\s+([\w$]+)", named_decl, text, flags=re.MULTILINE)
    text = re.sub(r"^([ \t]*)export\s+(async\s+function\*?|function\*?|class)\s+([\w$]+)", named_decl, text, flags=re.MULTILINE)

    def export_list(m):
        for raw in m.group(2).split(","):
            raw = raw.strip()
            if not raw:
                continue
            local, _, alias = raw.partition(" as ")
            local, alias = local.strip(), (alias.strip() or local.strip())
            if alias == "default":
                default_tail.append(f"module.exports = {local};")
            else:
                exported.append((local, alias))
        return m.group(1).rstrip()

    text = re.sub(r"^([ \t]*)export\s*\{([^}]*)\}\s*;?", export_list, text, flags=re.MULTILINE)

    def default_decl(m):
        kind, name = m.group(2), m.group(3)
        default_tail.append(f"module.exports = {name};")
        return f"{m.group(1)}{kind} {name}"

    text = re.sub(r"^([ \t]*)export\s+default\s+(async\s+function\*?|function\*?|class)\s+([\w$]+)", default_decl, text, flags=re.MULTILINE)
    text = re.sub(r"^([ \t]*)export\s+default\s+", r"\1module.exports = ", text, flags=re.MULTILINE)
    if re.search(r"^[ \t]*export\b", text, re.MULTILINE):
        return None
    # import.meta — CommonJS 에는 __filename·__dirname 이 이미 있다(다시 선언하면 SyntaxError)
    text = re.sub(r"\b(?:url\.)?fileURLToPath\(\s*import\.meta\.url\s*\)", "__filename", text)
    text = text.replace("import.meta.dirname", "__dirname").replace("import.meta.filename", "__filename")
    text = text.replace("import.meta.url", "require('url').pathToFileURL(__filename).href")
    if "import.meta" in text:
        return None
    text = re.sub(r"^[ \t]*(?:const|let|var)\s+__filename\s*=\s*__filename\s*;?[ \t]*(?:\r?\n)", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*(?:const|let|var)\s+__dirname\s*=\s*(?:path\.)?dirname\(\s*__filename\s*\)\s*;?[ \t]*(?:\r?\n)", "", text, flags=re.MULTILINE)
    if re.search(r"^[ \t]*(?:const|let|var)\s+__(?:filename|dirname)\b", text, re.MULTILINE):
        return None
    tail = list(default_tail)
    if exported:
        target = "module.exports" if default_tail else "module.exports"
        tail += [f"{target}.{alias} = {local};" for local, alias in exported]
    if tail:
        text = text.rstrip() + newline + newline + newline.join(tail) + newline
    return text


_ESM_DIRNAME_SHIM = ("import { fileURLToPath as __recoderFileURLToPath } from 'url';{nl}"
                     "import {{ dirname as __recoderDirname }} from 'path';{nl}"
                     "const __filename = __recoderFileURLToPath(import.meta.url);{nl}"
                     "const __dirname = __recoderDirname(__filename);{nl}")


def esm_dirname_shim(text: str) -> str:
    """ESM 파일이 선언 없이 __dirname/__filename 을 쓰면 ReferenceError — 마지막 import 뒤에 정의를 넣는다."""
    newline = "\r\n" if "\r\n" in text else "\n"
    shim = (f"import {{ fileURLToPath as __recoderFileURLToPath }} from 'url';{newline}"
            f"import {{ dirname as __recoderDirname }} from 'path';{newline}"
            f"const __filename = __recoderFileURLToPath(import.meta.url);{newline}"
            f"const __dirname = __recoderDirname(__filename);{newline}")
    #: 맨 위(셔뱅 다음)에 둔다 — import 는 끌어올려지고 import.meta.url 은 처음부터 쓸 수 있다. 예전엔 "마지막 import
    #: 뒤"를 정규식으로 찾다가 줄 끝 주석이 붙은 import 에서 함수 본문 안에 끼워 넣어 SyntaxError 가 났다.
    at = text.index("\n") + 1 if text.startswith("#!") and "\n" in text else 0
    return text[:at] + shim + text[at:]


def esm_require_shim(text: str) -> str:
    """ESM 파일이 require 를 쓰면 ReferenceError — createRequire 로 require 를 정의한다(마지막 import 뒤)."""
    if re.search(r"\bconst\s+require\s*=", text):
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    shim = (f"import {{ createRequire as __recoderCreateRequire }} from 'module';{newline}"
            f"const require = __recoderCreateRequire(import.meta.url);{newline}")
    #: import 는 끌어올려지므로 맨 위에 둬도 된다 — require 를 import 보다 먼저 쓰는 파일도 동작한다.
    at = text.index("\n") + 1 if text.startswith("#!") and "\n" in text else 0
    return text[:at] + shim + text[at:]


def _lexer_exports(text: str) -> set[str]:
    """Node(cjs-module-lexer)가 ESM 이름 가져오기로 내줄 수 있는 CommonJS 이름 — 보수적으로 좁게 본다."""
    active = _strip_js_comments(text)
    names = set(re.findall(r"\b(?:module\.)?exports\.([\w$]+)\s*=", active))
    for body in re.findall(r"\bmodule\.exports\s*=\s*\{([^{}]*)\}", active):
        for part in body.split(","):
            part = part.strip()
            m = re.fullmatch(r"""([\w$]+)|['"]?([\w$]+)['"]?\s*:\s*[\w$]+""", part)
            if not part:
                continue
            if not m:
                break  # 식별자가 아닌 값이 나오면 lexer 가 거기서 멈춘다
            names.add(m.group(1) or m.group(2))
    names.add("default")
    return names


def _uses_undeclared_dirname(text: str) -> bool:
    active = _strip_js_comments(text)
    uses = re.search(r"\b__(?:dirname|filename)\b", active)
    declared = re.search(r"\b(?:const|let|var)\s+__(?:dirname|filename)\b|\bfunction\s+__dirname\b", active)
    return bool(uses and not declared)


def _node_entry(files: "ProjectFiles", scripts: dict, main) -> Optional[str]:
    entry = _start_entry(scripts)
    if entry and files.exists(entry):
        return entry
    if isinstance(main, str) and files.exists(_norm(main)) and _norm(main).endswith((".js", ".mjs", ".cjs")):
        return _norm(main)
    for rel in ("server.js", "index.js", "app.js", "src/server.js", "src/index.js", "src/app.js",
                "server/index.js", "server/server.js", "backend/server.js", "backend/index.js"):
        text = files.read(rel)
        if text and _is_server_file(text):
            return rel
    return None


def module_format_plan(files: "ProjectFiles", entry: Optional[str], owner) -> Optional[dict]:
    """서버 진입 파일부터 불러오는 파일들의 모듈 형식이 어긋나면 고칠 계획을 돌려준다(없으면 None).

    반환: {"problems": [문장], "auto": bool, "why_not": str, "writes": {경로: 내용}, "renames": [(옛, 새)],
           "files": [관련 파일]}. writes 는 새 경로 기준 최종 내용이다(renames 의 옛 경로는 지운다).
    """
    if not entry or not entry.endswith((".js", ".mjs", ".cjs")):
        return None
    graph = _module_graph(files, entry)
    if not graph:
        return None
    scope = owner(entry)
    pkg_type = _package_type(files, scope)
    #: package.json 에 type 이 없으면 Node 22(ReCoder Dockerfile 기본)는 파일마다 문법을 보고 ESM·CommonJS 를
    #: 정한다(module syntax detection). 그때는 import 를 쓴 .js 도 정상이다 — 예전 Node(22 미만) 이미지만 아니면.
    detect = _package_type_raw(files, scope) == "" and not _dockerfile_node_is_old(files)
    syntax: dict[str, str] = {}

    def mode(rel: str) -> str:
        if rel.endswith(".mjs"):
            return "esm"
        if rel.endswith(".cjs"):
            return "cjs"
        raw = _package_type_raw(files, owner(rel))
        if raw == "module":
            return "esm"
        if raw == "" and detect:
            kind = syntax.get(rel) or _js_syntax(files.read(rel) or "")
            return "esm" if kind in ("esm", "mixed") else "cjs"
        return "cjs"

    syntax.update({rel: _js_syntax(files.read(rel) or "") for rel in graph})
    #: import 와 require 를 함께 쓰는 파일은 ESM 으로 본다 — ESM 이면 createRequire 로 require 를 만들어 주고,
    #: CommonJS 로 가면 import 를 require 로 바꾼다. 어느 쪽이든 확실히 고칠 수 있다.
    mixed_files = {rel for rel, kind in syntax.items() if kind == "mixed"}
    mixed = sorted(mixed_files)
    for rel in mixed:
        syntax[rel] = "esm"
    wrong = [rel for rel in graph if syntax[rel] in ("esm", "cjs") and syntax[rel] != mode(rel)]
    esm_files = [rel for rel in graph if mode(rel) == "esm" and syntax[rel] == "esm"] + \
                [rel for rel in wrong if syntax[rel] == "esm"]
    # ESM 으로 실행될 파일의 확장자 없는 상대 경로(ERR_MODULE_NOT_FOUND)와 선언 없는 __dirname
    problems: list[str] = []
    if wrong:
        shown = ", ".join(f"{r}({'import/export' if syntax[r] == 'esm' else 'require/module.exports'})" for r in wrong[:5])
        declared = 'ESM("type": "module")' if pkg_type == "module" else "CommonJS"
        problems.append(f"package.json 은 {declared} 인데 서버가 불러오는 파일의 형식이 다릅니다: {shown}")
    if mixed:
        problems.append(f"한 파일에 import 와 require 가 섞여 있습니다: {', '.join(mixed[:5])}")
    #: CommonJS 파일이 ESM 파일을 require 하면 Node 22 는 모듈 객체({ default })를 돌려줘 값이 undefined 가 되고,
    #: 예전 Node 는 ERR_REQUIRE_ESM 으로 멈춘다.
    required_esm: list[str] = []
    for rel in graph:
        if mode(rel) != "cjs":
            continue
        for spec in re.findall(r"""\brequire\s*\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""", _strip_js_comments(files.read(rel) or "")):
            tgt = _resolve_local(files, rel, spec)
            if tgt in syntax and tgt.endswith(".js") and mode(tgt) == "esm" and tgt not in required_esm:
                required_esm.append(tgt)
    if detect and required_esm:
        problems.append("CommonJS 파일이 ESM 파일을 require 합니다(값이 undefined 가 됨): " + ", ".join(required_esm[:5]))

    # 목표 형식: 진입 파일이 쓴 형식을 따른다(package.json 의 type 보다 코드가 사용자의 의도에 가깝다).
    entry_syntax = syntax.get(entry, "none")
    target = "esm" if (entry_syntax == "esm" or entry.endswith(".mjs")) else \
        ("cjs" if entry_syntax == "cjs" or entry.endswith(".cjs") else ("esm" if pkg_type == "module" else "cjs"))

    after_mode = {rel: mode(rel) for rel in graph}
    writes: dict[str, str] = {}
    renames: list[tuple[str, str]] = []
    why_not = ""
    manifest = f"{scope}/package.json" if scope else "package.json"
    foreign = [r for r in wrong + mixed if owner(r) != scope]
    if foreign:
        why_not = f"다른 package.json 아래 파일({foreign[0]})이라 자동으로 고치지 않습니다."
    elif detect:
        #: type 을 건드리지 않는다 — require 로 불리는 ESM 파일만 CommonJS 로 바꾼다.
        for rel in required_esm:
            converted = esm_to_cjs(files.read(rel) or "")
            if converted is None:
                why_not = f"{rel} 을(를) CommonJS 로 확실히 바꿀 수 없습니다(최상위 await·재수출·ESM 전용 패키지)."
                break
            writes[rel] = converted
        after_mode = {rel: ("cjs" if rel in writes else after_mode[rel]) for rel in graph}
    elif wrong and target == "esm":
        if pkg_type != "module":
            #: type 을 module 로 바꾸면 지금은 맞던 CommonJS .js 파일도 ESM 으로 해석된다 — 함께 .cjs 로.
            wrong += [r for r in graph if r.endswith(".js") and syntax[r] == "cjs" and r not in wrong]
            raw = files.read(manifest) or ""
            try:
                pkg = json.loads(raw)
                pkg["type"] = "module"
                newline = "\r\n" if "\r\n" in raw else "\n"
                writes[manifest] = json.dumps(pkg, ensure_ascii=False, indent=2).replace("\n", newline) + newline
            except (ValueError, TypeError):
                why_not = "package.json 을 읽지 못했습니다."
            #: type 을 바꾸면 그래프 밖의 CommonJS 설정 파일(tailwind.config.js 등)도 ESM 으로 해석된다 — 같이 .cjs 로.
            for rel in files.files():
                if owner(rel) == scope and rel.endswith(".js") and rel not in graph and "/node_modules/" not in f"/{rel}" \
                        and not rel.startswith(_BROWSER_DIRS) and _js_syntax(files.read(rel) or "") == "cjs":
                    wrong.append(rel)
            after_mode = {rel: ("esm" if rel.endswith(".js") else after_mode[rel]) for rel in graph}
        cjs_wrong = [r for r in wrong if (syntax.get(r) or _js_syntax(files.read(r) or "")) == "cjs"]
        esm_needed_by_cjs = []
        for rel in cjs_wrong:
            for _kw, _q, spec in _REL_SPEC_ANY.findall(_strip_js_comments(files.read(rel) or "")):
                tgt = _resolve_local(files, rel, spec)
                if tgt in graph and tgt not in cjs_wrong and syntax.get(tgt) == "esm":
                    esm_needed_by_cjs.append(tgt)
        if esm_needed_by_cjs:
            why_not = f"CommonJS 파일이 ESM 파일({esm_needed_by_cjs[0]})을 require 합니다 — 자동으로 고치지 않습니다."
        else:
            renames = [(r, r[:-3] + ".cjs") for r in cjs_wrong if not files.exists(r[:-3] + ".cjs")]
    elif wrong and target == "cjs":
        #: type 을 빼면 지금은 맞던 ESM .js 파일도 CommonJS 로 해석된다 — 함께 require 로 바꾼다.
        esm_wrong = [r for r in graph if syntax[r] == "esm" and (r in wrong or (pkg_type == "module" and r.endswith(".js")))]
        for rel in esm_wrong:
            converted = esm_to_cjs(files.read(rel) or "")
            if converted is None:
                why_not = f"{rel} 을(를) CommonJS 로 확실히 바꿀 수 없습니다(최상위 await·재수출·ESM 전용 패키지)."
                break
            writes[rel] = converted
        if not why_not and pkg_type == "module":
            raw = files.read(manifest) or ""
            try:
                pkg = json.loads(raw)
                pkg.pop("type", None)
                newline = "\r\n" if "\r\n" in raw else "\n"
                writes[manifest] = json.dumps(pkg, ensure_ascii=False, indent=2).replace("\n", newline) + newline
            except (ValueError, TypeError):
                why_not = "package.json 을 읽지 못했습니다."
            # type 을 빼면 그래프 밖의 ESM .js(설정·스크립트)가 CommonJS 로 해석된다 — .mjs 로.
            #: 번들러(Vite 등)가 읽는 화면 소스·설정은 package type 과 무관하다 — 이름을 바꾸면 index.html 의
            #: <script src="/src/main.js"> 가 깨진다.
            bundled_roots = tuple((f"{folder}/src/" if folder else "src/") for folder in _frontend_projects(files))
            for rel in files.files():
                if owner(rel) == scope and rel.endswith(".js") and rel not in graph and "/node_modules/" not in f"/{rel}" \
                        and not rel.startswith(_BROWSER_DIRS) and _js_syntax(files.read(rel) or "") == "esm" \
                        and not (bundled_roots and rel.startswith(bundled_roots)) \
                        and not re.search(r"(?:^|/)vite\.config\.js$", rel) \
                        and not files.exists(rel[:-3] + ".mjs"):
                    renames.append((rel, rel[:-3] + ".mjs"))
        after_mode = {rel: ("cjs" if rel.endswith(".js") else after_mode[rel]) for rel in graph}

    # 새 형식에서 ESM 으로 실행되는 파일: 확장자 없는 상대 경로 · 선언 없는 __dirname
    rename_map = dict(renames)
    ext_missing: list[str] = []
    dirname_files: list[str] = []
    named_from_cjs: list[str] = []
    final_modes: dict[str, str] = {}
    for rel in graph:
        new_rel = rename_map.get(rel, rel)
        final_modes[rel] = "cjs" if new_rel.endswith(".cjs") else ("esm" if new_rel.endswith(".mjs") else
                                                                     ("cjs" if rel in writes else after_mode.get(rel, mode(rel))))
    for rel in graph:
        if final_modes[rel] != "esm":
            continue
        text = writes.get(rel, files.read(rel) or "")
        for _kw, _q, spec in _REL_SPEC_ANY.findall(_strip_js_comments(text)):
            import posixpath
            tgt = _resolve_local(files, rel, spec)
            if tgt and not tgt.startswith("..") and posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec)) != tgt:
                ext_missing.append(f"{rel} → '{spec}'")
        if _uses_undeclared_dirname(text):
            dirname_files.append(rel)
        #: ESM 이 CommonJS 파일에서 이름으로 가져오면 Node 는 정적으로 찾을 수 있는 이름만 준다
        #: (module.exports = { greet: () => … } 는 못 찾는다 → "Named export not found").
        for names, spec in _ESM_NAMED_IMPORT.findall(_strip_js_comments(text)):
            tgt = _resolve_local(files, rel, spec)
            if tgt in final_modes and final_modes[tgt] == "cjs":
                wanted = {n.strip().split(" as ")[0].strip() for n in names.split(",") if n.strip() and not n.strip().startswith("type ")}
                if not wanted <= _lexer_exports(files.read(tgt) or ""):
                    named_from_cjs.append(f"{rel} ← {tgt}")
    if named_from_cjs:
        problems.append("ESM 이 CommonJS 파일에서 Node 가 찾지 못하는 이름을 가져옵니다(Named export not found): "
                        + ", ".join(named_from_cjs[:4]))
    if ext_missing:
        problems.append("ESM 은 상대 경로에 파일 확장자가 있어야 합니다(ERR_MODULE_NOT_FOUND): " + ", ".join(ext_missing[:4]))
    if dirname_files:
        problems.append("ESM 파일에는 __dirname·__filename 이 없습니다(ReferenceError): " + ", ".join(dirname_files[:4]))
    if not problems:
        return None
    #: import 와 module.exports/exports.x 를 함께 쓰는 파일은 require 만 만들어 줘도 ESM 에서 module 이 없어 죽는다
    #: (예전: createRequire 만 넣고 "고쳤다"고 보고). 어느 형식으로 정리할지 정해야 하므로 자동으로 고치지 않는다.
    mixed_exports = [rel for rel in mixed if re.search(r"\bmodule\.exports\b|(?<![\w$.])exports\.[\w$]+\s*=|\brequire\.main\b",
                                                       _strip_js_comments(files.read(rel) or ""))]
    if mixed_exports and not why_not:
        why_not = (f"{mixed_exports[0]} 이(가) import 와 module.exports 를 함께 씁니다 — 한 형식(import/export 또는 "
                   "require/module.exports)으로 정리해야 해서 자동으로 고치지 않습니다.")
    if why_not:
        return {"problems": problems, "auto": False, "why_not": why_not, "writes": {}, "renames": [],
                "files": (wrong + mixed)[:1] or [entry]}

    # 내용 계산: 모든 스코프 파일에서 경로를 새 이름으로, ESM 파일은 확장자를 채우고 .cjs 의 이름 가져오기를 구조 분해로
    cjs_old = {rel for rel, kind in final_modes.items() if kind == "cjs"}
    touched = set(graph) | {old for old, _new in renames}
    for rel in files.files():
        if owner(rel) == scope and rel.endswith(_JS_SUFFIXES) and "/node_modules/" not in f"/{rel}" and rel not in touched:
            body = files.read(rel) or ""
            if any(old.rsplit("/", 1)[-1][:-3] in body for old, _new in renames):
                touched.add(rel)
    for rel in sorted(touched):
        original = files.read(rel)
        if original is None:
            continue
        text = writes.get(rel, original)
        new_rel = rename_map.get(rel, rel)
        esm = new_rel.endswith(".mjs") or (new_rel.endswith(".js") and rel not in writes and
                                          after_mode.get(rel, mode(rel)) == "esm")
        if esm:
            #: 원래 경로로 판정해야 하므로 경로를 바꾸기 전에 한다
            text = esm_named_cjs_rewrite(text, cjs_old, rel, files)
        text = module_spec_rewrite(text, rel, rename_map, files, esm_importer=esm)
        if esm:
            if rel in mixed_files:
                text = esm_require_shim(text)
            if rel in dirname_files:
                text = esm_dirname_shim(text)
        if new_rel != rel:
            writes.pop(rel, None)
            writes[new_rel] = text
        elif text != original:
            writes[rel] = text
    # package.json scripts 가 이름이 바뀐 파일을 직접 실행하면 함께 바꾼다
    if renames:
        raw = writes.get(manifest, files.read(manifest) or "")
        updated = raw
        for old, new in renames:
            local_old, local_new = (old[len(scope) + 1:], new[len(scope) + 1:]) if scope else (old, new)
            updated = re.sub(rf"(?<![\w./-]){re.escape(local_old)}(?![\w.-])", local_new, updated)
        if updated != raw:
            writes[manifest] = updated
    return {"problems": problems, "auto": True, "why_not": "", "writes": writes, "renames": renames,
            "files": [entry]}


# ---------------------------------------------------------------------------
# 서버가 제공하는 화면 폴더가 Docker 빌드에서 만들어지는가
# ---------------------------------------------------------------------------
#
# 실기기(TEMP 쇼핑몰): 루트 package.json 에 `build:client` 만 있고 `build` 가 없어 Dockerfile 의
# `npm run build --if-present` 가 화면을 빌드하지 않았다. 서버는 client/dist 를 제공하도록 돼
# 있어 주소는 열리지만 화면이 나오지 않았다(ENOENT index.html).

def _join_args(args: str, base_dir: str) -> Optional[str]:
    """`__dirname, '..', 'client', 'dist'` → 파일 기준으로 푼 프로젝트 상대 경로."""
    import posixpath
    parts = [a.strip() for a in args.split(",")]
    if not parts:
        return None
    start = base_dir
    if parts[0] in ("__dirname", "import.meta.dirname"):
        parts = parts[1:]
    elif parts[0] in ("process.cwd()",):
        start, parts = "", parts[1:]
    else:
        start = ""  # 상대 문자열은 작업 폴더(= 프로젝트 루트) 기준
    pieces = []
    for part in parts:
        m = re.fullmatch(r"""(['"`])([^'"`$]*)\1""", part)
        if not m:
            return None
        pieces.append(m.group(2))
    joined = posixpath.normpath(posixpath.join(start or ".", *pieces)) if pieces else (start or ".")
    return None if joined.startswith("..") else ("" if joined == "." else joined)


def _served_dirs(files: "ProjectFiles", server_files: Iterable[str]) -> dict[str, str]:
    """서버 코드가 제공하는 정적 폴더 → 그 코드가 있는 파일."""
    import posixpath
    out: dict[str, str] = {}
    for rel in server_files:
        text = _strip_js_comments(files.read(rel) or "")
        base = posixpath.dirname(rel)
        #: const clientDist = path.join(__dirname, 'client/dist') 처럼 변수에 담아 쓰는 경우
        variables = {}
        for name, args in re.findall(r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*path\.(?:join|resolve)\(\s*([^()]*)\)", text):
            got = _join_args(args, base)
            if got is not None:
                variables[name] = got
        for m in re.finditer(r"(?:express|serveStatic|static)\.?\w*\(\s*(?:path\.(?:join|resolve)\(\s*([^()]*)\)|(['\"])([^'\"]+)\2|([\w$]+)\s*[,)])", text):
            if not re.match(r"express\.static|serveStatic|static\(", m.group(0)) and "static" not in m.group(0):
                continue
            got = _join_args(m.group(1), base) if m.group(1) else (
                _norm(m.group(3)) if m.group(3) else variables.get(m.group(4) or ""))
            if got:
                out.setdefault(got, rel)
        for m in re.finditer(r"sendFile\(\s*path\.(?:join|resolve)\(\s*([^()]*)\)", text):
            got = _join_args(m.group(1), base)
            if got and got.endswith(".html"):
                out.setdefault(posixpath.dirname(got), rel)
        for m in re.finditer(r"sendFile\(\s*path\.(?:join|resolve)\(\s*([\w$]+)\s*,\s*['\"]index\.html['\"]\s*\)", text):
            if m.group(1) in variables:
                out.setdefault(variables[m.group(1)], rel)
    return {k: v for k, v in out.items() if k}


def _vite_out_dir(files: "ProjectFiles", folder: str) -> Optional[str]:
    import posixpath
    for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs", "vite.config.mts"):
        text = files.read(f"{folder}/{name}" if folder else name)
        if text:
            m = re.search(r"""\boutDir\s*:\s*['"]([^'"]+)['"]""", _strip_js_comments(text))
            if m:
                joined = posixpath.normpath(posixpath.join(folder or ".", m.group(1)))
                return None if joined.startswith("..") else joined
            root = re.search(r"""\broot\s*:\s*['"]([^'"]+)['"]""", _strip_js_comments(text))
            if root:
                return posixpath.normpath(posixpath.join(folder or ".", root.group(1), "dist"))
    return None


def _is_frontend_build(command: str) -> bool:
    for pattern, _out in _FRONTEND_BUILDS:
        if pattern.search(command) and ("vite" not in pattern.pattern or re.search(r"\bvite\s+build\b", command)):
            return True
    return False


def _frontend_projects(files: "ProjectFiles") -> dict[str, tuple[str, str]]:
    """프런트엔드 프로젝트 폴더("" = 루트) → (빌드 결과 폴더, 빌드를 실행하는 스크립트 이름)."""
    out: dict[str, tuple[str, str]] = {}
    for rel in ["package.json"] + [r for r in files.files() if r.endswith("/package.json") and "/node_modules/" not in f"/{r}"]:
        folder = "" if rel == "package.json" else rel[: -len("/package.json")]
        try:
            pkg = json.loads(files.read(rel) or "")
        except ValueError:
            continue
        scripts = pkg.get("scripts") if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) else {}
        order = ["build"] + sorted(n for n in scripts if n != "build")
        for name in order:
            if name.startswith(("dev", "start", "serve", "preview", "test", "watch", "lint")):
                continue
            command = str(scripts.get(name, ""))
            if not _is_frontend_build(command):
                continue
            pattern_out = next(o for p, o in _FRONTEND_BUILDS if p.search(command))
            out_dir = (_vite_out_dir(files, folder) if re.search(r"\bvite\b", command) else None) or \
                (f"{folder}/{pattern_out}" if folder else pattern_out)
            out[folder] = (out_dir, name)
            break
    return out


def _script_runs(scripts: dict, files: "ProjectFiles", name: str) -> tuple[set[str], bool]:
    """(이 스크립트가 들어가 빌드하는 하위 폴더, 루트에서 프런트엔드 빌드 도구를 직접 실행하는가)."""
    runs: set[str] = set()
    direct = False

    def visit(script: str, depth: int) -> None:
        nonlocal direct
        cwd = ""
        for words in _script_commands(str(scripts.get(script, ""))):
            if words[0] == "cd" and len(words) > 1:
                cwd = _norm(words[1].strip("'\""))
                continue
            if words[0] in {"npm", "yarn", "pnpm"}:
                flag_dir, rest = _npm_dir_and_args(words)
                target = flag_dir or cwd
                installing = rest[:1] in (["install"], ["i"], ["ci"]) or (words[0] == "yarn" and not rest)
                if target and target not in {".", ""}:
                    if not installing:
                        runs.add(target)
                elif not installing and depth < 3:
                    sub = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else (rest[0] if rest else "")
                    if sub in scripts and sub != script:
                        visit(sub, depth + 1)
            elif not cwd and _is_frontend_build(" ".join(words)):
                direct = True

    if name in scripts:
        visit(name, 0)
    return runs, direct


def _vite_config_file(files: "ProjectFiles", folder: str) -> Optional[str]:
    for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs", "vite.config.mts"):
        rel = f"{folder}/{name}" if folder else name
        if files.exists(rel):
            return rel
    return None


def vite_out_dir_rewrite(text: str, new_out: str) -> Optional[str]:
    """vite.config 의 build.outDir 를 new_out 으로. outDir 가 없으면 build 블록에 넣는다. 확실하지 않으면 None."""
    if re.search(r"""\boutDir\s*:\s*['"][^'"]*['"]""", text):
        return re.sub(r"""(\boutDir\s*:\s*)(['"])[^'"]*\2""", lambda m: f"{m.group(1)}{m.group(2)}{new_out}{m.group(2)}", text, count=1)
    if re.search(r"\boutDir\s*:", text):
        return None  # 변수·식으로 정한 outDir 는 건드리지 않는다
    if re.search(r"\bbuild\s*:\s*\{", text):
        return re.sub(r"(\bbuild\s*:\s*\{)", lambda m: f"{m.group(1)} outDir: '{new_out}',", text, count=1)
    m = re.search(r"defineConfig\(\s*\{", text) or re.search(r"export\s+default\s+\{", text)
    if not m:
        return None
    return text[:m.end()] + f"\n  build: {{ outDir: '{new_out}' }}," + text[m.end():]


def frontend_build_plan(files: "ProjectFiles", scripts: dict, server_files: list[str]) -> Optional[dict]:
    """서버가 제공하는 화면 폴더를 Docker 빌드(npm run build)가 만들지 않으면 고칠 계획을 돌려준다.

    두 가지를 본다. (1) 루트 build 가 화면을 빌드하지 않는다(build:client 만 있음 — 실기기 TEMP).
    (2) 화면 빌드 결과 폴더와 서버가 제공하는 폴더가 다르다(vite outDir '../dist' 인데 서버는
    frontend/dist 를 제공 — 실기기 생성 쇼핑몰). 둘 다 주소는 열리는데 화면이 없다.
    """
    import posixpath
    served = _served_dirs(files, server_files)
    if not served:
        return None
    projects = _frontend_projects(files)
    build_runs, build_direct = _script_runs(scripts, files, "build")
    candidates = []
    for folder, (out_dir, script_name) in projects.items():
        if out_dir in served:
            candidates.append((folder, out_dir, script_name, None))
    if not candidates:
        #: 서버가 제공하는 폴더가 저장소에 없고(빌드 결과), 그 폴더를 만드는 프런트엔드가 없다 —
        #: 프런트엔드가 하나뿐이면 그 빌드 결과 폴더를 서버가 제공하는 폴더로 맞춘다(Vite 만).
        #: 빌드 결과 폴더처럼 보이는 것만(dist·build·public 등) — uploads·data 같은 실행 중 파일 폴더로
        #: outDir 를 돌리면 빌드가 그 폴더를 비운다.
        missing = [d for d in served if not files.has_dir(d)
                   and re.fullmatch(r"(?:[\w.-]*[-_])?(?:dist|build|public|out|www|static|client|frontend|web)",
                                    posixpath.basename(d.rstrip("/")) or "", re.I)]
        if len(projects) == 1 and len(missing) == 1:
            folder, (out_dir, script_name) = next(iter(projects.items()))
            config = _vite_config_file(files, folder) if re.search(r"\bvite\s+build\b", str(
                ((json.loads(files.read(f"{folder}/package.json" if folder else "package.json") or "{}").get("scripts") or {})
                 .get(script_name, "")))) else None
            if config:
                target = missing[0]
                rel = posixpath.relpath(target, folder or ".")
                new_text = vite_out_dir_rewrite(files.read(config) or "", rel)
                if new_text is not None:
                    candidates.append((folder, target, script_name, (config, out_dir, rel, new_text)))
    for folder, out_dir, script_name, vite_fix in candidates:
        if files.exists(f"{out_dir}/index.html"):
            continue  # 빌드 결과를 저장소에 함께 둔 프로젝트
        built = (folder and folder in build_runs) or (not folder and (build_direct or (script_name == "build")))
        if built and not vite_fix:
            continue
        new_build = None
        runner = None
        if not built:
            # 루트에서 그 화면을 빌드하는 스크립트를 찾는다(build:client 등)
            for name in sorted(scripts):
                if name == "build" or name.startswith(("pre", "post", "dev", "start", "test", "watch")):
                    continue
                runs, direct = _script_runs(scripts, files, name)
                if (folder and folder in runs) or (not folder and direct):
                    runner = name
                    break
            if runner:
                command = f"npm run {runner}"
            elif folder:
                command = f"npm --prefix {folder} run {script_name}"
            else:
                command = f"npm run {script_name}"
            existing = str(scripts.get("build") or "").strip()
            new_build = f"{command} && {existing}" if existing else command
        plan = {"folder": folder, "out_dir": out_dir, "server_file": served[out_dir], "runner": runner,
                "build": new_build}
        if vite_fix:
            config, old_out, rel, new_text = vite_fix
            plan.update({"vite_config": config, "vite_out_from": old_out, "vite_out_to": rel, "vite_config_text": new_text})
        return plan
    return None


def set_build_script(raw: str, value: str) -> str:
    package = json.loads(raw)
    scripts = package.setdefault("scripts", {})
    ordered = {}
    placed = False
    for key, val in scripts.items():
        if key == "build":
            continue
        if not placed and key.startswith("build:"):
            ordered["build"] = value
            placed = True
        ordered[key] = val
    if not placed:
        ordered["build"] = value
    package["scripts"] = ordered
    newline = "\r\n" if "\r\n" in raw else "\n"
    return json.dumps(package, ensure_ascii=False, indent=2).replace("\n", newline) + newline


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

    # 0) 이미지 보안 검사에서 배포가 막힐 것이 확실한 의존성 버전
    runtime_deps = package.get("dependencies") if isinstance(package.get("dependencies"), dict) else {}
    has_lock = any(files.exists(n) for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
    for name, (safe_major, safe_range, why) in KNOWN_VULNERABLE_NODE.items():
        major = _spec_major(runtime_deps.get(name))
        if major is not None and major < safe_major:
            result.issues.append(ReadinessIssue(
                "NODE_VULNERABLE_DEPENDENCY", ERROR,
                f"`{name}` {runtime_deps[name]} — {why}. 배포 직전 보안 검사(Trivy)가 배포를 차단합니다.",
                (f"`npm install {name}@{safe_range.lstrip('^~')}` 로 package.json 과 lock 파일을 함께 올리세요."
                 if has_lock else
                 f"package.json 의 {name} 를 {safe_range} 로 올리세요(자동 수정 가능, 원본은 .recoder/backups 에 보관)."),
                "package.json", not has_lock))

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
    #: 하위 폴더에 자기 package.json 이 있으면(client/ 의 React 앱 등) 그 폴더의 코드는 그
    #: package.json 기준으로 본다. 예전에는 루트 package.json 과 비교해 client 의 react·axios 를
    #: "선언 안 된 패키지"로 잘못 막았다(실기기).
    manifests: dict[str, set[str]] = {"": deps}
    for rel in files.files():
        if rel.endswith("/package.json") and "/node_modules/" not in f"/{rel}":
            folder = rel[: -len("/package.json")]
            try:
                sub = json.loads(files.read(rel) or "")
            except ValueError:
                continue
            if isinstance(sub, dict):
                names: set[str] = {str(sub.get("name") or "")}
                for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    if isinstance(sub.get(key), dict):
                        names.update(sub[key])
                manifests[folder] = names

    def owner(rel: str) -> str:
        best = ""
        for folder in manifests:
            if folder and (rel == folder or rel.startswith(folder + "/")) and len(folder) > len(best):
                best = folder
        return best

    undeclared: dict[str, dict[str, str]] = {}
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
        project = owner(rel)
        declared = manifests[project] | (deps if project else set())  # 하위 폴더는 위쪽 node_modules 도 찾는다
        #: 템플릿 문자열 안의 글자(코드 생성기·문서 예시의 `import x from 'y'`)는 의존성이 아니다.
        active = re.sub(r"`(?:\\.|[^`\\])*`", "``", _strip_js_comments(text))
        for pattern in _IMPORT_PATTERNS:
            for spec in pattern.findall(active):
                pkg = _package_of(spec)
                if pkg and pkg not in declared and pkg not in _NODE_BUILTINS and pkg != package.get("name"):
                    undeclared.setdefault(project, {}).setdefault(pkg, rel)
        r, s = _express_routes(text)
        routes |= r
        statics |= s
        result.local_data_files += [f for f in _local_sqlite_files(active) if f not in result.local_data_files]
    for project, missing in sorted(undeclared.items()):
        if workspaces and not project:
            continue
        names = sorted(missing)
        shown = ", ".join(f"`{n}`({missing[n]})" for n in names[:6])
        manifest = f"{project}/package.json" if project else "package.json"
        where = f"`cd {project} && npm install" if project else "`npm install"
        result.issues.append(ReadinessIssue(
            "NODE_UNDECLARED_DEPENDENCY", ERROR,
            f"코드가 불러오는 패키지가 {manifest} 에 없습니다: {shown}. 컨테이너에는 선언된 패키지만 설치됩니다.",
            f"{where} {' '.join(names[:6])}` 로 의존성에 추가하세요.", missing[names[0]]))

    _analyze_js_sources(files, result, manifests, owner, undeclared)

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

    # 4) 루트 build 가 하위 프로젝트(client/ 등)로 들어가서 빌드한다 — Dockerfile 이 그 폴더의
    #    의존성도 설치해야 한다(아래 Dockerfile 점검). 빌드한 화면을 서버가 제공하는지도 본다.
    runs, installs = _subproject_scripts(scripts, files)
    result.subprojects = [d for d in runs if d not in installs]
    for rel in entry_candidates:
        text = files.read(rel)
        if text and re.search(r"\.listen\s*\(", text) and re.search(r"\bexpress\s*\(", text):
            result.server_entry = rel
            break
    server_texts = [files.read(rel) or "" for rel in files.files()
                    if rel.endswith(_JS_SUFFIXES) and not any(rel.startswith(d + "/") for d in runs)
                    and "/node_modules/" not in rel]
    try:
        served_now = set(_served_dirs(files, [rel for rel in files.files() if rel.endswith(_JS_SUFFIXES)
                                             and not any(rel.startswith(d + "/") for d in runs) and "/node_modules/" not in rel]))
    except Exception:  # noqa: BLE001
        served_now = set()
    for folder in runs:
        out = _vite_out_dir(files, folder) or _frontend_out_dir(files, folder)
        if not out or not (routes or result.server_entry):
            continue
        #: path.join(__dirname, 'client', 'dist') 처럼 나눠 적은 경로도 제공하는 것으로 본다(글자 비교만 하면 오탐).
        if out in served_now or any(out in text for text in server_texts):
            continue
        #: 글자 "require(" 만 보면 createRequire 를 쓰는 ESM 서버도 CommonJS 로 보고 __dirname 을 넣어 죽인다.
        commonjs = bool(result.server_entry) and _js_syntax(files.read(result.server_entry) or "") == "cjs" \
            and package.get("type") != "module"
        result.issues.append(ReadinessIssue(
            "NODE_FRONTEND_NOT_SERVED", WARNING,
            f"`build` 가 {folder}/ 의 화면을 {out}/ 로 빌드하지만 서버가 그 폴더를 제공하지 않습니다. "
            "컨테이너에서는 API 만 응답하고 브라우저로 열면 화면이 나오지 않습니다(Cannot GET /).",
            (f"서버({result.server_entry})가 {out}/ 를 제공하고 /api 밖의 경로는 index.html 로 보내게 하세요"
             "(자동 수정 가능, 원본은 .recoder/backups 에 보관)." if commonjs else
             f"서버에서 express.static 으로 {out}/ 를 제공하도록 추가하세요."),
            result.server_entry or "", commonjs))

    runtime_names = set(runtime_deps)
    if result.server_entry:
        sub = owner(result.server_entry)
        if sub:
            result.runtime_subproject = sub
            try:
                sub_pkg = json.loads(files.read(f"{sub}/package.json") or "")
                if isinstance(sub_pkg, dict) and isinstance(sub_pkg.get("dependencies"), dict):
                    runtime_names |= set(sub_pkg["dependencies"])
            except ValueError:
                pass
    all_server_text = "\n".join(server_texts)
    result.env_names = sorted(set(re.findall(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)", all_server_text)))
    for kind, deps_for in (("postgres", {"pg", "postgres", "pg-promise"}), ("mongodb", {"mongoose", "mongodb"}),
                           ("redis", {"redis", "ioredis"})):
        if runtime_names & deps_for:
            result.services.append(kind)
    provisioned = {"PostgreSQL": "postgres", "MongoDB": "mongodb", "Redis": "redis"}
    reads_db_env = any(re.search(r"(DATABASE|POSTGRES|PG|MONGO|REDIS|DB_)", n.upper()) for n in result.env_names)
    for dep, label, pattern in _EXTERNAL_SERVICES:
        if provisioned.get(label) in result.services and reads_db_env:
            continue  # 로컬 Docker 배포가 함께 띄우고 접속 정보를 환경변수로 넘긴다(배포 계획에 안내)
        if dep in runtime_names and re.search(pattern, all_server_text):
            result.issues.append(ReadinessIssue(
                "NODE_EXTERNAL_SERVICE", WARNING,
                f"앱이 {label} 에 localhost 로 연결합니다. 로컬 Docker 배포는 앱 컨테이너 하나만 띄우므로 "
                f"컨테이너 안의 localhost 에는 {label} 이 없습니다 — DB 를 쓰는 API 는 오류를 돌려줍니다.",
                f"PC 에 {label} 을 띄웠다면 연결 주소의 localhost 를 host.docker.internal 로 바꿔 환경변수로 넘기거나, "
                f"docker compose 로 {label} 과 함께 띄우세요. 화면·헬스 확인은 그대로 동작합니다.", ""))
            break

    # 5) 서버 진입 파일에서 불러오는 파일의 모듈 형식(ESM·CommonJS)이 어긋나는가 — 컨테이너가 시작 직후 죽는다
    node_entry = _node_entry(files, scripts, main)
    try:
        fmt = module_format_plan(files, node_entry, owner)
    except Exception as exc:  # noqa: BLE001 - 점검 실패가 다른 판정을 막지 않는다
        print(f"[build_readiness] 모듈 형식 점검 생략: {exc}", file=sys.stderr)
        fmt = None
    if fmt:
        result.fix_data["module_format"] = fmt
        result.issues.append(ReadinessIssue(
            "NODE_MODULE_FORMAT_MISMATCH", ERROR,
            " / ".join(fmt["problems"]) + ". 컨테이너에서 서버가 시작하자마자 종료됩니다(SyntaxError·ERR_MODULE_NOT_FOUND).",
            ("모듈 형식을 서버 진입 파일에 맞춰 통일하세요(자동 수정 가능 — CommonJS 파일은 .cjs 로 바꾸거나 "
             "ESM 문법을 require 로 바꾸고, 원본은 .recoder/backups 에 보관)." if fmt["auto"] else
             f"모듈 형식을 하나로 통일하세요. {fmt['why_not']}"),
            (fmt.get("files") or [node_entry or ""])[0], bool(fmt["auto"])))

    # 6) 서버가 제공하는 화면 폴더를 Docker 빌드(npm run build)가 만들지 않는다 — 주소만 열리고 화면이 없다
    server_graph = _module_graph(files, node_entry) if node_entry else []
    if not server_graph:
        server_graph = [rel for rel in files.files() if rel.endswith(_JS_SUFFIXES) and "/node_modules/" not in f"/{rel}"
                        and not any(rel.startswith(d + "/") for d in runs) and _is_server_file(files.read(rel) or "")][:50]
    try:
        ui = frontend_build_plan(files, scripts, server_graph)
    except Exception as exc:  # noqa: BLE001
        print(f"[build_readiness] 화면 빌드 점검 생략: {exc}", file=sys.stderr)
        ui = None
    try:
        build_runs, build_direct = _script_runs(scripts, files, "build")
        built = {out for folder, (out, name) in _frontend_projects(files).items()
                 if (folder and folder in build_runs) or (not folder and (build_direct or name == "build"))}
    except Exception:  # noqa: BLE001
        built = set()
    #: 루트 build 가 만드는 화면 폴더는 소스에 없는 게 정상이다 — "정적 폴더 없음" 경고를 내지 않는다
    result.issues = [i for i in result.issues
                     if not (i.code == "NODE_STATIC_DIR_MISSING" and any(f"`{out}`" in i.message for out in built))]
    if ui:
        result.fix_data["frontend_build"] = ui
        where = f"{ui['folder']}/ 의 화면" if ui["folder"] else "화면"
        problems, fixes = [], []
        if ui.get("vite_config"):
            problems.append(f"화면은 {ui['vite_out_from']}/ 로 빌드되는데(vite outDir) 서버({ui['server_file']})는 {ui['out_dir']}/ 를 제공합니다")
            fixes.append(f"{ui['vite_config']} 의 outDir 를 '{ui['vite_out_to']}' 로")
        if ui.get("build"):
            problems.append(f"서버({ui['server_file']})가 {ui['out_dir']}/ 를 화면으로 제공하지만 루트 `build` 스크립트가 {where}을 빌드하지 않습니다"
                            f"{'(`' + ui['runner'] + '` 는 Docker 빌드에서 실행되지 않음)' if ui['runner'] else ''}")
            fixes.append(f"package.json 의 build 를 `{ui['build']}` 로")
        result.issues.append(ReadinessIssue(
            "NODE_FRONTEND_NOT_BUILT", ERROR,
            " / ".join(problems) + ". 배포 주소는 열리지만 화면이 나오지 않습니다(index.html 없음).",
            " · ".join(fixes) + " 맞추세요(자동 수정 가능, 원본은 .recoder/backups 에 보관).",
            ui.get("vite_config") or "package.json", True))

        #: 같은 폴더의 "정적 폴더 없음" 경고는 이 항목이 원인과 해결을 대신한다
        result.issues = [i for i in result.issues
                         if not (i.code == "NODE_STATIC_DIR_MISSING" and f"`{ui['out_dir']}`" in i.message)]

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


def _dockerfile_installs(text: str, folder: str) -> bool:
    """Dockerfile 이 하위 폴더의 의존성을 설치하는가."""
    joined = re.sub(r"\\\r?\n", " ", text)
    d = re.escape(folder.strip("/"))
    patterns = (
        rf"cd\s+(?:\./|/app/)?{d}/?\s*&&[^\n]*\b(?:npm|yarn|pnpm)\s+(?:ci|install|i)\b",
        rf"\b(?:npm|pnpm)\s+(?:ci|install|i)\b[^\n]*--prefix[= ](?:\./)?{d}\b",
        rf"--prefix[= ](?:\./)?{d}\b[^\n]*\b(?:ci|install)\b",
        rf"\byarn\s+--cwd\s+(?:\./)?{d}\b",
    )
    if any(re.search(p, joined) for p in patterns):
        return True
    workdir = None
    for line in joined.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "WORKDIR" and len(words) > 1:
            workdir = words[1].rstrip("/")
        elif words[0].upper() == "RUN" and workdir and workdir.endswith("/" + folder.strip("/")) \
                and re.search(r"\b(?:npm|yarn|pnpm)\s+(?:ci|install|i)\b", line):
            return True
    return False


def _install_line(files: "ProjectFiles", folder: str) -> str:
    if files.exists(f"{folder}/yarn.lock"):
        return f"RUN cd {folder} && yarn install --frozen-lockfile"
    if files.exists(f"{folder}/pnpm-lock.yaml"):
        return f"RUN cd {folder} && npm install -g pnpm && pnpm install --frozen-lockfile"
    return (f"RUN cd {folder} && if [ -f package-lock.json ]; then npm ci || npm install; "
            "else npm install; fi")


def add_subproject_installs(text: str, folders: list[str], files: "ProjectFiles") -> str:
    """빌드 RUN 앞에 하위 폴더 설치, 뒤에 그 폴더의 node_modules 정리를 넣는다.

    정리까지 하는 이유: react-scripts 같은 빌드 도구의 의존성이 실행 이미지에 남으면 이미지가
    수백 MB 커지고, 보안 검사(Trivy)가 그 안의 취약점으로 배포를 막는다.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    index = next((i for i, l in enumerate(lines)
                  if re.match(r"^\s*RUN\b", l, re.I) and re.search(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?build\b", l)), None)
    if index is None:
        raise ValueError("Dockerfile 에서 빌드(npm run build) 줄을 찾지 못했습니다. 직접 수정하세요.")
    end = index
    while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
        end += 1
    todo = [f for f in folders if not _dockerfile_installs(text, f)]
    if not todo:
        return text
    after = ["# ReCoder: 빌드에만 쓰는 하위 폴더 의존성은 실행 이미지에 넣지 않는다(크기·보안 검사).",
             "RUN rm -rf " + " ".join(f"{f}/node_modules" for f in todo)]
    before = ["# ReCoder: build 스크립트가 " + ", ".join(f"{f}/" for f in todo) + " 에서 빌드하므로 그 의존성도 설치한다."]
    before += [_install_line(files, f) for f in todo]
    lines[end + 1:end + 1] = after
    lines[index:index] = before
    return newline.join(lines)


def _runtime_install_anchors(text: str) -> Optional[tuple[int, int]]:
    """(deps 단계 설치 RUN 의 마지막 줄, 실행 단계의 node_modules 복사 줄). 템플릿 구조가 아니면 None."""
    lines = text.split("\n")
    stage = None
    deps_run_end = None
    copy_line = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*FROM\s+\S+(?:\s+AS\s+(\S+))?", line, re.I)
        if m:
            stage = (m.group(1) or "").lower()
        elif stage == "deps" and deps_run_end is None and re.match(r"^\s*RUN\b", line, re.I):
            end = i
            while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
                end += 1
            deps_run_end = end
            i = end
        elif re.match(r"^\s*COPY\s+--from=deps\b.*\s/app/node_modules\s", line + " ", re.I):
            copy_line = i
        i += 1
    return (deps_run_end, copy_line) if deps_run_end is not None and copy_line is not None else None


def add_runtime_subproject_install(text: str, folder: str) -> str:
    anchors = _runtime_install_anchors(text.replace("\r\n", "\n"))
    if anchors is None:
        raise ValueError("Dockerfile 구조를 확인하지 못했습니다. 서버 폴더 의존성 설치를 직접 추가하세요.")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    deps_end, copy_line = anchors
    chown = re.search(r"--chown=\S+", lines[copy_line])
    lines[copy_line + 1:copy_line + 1] = [
        f"COPY --from=deps {chown.group(0) + ' ' if chown else ''}/app/{folder}/node_modules ./{folder}/node_modules"]
    lines[deps_end + 1:deps_end + 1] = [
        f"# ReCoder: 서버가 {folder}/package.json 의 패키지를 쓰므로 그 폴더 의존성도 설치한다.",
        f"COPY {folder}/package.json {folder}/package-lock.json* ./{folder}/",
        f"RUN cd {folder} && if [ -f package-lock.json ]; then npm ci --omit=dev || npm install --omit=dev; "
        "else npm install --omit=dev; fi",
    ]
    return newline.join(lines)


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
    runtime_sub = result.runtime_subproject
    if runtime_sub and facts["cmd_entry"] and facts["cmd_entry"].startswith(runtime_sub + "/") \
            and not _dockerfile_installs(text, runtime_sub):
        anchors = _runtime_install_anchors(text)
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING", ERROR,
            f"서버({facts['cmd_entry']})가 {runtime_sub}/package.json 의 패키지(express 등)를 쓰는데 Dockerfile 은 "
            f"그 폴더의 의존성을 설치하지 않습니다. 컨테이너가 \"Cannot find module\" 로 바로 종료됩니다.",
            (f"Dockerfile 이 {runtime_sub}/ 의존성을 설치해 실행 이미지에 넣게 하세요(자동 수정 가능)." if anchors else
             f"Dockerfile 에 `RUN cd {runtime_sub} && npm install --omit=dev` 를 추가하세요."),
            dockerfile, anchors is not None))
    missing_sub = [d for d in result.subprojects if facts["runs_build"] and not _dockerfile_installs(text, d)]
    if missing_sub:
        shown = ", ".join(f"{d}/" for d in missing_sub)
        result.issues.append(ReadinessIssue(
            "DOCKERFILE_SUBPROJECT_DEPS_MISSING", ERROR,
            f"`npm run build` 가 {shown} 에서 빌드하지만 Dockerfile 은 그 폴더의 의존성을 설치하지 않습니다. "
            "빌드 단계에서 react-scripts 같은 도구를 찾지 못해 실패합니다.",
            f"Dockerfile 의 빌드 전에 {shown} 의존성 설치를 넣으세요(자동 수정 가능 — 설치 후 빌드하고, 실행 이미지에는 "
            "빌드 결과만 남깁니다).", dockerfile, True))
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
        #: Django·Flask 프로젝트가 Tailwind 빌드용 package.json 만 둔 경우는 Python 앱이다.
        if any(files.exists(f) for f in ("manage.py", "requirements.txt", "pyproject.toml")):
            try:
                pkg = json.loads(files.read("package.json") or "{}")
            except ValueError:
                pkg = {}
            if isinstance(pkg, dict):
                scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
                deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} \
                    if isinstance(pkg.get("dependencies") or {}, dict) and isinstance(pkg.get("devDependencies") or {}, dict) else {}
                node_server = any(k in scripts for k in ("start", "serve", "start:prod")) or pkg.get("main") or any(
                    d in deps for d in ("express", "next", "@nestjs/core", "koa", "fastify", "@hapi/hapi", "nuxt", "react-scripts", "vite"))
                if not node_server:
                    return "python"
        return "node"
    if any(files.exists(f) for f in ("requirements.txt", "pyproject.toml", "main.py", "app.py", "manage.py")):
        return "python"
    if files.exists("index.html"):
        return "static"
    return "unknown"


def _missing_versions_in(files: "ProjectFiles", manifest: str) -> list[dict]:
    try:
        package = json.loads(files.read(manifest) or "")
    except ValueError:
        return []
    if not isinstance(package, dict):
        return []
    try:
        from npm_registry import missing_versions  # type: ignore
    except ImportError:  # pragma: no cover
        from core.npm_registry import missing_versions  # type: ignore
    found = []
    for section in ("dependencies", "devDependencies"):
        deps = package.get(section)
        if isinstance(deps, dict):
            for item in missing_versions(deps):
                found.append({**item, "section": section})
    return found


def _check_versions_online(files: "ProjectFiles", result: Readiness) -> None:
    """레지스트리에 없는 의존성 버전(ETARGET 예정)을 찾는다. 네트워크가 안 되면 조용히 넘어간다."""
    for folder in [""] + list(result.subprojects):
        manifest = f"{folder}/package.json" if folder else "package.json"
        try:
            missing = _missing_versions_in(files, manifest)
        except Exception:  # noqa: BLE001 - 점검 실패가 계획을 막지 않는다
            continue
        if not missing:
            continue
        has_lock = any(files.exists(f"{folder}/{n}" if folder else n)
                       for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
        fixable = [m for m in missing if m["suggestion"]]
        parts = []
        for m in missing[:5]:
            if not m["exists"]:
                parts.append(f"`{m['name']}` — npm 에 이런 이름의 패키지가 없습니다")
            else:
                parts.append(f"`{m['name']}@{m['spec']}` — 이 범위에 맞는 버전이 없습니다"
                             + (f"(사용 가능: {m['suggestion']})" if m["suggestion"] else ""))
        auto = bool(fixable) and len(fixable) == len(missing) and not has_lock
        fix = ("package.json 의 버전을 " + ", ".join(f"{m['name']} {m['suggestion']}" for m in fixable)
               + " 로 고치세요(자동 수정 가능, 원본은 .recoder/backups 에 보관).") if auto else (
            "패키지 이름·버전을 npm 에서 확인해 고치세요"
            + (" (lock 파일이 있어 `npm install <패키지>@<버전>` 으로 함께 갱신해야 합니다)." if has_lock else "."))
        result.issues.append(ReadinessIssue(
            "NODE_DEPENDENCY_VERSION_NOT_FOUND", ERROR,
            f"{manifest} 의 의존성을 npm 에서 설치할 수 없습니다: " + "; ".join(parts)
            + ". 컨테이너 빌드가 `npm error ETARGET` 으로 멈춥니다.",
            fix, manifest, auto))


def analyze(workspace: str | Path, overlay: Optional[Mapping[str, Optional[str]]] = None,
            *, dockerfile: Optional[str] = "Dockerfile", online: bool = False) -> Readiness:
    """프로젝트(와 Dockerfile)를 읽어 빌드·실행 가능성을 판정한다.

    online=True 면 npm 레지스트리에 의존성 버전이 실제로 있는지도 본다(배포 계획·실패 진단).
    """
    files = ProjectFiles(Path(workspace), overlay)
    result = Readiness(runtime=detect_runtime(files))
    if result.runtime == "node":
        _analyze_node(files, result)
        if online:
            _check_versions_online(files, result)
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
    try:
        stream = path.open("w", encoding="utf-8", newline="")
    except PermissionError:
        if not path.exists():
            raise
        _make_writable(path)  # 읽기 전용 특성이 붙은 파일(Windows)
        stream = path.open("w", encoding="utf-8", newline="")
    with stream:
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


# ---------------------------------------------------------------------------
# 텍스트 변환 — 디스크 자동 수정과 코드 생성 결과(ops) 교정이 함께 쓴다
# ---------------------------------------------------------------------------

def vite_env_rewrite(text: str) -> str:
    """CRA 식 process.env 를 Vite 의 import.meta.env 로."""
    text = re.sub(r"\bprocess\.env\.NODE_ENV\b", "import.meta.env.MODE", text)
    text = re.sub(r"\bprocess\.env\.REACT_APP_([A-Za-z0-9_]+)", r"import.meta.env.VITE_\1", text)
    return re.sub(r"\bprocess\.env\.(?!VITE_)([A-Za-z_][A-Za-z0-9_]*)", r"import.meta.env.VITE_\1", text) \
        .replace("process.env.VITE_", "import.meta.env.VITE_")


def pg_numeric_parser_rewrite(text: str) -> str:
    """`require('pg')` 바로 뒤에 NUMERIC(1700) 을 숫자로 읽는 타입 파서를 넣는다."""
    if "setTypeParser" in text:
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    m = re.search(r"""^([ \t]*)(?:const|let|var)\s+(?:\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*['"]pg['"]\s*\)\s*;?[ \t]*(?=\r?$)""", text, re.MULTILINE)
    if not m:
        m = re.search(r"""^([ \t]*)import\s+(?!type\b)(?:([\w$]+)\s*,?\s*)?(?:\{[^}]*\})?\s*from\s+['"]pg['"]\s*;?[ \t]*(?=\r?$)""", text, re.MULTILINE)
        if not m:
            return text
        binding = m.group(2)
        head = "" if binding else f"{nl}{m.group(1)}import recoderPg from 'pg';"
        insert = f"{head}{nl}{m.group(1)}// ReCoder: NUMERIC/DECIMAL 을 문자열이 아닌 숫자로 받는다(화면의 toFixed 등).{nl}{m.group(1)}{binding or 'recoderPg'}.types.setTypeParser(1700, (value) => parseFloat(value));"
    else:
        insert = f"{nl}{m.group(1)}// ReCoder: NUMERIC/DECIMAL 을 문자열이 아닌 숫자로 받는다(화면의 toFixed 등).{nl}{m.group(1)}require('pg').types.setTypeParser(1700, (value) => parseFloat(value));"
    return text[:m.end()] + insert + text[m.end():]


def relative_api_rewrite(text: str) -> str:
    """'http://localhost:3001/api' → '/api', `http://localhost:3001/api/x` → `/api/x`, 'http://localhost:3001' → ''."""
    #: 따옴표 바로 뒤에 host(:포트)(/경로) 가 오고 곧바로 닫는 따옴표여야 한다 — `http://localhost:${PORT}` 처럼
    #: 포트가 변수인 것은 배포 환경에 맞춘 코드이므로 그대로 둔다.
    return re.sub(r"""(['"`])https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(/[^'"`\s]*)?(?=\1)""",
                  lambda m: m.group(1) + (m.group(2) or ""), text)


def router_link_rewrite(text: str) -> str:
    """<a href="/x">…</a> → <Link to="/x">…</Link>, react-router-dom import 에 Link 를 보탠다."""
    pattern = re.compile(r"""<a\s+href=(["'])(/[^"']*)\1([^>]*)>(.*?)</a>""", re.DOTALL)

    def convertible(m) -> bool:
        href, attrs = m.group(2), m.group(3)
        if re.search(r"\b(?:download|target=|rel=)", attrs):
            return False  # 새 창·파일 다운로드는 그대로 둔다
        if href.startswith(("/api/", "/uploads/", "/static/", "/assets/", "/files/")) or re.search(r"\.[a-z0-9]{2,5}(?:[?#]|$)", href):
            return False  # 서버가 주는 파일·API 는 화면 라우트가 아니다
        return True

    if not any(convertible(m) for m in pattern.finditer(text)):
        return text
    if re.search(r"""\bLink\b[^;\n]*\bfrom\s*['"](?!react-router-dom['"])""", text):
        return text  # 다른 Link(MUI·next/link)를 쓰는 파일은 건드리지 않는다
    text = pattern.sub(lambda m: f"<Link to={m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}>{m.group(4)}</Link>" if convertible(m) else m.group(0), text)
    imp = re.search(r"""^(\s*import\s*\{)([^}]*)(\}\s*from\s*['"]react-router-dom['"])""", text, re.MULTILINE)
    if imp:
        names = [n.strip() for n in imp.group(2).split(",") if n.strip()]
        if "Link" not in [n.split(" as ")[-1].strip() for n in names]:
            names.append("Link")
            text = text[:imp.start()] + imp.group(1) + " " + ", ".join(names) + " " + imp.group(3) + text[imp.end():]
    elif not re.search(r"""\bLink\b.*from\s*['"]react-router-dom['"]""", text):
        text = "import { Link } from 'react-router-dom';\n" + text
    return text


def rename_package_import(text: str, old: str, new: str) -> str:
    return re.sub(r"""(['"])""" + re.escape(old) + r"""(/[^'"]*)?\1""", lambda m: f"{m.group(1)}{new}{m.group(2) or ''}{m.group(1)}", text)


def jsx_reference_rewrite(text: str, importer_rel: str, old_rel: str, new_rel: str) -> str:
    """importer 안에서 **그 파일을 가리키는** 참조만 새 이름으로(index.html 의 /src/index.js, import './index.js').

    파일 이름만 보고 바꾸면 다른 폴더의 같은 이름(server/index.js)까지 바꾼다 — package.json 의
    main 이 server/index.jsx 가 돼 컨테이너가 시작하지 못했다(e2e 검증에서 잡힘).
    """
    import posixpath
    old_base, new_base = posixpath.basename(old_rel), posixpath.basename(new_rel)
    base_dir = posixpath.dirname(importer_rel)

    def resolve(spec: str) -> str:
        spec = spec.split("?")[0].split("#")[0]
        if spec.startswith("/"):
            return posixpath.normpath(posixpath.join(base_dir, spec.lstrip("/")))
        return posixpath.normpath(posixpath.join(base_dir, spec))

    def repl(m):
        quote, spec = m.group(1), m.group(2)
        if not spec.endswith(old_base) or resolve(spec) != old_rel:
            return m.group(0)
        return f"{quote}{spec[:-len(old_base)]}{new_base}{quote}"

    return re.sub(r"""(['"])([^'"\n]*)\1""", repl, text)


def serve_frontend(text: str, entry_rel: str, out_dir: str) -> str:
    """Express 서버가 빌드한 화면을 제공하게 한다 — 마지막 listen 호출 바로 앞에 넣는다.

    API·헬스 라우트는 앞에서 이미 정의돼 있으므로 그대로 이긴다. /api 로 시작하지 않는 나머지
    경로는 index.html 로 보내 React Router 같은 화면 라우팅이 새로고침에도 동작하게 한다.
    """
    import posixpath
    listens = list(re.finditer(r"^([ \t]*)(\w+)\.listen\s*\(", text, re.MULTILINE))
    if not listens:
        raise ValueError("서버 파일에서 app.listen(...) 을 찾지 못했습니다.")
    m = listens[-1]
    indent, app = m.group(1), m.group(2)
    #: `server = http.createServer(app); server.listen(...)` 이면 listen 을 부르는 것은 express 앱이 아니다 —
    #: createServer 에 넘긴 변수를 쓴다. 그 밖에는 listen 을 부른 변수가 곧 앱이다(팩토리 함수가 돌려준 앱 등).
    wrapped = re.search(rf"\b{re.escape(app)}\s*=\s*(?:new\s+)?(?:(?:[\w$.]+|require\s*\(\s*['\"][^'\"]+['\"]\s*\))\.)?(?:createServer|Server)\s*\(\s*(?:\{{[^}}]*\}}\s*,\s*)?([A-Za-z_$][\w$]*)\s*\)", text)
    if wrapped:
        app = wrapped.group(1)
    #: 404·오류 처리 미들웨어(app.use((req, res) => …), app.use('*', …), app.use(notFound)) 뒤에 넣으면
    #: 화면 요청이 그 앞에서 404 로 끝난다 — 그런 처리기가 있으면 첫 처리기 앞에 넣는다.
    catch_all = re.compile(
        rf"^([ \t]*){re.escape(app)}\.(?:use|all|get)\s*\(\s*(?:"
        r"(?:async\s*)?\(\s*(?:err|error|req|request)\b[^)]*\)\s*=>|"          # (req, res) => / (err, req, res, next) =>
        r"(?:async\s+)?function\s*\(\s*(?:err|error|req|request)\b|"          # function (req, res)
        r"['\"](?:\*|/\*|/\(\.\*\)|/:[\w]+\(\*\))['\"]|/\^?\.\*/|"      # '*', '/*', /.*/ 경로
        r"[\w$]*(?:[Nn]ot[Ff]ound|404|[Ee]rror)[\w$]*\s*\)"                 # app.use(notFoundHandler)
        r")", re.MULTILINE)
    #: 라우트보다 앞에 있는 (req, res, next) => 는 로거 같은 일반 미들웨어다 — 마지막 경로 등록 뒤의 것만 본다.
    #: 경로 문자열('/api'), 라우터 변수(app.use(routes)), require('./routes') 로 등록한 것을 경로 등록으로 본다.
    routes = [r for r in re.finditer(
        rf"\b{re.escape(app)}\.(?:get|post|put|patch|delete|use)\s*\(\s*(?:['\"`]/(?![*(])|require\s*\(|([A-Za-z_$][\w$]*)\s*[,)])", text)
        if not (r.group(1) and re.search(r"[Nn]ot[Ff]ound|404|[Ee]rror", r.group(1)))]
    after = routes[-1].end() if routes else 0
    first = next((c for c in catch_all.finditer(text, 0, m.start()) if c.start() > after), None)
    insert_at = m.start()
    if first:
        indent = first.group(1)
        insert_at = first.start()
        # 처리기 바로 위의 주석(// 404 등)은 처리기의 것이다 — 그 주석보다 앞에 넣는다
        while True:
            prev_end = text.rfind("\n", 0, insert_at - 1) + 1 if insert_at > 0 else 0
            line = text[prev_end:insert_at].strip()
            if insert_at > 0 and line.startswith("//"):
                insert_at = prev_end
            else:
                break
    rel = posixpath.relpath(out_dir, posixpath.dirname(entry_rel) or ".")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = newline.join([
        f"{indent}// ReCoder: 빌드한 화면({out_dir})을 같은 서버에서 제공한다. API 라우트는 위에서 먼저 처리된다.",
        f"{indent}const recoderUiDir = require('path').join(__dirname, '{rel}');",
        f"{indent}{app}.use(require('express').static(recoderUiDir));",
        f"{indent}{app}.get(/^\\/(?!api(?:\\/|$)).*/, (req, res, next) => res.sendFile(require('path').join(recoderUiDir, 'index.html'), (err) => err && next()));",
        "",
    ])
    return text[:insert_at] + block + newline + text[insert_at:]


def _make_writable(path: Path) -> None:
    """읽기 전용 파일(Windows 읽기 전용 특성·압축 해제본)도 고칠 수 있게 쓰기 권한을 준다."""
    import stat
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except OSError:
        pass


def apply_file_plan(root: Path, writes: Mapping[str, str], renames: list) -> list[str]:
    """계획(새 경로 기준 내용 + 이름 바꾸기)을 디스크에 쓴다. 바뀌는 원본은 모두 백업한다.

    **전부 되거나 전부 안 된다.** 중간에 실패하면(Windows 의 읽기 전용 파일·잠긴 파일 등) 쓴 파일을
    원래대로 돌리고 오류를 낸다 — 옛 .js 와 새 .cjs 가 함께 남으면 앱이 반쯤 바뀐 채로 남는다.
    """
    rename_map = {old: new for old, new in renames if (root / old).is_file() and not (root / new).exists()}
    skipped = [old for old, new in renames if old not in rename_map]
    if skipped:
        raise ValueError(f"이름을 바꿀 파일이 없거나 새 이름({skipped[0]})이 이미 있습니다. 배포 준비 점검을 다시 실행하세요.")
    renamed_to = set(rename_map.values())
    #: 쓰기 전에 바꿀 파일을 모두 읽어 본다 — UTF-8 이 아닌 파일(메모장 ANSI=CP949)을 읽다가 중간에 멈추면
    #: 반쯤 바뀐 채로 남고, 다시 쓰면 한글 주석이 깨진다. 하나라도 못 읽으면 아무것도 바꾸지 않는다.
    for rel in list(rename_map) + [r for r in writes if r not in renamed_to]:
        path = root / rel
        if not path.is_file():
            continue
        try:
            _read_raw(path)
        except UnicodeError as exc:
            raise ValueError(f"{rel} 이(가) UTF-8 이 아니라(예: 메모장 ANSI) 자동 수정하지 않았습니다. "
                             "편집기에서 UTF-8 로 저장한 뒤 다시 시도하세요.") from exc
        except OSError as exc:
            raise ValueError(f"{rel} 을(를) 읽지 못해 자동 수정하지 않았습니다: {exc}") from exc
    originals: dict[str, Optional[str]] = {}   # 복구용: 경로 → 원래 내용(None = 원래 없던 파일)
    changed: list[str] = []
    try:
        for old, new in rename_map.items():
            text = _read_raw(root / old)
            originals[old] = text
            originals[new] = None
            changed.append(_backup(root, old, text))
            _write_raw(root / new, writes.get(new, text))
        for rel, content in writes.items():
            if rel in renamed_to:
                continue
            path = root / rel
            if path.is_file():
                original = _read_raw(path)
                if original == content:
                    continue
                originals.setdefault(rel, original)
                changed.append(_backup(root, rel, original))
                _make_writable(path)
            else:
                originals.setdefault(rel, None)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_raw(path, content)
            changed.append(rel)
        for old, new in rename_map.items():
            try:
                (root / old).unlink()
            except PermissionError:
                _make_writable(root / old)
                (root / old).unlink()
            changed.append(f"{old} → {new}")
    except (OSError, UnicodeError) as exc:
        for rel, text in originals.items():
            path = root / rel
            try:
                if text is None:
                    if path.exists():
                        _make_writable(path)
                        path.unlink()
                elif not path.exists() or _read_raw(path) != text:
                    if path.exists():
                        _make_writable(path)
                    _write_raw(path, text)
            except (OSError, UnicodeError):
                pass
        raise ValueError(f"파일을 바꾸지 못해 원래대로 되돌렸습니다: {exc}. 파일이 다른 프로그램에서 열려 있거나 "
                         "읽기 전용인지 확인하세요.") from exc
    return changed


def apply_fix(workspace: str | Path, code: str) -> dict:
    """AUTO_FIXABLE 만 적용한다. 다시 판정한 결과를 돌려준다."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("유효한 프로젝트 폴더가 아닙니다.")
    if code not in AUTO_FIXABLE:
        raise ValueError("자동으로 고칠 수 없는 항목입니다. 안내에 따라 직접 수정하세요.")
    before = analyze(root, online=code == "NODE_DEPENDENCY_VERSION_NOT_FOUND")
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
    elif code == "NODE_VULNERABLE_DEPENDENCY":
        if not issue.auto_fix:
            raise ValueError("lock 파일이 있어 package.json 만 바꾸면 npm ci 가 실패합니다. 안내된 npm install 명령을 실행하세요.")
        manifest = root / "package.json"
        text = _read_raw(manifest)
        package = json.loads(text)
        deps = package.get("dependencies") or {}
        bumped = []
        for name, (safe_major, safe_range, _why) in KNOWN_VULNERABLE_NODE.items():
            major = _spec_major(deps.get(name))
            if major is not None and major < safe_major:
                bumped.append(name)
        if not bumped:
            raise ValueError("올릴 의존성을 찾지 못했습니다.")
        backup = _backup(root, "package.json", text)
        updated = text
        for name in bumped:
            pattern = re.compile(r'("' + re.escape(name) + r'"\s*:\s*")[^"]*(")')
            # 원래 서식(들여쓰기·줄바꿈)을 지키려고 값만 바꾼다. dependencies 안의 첫 항목이 대상이다.
            start = updated.find('"dependencies"')
            m = pattern.search(updated, start if start >= 0 else 0)
            if not m:
                raise ValueError(f"package.json 에서 {name} 항목을 찾지 못했습니다.")
            updated = updated[:m.start()] + m.group(1) + KNOWN_VULNERABLE_NODE[name][1] + m.group(2) + updated[m.end():]
        json.loads(updated)  # 결과가 여전히 올바른 JSON 인지 확인
        _write_raw(manifest, updated)
        changed += ["package.json", backup]
    elif code == "NODE_DEPENDENCY_VERSION_NOT_FOUND":
        if not issue.auto_fix:
            raise ValueError("자동으로 고칠 수 없습니다(없는 패키지이거나 lock 파일이 있음). 안내에 따라 직접 수정하세요.")
        manifest_rel = issue.file or "package.json"
        manifest = root / manifest_rel
        text = _read_raw(manifest)
        missing = _missing_versions_in(ProjectFiles(root), manifest_rel)
        if not missing or any(not m["suggestion"] for m in missing):
            raise ValueError("고칠 버전을 확정하지 못했습니다. npm 에서 버전을 확인해 직접 수정하세요.")
        backup = _backup(root, manifest_rel, text)
        updated = text
        for m in missing:
            start = updated.find(f'"{m["section"]}"')
            pattern = re.compile(r'("' + re.escape(m["name"]) + r'"\s*:\s*")[^"]*(")')
            found = pattern.search(updated, start if start >= 0 else 0)
            if not found:
                raise ValueError(f"{manifest_rel} 에서 {m['name']} 항목을 찾지 못했습니다.")
            updated = updated[:found.start()] + found.group(1) + m["suggestion"] + found.group(2) + updated[found.end():]
        json.loads(updated)
        _write_raw(manifest, updated)
        changed += [manifest_rel, backup]
    elif code == "DOCKERFILE_SUBPROJECT_DEPS_MISSING":
        dockerfile = root / "Dockerfile"
        text = _read_raw(dockerfile)
        updated = add_subproject_installs(text, before.subprojects, ProjectFiles(root))
        if updated == text:
            raise ValueError("이미 하위 폴더 의존성을 설치하고 있습니다.")
        backup = _backup(root, "Dockerfile", text)
        _write_raw(dockerfile, updated)
        changed += ["Dockerfile", backup]
    elif code == "NODE_FRONTEND_NOT_SERVED":
        if not issue.auto_fix or not before.server_entry:
            raise ValueError("서버 파일을 확정하지 못했습니다. express.static 으로 빌드 폴더를 직접 제공하세요.")
        files = ProjectFiles(root)
        outs = [o for o in (_frontend_out_dir(files, d) for d in before.subprojects) if o]
        if not outs:
            raise ValueError("빌드 결과 폴더를 확정하지 못했습니다.")
        entry = root / before.server_entry
        text = _read_raw(entry)
        updated = serve_frontend(text, before.server_entry, outs[0])
        backup = _backup(root, before.server_entry, text)
        _write_raw(entry, updated)
        changed += [before.server_entry, backup]
    elif code == "NODE_LOCAL_IMPORT_MISSING":
        import posixpath
        styles = before.fix_data.get("missing_styles") or []
        if not issue.auto_fix or not styles:
            raise ValueError("스타일 파일이 아닌 파일이 빠져 있어 자동으로 만들 수 없습니다. import 경로를 확인하세요.")
        for importer, spec in styles:
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
            target = root / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("/* ReCoder: 코드가 불러오지만 없던 스타일 파일 — 필요한 스타일을 채우세요. */\n", encoding="utf-8")
            changed.append(rel)
    elif code == "NODE_VITE_JSX_IN_JS":
        for rel in before.fix_data.get("jsx_in_js") or []:
            source = root / rel
            new_rel = rel[:-3] + ".jsx"
            if not source.is_file() or (root / new_rel).exists():
                continue
            text = _read_raw(source)
            backup = _backup(root, rel, text)
            os.replace(source, root / new_rel)
            changed += [f"{rel} → {new_rel}", backup]
            for other in ProjectFiles(root).files():
                if not other.endswith((".html",) + _JS_SUFFIXES):
                    continue
                body = _read_raw(root / other)
                updated = jsx_reference_rewrite(body, other, rel, new_rel)
                if updated != body:
                    changed.append(_backup(root, other, body))
                    _write_raw(root / other, updated)
                    changed.append(other)
    elif code == "NODE_VITE_PROCESS_ENV":
        for rel in before.fix_data.get("process_env") or []:
            text = _read_raw(root / rel)
            updated = vite_env_rewrite(text)
            if updated != text:
                changed += [rel, _backup(root, rel, text)]
                _write_raw(root / rel, updated)
        for env_file in (".env.example", ".env.sample"):
            path = root / env_file
            if path.is_file():
                text = _read_raw(path)
                updated = re.sub(r"(?m)^REACT_APP_", "VITE_", text)
                if updated != text:
                    changed += [env_file, _backup(root, env_file, text)]
                    _write_raw(path, updated)
    elif code == "NODE_IMPORT_PACKAGE_TYPO":
        typos = before.fix_data.get("package_typos") or {}
        for rel in ProjectFiles(root).files():
            if not rel.endswith(_JS_SUFFIXES):
                continue
            text = _read_raw(root / rel)
            updated = text
            for old_pkg, new_pkg in typos.items():
                updated = rename_package_import(updated, old_pkg, new_pkg)
            if updated != text:
                changed += [rel, _backup(root, rel, text)]
                _write_raw(root / rel, updated)
    elif code == "NODE_CLIENT_HARDCODED_LOCALHOST":
        for rel in before.fix_data.get("hardcoded_api") or []:
            text = _read_raw(root / rel)
            updated = relative_api_rewrite(text)
            if updated != text:
                changed += [rel, _backup(root, rel, text)]
                _write_raw(root / rel, updated)
        for env_file in (".env.example", ".env.sample"):
            path = root / env_file
            if path.is_file():
                text = _read_raw(path)
                updated = re.sub(r"(?m)^(VITE_[A-Z0-9_]*(?:API|BASE)[A-Z0-9_]*=)https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(/\S*)?$", lambda m: m.group(1) + (m.group(2) or ""), text)
                if updated != text:
                    changed += [env_file, _backup(root, env_file, text)]
                    _write_raw(path, updated)
    elif code == "NODE_ROUTER_ANCHOR_LINK":
        for rel in before.fix_data.get("router_anchor_files") or []:
            text = _read_raw(root / rel)
            updated = router_link_rewrite(text)
            if updated != text:
                changed += [rel, _backup(root, rel, text)]
                _write_raw(root / rel, updated)
    elif code == "NODE_PG_NUMERIC_STRINGS":
        for rel in before.fix_data.get("pg_numeric_files") or []:
            text = _read_raw(root / rel)
            updated = pg_numeric_parser_rewrite(text)
            if updated != text:
                #: pg 를 쓰는 파일마다 넣는다(타입 파서는 전역이라 여러 번 넣어도 같다) — 첫 파일만 고치면
                #: 시드 스크립트만 고쳐지고 서버는 그대로 문자열을 돌려준다.
                changed += [rel, _backup(root, rel, text)]
                _write_raw(root / rel, updated)
    elif code == "DOCKERFILE_RUNTIME_SUBPROJECT_DEPS_MISSING":
        dockerfile = root / "Dockerfile"
        text = _read_raw(dockerfile)
        updated = add_runtime_subproject_install(text, before.runtime_subproject or "")
        changed += ["Dockerfile", _backup(root, "Dockerfile", text)]
        _write_raw(dockerfile, updated)
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
    elif code == "NODE_MODULE_FORMAT_MISMATCH":
        plan = before.fix_data.get("module_format") or {}
        if not issue.auto_fix or not plan.get("auto"):
            raise ValueError(plan.get("why_not") or "모듈 형식을 자동으로 통일할 수 없습니다. 안내에 따라 직접 수정하세요.")
        changed += apply_file_plan(root, plan.get("writes") or {}, plan.get("renames") or [])
    elif code == "NODE_CLIENT_API_DOUBLE_PREFIX":
        writes = {}
        for rel, names in (before.fix_data.get("api_double_prefix") or {}).items():
            text = _read_raw(root / rel)
            updated = api_prefix_rewrite(text, names)
            if updated != text:
                writes[rel] = updated
        changed += apply_file_plan(root, writes, [])
    elif code == "NODE_IMPORT_NAME_MISSING":
        fixes = before.fix_data.get("missing_exports") or []
        if not issue.auto_fix or not fixes:
            raise ValueError("그 파일에 선언되지 않은 이름이라 자동으로 고칠 수 없습니다. 이름을 맞추세요.")
        writes: dict[str, str] = {}
        for target, name, kind in fixes:
            current = writes.get(target, _read_raw(root / target))
            updated = add_missing_export(current, name, kind)
            if updated is None:
                raise ValueError(f"{target} 에서 {name} 선언을 찾지 못했습니다.")
            writes[target] = updated
        changed += apply_file_plan(root, writes, [])
    elif code == "NODE_FRONTEND_NOT_BUILT":
        plan = before.fix_data.get("frontend_build") or {}
        if not plan.get("build") and not plan.get("vite_config"):
            raise ValueError("화면을 빌드할 명령을 확정하지 못했습니다.")
        if plan.get("vite_config"):
            config = root / plan["vite_config"]
            text = _read_raw(config)
            updated = vite_out_dir_rewrite(text, plan["vite_out_to"])
            if updated is None:
                raise ValueError("vite.config 의 outDir 를 바꾸지 못했습니다. 직접 맞추세요.")
            changed += [plan["vite_config"], _backup(root, plan["vite_config"], text)]
            _write_raw(config, updated)
        if plan.get("build"):
            manifest = root / "package.json"
            text = _read_raw(manifest)
            backup = _backup(root, "package.json", text)
            _write_raw(manifest, set_build_script(text, plan["build"]))
            changed += ["package.json", backup]
    after = analyze(root)
    return {"applied": bool(changed), "changed": changed,
            "message": "수정했습니다. 배포 내용을 다시 확인하세요." if changed else "변경할 내용이 없었습니다.",
            "readiness": after.to_dict()}


def expects_screen(workspace: str | Path) -> bool:
    """이 프로젝트가 브라우저 화면을 제공해야 하는가(배포 후 화면 확인 여부).

    화면 빌드 도구가 있는 package.json, 저장소에 있는 index.html, 서버 템플릿(templates/)이 근거다.
    API 만 있는 서버는 False — "/" 가 JSON·404 여도 정상이다.
    """
    files = ProjectFiles(Path(workspace))
    try:
        if _frontend_projects(files):
            return True
    except Exception:  # noqa: BLE001
        pass
    not_app = ("htmlcov", "coverage", "docs", "doc", "test", "tests", "__tests__", "examples", "example", "e2e",
               "node_modules", ".recoder", "site-packages", "reports", "report")
    for rel in files.files():
        name = rel.rsplit("/", 1)[-1]
        parts = rel.split("/")[:-1]
        if any(p in not_app or p.startswith(".") for p in parts):
            continue  # 커버리지 리포트·문서 사이트의 index.html 은 앱 화면이 아니다
        if name == "index.html" and len(parts) <= 2 and (not parts or parts[0] in (
                "public", "static", "src", "client", "frontend", "web", "www", "ui", "app", "site", "dist", "build")):
            return True
        if rel.startswith(("templates/", "app/templates/", "src/templates/", "views/")) and name.endswith((".html", ".ejs", ".hbs", ".pug", ".njk", ".jinja", ".jinja2")):
            return True
    return False
