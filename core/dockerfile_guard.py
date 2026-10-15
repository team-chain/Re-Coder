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

#: 공식 설치 스크립트(파이프 실행을 문서로 안내하는 곳). 호스트 전체가 아니라 그 스크립트 경로만(2.0.32: deno.land/x 는
#: 누구나 모듈을 올릴 수 있어 호스트 전체를 믿으면 안 된다).
TRUSTED_INSTALLERS = {
    "deb.nodesource.com", "rpm.nodesource.com", "sh.rustup.rs", "install.python-poetry.org", "astral.sh",
    "bun.sh/install", "get.pnpm.io", "get.docker.com", "raw.githubusercontent.com/nvm-sh", "deno.land/install.sh",
    "deno.land/x/install", "raw.githubusercontent.com/golangci/golangci-lint", "sdk.cloud.google.com",
    "raw.githubusercontent.com/helm/helm",
}
#: 토큰을 실어 보내도 되는 곳 — 그 토큰의 주인(레지스트리·코드 저장소) API.
TOKEN_HOSTS = {
    "api.github.com", "github.com", "codeload.github.com", "npm.pkg.github.com", "registry.npmjs.org",
    "gitlab.com", "pypi.org", "files.pythonhosted.org", "upload.pypi.org", "registry.yarnpkg.com",
}


@dataclass
class Violation:
    rule: str
    line: int
    message: str
    text: str


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """줄 이음(\\)을 합친 명령 줄 [(시작 줄 번호, 내용)].

    Docker 와 같게: 이음 중간의 주석 줄은 건너뛰고(명령을 끊지 않는다), `RUN <<EOF … EOF` heredoc 본문은 그 명령에 붙인다
    (2.0.32 — 예전에는 둘 다 명령을 끊어, 그 뒤 줄을 검사하지 못했다)."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    heredoc: Optional[str] = None
    for i, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.rstrip()
        if heredoc is not None:
            if line.strip() == heredoc:
                out.append((start, buf.strip()))
                buf, heredoc = "", None
            else:
                buf += " ; " + line
            continue
        if not buf:
            start = i
            if line.lstrip().startswith("#"):
                continue
        elif line.lstrip().startswith("#"):
            continue
        hd = re.search(r"<<-?\s*['\"]?(\w+)['\"]?\s*$", line)
        if hd and not line.endswith("\\"):
            buf += line[:hd.start()]
            heredoc = hd.group(1)
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


def _token_host(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "") in TOKEN_HOSTS
    except ValueError:
        return False


_SHELL = r"(sudo\s+)?(/usr)?(/bin/)?(ba|z|da|k|a)?sh\b"
_INTERP = r"(sudo\s+)?(/usr)?(/(local/)?bin/)?(python3?|node|perl|ruby|php)\b"
_PIPE_SH = re.compile(rf"\b(curl|wget)\b([^|;&]*)\|\s*({_SHELL}|{_INTERP})")
#: sh -c "$(curl …)" · bash <(curl …) · source <(wget …) · eval "$(curl …)" — 값으로 쓰는 VER=$(curl …) 는 실행이 아니다.
_SUBST_SH = re.compile(r"""(\b(ba|z|da|k|a)?sh\s+-c\s*['"]?\s*\$\(\s*(curl|wget)\b|\beval\s+['"]?\$\(\s*(curl|wget)\b|"""
                       r"""(\b(ba|z|da|k|a)?sh|\bsource|(^|[\s;&|])\.)\s+<\(\s*(curl|wget)\b)""")
_URL = re.compile(r"https?://[^\s'\"|;&)]+")
#: 비밀 파일을 **읽는** 것(2.0.32): 쓰기(echo … > .npmrc)·known_hosts 등록은 정상이다.
_SECRET_FILE = re.compile(r"(\.aws/credentials|\.aws/config|\.ssh/id_\w+|\bid_rsa\b|\bid_ed25519\b|\.git-credentials|\.netrc\b)")
#: 첫 인자(읽는 쪽)가 비밀 파일일 때만 — `cp .env.example .env`·`cat > .env` 처럼 쓰는 대상이면 정상.
_SECRET_READ = re.compile(r"\b(cat|head|tail|less|more|base64|xxd|od|source|cp|scp|rsync|tar\s+-?c\w*f?\s+\S+|zip\s+\S+)"
                          r"\s+(?:-\S+\s+)*(?!>)[\"']?(\S*?(\.npmrc|\.env|\.aws/\S+|\.ssh/id_\w+|\.git-credentials|\.netrc))[\"']?(?=\s|$|;|&|\|)")
_SECRET_ENV = re.compile(r"\$\{?(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|AWS_SESSION_TOKEN|GITHUB_TOKEN|NPM_TOKEN|\w*SECRET\w*|\w*API_KEY\w*|\w*TOKEN\w*)")
_ENV_DUMP = re.compile(r"\b(env|printenv|set|export\s+-p)\b\s*\|\s*(curl|wget|nc|ncat)\b")
_HOST_PATHS = re.compile(r"(/var/run/docker\.sock|/run/docker\.sock|(?<![\w.])/proc/(1|self)/|(?<![\w.])/host/|/etc/shadow)")
_DOWNLOAD_TO = re.compile(r"\b(curl|wget)\b[^;&|]*?\s(?:-o|-O|--output|--output-document)[\s=]+(\S+)")


def _runs_file(body: str, path: str) -> bool:
    """그 파일을 **실행**하는가 — 셸·인터프리터에 넘기거나 명령 자리에 둔다. `tar -xf 파일`·`chmod +x 파일`(tini·dumb-init 처럼
    내려받은 실행 파일을 설치만 하는 공식 방법)은 실행이 아니다(2.0.32)."""
    name = re.escape(path.strip("'\""))
    return bool(re.search(
        rf"(\b(ba|z|da|k|a)?sh|\bpython3?|\bnode|\bperl|\bruby|\bsource|(^|[\s;&|])\.)\s+(-\S+\s+)*['\"]?{name}\b"
        rf"|(^|;|&&|\|\||\bexec)\s*['\"]?(\./)?{name}(\s|$|;|&)",
        body))


def check(dockerfile: str) -> list[Violation]:
    out: list[Violation] = []
    remote: dict[str, str] = {}  # ADD <url> <대상> — 뒤에서 실행하는지 본다
    for line_no, line in _logical_lines(dockerfile):
        instr = line.split(None, 1)[0].upper() if line.split() else ""
        body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        if instr == "ADD":
            parts = [p for p in body.split() if not p.startswith("--")]
            if len(parts) >= 2 and _URL.match(parts[0]) and not _trusted(parts[0]):
                remote[parts[-1].rstrip("/")] = parts[0]
        if instr == "RUN":
            #: BuildKit 비밀·SSH 마운트는 비밀을 이미지에 남기지 않는 **올바른** 방법이다 — 그 대상 경로를 읽는 것은 정상.
            mounted = re.findall(r"--mount=[^\s]*type=(?:secret|ssh)[^\s]*", body)
            if re.search(r"--security=insecure", body):
                out.append(Violation("INSECURE_BUILD", line_no, "RUN --security=insecure 는 빌드 격리를 풉니다.", line))
            if re.search(r"--network=host", body):
                out.append(Violation("HOST_NETWORK", line_no, "RUN --network=host 는 빌드가 PC 의 네트워크를 그대로 씁니다.", line))
            urls = _URL.findall(body)
            piped = _PIPE_SH.search(body) or _SUBST_SH.search(body)
            if piped and (not urls or not all(_trusted(u) for u in urls)):
                out.append(Violation("PIPE_TO_SHELL", line_no,
                                     "내려받은 스크립트를 확인 없이 바로 실행합니다(" + (", ".join(urls[:2]) or "주소 미상") + ").", line))
            elif not piped:
                for m in _DOWNLOAD_TO.finditer(body):
                    url = next((u for u in urls if u in body[m.start():m.end() + 300]), "")
                    if not (url and _trusted(url)) and _runs_file(body[m.end():], m.group(2)):
                        out.append(Violation("PIPE_TO_SHELL", line_no,
                                             f"내려받은 파일({m.group(2)})을 확인 없이 실행합니다({url or '주소 미상'}).", line))
                        break
            for target, url in remote.items():
                if _runs_file(body, target) or _runs_file(body, target.rsplit("/", 1)[-1]):
                    out.append(Violation("PIPE_TO_SHELL", line_no, f"ADD 로 내려받은 {url} 을 확인 없이 실행합니다.", line))
                    break
            read = _SECRET_READ.search(body)
            if read and re.search(r"(?i)\.env[\w.-]*(example|sample|template|dist)", read.group(2) or ""):
                read = None
            if not mounted and (_SECRET_FILE.search(body) or read):
                out.append(Violation("SECRET_FILE", line_no, "빌드 중에 비밀 파일(.aws·.ssh 키·.npmrc·.env 등)을 읽습니다.", line))
            if re.search(r"\b(curl|wget|nc|ncat)\b", body) and _SECRET_ENV.search(body) and urls \
                    and not all(_token_host(u) for u in urls):
                out.append(Violation("SECRET_EXFIL", line_no, "비밀 환경변수를 바깥 주소로 보냅니다.", line))
            elif _ENV_DUMP.search(body):
                out.append(Violation("SECRET_EXFIL", line_no, "환경변수 전체를 바깥으로 보냅니다.", line))
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
