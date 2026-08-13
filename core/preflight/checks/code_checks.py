"""
코드 / 엔드포인트 정적 검사 (§30.1).

검사 2종:
  - MISSING_HEALTH_ENDPOINT  : recoder.yml 의 health_check_path 가 코드에 실제 정의됐는지
  - APP_ENTRYPOINT_NOT_FOUND : main.py / app.py / index.js 등 진입점 존재 여부

AST 기반 탐지는 First Run Wizard (D 영역) 의 본 작업. 본 검사는 텍스트 패턴
매칭으로 단순화 — 정확도는 떨어지지만 "있는지 없는지" 수준은 충분.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

try:
    from preflight import CheckResult
    from schemas import (
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )
except ImportError:  # pragma: no cover
    from core.preflight import CheckResult  # type: ignore
    from core.schemas import (  # type: ignore
        ContractStack,
        PreflightBlocker,
        PreflightCheckCode,
        PreflightSeverity,
        PreflightWarning,
        ReleaseContract,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PYTHON_FILES_GLOB = ("*.py",)
_NODE_FILES_GLOB = ("*.js", "*.ts", "*.mjs")

#: 진입점 후보 파일들. 스택별로 우선순위.
_ENTRYPOINT_CANDIDATES: dict[ContractStack, tuple[str, ...]] = {
    ContractStack.PYTHON_FASTAPI: ("main.py", "app.py", "app/main.py", "src/main.py", "src/app.py"),
    ContractStack.PYTHON_FLASK:   ("app.py", "main.py", "wsgi.py", "src/app.py", "src/main.py"),
    #: NODE_EXPRESS 는 "Next 가 아닌 모든 Node 프로젝트"의 폴백이라 NestJS·
    #: Fastify·Koa 도 여기로 온다. 그 생태계는 **TypeScript 진입점이 기본**이다
    #: (NestJS 는 `src/main.ts`). `.js` 만 두면 서버로 판정해 놓고 진입점을
    #: 못 찾아 막는 막다른 길이 된다.
    ContractStack.NODE_EXPRESS: (
        "index.js", "server.js", "app.js", "src/index.js", "src/server.js", "src/app.js",
        "src/main.ts", "src/index.ts", "src/app.ts", "src/server.ts",
        "main.ts", "index.ts", "server.ts", "app.ts",
        "dist/main.js", "dist/index.js", "dist/server.js",
    ),
    #: Next 는 **JS 와 TS 가 동등한 관례**다. `app/page.js`·`pages/index.jsx`
    #: 만 있는 순수 JS App Router/Pages Router 프로젝트도 유효한 앱이고,
    #: `src/` 아래로 옮기는 배치도 공식 지원이다. `.tsx` 하나만 두면 감지는
    #: Next 라고 해놓고 진입점을 못 찾아 배포 대상 선택이 통째로 막힌다.
    ContractStack.NODE_NEXT: (
        "next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs",
        # App Router — page 파일 (js/jsx/tsx 전부 관례)
        "app/page.tsx", "app/page.jsx", "app/page.js",
        "src/app/page.tsx", "src/app/page.jsx", "src/app/page.js",
        # Pages Router — _app 과 index 둘 다 (한쪽만 있는 프로젝트가 흔하다)
        "pages/_app.js", "pages/_app.jsx", "pages/_app.tsx",
        "pages/index.js", "pages/index.jsx", "pages/index.tsx",
        "src/pages/index.js", "src/pages/index.jsx", "src/pages/index.tsx",
    ),
    #: CUSTOM 은 "위 넷 중 어디에도 안 맞는 전부"다. 그래서 후보 목록이
    #: **배포 대상 감지기가 서버로 인정하는 런타임을 전부 덮어야** 한다.
    #:
    #: 안 그러면 "Spring 빌드를 찾았습니다 → 서버형"이라고 해놓고 바로
    #: 다음 단계에서 "진입점을 못 찾겠습니다"로 막는다. 확장은 `blocked` 가
    #: 참이면 배포 대상 선택을 통째로 비활성화하므로 **사용자가 아무것도
    #: 할 수 없는 막다른 길**이 된다.
    #:
    #: 실측으로 Spring(maven/gradle)·Rails·PHP·Procfile·docker-compose
    #: 6가지가 그 상태였다. Go 만 통과했는데 그건 `main.go` 가 우연히
    #: 목록에 있어서였다.
    #:
    #: `check_app_entrypoint` 은 `exists()` 로 보므로 **폴더도 후보가 된다**
    #: (`src/main/java` 처럼 진입점이 패키지 트리 깊숙이 있는 경우).
    ContractStack.CUSTOM: (
        # 파이썬·Node (스택 감지가 실패했을 때의 폴백)
        #
        # `src/main.py`·`src/app.py`·`app/main.py` 가 꼭 있어야 하는 이유:
        # 감지기는 Starlette·Sanic 같은 비 FastAPI/Flask 파이썬 서버도
        # 서버로 인정하는데, 그 계약 스택은 CUSTOM 이다. 파이썬은 내용
        # 프로브가 없으므로 **이 목록이 파이썬 진입점의 전부**다 — src 배치를
        # 빼면 "서버를 찾았습니다" 해놓고 진입점을 못 찾아 배포 대상 선택이
        # 통째로 막힌다.
        "main.py", "app.py", "src/main.py", "src/app.py", "app/main.py",
        "index.js", "server.js", "app.js",
        "src/index.js", "src/index.ts", "src/main.ts", "src/app.js", "src/app.ts",
        # Django — `_detect_preflight_contract_stack` 이 FastAPI/Flask 만
        # 구분하므로 Django 는 CUSTOM 으로 온다. 관례 진입점을 넣어 둔다.
        "manage.py", "wsgi.py", "asgi.py", "config/wsgi.py", "src/manage.py",
        # Go · Rust
        "main.go", "main.rs", "src/main.rs",
        # Ruby
        "config.ru", "app.rb", "main.rb",
        # PHP
        "public/index.php", "index.php", "artisan",
        # 컨테이너·프로세스 선언 — 시작 방법이 여기 적혀 있다.
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Procfile",
    ),
}


def _iter_source_files(
    workspace: Path, patterns: tuple[str, ...], max_files: int = 200
) -> list[Path]:
    """workspace 안에서 패턴에 매칭되는 파일 (재귀). node_modules / .venv / dist / .git 스킵."""
    skipped_dirs = {
        "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
        ".git", ".idea", ".vscode", "out", "coverage", ".pytest_cache",
    }
    found: list[Path] = []
    for pattern in patterns:
        for path in workspace.rglob(pattern):
            if any(part in skipped_dirs for part in path.parts):
                continue
            if len(found) >= max_files:
                return found
            found.append(path)
    return found


# ---------------------------------------------------------------------------
# 1. MISSING_HEALTH_ENDPOINT
# ---------------------------------------------------------------------------


# 각 프레임워크에서 라우터 정의를 인식하는 정규식 — 단순 패턴.
_HEALTH_PATTERNS: dict[ContractStack, list[re.Pattern[str]]] = {
    ContractStack.PYTHON_FASTAPI: [
        # @app.get("/health"), @router.get("/health")
        re.compile(r"""@\w+\.(?:get|post|api_route)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.PYTHON_FLASK: [
        # @app.route("/health")
        re.compile(r"""@\w+\.route\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.NODE_EXPRESS: [
        # app.get("/health"), router.get("/health")
        re.compile(r"""\b(?:app|router)\.(?:get|post|use)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    ],
    ContractStack.NODE_NEXT: [
        # Next.js API routes: pages/api/health.js, app/api/health/route.ts
    ],
    ContractStack.CUSTOM: [],
}


def _route_paths_in_files(
    workspace: Path, stack: ContractStack
) -> set[str]:
    """소스 코드에서 추출한 라우트 경로 집합 (간이 grep)."""
    patterns = _HEALTH_PATTERNS.get(stack, [])
    if not patterns:
        return set()

    if stack in {ContractStack.PYTHON_FASTAPI, ContractStack.PYTHON_FLASK}:
        files = _iter_source_files(workspace, _PYTHON_FILES_GLOB)
    elif stack == ContractStack.NODE_EXPRESS:
        files = _iter_source_files(workspace, _NODE_FILES_GLOB)
    else:
        files = []

    paths: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                paths.add(m.group(1))
    return paths


def _next_health_exists(workspace: Path, health_path: str) -> bool:
    """Next.js 의 health 라우트 (pages/api/health 또는 app/api/health/route) 탐지."""
    # 정규화
    clean = health_path.lstrip("/").rstrip("/")
    candidates = [
        workspace / "pages" / "api" / f"{clean}.js",
        workspace / "pages" / "api" / f"{clean}.ts",
        workspace / "pages" / "api" / clean / "index.js",
        workspace / "pages" / "api" / clean / "index.ts",
        workspace / "app" / "api" / clean / "route.js",
        workspace / "app" / "api" / clean / "route.ts",
    ]
    return any(c.exists() for c in candidates)


def check_missing_health_endpoint(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """recoder.yml 의 health_check_path 가 실제 코드에 라우터로 정의됐는지."""
    start = time.monotonic()
    stack = contract.project.stack
    health_path = contract.runtime.health_check_path

    details: dict = {
        "stack": stack.value,
        "health_check_path": health_path,
    }

    if stack == ContractStack.CUSTOM:
        # custom 은 라우트 형식이 천차만별 — 경고만.
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            warning=PreflightWarning(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                message="custom 스택은 health 경로 자동 검증 불가. 직접 확인하세요.",
                severity=PreflightSeverity.LOW,
            ),
            details=details,
        )

    if stack == ContractStack.NODE_NEXT:
        found = _next_health_exists(workspace, health_path)
        details["next_route_found"] = found
        if found:
            return CheckResult(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                passed=True,
                duration_ms=int((time.monotonic() - start) * 1000),
                details=details,
            )
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=False,
            duration_ms=int((time.monotonic() - start) * 1000),
            blocker=PreflightBlocker(
                code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
                message=f"Next.js API 라우트 {health_path} 를 찾을 수 없습니다.",
                fix_hint=f"pages/api{health_path}.ts 또는 app/api{health_path}/route.ts 를 추가하세요.",
                remediation_available=True,
                severity=PreflightSeverity.HIGH,
            ),
            details=details,
        )

    # Python (FastAPI/Flask) / Node Express
    found_paths = _route_paths_in_files(workspace, stack)
    details["routes_found"] = sorted(found_paths)[:30]  # 상위 30개만 details에 (로그 비대화 방지)

    # 정규화 — trailing slash 차이 흡수
    def _norm(s: str) -> str:
        return "/" + s.strip("/").lower() if s else "/"

    needle = _norm(health_path)
    matched = needle in {_norm(p) for p in found_paths}

    if matched:
        return CheckResult(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.MISSING_HEALTH_ENDPOINT,
            message=f"{stack.value} 코드에 {health_path} 라우터가 정의돼 있지 않습니다.",
            fix_hint=(
                f"진입점에 다음과 같은 코드를 추가하세요:\n"
                f"  @app.get('{health_path}')\n"
                f"  def health():\n"
                f"      return {{'status': 'ok'}}"
            ),
            remediation_available=True,
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# 2. APP_ENTRYPOINT_NOT_FOUND
# ---------------------------------------------------------------------------


#: 파일 이름만으로는 알 수 없어 **내용을 봐야** 하는 진입점.
#:
#: Java/Kotlin 은 진입점이 `src/main/java/com/x/App.java` 처럼 패키지 트리
#: 깊숙이 있고 이름도 제각각이다. 폴더 존재로 대신하면 라이브러리도 통과하므로
#: 실행 가능한 main 선언을 직접 찾는다.
_EXECUTABLE_ENTRYPOINT_PROBES: tuple[tuple[str, tuple[str, ...], "re.Pattern[str]"], ...] = (
    (
        "java",
        ("src/main/java/**/*.java", "src/*/src/main/java/**/*.java", "**/*.java"),
        # `static` 과 `void` 사이에 다른 합법 수식어가 올 수 있다 —
        # `static public void main` · `static final synchronized void main` 은
        # 전부 유효한 진입점 선언이다. `static\s+void` 로 붙여 쓰면 그런
        # 앱이 APP_ENTRYPOINT_NOT_FOUND 로 막힌다. `(?:\w+\s+)*` 는 세미콜론
        # 같은 비단어 문자를 넘지 못하므로 다른 문장으로 새지 않는다.
        re.compile(r"\bstatic\s+(?:\w+\s+)*void\s+main\s*\("),
    ),
    (
        "kotlin",
        ("src/main/kotlin/**/*.kt", "**/*.kt"),
        re.compile(r"^\s*(?:@\w+\s+)*fun\s+main\s*\(", re.M),
    ),
    (
        "go",
        ("cmd/**/*.go", "**/*.go"),
        re.compile(r"^\s*func\s+main\s*\(", re.M),
    ),
    (
        "php",
        ("public/*.php", "*.php"),
        re.compile(r"<\?php"),
    ),
)

#: 프로브가 훑을 파일 수 상한. 큰 저장소에서 몇 초씩 걸리면 안 된다.
_PROBE_MAX_FILES = 60

#: 프로브를 **켜 주는 매니페스트**. 워크스페이스 루트에 이 파일이 있어야만
#: 해당 언어의 프로브가 돈다.
#:
#: 이게 없으면 프로브는 "아무 언어든 실행 가능해 보이는 첫 파일"을 진입점으로
#: 받아들인다. 실측 시나리오: Maven 프로젝트에 Java main 이 없는데
#: `tools/generator.go` 에 `func main()` 이 있으면 — 그 Go 유틸을 앱의
#: 진입점이라고 판정해 버린다. 그러면 뜨지 않는 Java 이미지가
#: APP_ENTRYPOINT_NOT_FOUND 를 잃고 배포 준비 완료로 보고된다.
#:
#: 매니페스트는 "이 워크스페이스가 무슨 런타임이라고 주장하는가"의 근거다.
#: `pom.xml` 이 있으면 Java/Kotlin 프로브가, `go.mod` 가 있으면 Go 프로브가
#: 정당하다. 둘 다 있으면(폴리글랏) 둘 다 돈다 — 그건 오탐이 아니라 사실이다.
#:
#: 매니페스트가 하나도 없는 워크스페이스는 프로브가 전부 꺼진다. 그 경우
#: 이름 기반 후보(`main.py`·`main.go`·`Dockerfile` 등)가 이미 앞 단계에서
#: 처리했으므로, 여기 도달했다는 것 자체가 "진입점을 주장할 근거가 없다"는
#: 뜻이다 — 막는 것이 맞다.
_PROBE_RUNTIME_MANIFESTS: dict[str, tuple[str, ...]] = {
    "java":   ("pom.xml", "build.gradle", "build.gradle.kts",
               "settings.gradle", "settings.gradle.kts"),
    "kotlin": ("pom.xml", "build.gradle", "build.gradle.kts",
               "settings.gradle", "settings.gradle.kts"),
    "go":     ("go.mod", "go.work"),
    "php":    ("composer.json",),
}

#: 언어별 잡음 제거 규칙: (줄 주석 접두들, 블록주석 여부, 여러 줄 원시 문자열 구분자들)
#: 일반 문자열(`"`·`'`)은 모든 언어에서 지운다.
_PROBE_NOISE_RULES: dict[str, tuple[tuple[str, ...], bool, tuple[str, ...]]] = {
    "java":   (("//",), True, ('"""',)),   # Java 15+ 텍스트 블록
    "kotlin": (("//",), True, ('"""',)),   # Kotlin 원시 문자열
    "go":     (("//",), True, ("`",)),     # Go 백틱 원시 문자열
    "php":    (("//", "#"), True, ()),
}


def _strip_probe_noise(text: str, label: str) -> str:
    """주석과 문자열 리터럴 내용을 공백으로 바꾼다. **개행은 보존**한다.

    프로브는 원문 텍스트에 정규식을 그대로 돌렸다. 그래서 Java 라이브러리의
    문서 주석에 있는 예시 한 줄 —

        // public static void main(String[] args)

    — 이 실행 선언으로 매칭돼 APP_ENTRYPOINT_NOT_FOUND 를 억눌렀다.
    실행할 수 있는 것이 하나도 없는데 배포 준비 완료가 되는 형태다.

    개행을 보존하는 이유: Kotlin/Go 프로브가 `^\\s*fun main` 같은
    줄 앵커(re.M)를 쓴다. 지운 자리를 공백으로 채우면 줄 구조가 유지돼
    앵커 의미가 변하지 않는다.
    """
    line_prefixes, block_comments, raw_delims = _PROBE_NOISE_RULES.get(
        label, (("//",), True, ()))
    out: list[str] = []
    i, n = 0, len(text)

    def _blank(seg: str) -> str:
        return "".join("\n" if c == "\n" else " " for c in seg)

    while i < n:
        ch = text[i]

        # 여러 줄 원시 문자열 (이스케이프 없음) — 일반 따옴표보다 먼저 본다.
        matched_raw = False
        for delim in raw_delims:
            if text.startswith(delim, i):
                end = text.find(delim, i + len(delim))
                if end == -1:
                    end = n - len(delim)
                seg_end = end + len(delim)
                out.append(delim + _blank(text[i + len(delim):end]) + delim)
                i = seg_end
                matched_raw = True
                break
        if matched_raw:
            continue

        # 일반 문자열 — 내용만 지운다 (이스케이프 처리).
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "\n":       # 닫히지 않은 한 줄 문자열은 줄에서 끝낸다
                    break
                j += 1
            out.append(ch + _blank(text[i + 1:j]))
            if j < n and text[j] == ch:
                out.append(ch)
                j += 1
            i = j
            continue

        # 줄 주석
        matched_line = False
        for prefix in line_prefixes:
            if text.startswith(prefix, i):
                j = text.find("\n", i)
                if j == -1:
                    j = n
                out.append(_blank(text[i:j]))
                i = j
                matched_line = True
                break
        if matched_line:
            continue

        # 블록 주석
        if block_comments and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(_blank(text[i:j]))
            i = j
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _enabled_probe_labels(workspace: Path) -> set[str]:
    """루트 매니페스트가 켜 주는 프로브 라벨들."""
    enabled: set[str] = set()
    for label, manifests in _PROBE_RUNTIME_MANIFESTS.items():
        if any((workspace / m).is_file() for m in manifests):
            enabled.add(label)
    return enabled


def _find_executable_entrypoint(workspace: Path) -> Optional[str]:
    """실행 진입점을 **내용으로** 찾는다. 못 찾으면 None.

    반환값은 찾은 파일의 워크스페이스 상대 경로다(진단에 쓰인다).

    두 가지 방어가 걸려 있다:
    1. **매니페스트 스코프** — 루트에 그 런타임의 매니페스트가 있어야만
       해당 언어 프로브가 돈다 (`_PROBE_RUNTIME_MANIFESTS` 참고).
    2. **잡음 제거** — 주석·문자열 안의 `static void main` 예시는
       선언이 아니다 (`_strip_probe_noise` 참고).
    """
    skipped = {
        "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
        ".git", "target", ".gradle", "vendor", "out", "test", "tests",
    }
    enabled = _enabled_probe_labels(workspace)
    for label, patterns, pattern_re in _EXECUTABLE_ENTRYPOINT_PROBES:
        if label not in enabled:
            continue
        scanned = 0
        for glob_pattern in patterns:
            for path in workspace.glob(glob_pattern):
                if scanned >= _PROBE_MAX_FILES:
                    break
                try:
                    rel_parts = path.relative_to(workspace).parts
                except ValueError:
                    continue
                if any(part in skipped for part in rel_parts):
                    continue
                if not path.is_file():
                    continue
                scanned += 1
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")[:40_000]
                except OSError:
                    continue
                if pattern_re.search(_strip_probe_noise(text, label)):
                    return path.relative_to(workspace).as_posix()
            if scanned >= _PROBE_MAX_FILES:
                break
    return None


def check_app_entrypoint(
    workspace: Path,
    contract: ReleaseContract,
) -> CheckResult:
    """진입점 파일 (main.py / app.py / index.js 등) 이 워크스페이스에 존재하는지.

    스택별 후보 리스트를 순회하며 첫 매치를 찾는다.
    """
    start = time.monotonic()
    stack = contract.project.stack
    candidates = _ENTRYPOINT_CANDIDATES.get(stack, _ENTRYPOINT_CANDIDATES[ContractStack.CUSTOM])

    found: Optional[str] = None
    for rel in candidates:
        # **폴더가 아니라 파일이어야 한다.**
        #
        # 예전엔 `exists()` 로만 봐서 `src/main/java` 같은 **폴더가 있다는
        # 이유로** 진입점을 찾았다고 판정했다. 그러면 실행 가능한 main 이
        # 하나도 없는 라이브러리도 통과해, 뜨지 않는 이미지를 배포 준비
        # 완료로 보고한다 — 검사를 약화시키는 형태다.
        if (workspace / rel).is_file():
            found = rel
            break

    # 파일 후보로 못 찾았으면, **실행 진입점을 내용으로 확인**한다.
    # Java/Kotlin 처럼 진입점이 패키지 트리 깊숙이 있는 런타임을 위한 것이며,
    # 폴더 존재가 아니라 `static void main` / `func main` 을 실제로 찾는다.
    probe_hit: Optional[str] = None
    if found is None:
        probe_hit = _find_executable_entrypoint(workspace)

    details = {
        "stack": stack.value,
        "candidates": list(candidates),
        "found": found or probe_hit,
        "found_by": "candidate" if found else ("probe" if probe_hit else None),
    }
    found = found or probe_hit

    if found:
        return CheckResult(
            code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
            passed=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            details=details,
        )

    return CheckResult(
        code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
        passed=False,
        duration_ms=int((time.monotonic() - start) * 1000),
        blocker=PreflightBlocker(
            code=PreflightCheckCode.APP_ENTRYPOINT_NOT_FOUND,
            message=f"{stack.value} 진입점 파일을 찾을 수 없습니다.",
            fix_hint=f"다음 후보 중 하나를 생성하세요: {', '.join(candidates[:3])}",
            remediation_available=False,  # 자동 생성은 위험 — 사용자 가이드만
            severity=PreflightSeverity.HIGH,
        ),
        details=details,
    )
