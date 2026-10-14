"""
ReCoder Core — 빌드 전 Dockerfile 안전 검사 (2.0.29, OWASP AI 에이전트 위험 ASI02·ASI05)

AI 가 쓴 Dockerfile 의 RUN 은 사용자 PC 의 Docker 에서 실행된다(검증 빌드·로컬 배포·ECS 이미지 빌드). 프롬프트에 숨은
지시로 AI 가 "내려받아 바로 실행", 호스트 탈출, 비밀값 유출 줄을 넣으면 그대로 돈다. 빌드하기 전에 막는다.

막는 것(오류 — 빌드하지 않음)
  · 내려받은 스크립트를 바로 셸로 실행(curl|wget … | sh) — 잘 알려진 공식 설치 스크립트 주소는 예외
  · BuildKit 의 --security=insecure, RUN --network=host
  · 호스트 Docker 소켓·호스트 경로(/var/run/docker.sock, /proc, /host)
  · 비밀 파일 읽기(.aws/credentials, .ssh, .env)나 비밀 환경변수를 바깥 주소로 보내기
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

#: 공식 설치 스크립트(파이프 실행을 문서로 안내하는 곳).
TRUSTED_INSTALLERS = {
    "deb.nodesource.com", "rpm.nodesource.com", "sh.rustup.rs", "install.python-poetry.org", "astral.sh",
    "bun.sh", "get.pnpm.io", "get.docker.com", "raw.githubusercontent.com/nvm-sh", "deno.land",
}


@dataclass
class Violation:
    rule: str
    line: int
    message: str
    text: str


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """줄 이음(\\)을 합친 명령 줄 [(시작 줄 번호, 내용)]."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.rstrip()
        if not buf:
            start = i
            if line.lstrip().startswith("#"):
                continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        if buf.strip():
            out.append((start, buf.strip()))
        buf = ""
    if buf.strip():
        out.append((start, buf.strip()))
    return out


def _trusted(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host_path = (p.hostname or "") + p.path
    return p.scheme == "https" and any(host_path == t or host_path.startswith(t.rstrip("/") + "/") or (p.hostname or "") == t
                                       for t in TRUSTED_INSTALLERS)


_PIPE_SH = re.compile(r"\b(curl|wget)\b([^|;&]*)\|\s*(sudo\s+)?(ba|z|da|k)?sh\b|\b(curl|wget)\b([^|;&]*)\|\s*(python3?|node|perl|ruby)\b")
_URL = re.compile(r"https?://[^\s'\"|;&)]+")
_SECRET_FILE = re.compile(r"(\.aws/credentials|\.aws/config|\.ssh/|id_rsa|id_ed25519|\.git-credentials|\.npmrc|\.netrc)")
_SECRET_ENV = re.compile(r"\$\{?(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|AWS_SESSION_TOKEN|GITHUB_TOKEN|NPM_TOKEN|\w*SECRET\w*|\w*API_KEY\w*|\w*TOKEN\w*)")
_HOST_PATHS = re.compile(r"(/var/run/docker\.sock|/run/docker\.sock|(?<![\w.])/proc/(1|self)/|(?<![\w.])/host/|/etc/shadow)")


def check(dockerfile: str) -> list[Violation]:
    out: list[Violation] = []
    for line_no, line in _logical_lines(dockerfile):
        instr = line.split(None, 1)[0].upper() if line.split() else ""
        body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        if instr == "RUN":
            if re.search(r"--security=insecure", body):
                out.append(Violation("INSECURE_BUILD", line_no, "RUN --security=insecure 는 빌드 격리를 풉니다.", line))
            if re.search(r"--network=host", body):
                out.append(Violation("HOST_NETWORK", line_no, "RUN --network=host 는 빌드가 PC 의 네트워크를 그대로 씁니다.", line))
            m = _PIPE_SH.search(body)
            if m:
                urls = _URL.findall(body)
                if not urls or not all(_trusted(u) for u in urls):
                    out.append(Violation("PIPE_TO_SHELL", line_no,
                                         "내려받은 스크립트를 확인 없이 바로 실행합니다(" + (", ".join(urls[:2]) or "주소 미상") + ").", line))
            if _SECRET_FILE.search(body):
                out.append(Violation("SECRET_FILE", line_no, "빌드 중에 비밀 파일(.aws·.ssh·.npmrc 등)을 읽습니다.", line))
            if re.search(r"\b(curl|wget|nc|ncat)\b", body) and _SECRET_ENV.search(body) and _URL.search(body):
                out.append(Violation("SECRET_EXFIL", line_no, "비밀 환경변수를 바깥 주소로 보냅니다.", line))
        if instr in ("RUN", "VOLUME", "COPY", "ADD") and _HOST_PATHS.search(body):
            out.append(Violation("HOST_ACCESS", line_no, "호스트 Docker 소켓·시스템 경로에 접근합니다.", line))
        if instr in ("COPY", "ADD") and re.search(r"(^|\s)(\.aws|\.ssh|~/\.aws|~/\.ssh)(/|\s|$)", body):
            out.append(Violation("SECRET_FILE", line_no, "이미지에 비밀 폴더(.aws·.ssh)를 복사합니다.", line))
    return out


def message(violations: Iterable[Violation]) -> str:
    items = list(violations)
    return "; ".join(f"{v.line}행 {v.message}" for v in items[:5]) + (f" 외 {len(items) - 5}건" if len(items) > 5 else "")


REMEDY = ("이 줄을 지우거나, 필요한 도구는 기본 이미지의 패키지 관리자(apk·apt·npm·pip)로 설치하세요. "
          "Dockerfile 을 직접 쓰지 않았다면 프로젝트 파일에 숨은 지시가 있었는지 확인하세요.")


def issues(path: str, dockerfile: Optional[str]) -> list[dict]:
    """교정 루프·배포 점검용 문제(오류)."""
    return [{"code": "DOCKERFILE_UNSAFE", "severity": "error", "file": path,
             "message": f"Dockerfile 안전 검사 {v.line}행: {v.message}", "fix": REMEDY, "rule": v.rule}
            for v in check(dockerfile or "")]
