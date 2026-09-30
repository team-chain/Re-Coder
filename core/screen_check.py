"""배포한 주소가 **실제로 화면을 보여 주는지** 확인한다.

왜 필요한가
    헬스 경로(/health)가 200 이어도 화면은 비어 있을 수 있다. 실기기에서 로컬 Docker·S3 모두
    "주소만 뜨고 화면이 안 나오는" 배포가 성공으로 끝났다(TEMP 쇼핑몰 — 화면 빌드 폴더 없음,
    ESM/CommonJS 혼용). 이 모듈은 배포 직후 사람이 브라우저로 여는 것과 같은 확인을 한다.

두 단계
    1. HTTP: 첫 화면이 HTML 인가, HTML 이 부르는 JS·CSS 가 200 이고 올바른 형식인가
       (SPA 폴백이 없는 파일에 index.html 을 돌려주면 브라우저는 스크립트를 거부해 빈 화면이 된다),
       빌드하지 않은 개발용 index.html(/src/main.jsx)을 올리지 않았는가.
    2. 브라우저: PC 에 있는 Edge·Chrome·Chromium 을 화면 없이(headless) 띄워 스크립트 실행 뒤의
       DOM 을 받아 보이는 내용이 있는지 본다. 콘솔 오류(Uncaught …)도 함께 모은다.
       브라우저가 없으면 1단계 결과만으로 판정한다(판정 근거를 결과에 남긴다).

원칙
    - 확실한 것만 실패로 본다. API 가 느리거나 데이터가 없어도 화면 틀이 그려지면 통과다.
    - 사용자 데이터·자격증명을 보내지 않는다. 주소 하나를 GET 할 뿐이다.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional

_JS_TYPES = ("javascript", "ecmascript", "text/jsx", "application/x-javascript")
_ATTR = r"""(?:["']([^"']+)["']|([^\s"'>]+))"""
_UNBUILT_SCRIPT = re.compile(r"<script[^>]+src=" + _ATTR, re.I)
_SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc=" + _ATTR, re.I)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_HREF = re.compile(r"\bhref=" + _ATTR, re.I)
_REL = re.compile(r"\brel=" + _ATTR, re.I)
_DEV_SOURCE = re.compile(r"\.(?:jsx|tsx|ts)(?:\?.*)?$", re.I)


def _attr(m) -> str:
    return (m.group(1) or m.group(2) or "") if m else ""


def _unbuilt(markup: str) -> bool:
    return any(_DEV_SOURCE.search(_attr(m)) for m in _UNBUILT_SCRIPT.finditer(markup))


@dataclass
class ScreenResult:
    ok: bool
    url: str
    checked: str = "http"            # http | browser | skipped
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    title: str = ""
    text_sample: str = ""
    browser: str = ""
    code: str = ""                   # 실패 종류(진단 카드용)

    def to_dict(self) -> dict:
        return asdict(self)

    def diagnosis(self) -> dict:
        """배포 결과 화면의 진단 카드(build_failure 와 같은 모양)."""
        cause = " / ".join(self.problems[:3]) or "화면이 비어 있습니다."
        lines = [f"콘솔 오류: {e}" for e in self.console_errors[:4]]
        return {
            "code": self.code or "SCREEN_BLANK",
            "title": "배포 주소는 열리지만 화면이 표시되지 않습니다",
            "cause": cause,
            "fix": _FIX_BY_CODE.get(self.code, _FIX_BY_CODE["SCREEN_BLANK"]),
            "lines": lines,
            "step": "화면 확인 (브라우저로 첫 화면 열기)",
        }


_FIX_BY_CODE = {
    "SCREEN_NOT_HTML": "서버가 화면(빌드한 index.html)을 제공하도록 하세요. Deploy 화면의 '배포 준비 점검'에서 "
                       "자동 수정(화면 빌드 연결·정적 제공)을 적용한 뒤 다시 배포하세요.",
    "SCREEN_ASSET_MISSING": "화면 빌드 결과(JS·CSS)가 배포에 들어가지 않았습니다. 빌드 폴더를 다시 만들고"
                            "(npm run build) 그 폴더 전체를 배포하세요. 서버형이면 package.json 의 build 가 화면을 빌드하는지 확인하세요.",
    "SCREEN_UNBUILT": "빌드하지 않은 개발용 index.html 이 배포됐습니다. npm run build 로 만든 dist/ (또는 build/) 폴더를 배포하세요.",
    "SCREEN_SCRIPT_ERROR": "화면 코드가 실행 중 오류로 멈췄습니다. 아래 콘솔 오류의 파일·줄을 고친 뒤 다시 배포하세요"
                           "(process.env → import.meta.env, 없는 export 등은 배포 준비 점검이 자동으로 고칩니다).",
    "SCREEN_BLANK": "스크립트는 실행됐지만 보이는 내용이 없습니다. 첫 화면 컴포넌트가 무엇을 그리는지, "
                    "라우터 경로(basename)가 배포 주소와 맞는지 확인하세요.",
    "SCREEN_HTTP_ERROR": "첫 화면 요청이 오류로 끝났습니다. 컨테이너 로그에서 서버 오류를 확인하세요.",
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _opener(url: str):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        # 사내 프록시 환경에서도 내 PC 의 컨테이너는 프록시를 거치지 않는다.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _get(url: str, timeout: float = 10.0) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "ReCoder-ScreenCheck/1.0", "Accept": "text/html,*/*"})
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(200_000)
        except Exception:  # noqa: BLE001
            pass
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", body


def _visible_text(markup: str) -> str:
    body = re.search(r"<body\b[^>]*>(.*)</body>", markup, re.I | re.S)
    text = body.group(1) if body else markup
    text = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _has_visual(markup: str) -> bool:
    body = re.search(r"<body\b[^>]*>(.*)</body>", markup, re.I | re.S)
    part = body.group(1) if body else markup
    part = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", part, flags=re.I | re.S)
    return bool(re.search(r"<(img|svg|canvas|video|iframe|input|button)\b", part, re.I))


def _asset_urls(page_url: str, markup: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for m in _SCRIPT_SRC.finditer(markup):
        found.append(("script", urllib.parse.urljoin(page_url, _attr(m))))
    for tag in _LINK_TAG.findall(markup):
        rel = _attr(_REL.search(tag)).lower()
        href = _attr(_HREF.search(tag))
        if href and ("stylesheet" in rel or "modulepreload" in rel):
            found.append(("style" if "stylesheet" in rel else "script", urllib.parse.urljoin(page_url, href)))
    page_host = urllib.parse.urlparse(page_url).netloc
    #: 다른 사이트(CDN)는 판정하지 않는다 — 네트워크 정책에 따라 막힐 수 있다.
    return [(kind, u) for kind, u in found if urllib.parse.urlparse(u).netloc == page_host][:20]


def _http_stage(url: str, result: ScreenResult, wait_seconds: float) -> Optional[str]:
    """HTML 을 돌려주면 그 내용, 판정이 끝났으면(실패) None."""
    deadline = time.monotonic() + max(wait_seconds, 0)
    status, ctype, body = 0, "", b""
    last_error = ""
    while True:
        try:
            status, ctype, body = _get(url)
            if status < 500 or time.monotonic() >= deadline:
                break
        except Exception as exc:  # noqa: BLE001 - 연결 거부·시간 초과는 잠시 기다려 다시 본다
            last_error = str(exc)
            if time.monotonic() >= deadline:
                break
        time.sleep(1.5)
    if not status:
        result.code = "SCREEN_HTTP_ERROR"
        result.problems.append(f"{url} 에 연결하지 못했습니다({last_error or '응답 없음'}).")
        return None
    text = body.decode("utf-8", errors="replace")
    if status >= 400:
        result.code = "SCREEN_HTTP_ERROR" if status >= 500 else "SCREEN_NOT_HTML"
        snippet = _visible_text(text)[:160] if "html" in ctype.lower() else text.strip()[:160]
        result.problems.append(f"첫 화면({url})이 HTTP {status} 을(를) 돌려줍니다: {snippet or '(내용 없음)'}")
        return None
    if "html" not in ctype.lower() and not re.search(r"<html|<!doctype html", text[:500], re.I):
        result.code = "SCREEN_NOT_HTML"
        result.problems.append(f"첫 화면이 HTML 이 아니라 {ctype or '알 수 없는 형식'} 입니다: {text.strip()[:160]}")
        return None
    result.title = (re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S).group(1).strip()
                    if re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S) else "")
    if _unbuilt(text) or "%PUBLIC_URL%" in text:
        result.code = "SCREEN_UNBUILT"
        result.problems.append("빌드하지 않은 개발용 index.html 입니다(/src/*.jsx 를 직접 부르거나 %PUBLIC_URL% 이 남아 있음). "
                               "브라우저는 이 파일을 실행하지 못해 빈 화면이 됩니다.")
        return None
    for kind, asset in _asset_urls(url, text):
        try:
            a_status, a_type, a_body = _get(asset)
        except Exception as exc:  # noqa: BLE001
            result.code = "SCREEN_ASSET_MISSING"
            result.problems.append(f"화면 파일 {asset} 을(를) 받지 못했습니다({exc}).")
            continue
        a_type = a_type.lower()
        path = urllib.parse.urlparse(asset).path
        if a_status >= 400:
            result.code = "SCREEN_ASSET_MISSING"
            result.problems.append(f"화면 파일 {path} 이(가) HTTP {a_status} 입니다.")
        elif kind == "script" and ("text/html" in a_type or a_body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))):
            result.code = "SCREEN_ASSET_MISSING"
            result.problems.append(f"스크립트 {path} 대신 HTML(index.html)이 돌아옵니다 — 그 파일이 배포에 없습니다. "
                                   "브라우저가 스크립트를 거부해 화면이 비게 됩니다.")
        elif kind == "script" and a_type and not any(t in a_type for t in _JS_TYPES) and "octet-stream" not in a_type:
            result.code = "SCREEN_ASSET_MISSING"
            result.problems.append(f"스크립트 {path} 의 형식이 {a_type} 입니다 — 브라우저가 모듈 스크립트로 실행하지 않습니다.")
        elif kind == "script" and "octet-stream" in a_type:
            result.code = "SCREEN_ASSET_MISSING"
            result.problems.append(f"스크립트 {path} 가 application/octet-stream 으로 제공됩니다 — 모듈 스크립트는 실행되지 않습니다.")
        elif kind == "style" and "text/html" in a_type:
            result.warnings.append(f"스타일 {path} 대신 HTML 이 돌아옵니다(스타일 없이 표시됨).")
    if result.problems:
        return None
    return text


# ---------------------------------------------------------------------------
# 브라우저
# ---------------------------------------------------------------------------

def find_browser() -> Optional[str]:
    """PC 에 설치된 Chromium 계열 브라우저(Edge·Chrome·Chromium). 없으면 None."""
    override = os.environ.get("RECODER_BROWSER")
    if override and os.path.isfile(override):
        return override
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                           os.path.join(base, "Chromium", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/Applications/Chromium.app/Contents/MacOS/Chromium",
                       os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                     "microsoft-edge-stable"):
            path = shutil.which(name)
            if path:
                candidates.append(path)
        candidates.append("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


_CONSOLE_LINE = re.compile(r"CONSOLE\(\d+\)\]\s*\"(.*?)\",\s*source:\s*(\S*)\s*\((\d+)\)")
_CONSOLE_LINE_NEW = re.compile(r":(?:INFO|ERROR|WARNING):CONSOLE[^\]]*\]\s*\"(.*?)\"(?:,\s*source:\s*(\S*)\s*\((\d+)\))?")


def _render(browser: str, url: str, budget_ms: int, timeout: float) -> tuple[str, list[str]]:
    profile = tempfile.mkdtemp(prefix="recoder-screen-")
    args = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-sync", "--mute-audio", "--hide-scrollbars",
            f"--user-data-dir={profile}", "--enable-logging=stderr", "--v=0",
            f"--virtual-time-budget={budget_ms}", "--window-size=1280,900", "--dump-dom", url]
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        args.insert(1, "--no-proxy-server")
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")  # root 로 도는 리눅스(컨테이너·CI)에서만
    kwargs: dict = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    dom = proc.stdout.decode("utf-8", errors="replace")
    errors: list[str] = []
    for line in proc.stderr.decode("utf-8", errors="replace").splitlines():
        m = _CONSOLE_LINE.search(line) or _CONSOLE_LINE_NEW.search(line)
        if not m:
            continue
        message = m.group(1)
        source = m.group(2) or ""
        if re.search(r"Uncaught|Error|is not defined|Failed to|Cannot|MIME type|SyntaxError|TypeError|ReferenceError",
                     message):
            where = f" ({urllib.parse.urlparse(source).path}:{m.group(3)})" if source else ""
            if f"{message}{where}" not in errors:
                errors.append(f"{message}{where}")
    return dom, errors


def _browser_stage(url: str, result: ScreenResult, browser: str, markup: str) -> None:
    try:
        dom, errors = _render(browser, url, budget_ms=8000, timeout=45)
    except subprocess.TimeoutExpired:
        result.warnings.append("브라우저 확인이 시간 안에 끝나지 않아 HTTP 확인 결과로 판정했습니다.")
        return
    except OSError as exc:
        result.warnings.append(f"브라우저를 실행하지 못해 HTTP 확인 결과로 판정했습니다: {exc}")
        return
    if not dom.strip() or re.search(r"\bERR_(?:CONNECTION|NAME|PROXY|TIMED|ADDRESS|TUNNEL)[A-Z_]*", dom[:20000]) and \
            "neterror" in dom[:5000]:
        result.warnings.append("브라우저가 페이지를 불러오지 못해 HTTP 확인 결과로 판정했습니다.")
        return
    result.checked = "browser"
    result.browser = os.path.basename(browser)
    result.console_errors = errors[:8]
    text = _visible_text(dom)
    result.text_sample = text[:200]
    if text or _has_visual(dom):
        if errors:
            result.warnings.append("화면은 표시되지만 브라우저 콘솔에 오류가 있습니다: " + errors[0][:200])
        return
    result.ok = False
    if errors:
        result.code = "SCREEN_SCRIPT_ERROR"
        result.problems.append("화면 스크립트가 오류로 멈춰 아무것도 그리지 못했습니다: " + errors[0][:300])
    else:
        result.code = "SCREEN_BLANK"
        result.problems.append("스크립트 실행 뒤에도 페이지에 보이는 내용이 없습니다(빈 화면).")


def enabled() -> bool:
    """테스트 실행(RECODER_TEST_MODE=1)에서는 실제 네트워크·브라우저를 쓰지 않는다(RECODER_SCREEN_CHECK=1 이면 켠다)."""
    if os.environ.get("RECODER_SCREEN_CHECK") == "1":
        return True
    if os.environ.get("RECODER_SCREEN_CHECK") == "0":
        return False
    return os.environ.get("RECODER_TEST_MODE") != "1"


def check_screen(url: str, *, wait_seconds: float = 20.0, use_browser: bool = True) -> ScreenResult:
    """url 의 첫 화면이 실제로 보이는지 확인한다."""
    result = ScreenResult(ok=True, url=url)
    markup = _http_stage(url, result, wait_seconds)
    if markup is None:
        result.ok = False
        return result
    browser = find_browser() if use_browser and os.environ.get("RECODER_SCREEN_BROWSER", "1") != "0" else None
    if browser:
        _browser_stage(url, result, browser, markup)
    else:
        # 브라우저 없이: 스크립트 없는 HTML 인데 보이는 내용도 없으면 빈 화면이다.
        if not _SCRIPT_SRC.search(markup) and not _visible_text(markup) and not _has_visual(markup):
            result.ok = False
            result.code = "SCREEN_BLANK"
            result.problems.append("첫 화면 HTML 에 보이는 내용도 실행할 스크립트도 없습니다(빈 화면).")
        else:
            result.warnings.append("PC 에서 Edge·Chrome 을 찾지 못해 HTML·JS·CSS 응답까지만 확인했습니다.")
    return result


if __name__ == "__main__":  # pragma: no cover - 수동 확인용
    import json
    print(json.dumps(check_screen(sys.argv[1]).to_dict(), ensure_ascii=False, indent=2))
