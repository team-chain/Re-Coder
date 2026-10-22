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
#: 2.0.33 `-sSLo 파일`·`-qO 파일`·`--output=파일`·`> 파일` 도 내려받기다(curl -O 는 주소의 끝 이름으로 저장).
_DOWNLOAD_TO = re.compile(r"\b(curl|wget)\b[^;&|]*?(?:\s(?:-[a-zA-Z]*[oO]|--output|--output-document)(?:[\s=]+|(?=/))|\s*>>?\s*)(\S+)")
#: 내려받은 파일의 체크섬·서명을 확인하는 RUN — 공식 설치 문서가 안내하는 안전한 방법이라 실행해도 막지 않는다.
_VERIFIED = re.compile(r"\b(sha(1|224|256|384|512)sum|shasum)\b[^;&]*\s(-c|--check)\b|\bgpg\b[^;&|]*--verify|\bcosign\s+verify"
                       r"|\bminisign\b[^;&|]*\s-V\b")
#: 명령 앞에 붙어 다른 프로그램을 실행시키는 말(sudo sh · env sh · busybox sh …).
_PREFIX_WORDS = {"sudo", "env", "busybox", "command", "exec", "nice", "nohup", "stdbuf", "xargs", "time", "doas", "su-exec",
                 "gosu", "setsid", "unbuffer", "timeout"}
_RUNNER = re.compile(r"^((ba|z|da|k|a|fi)?sh|python[\d.]*|pypy3?|node(js)?|deno|bun|perl|ruby|php[\d.]*|pwsh|lua[\d.]*)$")
_PATH_DIRS = ("/usr/local/bin/", "/usr/local/sbin/", "/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/root/.local/bin/")
_FETCH_CODE = re.compile(r"urlopen|urllib\.request|requests\.(get|post)|httpx\.|\bfetch\(|https?\.get\(|require\(\s*['\"]https?['\"]"
                         r"|LWP::|open-uri|Net::HTTP|file_get_contents\(\s*['\"]https?|Invoke-WebRequest|\biwr\b")
_EXEC_CODE = re.compile(r"\bexec\s*\(|\beval\s*\(|\bFunction\s*\(|\bvm\.run|\bcompile\s*\(|\beval\s|Invoke-Expression|\biex\b"
                        r"|__import__\(\s*['\"](os|subprocess)|subprocess\.|child_process|os\.system")
_INLINE_FLAG = re.compile(r"\b(python[\d.]*|node|deno|bun|perl|ruby|php[\d.]*|pwsh)\b[^;&|]*?\s(-c|-e|-r|--eval|-Command)\b")
#: 토큰을 실어 보내는 인자 — 본문·폼·업로드(헤더의 인증 값은 정상).
_DATA_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F", "--form", "--form-string",
               "-T", "--upload-file", "--post-data", "--body-data", "--post-file", "--body-file"}
_AUTH_HEADERS = re.compile(r"(?i)^\s*(authorization|proxy-authorization|private-token|job-token|deploy-token|x-api-key|"
                           r"x-github-token|x-gitlab-token|npm-otp)\s*:")


def _segments(body: str) -> list[str]:
    """`;`·`&&`·`||`·`&` 로 나눈 명령 묶음(파이프 `|` 는 묶음 안에 남는다)."""
    return [seg for seg in re.split(r"\s*(?:;|&&|\|\||(?<![|&>])&(?![&>]))\s*", body) if seg.strip()]


def _command_word(stage: str) -> str:
    """파이프 한 단계에서 실제로 실행되는 프로그램 이름 — sudo·env VAR=값·busybox·xargs 같은 앞말은 건너뛴다."""
    words = stage.strip().split()
    i = 0
    while i < len(words):
        word = words[i].strip("'\"()")
        base = word.rsplit("/", 1)[-1]
        if base in _PREFIX_WORDS:
            i += 1
            if base == "timeout" and i < len(words) and re.match(r"^\d", words[i]):
                i += 1
            continue
        if word.startswith("-") or re.match(r"^\w+=", word):
            i += 1
            continue
        return base
    return ""


def _inline_runs_stdin(stage: str) -> bool:
    """`… | python3 -c "json 읽기"` 처럼 코드를 직접 주고 표준 입력은 데이터로만 쓰는가."""
    m = re.search(r"\s(-c|-e|--eval)\s+(.+)$", stage)
    return bool(m) and not _EXEC_CODE.search(m.group(2)) and "stdin.read" not in m.group(2)


def _pipes_to_runner(segment: str) -> bool:
    """curl·wget 의 출력이 뒤 단계에서 셸·인터프리터로 들어가는가(tee·env·busybox·xargs 를 거쳐도)."""
    stages = re.split(r"(?<!\|)\|(?!\|)", segment)
    first = next((i for i, st in enumerate(stages) if re.search(r"\b(curl|wget)\b", st)), None)
    if first is None:
        return False
    for stage in stages[first + 1:]:
        word = _command_word(stage)
        if _RUNNER.match(word):
            if re.match(r"^(python|pypy|node|deno|bun|perl|ruby|php|pwsh)", word) and _inline_runs_stdin(stage):
                continue
            return True
    return False


def _secret_in_payload(segment: str) -> bool:
    """토큰 주인 서버(api.github.com 등)로 보내더라도, 비밀값을 **본문·폼·업로드·인증 아닌 헤더**에 실으면 유출이다
    (예: gist 만들기로 토큰을 올리기). 인증 헤더(Authorization)·주소의 사용자 자리만 정상."""
    import shlex
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    i = 0
    while i < len(words):
        word = words[i]
        flag, value = word, None
        if word.startswith("--") and "=" in word:
            flag, value = word.split("=", 1)
        elif re.match(r"^-[dFTH].+", word):
            flag, value = word[:2], word[2:]
        if flag in _DATA_FLAGS or flag in ("-H", "--header"):
            if value is None and i + 1 < len(words):
                value = words[i + 1]
                i += 1
            if value and _SECRET_ENV.search(value) and not (flag in ("-H", "--header") and _AUTH_HEADERS.match(value)):
                return True
        i += 1
    return False


def _mount_targets(body: str) -> list[str]:
    """RUN --mount=type=secret 이 내용을 넣어 주는 경로(target= 또는 기본 /run/secrets/<id>)."""
    targets: list[str] = []
    for opts in re.findall(r"--mount=(\S+)", body):
        kv = dict(part.split("=", 1) for part in opts.split(",") if "=" in part)
        if kv.get("type") != "secret":
            continue
        target = kv.get("target") or kv.get("dst") or kv.get("destination")
        targets.append(target or f"/run/secrets/{kv.get('id') or kv.get('source') or ''}")
    return [t for t in targets if t]


def _download_name(path: str, url: str) -> str:
    """curl -O 는 주소의 마지막 이름으로 저장한다."""
    path = path.strip("'\"")
    if _URL.match(path):
        return urlparse(path).path.rsplit("/", 1)[-1]
    return path


def _runs_file(body: str, path: str) -> bool:
    """그 파일을 **실행**하는가 — 셸·인터프리터에 넘기거나 명령 자리에 둔다. `tar -xf 파일`·`chmod +x 파일`(tini·dumb-init 처럼
    내려받은 실행 파일을 설치만 하는 공식 방법)은 실행이 아니다(2.0.32)."""
    path = path.strip("'\"")
    if not path or path == "-":
        return False
    name = re.escape(path)
    lead = r"(^|;|&&|\|\||\||\bexec|\bsudo|\benv|\bbusybox|\bxargs)\s*(?:\S+=\S+\s+)*"
    if re.search(
        rf"(\b(ba|z|da|k|a|fi)?sh|\bpython[\d.]*|\bnode|\bdeno|\bbun|\bperl|\bruby|\bphp|\bsource|(^|[\s;&|])\.)\s+(-\S+\s+)*['\"]?{name}\b"
        rf"|{lead}['\"]?(\./)?{name}(\s|$|;|&|\|)",
        body):
        return True
    #: 2.0.33 PATH 폴더(/usr/local/bin 등)에 내려받은 것은 이름만으로도 실행된다.
    if path.startswith(_PATH_DIRS):
        base = re.escape(path.rsplit("/", 1)[-1])
        return bool(base) and bool(re.search(rf"{lead}{base}(\s|$|;|&|\|)", body))
    return False


def check(dockerfile: str) -> list[Violation]:
    out: list[Violation] = []
    remote: dict[str, str] = {}  # ADD <url> <대상> — 뒤에서 실행하는지 본다
    #: 2.0.33 앞 RUN 에서 내려받아(확인 없이) 뒤 RUN 에서 실행하는 것도 본다.
    fetched: dict[str, str] = {}
    for line_no, line in _logical_lines(dockerfile):
        instr = line.split(None, 1)[0].upper() if line.split() else ""
        body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        if instr == "ADD":
            parts = [p for p in body.split() if not p.startswith("--")]
            if len(parts) >= 2 and _URL.match(parts[0]) and not _trusted(parts[0]):
                remote[parts[-1].rstrip("/")] = parts[0]
        if instr == "RUN":
            #: BuildKit 비밀 마운트는 비밀을 이미지에 남기지 않는 **올바른** 방법이다 — 그 마운트 경로를 읽는 것만 정상이다
            #: (2.0.33: 전에는 마운트가 하나라도 있으면 그 RUN 의 비밀 파일 검사를 통째로 껐다).
            targets = _mount_targets(body)
            command = re.sub(r"--mount=\S+", " ", body)
            if re.search(r"--security=insecure", body):
                out.append(Violation("INSECURE_BUILD", line_no, "RUN --security=insecure 는 빌드 격리를 풉니다.", line))
            if re.search(r"--network=host", body):
                out.append(Violation("HOST_NETWORK", line_no, "RUN --network=host 는 빌드가 PC 의 네트워크를 그대로 씁니다.", line))
            urls = _URL.findall(command)
            verified = bool(_VERIFIED.search(command))
            piped_urls: list[str] = []
            for seg in _segments(command):
                if _pipes_to_runner(seg):
                    seg_urls = _URL.findall(seg)
                    if not seg_urls or not all(_trusted(u) for u in seg_urls):
                        piped_urls = seg_urls or ["주소 미상"]
                        break
            if not piped_urls and _SUBST_SH.search(command) and (not urls or not all(_trusted(u) for u in urls)):
                piped_urls = urls or ["주소 미상"]
            if not piped_urls and _INLINE_FLAG.search(command) and _FETCH_CODE.search(command) and _EXEC_CODE.search(command) \
                    and not (urls and all(_trusted(u) for u in urls)):
                piped_urls = urls or ["인라인 코드"]
            if piped_urls:
                out.append(Violation("PIPE_TO_SHELL", line_no,
                                     "내려받은 스크립트를 확인 없이 바로 실행합니다(" + ", ".join(piped_urls[:2]) + ").", line))
            else:
                flagged = False
                for m in _DOWNLOAD_TO.finditer(command):
                    url = next((u for u in _URL.findall(command[m.start():m.end() + 300])), "")
                    target = _download_name(m.group(2), url)
                    if not target or target == "-" or target.startswith("/dev/") or (url and _trusted(url)) or verified:
                        continue
                    if _runs_file(command[m.end():], target):
                        out.append(Violation("PIPE_TO_SHELL", line_no,
                                             f"내려받은 파일({target})을 확인 없이 실행합니다({url or '주소 미상'}).", line))
                        flagged = True
                        break
                    fetched[target] = url or "주소 미상"
                if not flagged:
                    for target, url in list(remote.items()) + [(t, u) for t, u in fetched.items()
                                                               if not any(m.group(2).strip("'\"") == t
                                                                          for m in _DOWNLOAD_TO.finditer(command))]:
                        if verified:
                            break
                        if _runs_file(command, target) or (target in remote and _runs_file(command, target.rsplit("/", 1)[-1])):
                            out.append(Violation("PIPE_TO_SHELL", line_no, f"앞에서 내려받은 {url} 을 확인 없이 실행합니다.", line))
                            break
            #: 마운트 경로(/run/secrets/npmrc, target=/root/.aws/credentials 등)만 빼고 본다.
            scan = command
            for target_path in sorted(targets, key=len, reverse=True):
                scan = scan.replace(target_path, " ")
            read = _SECRET_READ.search(scan)
            if read and re.search(r"(?i)\.env[\w.-]*(example|sample|template|dist)", read.group(2) or ""):
                read = None
            secret_file = _SECRET_FILE.search(scan)
            if secret_file or read:
                out.append(Violation("SECRET_FILE", line_no, "빌드 중에 비밀 파일(.aws·.ssh 키·.npmrc·.env 등)을 읽습니다.", line))
            exfil = False
            for seg in _segments(command):
                if not (re.search(r"\b(curl|wget|nc|ncat|socat)\b", seg) and _SECRET_ENV.search(seg)):
                    continue
                seg_urls = _URL.findall(seg)
                if re.search(r"\b(nc|ncat|socat)\b", seg) or (seg_urls and not all(_token_host(u) for u in seg_urls)) \
                        or (seg_urls and _secret_in_payload(seg)):
                    exfil = True
                    break
            if exfil:
                out.append(Violation("SECRET_EXFIL", line_no, "비밀 환경변수를 바깥 주소로 보냅니다.", line))
            elif _ENV_DUMP.search(command):
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
