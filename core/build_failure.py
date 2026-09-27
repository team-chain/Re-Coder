"""`docker build` 실패 출력 → 사람이 읽을 원인·해결책·핵심 줄.

왜 필요한가
    BuildKit 은 마지막에 실패한 Dockerfile 줄과 요약만 찍는다. 실제 원인
    (``Could not find a required file``, ``Cannot find module`` …)은 그보다
    위에 있어서, 마지막 몇 줄만 보여 주면 사용자는 "무엇이" 실패했는지 모른다.
    실기기 test temp 에서 정확히 그 상태였다.

동작
    1. BuildKit plain 출력의 ``#12 0.761`` 접두어를 걷어낸다.
    2. 알려진 실패 패턴을 찾아 원인·해결책을 붙인다(없으면 일반 안내).
    3. 원인 근처의 **실제 에러 줄**을 최대 12줄 추려 준다.
    4. 정적 점검(build_readiness)에 같은 원인이 이미 잡혀 있으면 그 해결책을 우선한다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

_PREFIX = re.compile(r"^(?:#\d+\s+(?:\d+\.\d+(?:\s+|$))?|\s*\d+\.\d{1,3}(?:\s|$))")
_NOISE = re.compile(
    r"^(?:\[\+\] Building|=> |#\d+ (?:\[internal\]|DONE|CACHED|sha256:|resolve |transferring|load )|"
    r"-{5,}|\s*\d+ \|\s|Dockerfile:\d+|View build details|WARNING: )"
)


@dataclass
class BuildDiagnosis:
    code: str
    title: str
    cause: str
    fix: str
    lines: list[str] = field(default_factory=list)
    step: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# (코드, 정규식, 제목, 원인, 해결책) — 먼저 맞는 것이 이긴다. {0} 은 첫 캡처 그룹.
_RULES: list[tuple[str, re.Pattern[str], str, str, str]] = [
    ("CRA_ENTRY_MISSING", re.compile(r"Could not find a required file\.\s*Name:\s*(\S+)(?:\s*Searched in:\s*(\S+))?", re.I),
     "빌드 진입 파일 없음",
     "`react-scripts build` 가 {path} 를 찾지 못했습니다. package.json 의 build 스크립트가 Create React App 을 가정하지만 프로젝트에 해당 파일이 없습니다.",
     "React 앱을 빌드하려면 src/index.js 를 만드세요. public/ 파일을 서버가 그대로 제공하는 구조라면 package.json 의 build 스크립트를 지우세요."),
    ("NPM_LOCK_OUT_OF_SYNC", re.compile(r"`npm ci` can only install packages when your package\.json and package-lock\.json|lock file.*out of sync|Missing: \S+ from lock file", re.I),
     "package-lock.json 이 package.json 과 다름",
     "package.json 을 고친 뒤 package-lock.json 을 갱신하지 않아 `npm ci` 가 설치를 거부했습니다.",
     "프로젝트 폴더에서 `npm install` 을 한 번 실행해 package-lock.json 을 갱신한 뒤 다시 배포하세요."),
    ("NPM_VERSION_NOT_FOUND", re.compile(r"No matching version found for ((?:@[\w.-]+/)?[\w.-]+@[^\s]+?)\.?(?:\s|$)", re.I),
     "없는 패키지 버전",
     "npm 레지스트리에 `{0}` 가 없습니다. package.json 의 버전 범위를 만족하는 버전이 배포된 적이 없습니다(AI 가 만든 코드에서 흔합니다).",
     "package.json 의 해당 의존성을 실제로 있는 버전으로 고치세요. 배포 준비 점검의 자동 수정이 가장 가까운 버전을 넣어 줍니다."),
    ("NPM_PACKAGE_NOT_FOUND", re.compile(r"npm (?:ERR!|error) 404 Not Found - GET \S+/((?:@[\w.-]+(?:%2f|/))?[\w.-]+)", re.I),
     "없는 패키지",
     "npm 레지스트리에 `{0}` 패키지가 없습니다. 이름이 틀렸거나 비공개 패키지입니다.",
     "package.json 의 패키지 이름을 확인하세요."),
    ("NPM_SCRIPT_MISSING", re.compile(r"Missing script:\s*\"?([\w:-]+)\"?", re.I),
     "npm 스크립트 없음",
     "`{0}` 스크립트가 package.json 에 없습니다.",
     "package.json 의 scripts 에 `{0}` 를 추가하거나 Dockerfile 의 명령을 고치세요."),
    ("NODE_MODULE_NOT_FOUND", re.compile(r"(?:Cannot find module|Module not found: (?:Error: )?Can't resolve)\s+'([^']+)'", re.I),
     "모듈을 찾을 수 없음",
     "`{0}` 를 불러오지 못했습니다. package.json 에 선언되지 않았거나 경로가 틀렸습니다.",
     "외부 패키지라면 `npm install {0}` 로 의존성에 추가하고, 프로젝트 파일이라면 경로와 파일명 대소문자를 확인하세요(Linux 컨테이너는 대소문자를 구분합니다)."),
    ("COMMAND_NOT_FOUND", re.compile(r"(?:sh|/bin/sh|bash): (?:(?:line )?\d+: )?([\w.@/-]+): (?:not found|command not found)", re.I),
     "명령을 찾을 수 없음",
     "빌드 중 `{0}` 명령이 없었습니다. 해당 도구가 의존성에 없거나 devDependencies 가 설치되지 않은 단계에서 실행됐습니다.",
     "`{0}` 를 제공하는 패키지를 package.json 의존성에 추가하거나, 쓰지 않는 스크립트라면 지우세요."),
    ("TYPESCRIPT_ERROR", re.compile(r"error (TS\d+):", re.I),
     "TypeScript 컴파일 오류",
     "TypeScript 타입 검사({0})에서 빌드가 멈췄습니다.",
     "아래 줄의 파일·줄 번호를 고치거나, PC 에서 `npm run build` 로 같은 오류를 재현해 확인하세요."),
    ("NATIVE_MODULE_BUILD", re.compile(r"gyp ERR!|node-pre-gyp|prebuild-install (?:warn|err)|not found: (?:make|python3?|g\+\+)", re.I),
     "네이티브 모듈 컴파일 실패",
     "C/C++ 로 빌드되는 패키지(sqlite3·bcrypt 등)를 컨테이너에서 컴파일하지 못했습니다. Alpine 이미지에는 컴파일 도구가 없습니다.",
     "Dockerfile 의 베이스 이미지를 `node:22-slim` 으로 바꾸거나, 설치 단계 전에 `RUN apk add --no-cache python3 make g++` 를 추가하세요."),
    ("PIP_NO_DISTRIBUTION", re.compile(r"No matching distribution found for\s+(\S+)|Could not find a version that satisfies the requirement\s+(\S+)", re.I),
     "Python 패키지 설치 실패",
     "requirements 의 `{0}` 를 설치할 수 없습니다. 이름이나 버전이 틀렸거나 이 Python 버전을 지원하지 않습니다.",
     "requirements.txt 의 패키지 이름·버전을 확인하세요. 로컬에서 되던 버전이라면 Dockerfile 의 Python 버전을 맞추세요."),
    ("PY_MODULE_NOT_FOUND", re.compile(r"ModuleNotFoundError: No module named '([^']+)'", re.I),
     "Python 모듈 없음",
     "`{0}` 모듈을 찾지 못했습니다.",
     "requirements.txt 에 해당 패키지를 추가하세요."),
    ("COPY_SOURCE_MISSING", re.compile(r"failed to compute cache key: .*?\"?/?([^\"\s]+)\"?: not found|COPY failed: .*?(\S+): no such file", re.I),
     "복사할 파일 없음",
     "Dockerfile 의 COPY 가 `{0}` 를 찾지 못했습니다. 파일이 없거나 .dockerignore 에서 제외됐습니다.",
     "파일이 있는지, .dockerignore 가 그 파일을 제외하지 않는지 확인하세요."),
    ("BASE_IMAGE_PULL", re.compile(r"failed to resolve source metadata|pull access denied|manifest (?:for \S+ )?not found|TLS handshake timeout|i/o timeout|no such host", re.I),
     "베이스 이미지를 받지 못함",
     "Docker Hub 등에서 베이스 이미지를 내려받지 못했습니다(네트워크·이미지 이름·로그인 문제).",
     "인터넷 연결과 Docker Desktop 로그인 상태를 확인하고, Dockerfile 의 FROM 이미지 이름·태그가 맞는지 확인하세요."),
    ("DISK_FULL", re.compile(r"no space left on device", re.I),
     "디스크 공간 부족",
     "Docker 가 쓸 디스크 공간이 부족합니다.",
     "Docker Desktop → Troubleshoot 또는 `docker system prune` 으로 쓰지 않는 이미지를 정리하세요."),
    ("DOCKER_DAEMON", re.compile(r"Cannot connect to the Docker daemon|error during connect|docker daemon is not running", re.I),
     "Docker 가 실행 중이 아님",
     "Docker 엔진에 연결하지 못했습니다.",
     "Docker Desktop 을 실행한 뒤 다시 배포하세요."),
    ("SQLITE_CANTOPEN", re.compile(r"SQLITE_CANTOPEN|unable to open database file", re.I),
     "DB 파일을 열 수 없음",
     "컨테이너 사용자에게 작업 폴더 쓰기 권한이 없어 SQLite 파일을 만들지 못했습니다.",
     "Dockerfile 의 USER 앞에 작업 폴더 chown 을 추가하세요(배포 준비 점검의 자동 수정)."),
    ("PERMISSION_DENIED", re.compile(r"EACCES: permission denied[^\n]*|PermissionError: \[Errno 13\][^\n]*", re.I),
     "파일 권한 없음",
     "컨테이너 사용자에게 필요한 파일·폴더 권한이 없습니다.",
     "쓰기가 필요한 폴더를 Dockerfile 에서 앱 사용자 소유로 바꾸세요(`RUN chown <사용자> <폴더>`)."),
    ("HOST_PORT_IN_USE", re.compile(r"Bind for [^\s]*:(\d+) failed: port is already allocated|"
                                    r"ports are not available: exposing port TCP [^\s]*:(\d+)|"
                                    r"listen tcp[^\n]*:(\d+): bind: (?:address already in use|Only one usage)", re.I),
     "PC 포트가 이미 사용 중",
     "PC 의 {0} 포트를 다른 컨테이너나 프로그램이 쓰고 있어 새 컨테이너를 시작하지 못했습니다.",
     "'새 배포'로 계획을 다시 만들면 비어 있는 포트로 자동 조정됩니다. {0} 포트를 꼭 써야 하면 "
     "`docker ps --filter publish={0}` 로 쓰고 있는 컨테이너를 확인해 멈추세요."),
    ("PORT_IN_USE", re.compile(r"EADDRINUSE|Address already in use", re.I),
     "포트 사용 중",
     "앱이 듣는 포트를 다른 프로세스가 이미 쓰고 있습니다.",
     "같은 포트를 쓰는 다른 컨테이너·프로그램을 멈추거나 앱 포트를 바꾸세요."),
    ("NPM_ERROR", re.compile(r"npm (?:ERR!|error) (?:code )?(E[A-Z0-9]+)", re.I),
     "npm 설치·실행 오류",
     "npm 이 {0} 오류로 멈췄습니다.",
     "아래 줄을 확인하세요. 대부분 package.json 의 의존성 이름·버전 문제입니다."),
]

_ERROR_LINE = re.compile(
    r"(?:error|ERR!|failed|cannot|can't|could not|not found|missing|exception|traceback|fatal|denied|"
    r"no such file|unexpected|syntaxerror|\bName:|Searched in:)", re.I)


def _clean(output: str) -> list[str]:
    lines = []
    for raw in (output or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _PREFIX.sub("", raw.rstrip())
        if line.strip():
            lines.append(line)
    return lines


def _failed_step(lines: list[str]) -> str:
    for line in reversed(lines):
        m = re.search(r"ERROR \[([^\]]+)\] (RUN .+?)(?::)?$", line) or re.search(r'process "/bin/sh -c (.+?)" did not complete', line)
        if m:
            return (m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)).strip()[:200]
    return ""


def _key_lines(lines: list[str], anchor: Optional[int]) -> list[str]:
    """원인 줄 주변과 에러로 보이는 줄. BuildKit 요약·Dockerfile 발췌는 뺀다."""
    useful = [(i, l) for i, l in enumerate(lines) if not _NOISE.match(l.strip())]

    def unique(items: list[str]) -> list[str]:  # BuildKit 은 실패 출력을 요약에서 한 번 더 찍는다
        seen: set[str] = set()
        return [x for x in items if not (x.strip() in seen or seen.add(x.strip()))]

    if anchor is not None:
        window = unique([l for i, l in useful if anchor - 3 <= i <= anchor + 6])
        if window:
            return window[:12]
    picked = unique([l for _, l in useful if _ERROR_LINE.search(l)])
    return (picked or unique([l for _, l in useful]))[-12:]


def diagnose(output: str, readiness_issues: Optional[list] = None, stage: str = "build") -> BuildDiagnosis:
    """stage="run" 이면 컨테이너 로그(시작 직후 종료)를 진단한다."""
    lines = _clean(output)
    text = "\n".join(lines)
    step = _failed_step(lines)
    for code, pattern, title, cause, fix in _RULES:
        m = pattern.search(text)
        if not m:
            continue
        value = next((g for g in m.groups() if g), "") if m.groups() else ""
        path = value
        if code == "CRA_ENTRY_MISSING" and m.group(2):
            # 컨테이너 경로(/app/src) → 프로젝트 기준 경로(src/index.js)
            folder = re.sub(r"^/(?:app|usr/src/app|srv|workspace)/?", "", m.group(2).rstrip("/"))
            path = f"{folder}/{value}" if folder else value
        anchor = text[:m.start()].count("\n")
        diagnosis = BuildDiagnosis(code, title, cause.format(value, path=path), fix.format(value, path=path),
                                   _key_lines(lines, anchor), step)
        break
    else:
        if stage == "run":
            diagnosis = BuildDiagnosis(
                "UNKNOWN", "컨테이너 실행 실패", "컨테이너가 시작 직후 종료됐거나 요청에 응답하지 않습니다.",
                "아래 앱 로그에서 원인을 확인하세요. PC 에서 같은 시작 명령(npm start 등)으로 재현할 수 있습니다.",
                _key_lines(lines, None), "")
        else:
            diagnosis = BuildDiagnosis(
                "UNKNOWN", "이미지 빌드 실패",
                f"Docker 빌드가 `{step}` 단계에서 멈췄습니다." if step else "Docker 빌드가 실패했습니다.",
                "아래 줄에서 원인을 확인하세요. 같은 명령을 프로젝트 폴더에서 직접 실행하면 재현할 수 있습니다.",
                _key_lines(lines, None), step)
    # 정적 점검이 이미 같은 원인을 짚었다면 그 해결책(자동 수정 포함)을 앞세운다.
    for issue in readiness_issues or []:
        severity = getattr(issue, "severity", None) or (issue.get("severity") if isinstance(issue, dict) else None)
        if severity != "error":
            continue
        message = getattr(issue, "message", None) or issue.get("message", "")
        fix = getattr(issue, "fix", None) or issue.get("fix", "")
        code = getattr(issue, "code", None) or issue.get("code", "")
        related = (diagnosis.code == "CRA_ENTRY_MISSING" and code in {"NODE_UNUSED_BUILD_SCRIPT", "NODE_BUILD_ENTRY_MISSING"}) \
            or (diagnosis.code in {"NODE_MODULE_NOT_FOUND", "COMMAND_NOT_FOUND"} and code in {"NODE_UNDECLARED_DEPENDENCY", "NODE_BUILD_TOOL_MISSING", "DOCKERFILE_SUBPROJECT_DEPS_MISSING"}) \
            or (diagnosis.code in {"NPM_VERSION_NOT_FOUND", "NPM_PACKAGE_NOT_FOUND", "NPM_ERROR"} and code == "NODE_DEPENDENCY_VERSION_NOT_FOUND") \
            or (diagnosis.code == "PY_MODULE_NOT_FOUND" and code.startswith("PY_")) \
            or (diagnosis.code in {"SQLITE_CANTOPEN", "PERMISSION_DENIED"} and code == "DOCKERFILE_WORKDIR_NOT_WRITABLE") \
            or (diagnosis.code == "UNKNOWN" and stage == "run" and code in {"DOCKERFILE_PORT_MISMATCH", "NODE_START_ENTRY_MISSING", "DOCKERFILE_ENTRY_MISSING"})
        if related:
            diagnosis.cause = f"{diagnosis.cause} {message}".strip()
            diagnosis.fix = fix
            break
    return diagnosis
