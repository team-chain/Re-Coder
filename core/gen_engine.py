"""대규모 코드 생성 엔진 — 요청이 아무리 커도 자르지 않고 끝까지 만든다.

AI 호출 한 번에 앱 전체를 담지 않는다(게이트웨이는 호출당 30초·4096 토큰). 대신 개발팀처럼:

1. 설계 먼저 — 요약, 파일 사이의 약속(API·DB·타입·인증), 파일 목록을 받는다.
   목록은 25개씩 "더 있음"이 끝날 때까지 이어 받고, 약속이 길면 조각으로 이어 쓴다. 상한 없음.
2. 층별로 — 공통 기반(설정·스키마·DB·인증)을 먼저 순서대로 만들고, 그것을 실제 코드로 보면서
   기능·화면 파일을 여러 에이전트가 **동시에** 만든다. 약속과 기반 코드를 공유하니 합쳐도 맞는다.
3. 파일 하나가 응답 한도를 넘으면 150줄씩 **이어 쓴다**(끝날 때까지).
4. 만들 때마다 확인 — 문법(JSON·Python·Node)과 하드코딩된 비밀을 바로 검사하고, 틀리면 바뀔 부분만
   고친다(파일 전체를 다시 쓰지 않아 멀쩡한 코드가 망가지지 않는다).
5. 파일 하나 끝날 때마다 체크포인트 — 멈춰도 다 만든 것은 남고 같은 작업 ID 로 이어 만든다.
6. 일시적 오류(과부하·네트워크)는 기다렸다 다시 하고, 게이트웨이 분당 한도는 미리 지킨다.
7. 받은 내용은 저장 전에 검사한다 — AI 가 파일 내용 자리에 응답 형식(JSON)을 통째로 넣으면 실제 내용만 꺼내고,
   꺼낼 수 없으면 버리고 다시 받는다. 체크포인트를 불러올 때도 같은 검사를 한다(망가진 조각을 이어 쓰지 않는다).
8. 같은 파일이 실패하면 방법을 바꾼다 — 조각을 줄이고(150→80→40줄), 처음부터 다시 쓰고, 그래도 안 되면 그 파일만
   "실패"로 두고 나머지를 끝까지 만든다. 같은 자리를 끝없이 반복하지 않고, 멈춘 이유를 항상 남긴다.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Any, Callable

from llm.base import LLMError, LLMErrorType, LLMRequest
from code_output import CODE_OUTPUT_SCHEMA, CodeOutputError, parse_code_output
import generation_jobs as jobs

PAGE_FILES = 25          # 파일 목록 한 번에 받는 개수(응답 한도 안에 확실히 들어가는 크기)
BATCH = 2                # 작은 파일은 두 개씩 묶어 호출 수를 줄인다(넘치면 하나씩 → 이어 쓰기)
PART_LINES = 150         # 이어 쓰기 한 조각의 줄 수
MAX_FILE_TRIES = 3       # 한 파일을 이번 실행에서 시도하는 횟수 — 넘으면 그 파일만 실패로 두고 나머지를 계속 만든다
#: 파일 내용을 글자 그대로 받을 때 "다 썼다" 표시(파일에는 넣지 않는다).
END_MARKER = "<<<RECODER_END_OF_FILE>>>"
#: 글자 그대로 받는 한 번의 응답 상한 — 게이트웨이는 4096 으로 깎지만 끊겨도 받은 만큼 쓰므로 상관없다.
STREAM_MAX_TOKENS = 8192
#: 공급자가 끊김 여부를 모를 때 이 길이 이상이면 끊긴 것으로 본다(짧은 응답은 끝 표시만 빠뜨린 것).
STREAM_CUT_GUESS = 3000
#: 시도마다 쓰는 방법 — 같은 방법을 되풀이하지 않는다.
STRATEGY_TEXT = ("한 번에 받기", "글자 그대로 이어 받기", "맥락을 줄여 처음부터 이어 받기")
PART_SAFETY = 400        # 무한 반복 방지용(약 6만 줄). 진행이 없을 때만 실제로 멈춘다
FILES_SAFETY = 600       # 무한 목록 방지용. 진행이 없을 때만 실제로 멈춘다
FIX_ROUNDS = 2           # 파일별 즉시 교정 횟수
_SECRET_FIX_KINDS = {"generic_secret_assignment", "aws_access_key", "private_key", "github_token",
                     "slack_token", "stripe_key", "google_api_key", "jwt"}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "contracts": {"type": "string"},
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"file": {"type": "string", "minLength": 1}, "purpose": {"type": "string"},
                           "layer": {"type": "integer"},
                           #: 이 파일이 불러올 프로젝트 파일(목록의 경로) — 그 파일이 먼저 완성된 뒤 실제 내용을 보고 쓴다
                           "uses": {"type": "array", "items": {"type": "string"}},
                           #: 예상 크기 — large 는 처음부터 글자 그대로 이어 받는다(한 번에 받다 잘려 버리는 호출을 아낀다)
                           "size": {"type": "string", "enum": ["small", "medium", "large"]}},
            "required": ["file"], "additionalProperties": False}},
        "more": {"type": "boolean"},
    },
    "required": ["summary", "contracts", "files", "more"],
    "additionalProperties": False,
}
PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "files": PLAN_SCHEMA["properties"]["files"],
        "more": {"type": "boolean"},
    },
    "required": ["files", "more"],
    "additionalProperties": False,
}
PART_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string"}, "done": {"type": "boolean"}},
    "required": ["content", "done"],
    "additionalProperties": False,
}
EDIT_SCHEMA = {
    "type": "object",
    "properties": {"edits": {"type": "array", "items": {
        "type": "object",
        "properties": {"find": {"type": "string"}, "replace": {"type": "string"}},
        "required": ["find", "replace"], "additionalProperties": False}}},
    "required": ["edits"],
    "additionalProperties": False,
}


def _ca():
    import code_agent as ca  # 지연 import — code_agent 가 이 모듈을 부른다
    return ca


def _norm(path: str) -> str:
    out = posixpath.normpath(str(path or "").strip().replace("\\", "/"))
    while out.startswith("./"):
        out = out[2:]
    return out.lstrip("/").casefold()


# ── 호출 속도 · 재시도 ─────────────────────────────────────────────────

class RateLimiter:
    """분당 호출 수 상한(게이트웨이 GW_DEFAULT_RPM). 넘기기 전에 기다려 429 를 피한다."""

    def __init__(self, rpm: int) -> None:
        self.rpm = max(0, rpm)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, on_wait: Callable[[float], None] | None = None) -> None:
        if not self.rpm:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                if len(self._calls) < self.rpm:
                    self._calls.append(now)
                    return
                wait = 60 - (now - self._calls[0]) + 0.2
            if on_wait:
                on_wait(wait)
            time.sleep(min(wait, 5))


def _default_limiter() -> RateLimiter:
    try:
        from llm.gateway_provider import gateway_enabled
    except ImportError:  # pragma: no cover
        gateway_enabled = lambda: False  # noqa: E731
    env = os.environ.get("RECODER_LLM_RPM")
    if env is not None:
        try:
            return RateLimiter(int(env))
        except ValueError:
            pass
    #: 게이트웨이 기본 한도는 학생당 분당 10회 — 한 칸 남겨 다른 기능(채팅 등)이 막히지 않게 한다.
    return RateLimiter(9 if gateway_enabled() else 0)


def _transient(exc: Exception) -> tuple[bool, float]:
    """(다시 해 볼 만한가, 기다릴 초)."""
    if isinstance(exc, LLMError):
        text = str(exc)
        if exc.error_type == LLMErrorType.QUOTA_EXCEEDED:
            # 분당 한도는 1분 기다리면 풀린다. 일일·총량 한도는 기다려도 안 풀린다 → 일시 정지.
            return ("분당" in text or "rate" in text.lower()), 61.0
        if exc.error_type in (LLMErrorType.THROTTLING, LLMErrorType.SERVICE_ERROR) or exc.retryable:
            return True, 0.0
        return False, 0.0
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True, 0.0
    return False, 0.0


def _is_truncation(exc: Exception) -> bool:
    return isinstance(exc, LLMError) and exc.error_type == LLMErrorType.STRUCTURED_OUTPUT and "잘렸" in str(exc)


class Paused(Exception):
    """기다려도 안 풀리는 실패(일일 한도 등) — 체크포인트를 남기고 멈춘다."""


#: 실패 종류 → 사람이 읽을 이름(진행 기록·멈춤 이유·로그에 같은 말을 쓴다).
ERROR_TEXT = {
    "truncation": "응답이 길이 한도에서 잘림",
    "format": "응답 형식이 맞지 않음",
    "quota": "AI 사용 한도",
    "network": "AI 연결이 불안정함",
    "other": "알 수 없는 오류",
}


def error_kind(exc: BaseException) -> str:
    if _is_truncation(exc):  # type: ignore[arg-type]
        return "truncation"
    if isinstance(exc, LLMError):
        if exc.error_type == LLMErrorType.STRUCTURED_OUTPUT:
            return "format"
        if exc.error_type == LLMErrorType.QUOTA_EXCEEDED:
            return "quota"
        if exc.error_type in (LLMErrorType.THROTTLING, LLMErrorType.SERVICE_ERROR) or exc.retryable:
            return "network"
        return "other"
    if isinstance(exc, (CodeOutputError, ValueError)):
        return "format"
    if isinstance(exc, Paused):
        return "quota" if "한도" in str(exc) else "network"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    return "other"


def _recoverable(exc: BaseException) -> bool:
    """방법을 바꿔 다시 해 볼 수 있는 실패인가(잘림·형식). 인증 오류 같은 것은 바로 멈춘다."""
    return error_kind(exc) in ("truncation", "format")


# ── 받은 내용 검사(응답 형식이 파일 내용에 섞이는 것 막기) ──────────────────

#: 파일 내용 자리에 AI 응답 형식(JSON)이 통째로 들어온 모양 — {"summary": …, "ops": [ … ]} 또는 {"content": …, "done": …}
_WRAP_START = re.compile(r'^\s*\{\s*"(?:summary|ops|content|done|action|file|rationale)"\s*:')
_WRAP_TRACE = re.compile(r'"rationale"\s*:\s*"[^"\n]{0,400}"\s*\}\s*\]\s*\}?\s*$|"action"\s*:\s*"(?:create|edit)"\s*,\s*"file"\s*:', re.S)
_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}


def _decode_json_string(s: str, start: int) -> str:
    """s[start:] 이 JSON 문자열 본문(여는 따옴표 다음)이라 보고 닫는 따옴표까지(잘렸으면 끝까지) 풀어 돌려준다."""
    out: list[str] = []
    i, n = start, len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            break
        if ch == "\\":
            if i + 1 >= n:
                break
            nxt = s[i + 1]
            if nxt == "u":
                hexs = s[i + 2:i + 6]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", hexs):
                    break
                out.append(chr(int(hexs, 16)))
                i += 6
                continue
            out.append(_ESC.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    try:  # 서로게이트 쌍(이모지 등) 합치기
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def is_wrapped(path: str, text: str) -> bool:
    """파일 내용이 AI 응답 형식(JSON)으로 싸여 있거나 그 흔적이 남았는가."""
    if not isinstance(text, str) or not text.strip():
        return False
    if path.lower().endswith(".json"):
        head = text.lstrip()[:600]
        return bool(re.match(r'\{\s*"(?:summary|ops)"\s*:', head) and re.search(r'"ops"\s*:\s*\[', text)) \
            or bool(re.match(r'\{\s*"content"\s*:\s*"', head) and re.search(r'"done"\s*:\s*(?:true|false)', text))
    return bool(_WRAP_START.match(text) or _WRAP_TRACE.search(text))


def unwrap_content(path: str, text: str) -> str | None:
    """AI 응답 형식으로 싸인 내용에서 그 파일의 실제 내용을 꺼낸다. 꺼낼 수 없으면 None.

    응답이 길이 한도에서 잘려 JSON 이 닫히지 않았어도(이어 쓰기 조각에서 흔하다) content 문자열을 끝까지 풀어 쓴다.
    """
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        ops = data.get("ops")
        if isinstance(ops, list):
            items = [op for op in ops if isinstance(op, dict)]
            mine = [op for op in items if _norm(str(op.get("file") or "")) == _norm(path)]
            #: 이 파일 것이 있으면 그것, 없으면 하나뿐일 때만(여러 파일이면 어느 것인지 추측하지 않는다)
            pick = mine or (items if len(items) == 1 else [])
            content = pick[0].get("content") if pick else None
            return content if isinstance(content, str) and content.strip() else None
        content = data.get("content")
        return content if isinstance(content, str) and content.strip() else None
    #: 잘린 JSON — 이 파일의 content 문자열을 찾아 끝까지 푼다.
    name = re.escape(posixpath.basename(str(path or "")))
    anchor = re.search(rf'"file"\s*:\s*"[^"]*{name}"', text) if name else None
    m = re.search(r'"content"\s*:\s*"', text[anchor.end():] if anchor else text)
    if not m:
        return None
    body = _decode_json_string(text, (anchor.end() if anchor else 0) + m.end())
    return body if body.strip() else None


def _unescape_flat(text: str) -> str:
    """줄바꿈이 \\n 글자로 들어온 한 줄짜리 내용(JSON 문자열을 두 번 감싼 응답)을 원래 줄로 되돌린다."""
    if "\n" in text or text.count("\\n") < 3:
        return text
    return re.sub(r'\\(["\\/nrt])', lambda m: _ESC.get(m.group(1), m.group(1)), text)


_DOUBLED_SIGN = re.compile(r"\$\{\{|(?:\)|=>|\belse)[ \t]*\{\{[ \t]*$", re.MULTILINE)


def undouble_braces(path: str, text: str) -> str:
    """AI 가 코드의 중괄호를 두 겹({{ … }})으로 쓴 경우 한 겹으로(실기기 2.0.10 Haiku: 결제 화면 3개가
    `=> {{`·`${{orderId}}`·`toLocaleString('en-US', {{ … }})` 로 와서 tsc 가 TS1005 로 멈췄다).
    JSX 속성 객체(style={{ … }})는 원래 두 겹이라 그대로 둔다. 따옴표 문자열·주석은 건드리지 않는다.
    징후(`${{`, 줄 끝의 `) {{`·`=> {{`)가 없으면 그대로, 고친 결과의 괄호 수가 맞지 않으면 그대로 돌려준다."""
    if not str(path).lower().endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) or "{{" not in text:
        return text
    if len(_DOUBLED_SIGN.findall(text)) < 2:
        return text
    out: list[str] = []
    stack: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"" :
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("{{", i):
            k = len(out) - 1
            prev = ""
            joined = "".join(out[-3:]).rstrip()
            prev = joined[-1:] if joined else ""
            if prev == "=":
                out.append("{{")
                stack += ["L", "L"]
            else:
                out.append("{")
                stack.append("D")
            i += 2
            continue
        if ch == "{":
            out.append("{")
            stack.append("L")
            i += 1
            continue
        if text.startswith("}}", i) and stack and stack[-1] == "D":
            out.append("}")
            stack.pop()
            i += 2
            continue
        if ch == "}":
            out.append("}")
            if stack:
                stack.pop()
            i += 1
            continue
        out.append(ch)
        i += 1
    fixed = "".join(out)
    if stack or fixed.count("{") != fixed.count("}") or len(_DOUBLED_SIGN.findall(fixed)) > 0:
        return text
    return fixed


def clean_content(path: str, text: str) -> str | None:
    """저장해도 되는 파일 내용으로 다듬는다 — 코드펜스·응답 형식·이중 이스케이프를 벗긴다. 못 쓰는 내용이면 None."""
    if not isinstance(text, str):
        return None
    out = text
    if out.lstrip().startswith("```"):
        out = re.sub(r"^\s*```[\w.+-]*\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out)
    for _ in range(2):  # 두 겹으로 싸인 경우까지
        if not is_wrapped(path, out):
            break
        inner = unwrap_content(path, out)
        if inner is None:
            return None
        out = inner
    if not path.lower().endswith(".json"):  # JSON 파일의 한 줄 문자열 속 \n 은 정상이다
        out = _unescape_flat(out)
    if is_wrapped(path, out):
        return None
    return undouble_braces(path, out)


# ── 엔진 ───────────────────────────────────────────────────────────────

class LargeGeneration:
    def __init__(self, prompt: str, *, target_folder: str = "", new_project: bool = False,
                 job_id: str = "", fingerprint: str = "", concurrency: int | None = None,
                 emit: Callable[[dict], None] | None = None, limiter: RateLimiter | None = None,
                 security_review: bool = True, skip_failed: bool = False,
                 plan_hook: Callable[[list[dict]], tuple[list[dict], list[dict], str]] | None = None,
                 root: str = "") -> None:
        self.prompt = prompt
        #: 프로젝트 루트(디스크) — 기존 프로젝트에 더할 때 이미 있는 파일을 "없는 파일"로 보지 않게.
        self.root = root
        #: 설계 직후 목록을 고치고 고정 파일을 미리 넣는다(결제 모듈 등) → (새 목록, 미리 만든 ops, 약속에 붙일 글).
        self.plan_hook = plan_hook
        #: 응답 형식(ops JSON) 지시를 뺀 맥락 — 설계·조각·교정 호출은 각자 다른 형식으로 답해야 하므로
        #: 이 지시가 같이 들어가면 AI 가 조각 내용 자리에 ops JSON 을 통째로 넣는다(실기기 pages.css).
        self.context_prompt = _without_output_format(prompt)
        #: 이어 만들 때 실패한 파일을 빼고 결과를 받는다(사용자가 고른 경우만).
        self.skip_failed = skip_failed
        self.skipped: list[dict] = []
        #: AI 가 끝내 못 써서 설계로 자동 작성한 문서 [{file, reason}]
        self.fallback_docs: list[dict] = []
        self.target_folder = target_folder
        self.new_project = new_project
        self.job_id = job_id or jobs.new_job_id()
        self.fp = fingerprint
        self.emit = emit or (lambda e: None)
        self.limiter = limiter or _default_limiter()
        self.security_review = security_review
        if concurrency is None:
            try:
                concurrency = int(os.environ.get("RECODER_GEN_CONCURRENCY", "3"))
            except ValueError:
                concurrency = 3
        self.concurrency = max(1, min(8, concurrency))
        self.state: dict[str, Any] = {"fingerprint": fingerprint, "manifest": None, "files": [],
                                      "ops": {}, "partial": {}, "attempts": {}, "failed": {}}
        self.last_resp = None
        self._lock = threading.Lock()
        self._started = time.monotonic()

    # ── 저장 / 이벤트 ──
    def _save(self) -> None:
        if self.fp:
            with self._lock:
                snapshot = json.loads(json.dumps(self.state, ensure_ascii=False))
            jobs.save(self.job_id, snapshot)

    def _event(self, step: str, **data: Any) -> None:
        total = len(self.state.get("files") or [])
        done = len(self.state.get("ops") or {})
        self.emit({"step": step, "done_count": done, "total": total, "job_id": self.job_id,
                   "elapsed": round(time.monotonic() - self._started, 1), **data})

    def _log(self, path: str, kind: str, message: str) -> None:
        """코어 로그 한 줄 — 멈춘 이유를 추측하지 않도록 작업·파일·종류를 남긴다(값·코드 원문은 남기지 않는다)."""
        print(f"[gen_engine] job={self.job_id} file={path or '-'} kind={kind}: {message[:240]}", flush=True)

    def _note_failure(self, target: dict, exc: BaseException, agent: str) -> int:
        """파일 하나가 실패했다 — 시도 횟수를 올리고 다음 방법을 알린다. 올린 뒤의 시도 횟수를 돌려준다."""
        key, path = _norm(target["file"]), target["file"]
        kind = error_kind(exc)
        with self._lock:
            info = dict(self.state["attempts"].get(key) or {})
            info.update(tries=int(info.get("tries", 0)) + 1, last=kind, message=str(exc)[:300], file=path)
            self.state["attempts"][key] = info
            self.state["partial"].pop(path, None)
        tries = info["tries"]
        self._log(path, kind, f"{tries}번째 실패 — {exc}")
        if tries < MAX_FILE_TRIES:
            how = STRATEGY_TEXT[min(tries, len(STRATEGY_TEXT) - 1)]
            self._event("file_retry", agent=agent, file=path, kind=kind, attempt=tries + 1,
                        message=f"{path} — {ERROR_TEXT[kind]} → {how}로 다시 씁니다 ({tries + 1}/{MAX_FILE_TRIES})")
        self._save()
        return tries

    def _finish_fallback_doc(self, target: dict, agent: str) -> None:
        """문서를 끝내 못 받았다 — 설계에서 기본 문서를 만들어 둔다(앱 동작과 무관, "자동 작성" 으로 표시)."""
        path = target["file"]
        with self._lock:
            ops = [dict(op) for op in self.state["ops"].values()]
        content = fallback_doc(path, self.state.get("manifest") or {}, ops)
        info = self.state["attempts"].get(_norm(path)) or {}
        kind = info.get("last", "other")
        self._log(path, kind, f"{MAX_FILE_TRIES}번 시도해도 실패 — 설계에서 기본 문서를 자동 작성")
        self.fallback_docs.append({"file": path, "reason": ERROR_TEXT.get(kind, kind)})
        op = {"action": "create", "file": path, "content": content, "language": "markdown",
              "rationale": f"AI 작성이 {ERROR_TEXT.get(kind, kind)}(으)로 실패해 설계에서 자동 작성", "fallback": True}
        with self._lock:
            self.state["ops"][_norm(path)] = op
            self.state["failed"].pop(_norm(path), None)
        self._save()
        self._event("file_fallback", agent=agent, file=path, kind=kind,
                    message=f"{path} — AI 가 끝내 못 써서 설계로 기본 문서를 자동 작성했습니다")
        self._event("file_done", agent=agent, file=path, lines=content.count("\n") + 1, message=f"{path} 완료(자동 작성)")

    def _fail(self, target: dict, agent: str) -> None:
        key, path = _norm(target["file"]), target["file"]
        info = self.state["attempts"].get(key) or {}
        kind = info.get("last", "other")
        with self._lock:
            self.state["failed"][key] = {"file": path, "kind": kind, "reason": ERROR_TEXT.get(kind, kind),
                                         "detail": str(info.get("message") or "")[:300]}
        self._save()
        self._log(path, kind, f"{MAX_FILE_TRIES}번 시도해도 실패 — 이 파일만 두고 나머지를 계속 만듦")
        self._event("file_failed", agent=agent, file=path, kind=kind,
                    message=f"{path} — {MAX_FILE_TRIES}번 시도해도 {ERROR_TEXT.get(kind, kind)} · 나머지를 먼저 만듭니다")

    def resume_from(self, saved: dict | None) -> bool:
        if not saved or not saved.get("manifest") or not saved.get("files"):
            return False
        self.state = {"fingerprint": self.fp, "manifest": saved["manifest"], "files": saved["files"],
                      "ops": dict(saved.get("ops") or {}), "partial": dict(saved.get("partial") or {}),
                      "attempts": dict(saved.get("attempts") or {}), "failed": dict(saved.get("failed") or {})}
        repaired = self._check_saved()
        failed = list(self.state["failed"].values())
        if failed and not self.skip_failed:
            #: 사용자가 [이어서 만들기]를 다시 눌렀다 — 실패했던 파일을 가장 작은 조각·처음부터로 한 번 더 시도한다.
            for key in list(self.state["failed"]):
                self.state["attempts"][key] = dict(self.state["attempts"].get(key) or {}, tries=MAX_FILE_TRIES - 1)
                self.state["partial"].pop(self.state["failed"][key].get("file", ""), None)
            self.state["failed"] = {}
        note = f" · 망가진 조각 {repaired}개 정리" if repaired else ""
        note += f" · 실패했던 파일 {len(failed)}개 {'빼고 결과 받기' if self.skip_failed else '다시 시도'}" if failed else ""
        self._event("resumed", message=f"멈춘 지점부터 이어서 만듭니다 — {len(self.state['ops'])}/{len(self.state['files'])}개 완료{note}")
        return True

    def _check_saved(self) -> int:
        """불러온 체크포인트의 조각·완성 파일을 다시 검사한다 — 망가진 것을 이어 쓰거나 결과로 내지 않게.

        조각에 응답 형식이 섞였으면 실제 내용만 꺼내 이어 쓰고, 꺼낼 수 없으면 그 조각을 버려 처음부터 다시 쓴다.
        완성 파일이 망가졌으면 완성 목록에서 빼서 다시 만든다. 고친 수를 돌려준다.
        """
        fixed = 0
        for path, part in list(self.state["partial"].items()):
            written = (part or {}).get("written", "") if isinstance(part, dict) else ""
            cleaned = clean_content(path, written) if written else None
            if not cleaned or not cleaned.strip():
                self.state["partial"].pop(path, None)
                fixed += 1
                self._log(path, "format", "저장된 조각이 망가져 버리고 처음부터 다시 씀")
            elif cleaned != written:
                self.state["partial"][path] = dict(part, written=cleaned)
                fixed += 1
                self._log(path, "format", "저장된 조각에서 응답 형식을 벗겨 실제 내용만 이어 씀")
        for key, op in list(self.state["ops"].items()):
            content = op.get("content") if isinstance(op, dict) else None
            cleaned = clean_content(str((op or {}).get("file") or key), content) if isinstance(content, str) else None
            if cleaned is None:
                self.state["ops"].pop(key, None)
                fixed += 1
            elif cleaned != content:
                self.state["ops"][key] = dict(op, content=cleaned)
                fixed += 1
        if fixed:
            self._save()
        return fixed

    # ── AI 호출(속도 제한 + 재시도) ──
    def call(self, prompt: str, schema: dict | None, operation: str, max_tokens: int, agent: str = "", raw: bool = False) -> Any:
        ca = _ca()
        attempt, waits = 0, (3, 8, 20, 45, 60)
        while True:
            self.limiter.acquire(lambda s: self._event("waiting", agent=agent, seconds=round(s),
                                                       message=f"AI 호출 한도 때문에 {round(s)}초 기다리는 중"))
            try:
                resp = ca.get_router().call(
                    LLMRequest(prompt=prompt, json_schema=None if raw else schema, max_tokens=max_tokens,
                               temperature=0.2, raw_text=raw),
                    agent="code_agent", operation=operation)
                self.last_resp = resp
                return resp
            except Exception as exc:  # noqa: BLE001
                if _is_truncation(exc):
                    raise
                retry, wait = _transient(exc)
                if not retry or attempt >= len(waits):
                    if isinstance(exc, LLMError) and exc.error_type == LLMErrorType.QUOTA_EXCEEDED:
                        raise Paused(f"AI 사용 한도에 걸렸습니다: {exc}") from exc
                    if retry:
                        raise Paused(f"AI 연결이 계속 불안정합니다: {exc}") from exc
                    raise
                delay = max(wait, waits[attempt])
                attempt += 1
                self._event("retry", agent=agent, seconds=delay,
                            message=f"일시적 오류로 {delay:.0f}초 뒤 다시 시도합니다 ({attempt}/{len(waits)})")
                time.sleep(delay)

    # ── 1. 설계: 요약 · 약속 · 파일 목록(상한 없이 이어 받기) ──
    def plan(self) -> None:
        ca = _ca()
        if self.state.get("manifest") and self.state.get("files"):
            return
        self._event("planning", message="설계 중 — 파일 사이의 약속과 만들 파일 목록을 정리합니다")
        header = self.context_prompt + f"""

[대규모 생성 1단계 — 설계] 이 요청은 파일이 많아 여러 에이전트가 나눠 만듭니다. 이번 응답에서는 **파일 내용을 쓰지 말고**
아래 JSON 만 주세요.
- summary: 무엇을 만드는지 한국어 한 줄
- contracts: 파일 사이의 정확한 약속 — API 경로/메서드/요청/응답/인증 필드, DB 테이블/열/타입, 모듈 export 이름과 시그니처,
  페이지 경로, 환경 변수 이름, 통화·금액 단위, 주문/결제 상태 전이, 패키지 이름·버전. 다른 에이전트가 이것만 보고 맞춰 씁니다.
- files: 만들거나 고칠 파일. 이번 응답에는 최대 {PAGE_FILES}개만 넣고, 더 있으면 more=true(다음 응답에서 이어서 받습니다).
  각 파일의 layer: 0=공통 기반(설정·패키지·DB 스키마/연결·인증·공용 타입/유틸), 1=기능(API·서비스·모델), 2=화면·진입점·문서.
  각 파일의 size: 예상 길이 small(~80줄)·medium(~250줄)·large(그보다 김).
  각 파일의 uses: 이 파일이 import 할 **프로젝트 안의 파일** 경로(이 목록에 있는 경로 그대로, 외부 패키지는 빼고).
  ReCoder 는 uses 의 파일을 먼저 완성하고, 그 실제 내용을 보여 주며 이 파일을 쓰게 합니다(서로 상관없는 파일은 동시에).
  contracts 에는 화면 공용 컴포넌트의 props(이름·타입·필수 여부), 훅·Context 가 돌려주는 필드 이름과 타입,
  공용 타입(예: CartItem 의 필드)도 적으세요 — 화면 파일들이 이것을 글자 그대로 씁니다.
  의존성 순서로 나열하세요. root package.json/Dockerfile/.dockerignore/README, 로그인·회원가입, DB 초기화 등 실행에 필요한 파일을
  빠뜨리지 마세요. 파일 수는 줄이지 말고 실제로 필요한 만큼 모두 적으세요."""
        files: list[dict] = []
        try:
            resp = self.call(header, PLAN_SCHEMA, "generate_code_manifest", 6000, agent="planner")
            data = ca._extract_json(resp.text)
            summary = str(data.get("summary") or "").strip()
            contracts = str(data.get("contracts") or "").strip()
            more = bool(data.get("more"))
            files = self._merge_files(files, data.get("files") or [])
        except (LLMError, CodeOutputError, ValueError) as exc:
            if isinstance(exc, LLMError) and not _is_truncation(exc) and exc.error_type != LLMErrorType.STRUCTURED_OUTPUT:
                raise
            # 약속이 길어 한 응답에 안 들어간다 — 요약은 짧게 받고 약속은 조각으로 이어 쓴다.
            self._event("planning", message="약속 문서가 길어 나눠서 받는 중")
            resp = self.call(self.context_prompt + "\n\n이 요청이 무엇을 만드는지 한국어 한 줄로만 JSON {\"summary\": \"...\"} 으로 답하세요.",
                             {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                             "generate_code_manifest", 512, agent="planner")
            summary = str(ca._extract_json(resp.text).get("summary") or "").strip()
            contracts = self.write_in_parts(
                header + "\n\n지금은 파일 목록 없이 contracts(파일 사이의 약속) 문서만 씁니다.",
                "파일 사이의 약속(contracts) 문서", agent="planner", operation="generate_code_contracts")
            more = True
        manifest = {"summary": summary, "contracts": contracts}
        stalls = 0
        while more and len(files) < FILES_SAFETY:
            listed = "\n".join(f"- {f['file']}" for f in files) or "(아직 없음)"
            page_prompt = self.context_prompt + f"""

[대규모 생성 1단계 — 파일 목록 이어 받기]
요약: {summary}
약속:
{contracts}
이미 받은 파일(다시 쓰지 마세요):
{listed}
아직 목록에 없는 파일을 의존성 순서로 최대 {PAGE_FILES}개 더 주세요(파일 내용은 쓰지 마세요). 각 파일에 layer(0/1/2)와
uses(import 할 프로젝트 파일 경로)를 붙이고,
아직 남았으면 more=true, 이것으로 끝이면 more=false."""
            resp = self.call(page_prompt, PAGE_SCHEMA, "generate_code_manifest", 4096, agent="planner")
            try:
                page = ca._extract_json(resp.text)
            except ValueError:
                page = {"files": [], "more": True}
            before = len(files)
            files = self._merge_files(files, page.get("files") or [])
            more = bool(page.get("more"))
            self._event("planning", message=f"파일 목록 {len(files)}개 정리됨")
            stalls = stalls + 1 if len(files) == before else 0
            if stalls >= 2:
                break
        if not files:
            raise RuntimeError("AI 가 만들 파일 목록을 돌려주지 않았습니다.")
        files = [dict(f, file=self._rel(f["file"])) for f in files]
        dedup: dict[str, dict] = {}
        for f in files:
            dedup.setdefault(_norm(f["file"]), f)
        files = list(dedup.values())
        if self.new_project:
            files = ca._complete_fullstack_manifest(files)
        for f in files:
            if not isinstance(f.get("layer"), int) or f["layer"] not in (0, 1, 2):
                f["layer"] = _guess_layer(f["file"])
        files = self._complete_plan(files, manifest)
        fixed_ops: list[dict] = []
        if self.plan_hook:
            try:
                files, fixed_ops, extra = self.plan_hook(files)
                if extra:
                    manifest["contracts"] = (manifest.get("contracts") or "") + extra
            except Exception as exc:  # noqa: BLE001 — 고정 파일을 못 넣어도 생성은 계속한다(점검이 결제 약속을 다시 본다)
                self._log("", "other", f"설계 보강 생략: {exc}")
                fixed_ops = []
        self.state["manifest"] = manifest
        self.state["files"] = files
        for op in fixed_ops:
            self.state["ops"][_norm(op["file"])] = op
        self._save()
        waves = {layer: sum(1 for f in files if f["layer"] == layer) for layer in (0, 1, 2)}
        self._event("planned", message=f"설계 완료 — 파일 {len(files)}개 (기반 {waves[0]} · 기능 {waves[1]} · 화면 {waves[2]})",
                    summary=summary, files=[{"file": f["file"], "layer": f["layer"], "purpose": f.get("purpose", "")} for f in files])
        for op in fixed_ops:
            #: ReCoder 가 넣은 고정 파일(결제 모듈) — AI 가 쓰지 않으므로 바로 완료로 알린다(목록에 대기로 남지 않게).
            self._event("file_done", agent="planner", file=op["file"], lines=op["content"].count("\n") + 1,
                        message=f"{op['file']} — ReCoder 결제 모듈(검증된 고정 파일)을 넣었습니다")

    def _complete_plan(self, files: list[dict], manifest: dict) -> list[dict]:
        """설계 목록의 빈 곳을 메운다 — 결정적으로 알 수 있는 것(진입점·DB 스키마·uses) + 검토 에이전트가 약속과 대조."""
        for f in files:
            f["uses"] = [self._rel(u) for u in (f.get("uses") or []) if isinstance(u, str) and u.strip()]
        files = list(files)
        added: list[dict] = []
        gaps = plan_gaps(files, manifest, self._exists_on_disk)
        files += gaps
        added += gaps
        #: 기존 프로젝트의 작은 수정(파일 몇 개)에는 검토 호출을 쓰지 않는다 — 새 앱이거나 목록이 클 때만.
        rounds = 2 if (self.new_project or len(files) >= 12) else 0
        for _round in range(rounds):
            more = self._review_plan(files, manifest)
            if not more:
                break
            files += more
            added += more
            gaps = plan_gaps(files, manifest, self._exists_on_disk)
            files += gaps
            added += gaps
        if self.new_project and any(f.get("planned_by") for f in added):
            #: 서버가 새로 생겼으면 루트 package.json·Dockerfile·.dockerignore 도 필요하다(처음 보강은 서버가 없어 건너뛰었다).
            keys = {_norm(f["file"]) for f in files}
            for f in _ca()._complete_fullstack_manifest(files):
                if _norm(f["file"]) not in keys:
                    f = dict(f, layer=f.get("layer") if f.get("layer") in (0, 1, 2) else _guess_layer(f["file"]), planned_by="recoder")
                    files.append(f)
                    added.append(f)
                    keys.add(_norm(f["file"]))
        if added:
            names = ", ".join(f["file"] for f in added[:6]) + (f" 외 {len(added) - 6}개" if len(added) > 6 else "")
            self._event("plan_added", agent="review", files=[{"file": f["file"], "layer": f["layer"]} for f in added],
                        message=f"설계 검토 — 실행에 필요한데 목록에 없던 파일 {len(added)}개 추가: {names}")
        return files

    def _review_plan(self, files: list[dict], manifest: dict) -> list[dict]:
        """검토 에이전트 — 약속(contracts)과 파일 목록을 대조해 앱이 빌드·실행되는 데 빠진 파일만 받는다."""
        ca = _ca()
        listed = "\n".join(f"- {f['file']}: {str(f.get('purpose') or '')[:140]}" for f in files)
        contracts = str(manifest.get("contracts") or "")
        if len(contracts) > 24_000:
            contracts = contracts[:24_000] + "\n…(약속 문서 뒷부분 생략)"
        prompt = self.context_prompt + f"""

[대규모 생성 1단계 — 설계 검토] 당신은 검토 에이전트입니다. 아래 파일 목록대로 만들면 앱이 **빌드되고 실제로 실행되는지**
점검하고, 빠진 파일만 주세요(이미 목록에 있는 파일은 다시 쓰지 마세요. 파일 내용도 쓰지 마세요).
반드시 확인할 것:
- 서버: 진입점이 약속의 모든 API 를 라우터로 연결하는가 · API 경로 묶음(/api/auth, /api/products, /api/orders, /api/cart, /api/admin …)마다
  라우트(또는 컨트롤러) 파일과 그 서비스가 있는가 · DB 스키마(Prisma 면 schema.prisma)·DB 연결 파일·초기화/시드 ·
  인증 미들웨어·토큰 유틸 · 공용 오류/응답 유틸 · 공용 타입
- 화면: 진입점(index.html·main·App) · 약속의 모든 화면 경로마다 페이지 · API 호출 모듈 · 로그인 상태 관리
- 설정: 폴더마다 package.json·tsconfig·빌드 설정, 루트 package.json·Dockerfile·.dockerignore
- 각 파일의 uses 에 적은 파일이 목록에 있는가
요약: {manifest.get('summary', '')}
약속:
{contracts or '(없음)'}
현재 파일 목록:
{listed}
빠진 파일이 없으면 files=[] 와 more=false. 있으면 빠진 파일마다 layer(0/1/2)·uses·purpose(무엇을 내보내고 무엇을 하는지, 한 문장)를 붙이고 more=false.
빠진 파일이 많으면 실행에 꼭 필요한 순서(서버 진입점·DB·라우트·인증 → 화면)로 최대 25개만 주세요."""
        try:
            #: 4096 으로는 서버 전체가 빠진 목록(20개+)을 다 못 적어 응답이 잘리고 검토가 통째로 생략됐다(실기기 job 4a06).
            resp = self.call(prompt, PAGE_SCHEMA, "review_code_manifest", 8192, agent="review")
            page = ca._extract_json(resp.text)
            if not isinstance(page, dict):
                return []
        except Paused:
            raise
        except LLMError as exc:
            if not (_is_truncation(exc) or exc.error_type == LLMErrorType.STRUCTURED_OUTPUT):
                raise  # 한도·인증 등 — 생성 자체를 멈추고 이어 만들기로
            self._log("", "other", f"설계 검토 생략: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001 — 검토는 보강이다. 응답이 이상해도 생성은 계속한다(결정적 빈 곳 메우기·마저 만들기가 남는다)
            self._log("", "other", f"설계 검토 생략: {exc}")
            return []
        merged = self._merge_files(files, page.get("files") or [])
        out = []
        for f in merged[len(files):]:
            f = dict(f, file=self._rel(f["file"]))
            if not isinstance(f.get("layer"), int) or f["layer"] not in (0, 1, 2):
                f["layer"] = _guess_layer(f["file"])
            f["uses"] = [self._rel(u) for u in f.get("uses") or []]
            f["planned_by"] = "review"
            out.append(f)
        return out

    def _fill_missing_imports(self) -> None:
        """다 만든 뒤 — 코드가 불러오는데 아무 데도 없는 프로젝트 파일을 마저 만든다(불러오는 쪽 코드를 보여 주며).
        설계 검토로도 빠진 파일이 남는 경우의 안전망. 새 파일이 또 다른 없는 파일을 부르면 최대 3번까지 되풀이한다."""
        for _round in range(3):
            with self._lock:
                contents = {op["file"]: op.get("content") or "" for op in self.state["ops"].values()}
            planned = {_norm(f["file"]) for f in self.state["files"]}
            missing = missing_local_imports(contents, self._exists_on_disk)
            new = []
            for path, info in missing.items():
                if _norm(path) in planned:
                    continue
                names = ", ".join(info["names"][:20]) or "(가져오기만 함)"
                importers = info["importers"][:8]
                new.append({"file": path, "layer": _guess_layer(path), "uses": [], "importers": importers,
                            "planned_by": "import",
                            "purpose": (f"설계에서 빠졌지만 {', '.join(importers)} 가 불러오는 파일. 이 이름을 그 형태 그대로 내보내세요"
                                        f"(default(X)=기본 내보내기): {names}. 불러오는 쪽 코드가 쓰는 인자·반환 모양에 맞게, "
                                        "약속(contracts)대로 실제 동작을 구현하세요(빈 껍데기·TODO 금지).")})
            if not new:
                return
            with self._lock:
                self.state["files"] = self.state["files"] + new
            self._save()
            names = ", ".join(f["file"] for f in new[:6]) + (f" 외 {len(new) - 6}개" if len(new) > 6 else "")
            self._event("plan_added", agent="review", files=[{"file": f["file"], "layer": f["layer"]} for f in new],
                        message=f"전체 점검 — 코드가 불러오는데 없는 파일 {len(new)}개를 마저 만듭니다: {names}")
            self._schedule()

    def _exists_on_disk(self, path: str) -> bool:
        if not self.root:
            return False
        try:
            return os.path.isfile(os.path.join(self.root, *path.replace("\\", "/").split("/")))
        except (OSError, ValueError):
            return False

    def _merge_files(self, files: list[dict], items: list) -> list[dict]:
        seen = {_norm(f["file"]) for f in files}
        out = list(files)
        for item in items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("file") or "").strip().replace("\\", "/")
            key = _norm(path)
            if not path or key in seen or path.startswith("/") or ".." in path.split("/"):
                continue
            seen.add(key)
            out.append({"file": path, "purpose": str(item.get("purpose") or "").strip(),
                        "layer": item.get("layer") if isinstance(item.get("layer"), int) else None,
                        "uses": [str(u).strip().replace("\\", "/") for u in (item.get("uses") or [])
                                 if isinstance(u, str) and u.strip()][:40],
                        **({"size": item["size"]} if item.get("size") in ("small", "medium", "large") else {})})
        return out

    def _rel(self, path: str) -> str:
        return _ca()._relative_to_target([{"file": path}], self.target_folder)[0]["file"]

    # ── 공통 프롬프트 ──
    def _context(self, files_done: list[dict], targets: list[str] | None = None) -> str:
        m, files = self.state["manifest"], self.state["files"]
        listing = "\n".join(f"- {f['file']}: {f.get('purpose', '')}" for f in files)
        done_part = (self._dependency_context(files_done, targets) if targets
                     else _ca()._completed_files_context(files_done))
        return f"""

[대규모 생성 2단계] 전체 파일 목록과 파일 사이의 약속은 아래와 같습니다. 다른 파일은 다른 에이전트가 같은 약속으로 동시에 작성합니다.
전체 요약: {m.get('summary', '')}
약속(반드시 지킬 것 — 이름·경로·필드·타입을 글자 그대로 맞추세요):
{m.get('contracts', '') or '(없음 — 파일 목록과 요청에서 일관되게 정하세요)'}
전체 파일 목록:
{listing}""" + done_part

    def _dependency_context(self, files_done: list[dict], targets: list[str]) -> str:
        """쓸 파일이 실제로 기대는 파일을 보여 준다 — uses 의 파일은 전체 내용, 나머지 완성된 파일은 내보내는 이름·모양만.

        예전엔 목록 앞쪽 파일부터 64k 를 채워(서버 파일이 먼저 들어감) 화면 파일이 정작 쓰는 컴포넌트·훅을 못 봤다 —
        ErrorAlert 의 onClose 가 필수인 줄 모르고 10개 화면이 빼먹거나, useCart 에 없는 total 을 꺼냈다(실기기 카페 2.0.8)."""
        by_key = {_norm(op["file"]): op for op in files_done if op.get("content")}
        wanted: list[str] = []
        callers: list[str] = []
        for t in targets:
            entry = next((f for f in self.state["files"] if _norm(f["file"]) == _norm(t)), None)
            for imp in (entry or {}).get("importers") or []:
                k = _norm(imp)
                if k in by_key and k not in callers:
                    callers.append(k)
            for u in (entry or {}).get("uses") or []:
                k = self._resolve_use(u)
                if k and k in by_key and k not in wanted and k not in {_norm(x) for x in targets}:
                    wanted.append(k)
        sides_all = _sides_of(self.state["files"])
        sides = {_side(t, sides_all) for t in targets}
        if any(t.endswith((".ts", ".js", ".mjs", ".cjs")) for t in targets):
            #: 서버 코드는 DB 모델·관계 이름·enum 값을 스키마 그대로 써야 한다 — 같은 쪽의 Prisma 스키마를 함께 보여 준다
            #: (실기기 2.0.10: 스키마는 PAID·BigInt·items, 코드는 'paid'·number·order_items 로 따로 써서 tsc 오류 수십 건).
            for k, op in by_key.items():
                if op["file"].endswith(".prisma") and _side(op["file"], sides_all) in sides and k not in wanted:
                    wanted.insert(0, k)
        if not wanted:
            #: 설계가 uses 를 적지 않았다 — 같은 쪽의 더 아래 단계 파일(가까운 단계 먼저)을 전체 내용으로 보여 준다.
            top = max(_rank(t) for t in targets)
            near = sorted((k for k, op in by_key.items() if _side(op["file"], sides_all) in sides
                           and 0 < _rank(op["file"]) < top and k not in {_norm(x) for x in targets}),
                          key=lambda k: -_rank(by_key[k]["file"]))
            wanted = near
        blocks, used = [], 0
        full = set()
        for k in callers:
            op = by_key[k]
            block = (f"\n[이 파일을 불러오는 파일 — 여기서 import 하는 이름·함수 인자·반환 모양·타입을 그대로 내보내세요] {op['file']}\n"
                     f"```\n{op['content']}\n```\n")
            if used + len(block) <= 48_000:
                blocks.append(block)
                used += len(block)
                full.add(k)
        for k in wanted:
            op = by_key[k]
            block = f"\n[이미 생성한 파일 — 이 파일이 기대는 코드: 이름·props·반환 필드·함수 인자·API 경로를 이 내용과 글자 그대로 맞추세요] {op['file']}\n```\n{op['content']}\n```\n"
            if used + len(block) <= 48_000:
                blocks.append(block)
                used += len(block)
                full.add(k)
        summaries, sused = [], 0
        ordered = sorted(by_key.items(), key=lambda kv: (0 if _side(kv[1]["file"], sides_all) in sides else 1, _rank(kv[1]["file"])))
        for k, op in ordered:
            if k in full:
                continue
            text = _export_summary(op["file"], op["content"])
            if not text:
                continue
            block = f"- {op['file']}\n{text}\n"
            if sused + len(block) > 28_000:
                continue
            summaries.append(block)
            sused += len(block)
        out = "".join(blocks)
        if summaries:
            out += ("\n[이미 완성된 다른 파일이 실제로 내보내는 것 — 이 파일들을 쓸 때는 아래 이름·props·타입·인자를 그대로 쓰세요."
                    " 필수 props 를 빼거나 없는 필드를 꺼내지 마세요]\n" + "".join(summaries))
        return out

    def _resolve_use(self, use: str) -> str | None:
        keys = {_norm(f["file"]): f["file"] for f in self.state["files"]}
        u = _norm(self._rel(use)) if use else ""
        if u in keys:
            return u
        stem = re.sub(r"\.(js|jsx|ts|tsx|mjs|cjs)$", "", u)
        for cand in (stem + ".ts", stem + ".tsx", stem + ".js", stem + ".jsx", stem + "/index.ts", stem + "/index.tsx", stem + "/index.js"):
            if cand in keys:
                return cand
        return None

    # ── 3. 이어 쓰기 ──
    def write_in_parts(self, base_prompt: str, label: str, *, agent: str = "", operation: str = "generate_code_file_part",
                       path: str = "", lean: bool = False) -> str:
        """한 응답에 다 들어가지 않는 내용을 **글자 그대로** 이어 받는다(AI 가 지시를 지킨다고 기대하지 않는다).

        · 내용은 JSON 이 아니라 그대로 받는다 — 이스케이프로 길이가 늘지 않고, 응답 길이 한도에서 끊겨도 받은 만큼 쓴다.
          끝은 END_MARKER 로 안다. 끊겼으면 마지막 불완전한 줄만 버리고 "그 다음 줄부터" 이어 받는다.
          (AI 가 "최대 N줄" 같은 지시를 무시하고 한 번에 다 쓰려 해도 매번 진행이 생긴다 — 실기기 README.)
        · AI 가 그래도 JSON({"content", "done"} 이나 ops)으로 싸 보내면 실제 내용만 꺼낸다(clean_content).
        · 이미 쓴 줄을 다시 쓰면 걷어 낸다. 새 내용이 3번 연속 없으면 CodeOutputError — 부르는 쪽이 방법을 바꾼다.
        """
        with self._lock:
            partial = self.state["partial"].get(path) if path else None
        written = (partial or {}).get("written", "")
        part = int((partial or {}).get("parts", 0))
        stalls = 0
        name = path or label
        while part < PART_SAFETY:
            if not written:
                p = base_prompt + f"""

[파일 작성 — 응답 형식] {label} 의 내용만 **글자 그대로** 쓰세요.
- 설명 문장·마크다운 코드펜스(```)·JSON 으로 감싸지 마세요. 첫 글자부터 파일 내용입니다.
- 다 쓰면 마지막에 새 줄로 정확히 {END_MARKER} 한 줄을 쓰세요. 이 줄은 파일에 들어가지 않습니다.
- 응답 길이 한도 때문에 중간에 끊겨도 괜찮습니다. 끊기면 다음 요청에서 이어 쓰게 합니다."""
            else:
                p = base_prompt + f"""

[파일 작성 — 이어 쓰기] {label} 을(를) 쓰던 중 응답 길이 한도에서 끊겼습니다. 아래는 지금까지 쓴 내용입니다.
```
{_part_context(written)}
```
- 이 내용 **바로 다음 줄부터** 이어서 글자 그대로 쓰세요. 이미 쓴 줄을 다시 쓰지 마세요.
- 설명 문장·코드펜스·JSON 으로 감싸지 마세요.
- 다 쓰면 마지막에 새 줄로 정확히 {END_MARKER} 한 줄을 쓰세요. 또 끊겨도 괜찮습니다."""
            resp = self.call(p, None, operation, STREAM_MAX_TOKENS, agent=agent, raw=True)
            text = str(getattr(resp, "text", "") or "")
            meta = getattr(resp, "metadata", None) or {}
            piece, done, intact = _stream_piece(name, text)
            #: 끊겼는가 — 공급자가 알려 주면 그대로, 모르면 응답이 길면 끊긴 것으로 본다.
            known = meta.get("truncated")
            cut = (not done) and not intact and (known is True or (known is None and len(text) >= STREAM_CUT_GUESS))
            if cut and piece and not piece.endswith("\n") and "\n" in piece:
                #: 마지막 불완전한 줄은 버린다 — 다음 요청이 그 줄부터 다시 쓴다.
                piece = piece[: piece.rfind("\n") + 1]
            before = len(written)
            piece = _strip_overlap(written, piece)
            if written and piece and not written.endswith("\n"):
                written += "\n"
            written += piece
            grew = len(written) - before
            part += 1
            if not done and not cut and grew <= 0 and written.strip():
                #: 끊기지 않은 응답이 더 쓸 게 없다 — 끝 표시만 빠뜨린 것이다(다 쓴 것으로 본다).
                done = True
            self._log(name, "stream", f"{part}번째 응답 · 받은 {len(text)}자 · 늘어난 {max(0, grew)}자 · "
                                      f"{'끝' if done else ('끊김' if cut else '끝 표시 없음')}")
            if path:
                with self._lock:
                    self.state["partial"][path] = {"written": written, "parts": part, "mode": "stream"}
                self._save()
                if not done:
                    self._event("file_part", agent=agent, file=path, part=part, lines=written.count("\n") + 1,
                                message=f"{path} 이어 쓰는 중 · {part}번째 응답 · {written.count(chr(10)) + 1}줄")
            if done:
                break
            stalls = stalls + 1 if grew <= 0 or not piece.strip() else 0
            if stalls >= 3:
                raise CodeOutputError(f"{label} 을(를) 이어 쓰는 중 더 진행되지 않았습니다.")
        else:
            raise CodeOutputError(f"{label} 이(가) 비정상적으로 길어 멈췄습니다({PART_SAFETY}번).")
        if not written.strip():
            #: 내용 없이 "끝"이라고 하면 빈 파일이 된다 — 조용히 넘기지 않는다(방법을 바꿔 다시 시도).
            raise CodeOutputError(f"{label} 의 내용을 받지 못했습니다.")
        if path:
            with self._lock:
                self.state["partial"].pop(path, None)
        return written if written.endswith("\n") else written + "\n"

    # ── 2. 파일 생성 ──
    def _ask_files(self, batch: list[dict], files_done: list[dict], agent: str) -> list[dict]:
        """파일 여러 개를 한 번에 받는다. 받은 것 중 검사를 통과한 것만 돌려준다(빠진 파일은 부르는 쪽이 하나씩).
        응답이 잘리거나 형식이 틀리면 빈 목록 — 그 파일들은 하나씩·조각으로 다시 만든다(실패로 세지 않는다)."""
        ca = _ca()
        base = self.prompt + self._context(files_done, [f["file"] for f in batch])
        wanted = "\n".join(f"- {f['file']}" for f in batch)
        p = base + "".join(_file_hint(f["file"]) for f in batch if _file_hint(f["file"])) \
            + f"\n\n이번 응답의 ops 에는 **아래 파일만** 전체 내용으로 작성하세요(다른 파일은 넣지 마세요):\n{wanted}"
        for f in batch:
            self._event("file_start", agent=agent, file=f["file"], message=f"{f['file']} 작성 중")
        try:
            resp = self.call(p, CODE_OUTPUT_SCHEMA, "generate_code_part", ca._CODE_AGENT_MAX_TOKENS, agent=agent)
            _data, ops = parse_code_output(resp.text)
        except (LLMError, CodeOutputError) as exc:
            if not (_is_truncation(exc) or isinstance(exc, CodeOutputError)
                    or (isinstance(exc, LLMError) and exc.error_type == LLMErrorType.STRUCTURED_OUTPUT)):
                raise
            return []
        ops = ca._relative_to_target(ops, self.target_folder)
        keys = {_norm(f["file"]): f for f in batch}
        out = []
        for op in ops:
            target = keys.get(_norm(op["file"]))
            if target is None:
                continue
            cleaned = clean_content(op["file"], op.get("content"))
            if placeholder_file(target["file"]) and not (cleaned or "").strip():
                out.append(dict(op, file=target["file"], content=""))
                continue
            if cleaned is None or not cleaned.strip():
                self._log(op["file"], "format", "받은 파일 내용에 응답 형식이 섞여 버리고 다시 받음")
                continue
            out.append(dict(op, file=target["file"], content=cleaned))
        return out

    def _gen_parts(self, target: dict, files_done: list[dict], agent: str, lean: bool = False) -> dict:
        path = target["file"]
        resuming = path in self.state["partial"]
        self._event("file_split", agent=agent, file=path, lean=lean,
                    message=(f"{path} 쓰던 내용에 이어서 씁니다" if resuming else
                             f"{path} 을(를) 글자 그대로 이어 받습니다" + (" (맥락을 줄여서)" if lean else "")))
        #: lean — 이미 만든 파일 본문을 빼고 약속·목록만 보낸다(입력이 커서 생기는 실패를 피한다).
        context = self._context([] if lean else files_done, None if lean else [path])
        base = self.context_prompt + context + f"\n\n지금은 {path} 파일 하나만 작성합니다." + _file_hint(path)
        content = self.write_in_parts(base, f"{path} 파일", agent=agent, path=path)
        return {"action": "create", "file": path, "content": content, "language": "",
                "rationale": "응답 길이 한도 때문에 이어 받아 작성"}

    def _produce(self, target: dict, files_done: list[dict], agent: str) -> dict:
        """파일 하나를 지금 시도 횟수에 맞는 방법으로 만든다 — 같은 방법을 되풀이하지 않는다.
        첫 시도: 한 번에(크다고 예상한 파일은 바로 이어 받기) → 넘치면 글자 그대로 이어 받기.
        두 번째: 처음부터 글자 그대로 이어 받기. 세 번째: 맥락을 줄여 처음부터 이어 받기."""
        key = _norm(target["file"])
        tries = int((self.state["attempts"].get(key) or {}).get("tries", 0))
        if tries == 0 and target["file"] not in self.state["partial"] and target.get("size") != "large":
            got = self._ask_files([target], files_done, agent)
            if got:
                return got[0]
        return self._gen_parts(target, files_done, agent, lean=tries >= 2)

    def _run_single(self, target: dict, files_done: list[dict], agent: str) -> None:
        """파일 하나 — 실패하면 방법을 바꿔 다시, 정해진 횟수를 넘으면 그 파일만 실패로 두고 돌아간다."""
        key = _norm(target["file"])
        if placeholder_file(target["file"]) and key not in self.state["ops"]:
            self._finish({"action": "create", "file": target["file"], "content": "", "language": "",
                          "rationale": "빈 자리표시 파일(폴더를 남기기 위함) — ReCoder 가 바로 만듦"}, files_done, agent)
            return
        while True:
            if key in self.state["ops"]:
                return
            tries = int((self.state["attempts"].get(key) or {}).get("tries", 0))
            if tries >= MAX_FILE_TRIES:
                if _is_doc(target["file"]):
                    #: 문서는 앱 동작을 막지 않는다 — 설계(요약·약속·실행 명령·환경 변수)로 기본 문서를 만들어 둔다.
                    self._finish_fallback_doc(target, agent)
                else:
                    self._fail(target, agent)
                return
            try:
                op = self._produce(target, files_done, agent)
                problem = implausible(op["file"], op.get("content", ""))
                if problem:
                    raise CodeOutputError(f"{op['file']} 의 받은 내용이 {problem} — 다른 방법으로 다시 받습니다.")
                self._finish(op, files_done, agent)
                return
            except Paused:
                raise
            except Exception as exc:  # noqa: BLE001 — 방법을 바꿀 수 있는 실패만 여기서 받는다
                if not _recoverable(exc):
                    raise
                self._note_failure(target, exc, agent)

    def _finish(self, op: dict, files_done: list[dict], agent: str) -> None:
        """검사·교정 뒤 **바로** 저장한다 — 같이 맡은 다른 파일이 실패해도 이 파일은 남는다."""
        op = self._verify_and_fix(op, files_done, agent)
        with self._lock:
            self.state["ops"][_norm(op["file"])] = op
            self.state["failed"].pop(_norm(op["file"]), None)
        self._save()
        self._event("file_done", agent=agent, file=op["file"], lines=op["content"].count("\n") + 1,
                    message=f"{op['file']} 완료")

    # ── 4. 만들 때마다 확인 ──
    def _verify_and_fix(self, op: dict, files_done: list[dict], agent: str) -> dict:
        for round_ in range(FIX_ROUNDS + 1):
            problems = _check_file(op["file"], op["content"])
            if self.security_review:
                problems += _secret_problems(op["file"], op["content"])
            if not problems:
                if round_:
                    self._event("verified", agent=agent, file=op["file"], message=f"{op['file']} 고친 뒤 확인 통과")
                return op
            if round_ == FIX_ROUNDS:
                self._event("verify_failed", agent=agent, file=op["file"], message=f"{op['file']}: {problems[0]}")
                op.setdefault("verify_issues", problems)
                return op
            self._event("fixing", agent=agent, file=op["file"], message=f"{op['file']} 고치는 중 — {problems[0]}")
            try:
                op = dict(op, content=self._fix(op, problems, files_done, agent))
            except (LLMError, CodeOutputError, ValueError) as exc:
                if isinstance(exc, LLMError) and not (_is_truncation(exc) or exc.error_type == LLMErrorType.STRUCTURED_OUTPUT):
                    raise
                op.setdefault("verify_issues", problems)
                return op
        return op

    def _fix(self, op: dict, problems: list[str], files_done: list[dict], agent: str) -> str:
        ca = _ca()
        listing = "\n".join(f"- {p}" for p in problems)
        p = self.context_prompt + self._context(files_done, [op["file"]]) + f"""

[즉시 교정] 방금 작성한 {op['file']} 에 아래 문제가 있습니다.
{listing}
비밀값은 코드에 두지 말고 환경 변수(process.env.X / os.environ["X"])로 읽게 바꾸세요.
**파일 전체를 다시 쓰지 말고** 바꿀 부분만 edits 로 주세요. find 는 현재 파일에 정확히 한 번 나오는 원문 그대로,
replace 는 바꿀 내용입니다.
현재 파일:
```
{ca._prompt_body(op['content'], 60_000)}
```"""
        resp = self.call(p, EDIT_SCHEMA, "generate_code_fix", 4096, agent=agent)
        data = ca._extract_json(resp.text)
        content = op["content"]
        applied = 0
        for edit in data.get("edits") or []:
            if not isinstance(edit, dict):
                continue
            find, replace = edit.get("find"), edit.get("replace")
            if isinstance(find, str) and isinstance(replace, str) and find and content.count(find) == 1:
                content = content.replace(find, replace, 1)
                applied += 1
        if not applied:
            raise ValueError("교정 내용을 적용할 수 없습니다.")
        return content

    # ── 실행 ──
    def run(self) -> tuple[dict, list[dict], Any]:
        try:
            self.plan()
            self._schedule()
            self._fill_missing_imports()
        except Paused as exc:
            self._save()
            self._log("", error_kind(exc), f"멈춤 — {exc}")
            self._event("paused", kind=error_kind(exc), message=f"멈춤 — {exc}")
            raise jobs.GenerationPaused(
                f"{exc} — 지금까지 만든 {len(self.state['ops'])}/{len(self.state['files'])}개는 저장했습니다. "
                "[이어서 만들기]로 멈춘 지점부터 계속할 수 있습니다.",
                self.job_id, len(self.state["ops"]), len(self.state["files"]), reason=str(exc)) from exc
        except (LLMError, CodeOutputError, RuntimeError, OSError) as exc:
            self._save()
            self._log("", error_kind(exc), f"멈춤 — {exc}")
            self._event("paused", kind=error_kind(exc), message=f"멈춤 — {ERROR_TEXT[error_kind(exc)]}: {str(exc)[:160]}")
            if self.state.get("files"):
                raise jobs.GenerationPaused(
                    f"생성이 중간에 멈췄습니다({exc}). 지금까지 만든 {len(self.state['ops'])}/{len(self.state['files'])}개는 "
                    "저장했습니다. [이어서 만들기]로 멈춘 지점부터 계속할 수 있습니다.",
                    self.job_id, len(self.state["ops"]), len(self.state["files"]), reason=str(exc)) from exc
            raise
        files = self.state["files"]
        failed = [dict(v) for v in self.state["failed"].values()]
        if failed and not self.skip_failed:
            #: 나머지는 다 만들었고 이 파일들만 남았다 — 이유와 함께 멈추고, 사용자가 [다시 쓰기]/[빼고 받기]를 고른다.
            names = ", ".join(f["file"] for f in failed[:4]) + (f" 외 {len(failed) - 4}개" if len(failed) > 4 else "")
            reason = f"{names} — {MAX_FILE_TRIES}번 시도해도 {failed[0]['reason']}"
            self._event("paused", kind=failed[0]["kind"], message=f"멈춤 — {reason}")
            raise jobs.GenerationPaused(
                f"파일 {len(failed)}개를 만들지 못했습니다({reason}). 나머지 {len(self.state['ops'])}/{len(files)}개는 저장했습니다. "
                "[이 파일 다시 쓰기] 또는 [이 파일 빼고 결과 받기]를 고르세요.",
                self.job_id, len(self.state["ops"]), len(files), reason=reason, failed=failed)
        if failed:
            self.skipped = failed
        missing = [f["file"] for f in files if _norm(f["file"]) not in self.state["ops"]
                   and not (self.skip_failed and _norm(f["file"]) in self.state["failed"])]
        if missing:
            raise CodeOutputError(f"일부 파일을 만들지 못했습니다: {', '.join(missing[:5])}")
        ops = [self.state["ops"][_norm(f["file"])] for f in files if _norm(f["file"]) in self.state["ops"]]
        self._event("generated", message=f"파일 {len(ops)}개 작성 완료 — 전체 점검 중"
                    + (f" (만들지 못한 {len(self.skipped)}개는 뺌)" if self.skipped else ""))
        return ({"summary": str(self.state["manifest"].get("summary") or ""), "skipped": self.skipped,
                 #: 전체 점검 단계가 빠진 파일을 마저 만들 때 같은 약속을 지키게 넘긴다(결과 화면에는 쓰지 않는다)
                 "contracts": str(self.state["manifest"].get("contracts") or ""),
                 "fallback_docs": self.fallback_docs or [{"file": op["file"], "reason": "자동 작성"} for op in ops if op.get("fallback")]},
                ops, self.last_resp)

    def _deps(self, files: list[dict]) -> dict[str, list[tuple[str, bool]]]:
        """파일마다 먼저 끝나야 하는 파일 [(경로 키, 설계가 직접 적은 것인가)].

        · 설계의 uses(이 파일이 import 할 파일) — 그 파일의 실제 내용을 보고 써야 이름·props 가 맞는다.
        · 같은 쪽(frontend/·backend/ …) 안에서 더 아래 단계(_rank: 설정 → 타입·유틸 → 서비스·API·Context → 훅·라우트
          → 컴포넌트 → 화면 → 진입점)의 파일 — uses 를 빠뜨려도 화면이 컴포넌트보다 먼저 써지지 않게.
        다른 쪽(화면 ↔ 서버)끼리는 기다리지 않는다(약속 문서로 맞춘다)."""
        keys = {_norm(f["file"]): f for f in files}
        sides = _sides_of(files)
        out: dict[str, list[tuple[str, bool]]] = {}
        for f in files:
            k = _norm(f["file"])
            side, rank = _side(f["file"], sides), _rank(f["file"])
            deps: dict[str, bool] = {}
            for u in f.get("uses") or []:
                d = self._resolve_use(u)
                if d and d != k and d in keys:
                    deps[d] = True
            #: 설계가 uses 를 적었으면 그 파일과 공용 타입·유틸(1단계)만 기다린다 — 상관없는 컴포넌트까지 기다리지 않게.
            #: 적지 않았으면 같은 쪽의 더 아래 단계를 모두 기다린다. 설정·패키지(0단계)와 문서는 서로 기다리지 않는다.
            explicit = bool(deps)
            for g in files:
                d = _norm(g["file"])
                gr = _rank(g["file"])
                if d == k or d in deps or _side(g["file"], sides) != side or gr == 0 or gr >= rank or rank >= 7:
                    continue
                if explicit and gr != 1:
                    continue
                deps[d] = False
            out[k] = list(deps.items())
        return out

    def _schedule(self) -> None:
        """설계의 의존 순서대로, 서로 상관없는 파일은 동시에 만든다(공통 기반도 — 예전엔 한 명이 순서대로 써서 가장 오래 걸렸다).
        파일마다 시작하는 순간까지 완성된 파일(특히 그 파일이 불러오는 파일)의 실제 내용을 보고 쓴다."""
        files = self.state["files"]
        todo = [f for f in files if _norm(f["file"]) not in self.state["ops"]
                and not (self.skip_failed and _norm(f["file"]) in self.state["failed"])]
        if not todo:
            return
        deps = self._deps(files)
        sides = _sides_of(files)
        pending = {_norm(f["file"]): f for f in todo}
        running: set[str] = set()
        workers = self.concurrency
        self._event("wave", layer=-1, agents=workers,
                    message=f"파일 {len(todo)}개 — 에이전트 {workers}명이 의존 순서대로 동시에 만듭니다(먼저 끝난 파일을 보고 다음 파일을 씁니다)")
        slots = [f"agent-{i + 1}" for i in range(workers)]
        free = list(slots)

        def unmet(k: str) -> list[tuple[str, bool]]:
            return [(d, e) for d, e in deps.get(k, []) if d in pending or d in running]

        def task(batch: list[dict], slot: str) -> None:
            with self._lock:
                context = [dict(op) for op in self.state["ops"].values()]
            self._run_batch(batch, context, slot)

        first_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gen") as pool:
            futures: dict = {}
            while (pending or futures) and first_error is None:
                ready = [k for k in pending if not unmet(k)]
                if not ready and not futures and pending:
                    #: 서로 기다리는 파일(설계의 uses 가 단계와 엇갈림) — 직접 적은 의존이 남지 않은 것부터 푼다.
                    soft = [k for k in pending if all(not e for _d, e in unmet(k))]
                    pool_keys = soft or list(pending)
                    ready = [min(pool_keys, key=lambda k: (_rank(pending[k]["file"]), len(unmet(k))))]
                ready.sort(key=lambda k: (_rank(pending[k]["file"]), list(pending).index(k)))
                while ready and free:
                    k = ready.pop(0)
                    batch = [pending.pop(k)]
                    #: 작은 파일은 같은 쪽·같은 단계의 다른 작은 파일과 둘씩 묶어 호출 수를 줄인다
                    if _small(batch[0], self.state):
                        mate = next((m for m in ready if _small(pending[m], self.state)
                                     and _side(pending[m]["file"], sides) == _side(batch[0]["file"], sides)
                                     and _rank(pending[m]["file"]) == _rank(batch[0]["file"])), None)
                        if mate and BATCH > 1:
                            ready.remove(mate)
                            batch.append(pending.pop(mate))
                    slot = free.pop(0)
                    for f in batch:
                        running.add(_norm(f["file"]))
                    futures[pool.submit(task, batch, slot)] = (batch, slot)
                if not futures:
                    continue
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for fut in done:
                    batch, slot = futures.pop(fut)
                    free.append(slot)
                    for f in batch:
                        running.discard(_norm(f["file"]))
                    exc = fut.exception()
                    if exc and first_error is None:
                        first_error = exc
            if first_error is not None:
                for fut in futures:
                    fut.cancel()
        if first_error is not None:
            raise first_error

    def _run_batch(self, batch: list[dict], context: list[dict], agent: str) -> None:
        """작은 파일은 두 개씩 한 번에 받고, 받은 것은 하나씩 바로 저장한다. 빠진 파일·이어 쓰던 파일은 하나씩."""
        todo = [f for f in batch if _norm(f["file"]) not in self.state["ops"]]
        for f in [f for f in todo if placeholder_file(f["file"])]:
            self._finish({"action": "create", "file": f["file"], "content": "", "language": "",
                          "rationale": "빈 자리표시 파일(폴더를 남기기 위함) — ReCoder 가 바로 만듦"}, context, agent)
        todo = [f for f in todo if _norm(f["file"]) not in self.state["ops"]]
        fresh = [f for f in todo if f["file"] not in self.state["partial"] and f.get("size") != "large"
                 and not int((self.state["attempts"].get(_norm(f["file"])) or {}).get("tries", 0))]
        if len(fresh) > 1:
            for op in self._ask_files(fresh, context, agent):
                try:
                    self._finish(op, context, agent)
                except Paused:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if not _recoverable(exc):
                        raise
                    self._note_failure({"file": op["file"]}, exc, agent)
        for f in todo:
            self._run_single(f, context, agent)


# ── 보조 ───────────────────────────────────────────────────────────────

_SIDE_SKIP = {"src", "app", "lib"}


def _side(path: str, sides: "frozenset[str] | None" = None) -> str:
    """frontend/·backend/ 처럼 따로 빌드되는 쪽(그 폴더에 package.json 등이 있는 첫 폴더). 그 밖은 루트 ''."""
    parts = path.replace("\\", "/").strip("/").split("/")
    if len(parts) < 2:
        return ""
    head = parts[0].lower()
    if sides is not None:
        return head if head in sides else ""
    return head if head not in _SIDE_SKIP else ""


def _sides_of(files: list[dict]) -> frozenset:
    marks = {"package.json", "requirements.txt", "pyproject.toml", "go.mod", "pom.xml", "build.gradle", "cargo.toml"}
    out = set()
    for f in files:
        parts = str(f.get("file") or "").replace("\\", "/").strip("/").split("/")
        if len(parts) == 2 and parts[1].lower() in marks:
            out.add(parts[0].lower())
    return frozenset(out)


def _rank(path: str) -> int:
    """같은 쪽 안에서 먼저 써야 하는 순서(작을수록 먼저). 경로만 보고 정한다."""
    p = "/" + path.replace("\\", "/").lower()
    name = posixpath.basename(p)
    stem = re.sub(r"\.[^.]+$", "", name)
    if name.endswith((".md", ".mdx", ".txt")) or re.search(r"/(docs?|tests?|__tests__|e2e)/|\.(test|spec)\.", p) or "/seed" in p:
        return 7
    if name in {"package.json", "requirements.txt", "pyproject.toml", "go.mod", "dockerfile", ".dockerignore", ".gitignore",
                "index.html", ".env.example", ".env.sample"} or re.match(r"(tsconfig.*|.*\.config)\.(json|js|cjs|mjs|ts)$", name) \
            or name.endswith((".prisma", ".sql", ".css", ".scss", ".d.ts", ".yml", ".yaml", ".toml", ".ini")):
        return 0
    if re.search(r"/(types?|constants?|config|utils?|lib|helpers?|db|database|models?|schemas?|validators?|interfaces)/", p) \
            or stem in {"types", "constants", "config", "db", "database", "models", "schema"}:
        return 1
    if re.search(r"/(services?|middlewares?|api|context|contexts|store|stores|repositories|repos|clients?|providers?)/", p):
        return 2
    if re.search(r"/(hooks?|routes?|routers?|controllers?|handlers?|resolvers?)/", p):
        return 3
    if re.search(r"/(components?|layouts?|widgets?|ui)/", p):
        return 4
    if re.search(r"/(pages?|views?|screens?|routes?/_?app)/", p):
        return 5
    if stem in {"app", "main", "index", "server", "router", "routes", "_app", "manage", "wsgi", "asgi"}:
        return 6
    return 3


def _small(f: dict, state: dict) -> bool:
    return (f.get("size") != "large" and f["file"] not in state["partial"]
            and not int((state["attempts"].get(_norm(f["file"])) or {}).get("tries", 0)))


_SIG_SUFFIX = re.compile(r"(Props|State|Return|Result|Options|Context|ContextType|Value|Params|Request|Response|Item|Payload)$")


def _export_summary(path: str, content: str, limit: int = 2400) -> str:
    """파일이 바깥에 내보내는 이름과 모양(다른 파일이 맞춰 쓸 것) — 인터페이스·타입 본문, 함수·컴포넌트 시그니처."""
    if not content:
        return ""
    low = path.lower()
    lines = content.split("\n")
    out: list[str] = []
    if low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts")):
        i = 0
        while i < len(lines):
            line = lines[i]
            strip = line.strip()
            m_block = re.match(r"(export\s+)?(?:declare\s+)?(interface|type|enum)\s+([A-Za-z_$][\w$]*)", strip)
            if m_block and (m_block.group(1) or _SIG_SUFFIX.search(m_block.group(3))) and not line.startswith((" ", "\t")):
                depth, j, block = 0, i, []
                while j < len(lines) and j - i < 40:
                    block.append(lines[j])
                    depth += lines[j].count("{") - lines[j].count("}")
                    if depth <= 0 and (lines[j].rstrip().endswith((";", "}")) or (m_block.group(2) != "type" and "{" in "".join(block))):
                        if depth <= 0 and ("{" in "".join(block) or lines[j].rstrip().endswith(";")):
                            break
                    j += 1
                out.extend(block)
                i = j + 1
                continue
            if re.match(r"export\s+(default\s+)?(async\s+)?function\b", strip) or re.match(r"export\s+(const|let)\s+[\w$]+", strip) \
                    or re.match(r"export\s+(default\s+)?class\b", strip):
                sig = [line]
                j = i
                while not re.search(r"(\{|=>|=)\s*$|;\s*$", lines[j].rstrip()) and j - i < 8 and j + 1 < len(lines):
                    j += 1
                    sig.append(lines[j])
                out.append("\n".join(sig).rstrip(" {"))
                i = j + 1
                continue
            if re.match(r"export\s+default\s+[\w$]+\s*;?$", strip) or re.match(r"export\s*\{", strip) or re.match(r"export\s+\*", strip):
                out.append(strip)
            elif re.search(r"createContext<", strip):
                out.append(strip)
            i += 1
    elif low.endswith(".py"):
        out = [l for l in lines if re.match(r"(async\s+def|def|class)\s+[A-Za-z_]", l)]
    elif low.endswith(".sql"):
        out = [l for l in lines if re.match(r"\s*(create\s+(table|type|index|unique)|\s+\w+\s+\w+.*,?$)", l, re.I)][:60]
    text = "\n".join(out).strip()
    return text[:limit]


def _batches(files: list[dict], size: int) -> list[list[dict]]:
    return [files[i:i + size] for i in range(0, len(files), size)]


def _guess_layer(path: str) -> int:
    p = path.replace("\\", "/").lower()
    name = posixpath.basename(p)
    if name in {"package.json", "requirements.txt", "pyproject.toml", "tsconfig.json", ".env.example", "dockerfile",
                ".dockerignore", "go.mod"} or re.search(r"(^|/)(config|db|database|schema|models?|migrations?|lib|utils?|types?|middleware|auth)(/|\.|$)", p):
        return 0
    if re.search(r"(^|/)(public|views?|pages?|components?|templates?|static|client|frontend|web)(/|$)", p) or name.endswith((".html", ".css", ".md")):
        return 2
    return 1


_PROSE_PREFIX = re.compile(r"\s*((?:[^\n`;{}()<>=]{1,200}\n){1,3})\s*```[\w.+-]*[ \t]*\n")


def _stream_piece(path: str, text: str) -> tuple[str, bool, bool]:
    """글자 그대로 받은 응답 → (파일에 넣을 내용, 다 썼는가, 온전한가).

    온전한가: JSON 으로 싸여 와서 끝까지 해석됐다(응답이 끊기지 않았다) — 마지막 줄을 버리지 않는다.

    끝 표시가 있으면 다 쓴 것. AI 가 지시를 어기고 JSON({"content", "done"} 이나 ops)으로 싸 보내면 실제 내용과
    done 을 꺼낸다. 코드펜스는 벗긴다. 끝 표시 뒤의 말(설명 등)은 버린다.
    """
    raw = text or ""
    done = False
    intact = False
    #: "파일 내용입니다:" 같은 말 한두 줄 뒤에 코드펜스로 시작하면 말은 버린다(코드처럼 보이는 줄은 건드리지 않는다)
    prose = _PROSE_PREFIX.match(raw)
    if prose and not path.lower().endswith(_DOC_SUFFIXES):
        raw = raw[len(prose.group(1)):].lstrip(" \t") if raw[len(prose.group(1)):].lstrip().startswith("```") else raw
    at = raw.find(END_MARKER)
    if at >= 0:
        raw, done = raw[:at], True
    stripped = raw.strip()
    if stripped.startswith("{") and is_wrapped(path, stripped):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            intact = True
            done = done or data.get("done") is True
        inner = clean_content(path, stripped)
        raw = inner if inner is not None else ""
        if END_MARKER in raw:
            raw, done = raw[: raw.find(END_MARKER)], True
    else:
        #: AI 가 습관적으로 감싼 코드펜스 — 여는 펜스를 벗겼을 때만 끝의 닫는 펜스도 벗긴다(문서 안의 펜스는 내용이다).
        fenced = bool(re.match(r"\s*```[\w.+-]*[ \t]*\n", raw))
        if fenced:
            raw = re.sub(r"^\s*```[\w.+-]*[ \t]*\n", "", raw, count=1)
            if done:
                raw = re.sub(r"\n?```[ \t]*\s*$", "\n", raw)
    if done and raw and not raw.endswith("\n"):
        raw += "\n"
    if done and raw.endswith("\n\n"):
        raw = raw.rstrip("\n") + "\n"
    return raw, done, intact


_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc")
_CODE_SIGN = re.compile(r"\b(?:import|export|function|const|let|var|class|interface|type|enum|require\(|module\.exports)\b|=>|<[A-Za-z]")


#: 원래 비어 있는 파일 — AI 에게 맡기지 않고 빈 파일로 만든다(실기기 2.0.10: prisma/migrations/.gitkeep 을 AI 가 맞게
#: 빈 내용으로 돌려줬는데 "내용이 비어 있음" 으로 3번 거절해 생성 전체가 멈췄다).
_EMPTY_FILES = {".gitkeep", ".keep", ".npmkeep", ".gitkeep.txt", "py.typed", ".nojekyll", "__init__.py"}


def placeholder_file(path: str) -> bool:
    """내용이 없어도 정상인 자리표시 파일인가."""
    return posixpath.basename(str(path or "").replace("\\", "/")).lower() in _EMPTY_FILES


def implausible(path: str, content: str) -> str:
    """받은 내용이 그 파일 종류로 보기 어려우면 이유(글자 그대로 받을 때 AI 의 말이 파일에 들어가는 것을 막는다)."""
    low = str(path or "").lower()
    body = (content or "").strip()
    if not body:
        return "" if placeholder_file(path) else "내용이 비어 있음"
    if low.endswith(".json"):
        #: 문법 오류는 즉시 교정(바뀔 부분만)이 고친다 — 여기서는 JSON 으로 시작조차 안 하는 말만 걸러 낸다
        return "" if body[:1] in "{[" else "JSON 으로 보이지 않음"
    if low.endswith((".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte")) and not _CODE_SIGN.search(body):
        return "코드로 보이지 않음"
    if low.endswith((".css", ".scss", ".less")) and ("{" not in body or body.count("{") != body.count("}")):
        return "스타일 규칙으로 보이지 않음"
    if low.endswith((".html", ".htm")) and not re.search(r"<[A-Za-z!]", body):
        return "HTML 로 보이지 않음"
    return ""


def fallback_doc(path: str, manifest: dict, ops: list[dict]) -> str:
    """AI 가 끝내 못 쓴 문서를 설계와 이미 만든 파일에서 만든다 — 지어내지 않고 아는 사실만 적는다."""
    summary = str(manifest.get("summary") or "").strip()
    files = {str(op.get("file") or ""): str(op.get("content") or "") for op in ops}
    env: list[str] = []
    for name, text in files.items():
        if name.lower().endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py")):
            env += re.findall(r"process\.env\.([A-Z_][A-Z0-9_]*)", text)
            env += re.findall(r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]", text)
            env += re.findall(r"os\.environ\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]", text)
    env = sorted(set(env) - {"NODE_ENV", "PORT"}) + (["PORT"] if "PORT" in env else [])
    scripts: dict[str, str] = {}
    try:
        pkg = json.loads(files.get("package.json") or "{}")
        scripts = {k: str(v) for k, v in (pkg.get("scripts") or {}).items()} if isinstance(pkg, dict) else {}
    except ValueError:
        pass
    title = summary or posixpath.basename(path)
    out = [f"# {title}", "",
           "> 이 문서는 AI 작성이 실패해 ReCoder 가 설계와 만들어진 파일에서 자동으로 만든 기본 문서입니다. 필요하면 내용을 보태세요.", ""]
    if posixpath.basename(path).lower() not in ("readme.md", "readme.markdown", "readme.rst", "readme.txt"):
        return "\n".join(out).rstrip() + "\n"
    out += ["## 실행", ""]
    if "package.json" in files:
        out += ["```bash", "npm install"]
        for k in ("build", "start"):
            if k in scripts:
                out.append(f"npm run {k}" if k != "start" else "npm start")
        out += ["```", ""]
    elif "requirements.txt" in files:
        out += ["```bash", "pip install -r requirements.txt", "```", ""]
    dockerfile = next((t for f, t in files.items() if posixpath.basename(f) == "Dockerfile"), None)
    if dockerfile is not None:
        port = re.search(r"^\s*EXPOSE\s+(\d+)", dockerfile, re.M | re.I)
        publish = f" -p {port.group(1)}:{port.group(1)}" if port else ""
        out += ["Docker 로 실행:", "", "```bash", "docker build -t app .", f"docker run --env-file .env{publish} app", "```", ""]
    if env:
        out += ["## 환경 변수", "", "실제 값은 `.env` 에만 두고 저장소에 올리지 마세요.", "", "```"]
        out += [f"{name}=<{name}>" for name in env[:40]]
        out += ["```", ""]
    if scripts:
        out += ["## 스크립트", ""] + [f"- `npm run {k}` — `{v}`" for k, v in list(scripts.items())[:12]] + [""]
    return "\n".join(out).rstrip() + "\n"


def _is_doc(path: str) -> bool:
    """앱 동작에 필요 없는 문서인가 — 끝내 못 쓰면 설계로 기본 문서를 만든다(앱을 막지 않는다)."""
    low = str(path or "").replace("\\", "/").lower()
    return low.endswith(_DOC_SUFFIXES) and not low.startswith(("docs/adr/",)) and "/adr/" not in low


def _file_hint(path: str) -> str:
    """파일 종류별로 꼭 지킬 것 — 문서 길이, Prisma 스키마를 코드와 같은 모양으로."""
    hint = _doc_hint(path)
    if str(path).endswith(".prisma"):
        #: 실기기 2.0.10(Haiku): 스키마만 Prisma 관례(PAID @map, BigInt, @@validate)로 써서 코드와 어긋났다.
        hint += ("\nPrisma 스키마 규칙: enum 값은 약속·코드가 쓰는 글자 그대로 쓰세요('paid' 를 쓰면 값도 paid — 대문자 이름+@map 금지). "
                 "금액·수량·재고는 Int(BigInt 금지 — 코드가 number 로 다룸). 관계 필드 이름은 약속에 적힌 이름 그대로(예: order_items). "
                 "@default 는 그 enum 값 그대로. Prisma 에 없는 속성(@@validate, @@check, @check)은 쓰지 마세요(값 검사는 코드에서).")
    if re.search(r"\.(?:tsx|jsx|vue|svelte)$", str(path)) or re.search(r"(^|/)(?:frontend|client|web)/src/", str(path)):
        #: 실기기 2.0.12 쇼핑몰: 화면 문구가 한국어·영어로 섞였고(Add to Cart / 장바구니), 원화를 /100 으로 표시했고,
        #: api 클라이언트(baseURL '/api') 호출에 '/api' 를 또 붙였고, 받아 온 값을 useRef 에만 넣어 화면이 다시 그려지지 않았다.
        hint += ("\n화면 규칙: 화면에 보이는 문구(제목·버튼·라벨·안내·오류)는 사용자 요청과 같은 언어로 통일하세요(한국어 요청이면 모두 한국어). "
                 "원화(KRW)는 소수 단위가 없습니다 — 저장된 금액 정수를 그대로 원으로 표시하세요(100 으로 나누지 말 것). "
                 "api 클라이언트의 baseURL 이 '/api' 면 호출 경로에 '/api' 를 다시 붙이지 마세요. "
                 "화면에 보여 줄 값(받아 온 데이터·로딩·오류)은 useState 로 두세요(useRef 는 다시 그리지 않습니다). "
                 "스타일은 Tailwind 유틸리티 클래스로 일관되게 쓰세요.")
    return hint


def _doc_hint(path: str) -> str:
    if not _is_doc(path):
        return ""
    return ("\n문서는 실행에 꼭 필요한 것만 짧게 씁니다: 무엇인지 한두 줄, 준비물, 설치·환경 변수 이름(값은 <자리표시>)·"
            "DB 초기화·실행·Docker 명령. 각 항목은 몇 줄로, 긴 설명·예시 응답·전체 API 목록은 넣지 마세요.")


#: code_agent._build_code_prompt 끝의 응답 형식 지시(ops JSON) 시작 문구. 설계·조각·교정 호출에서는 뺀다.
OUTPUT_FORMAT_MARKER = "아래 JSON 형식으로만 응답하세요"


def _without_output_format(prompt: str) -> str:
    at = prompt.rfind(OUTPUT_FORMAT_MARKER)
    return prompt[:at].rstrip() if at > 0 else prompt


def _part_context(written: str, limit: int = 40_000) -> str:
    if len(written) <= limit:
        return written
    return written[:6_000] + "\n/* … (중간 생략 — 이미 작성됨) … */\n" + written[-(limit - 6_000):]


def _strip_overlap(written: str, piece: str) -> str:
    """이어 받은 조각에서 이미 쓴 줄을 걷어 낸다.

    · 끝부분을 다시 쓴 경우(가장 흔함): 이미 쓴 마지막 줄들과 조각의 첫 줄들이 같으면 뺀다(최대 60줄).
    · 처음부터 다시 쓴 경우: 조각이 이미 쓴 내용의 첫 줄들과 같게 시작하면, 이미 쓴 만큼을 줄 단위로 건너뛴다.
    """
    if not written or not piece:
        return piece
    lines = piece.split("\n")
    done_lines = written.rstrip("\n").split("\n")
    norm = lambda xs: [x.rstrip() for x in xs]  # noqa: E731
    #: 처음부터 다시 쓰기 — 첫 3줄(내용 있는 것)이 같으면 같은 만큼 건너뛴다
    head = [x for x in norm(done_lines[:3])]
    if len(done_lines) >= 3 and norm(lines[:3]) == head and any(h.strip() for h in head):
        k = 0
        while k < len(lines) and k < len(done_lines) and lines[k].rstrip() == done_lines[k].rstrip():
            k += 1
        return "\n".join(lines[k:])
    tail = done_lines[-60:]
    for n in range(min(len(tail), len(lines)), 0, -1):
        h = norm(lines[:n])
        if h == norm(tail[-n:]) and any(x.strip() for x in h):
            return "\n".join(lines[n:])
    return piece


_JSX = re.compile(r"(return\s*\(?\s*<[A-Za-z]|=>\s*\(?\s*<[A-Za-z]|^\s*<[A-Za-z][\w.]*[\s>/])", re.M)


def _check_file(path: str, content: str) -> list[str]:
    """로컬에서 바로 할 수 있는 문법 확인. 도구가 없으면 건너뛴다(거짓 경보보다 낫다)."""
    name = path.lower()
    try:
        if name.endswith(".json"):
            json.loads(content)
        elif name.endswith(".py"):
            compile(content, path, "exec")
        elif name.endswith((".js", ".cjs", ".mjs")) and not _JSX.search(content):
            node = shutil.which("node")
            if node:
                suffix = ".mjs" if name.endswith(".mjs") or re.search(r"^\s*(import|export)\s", content, re.M) else ".cjs"
                with tempfile.TemporaryDirectory() as tmp:
                    f = os.path.join(tmp, "check" + suffix)
                    with open(f, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    r = subprocess.run([node, "--check", f], capture_output=True, text=True, timeout=15)
                    if r.returncode != 0:
                        msg = (r.stderr or r.stdout or "").strip().splitlines()
                        detail = next((m for m in msg if "Error" in m), msg[-1] if msg else "문법 오류")
                        return [f"문법 오류: {detail[:200]}"]
    except SyntaxError as exc:
        return [f"문법 오류: {exc.msg} ({exc.lineno}번째 줄)"]
    except ValueError as exc:
        return [f"JSON 형식 오류: {str(exc)[:200]}"]
    except (OSError, subprocess.SubprocessError):
        return []
    return []


def _secret_problems(path: str, content: str) -> list[str]:
    try:
        from security_scan import is_doc_like, scan_text_for_secrets
    except ImportError:  # pragma: no cover
        return []
    #: 문서·예시 파일(README·.env.example)은 AI 에게 다시 쓰게 하지 않는다 — 생성이 끝날 때 키 모양 값을
    #: 자리표시로 바꾼다(code_agent · security_scan.redact_doc_secrets, 배포 보안 검사와 같은 기준).
    if is_doc_like(path) or posixpath.basename(path).startswith(".env."):
        return []
    out = []
    for hit in scan_text_for_secrets(content, path) or []:
        kind = str(hit.get("type") or hit.get("name") or hit.get("rule") or "")
        line = hit.get("line") or hit.get("lineno") or ""
        if kind:
            out.append(f"하드코딩된 비밀값({kind}) {line}번째 줄")
    return out[:5]


#: 전체 점검 교정 — 고칠 파일과 함께 그 파일이 쓰는 다른 파일(이름·타입 정의)을 보고, 필요하면 그 파일도 고친다.
FIX_SCHEMA = {
    "type": "object",
    "properties": {"edits": {"type": "array", "items": {
        "type": "object",
        "properties": {"file": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}},
        "required": ["find", "replace"], "additionalProperties": False}}},
    "required": ["edits"],
    "additionalProperties": False,
}
_REL_IMPORT = re.compile(r"""(?:from\s*|import\s*\(?\s*|require\(\s*)['"](\.{1,2}/[^'"]+)['"]""")


def _related(ops: list[dict], index: int, limit: int = 36_000, hint: str = "") -> list[int]:
    """이 파일이 불러오는 프로젝트 파일(한 단계) — 이름·타입·함수 모양을 맞추려면 정의를 봐야 한다.

    오류 문장에 나온 이름(OrderListResponse·useCart·PaginationProps …)을 정의하는 파일을 먼저 넣는다 — import 순서대로
    채우면 미들웨어 파일들로 한도가 차서 정작 타입 정의 파일이 빠졌다(TEMP 2.0.8 점검: orders.ts 에 types/ 가 빠짐)."""
    by_norm = {_norm(op["file"]): i for i, op in enumerate(ops)}
    me = ops[index]
    base = posixpath.dirname(str(me["file"]).replace("\\", "/"))
    found: list[int] = []
    for spec in _REL_IMPORT.findall(str(me.get("content") or "")):
        target = posixpath.normpath(posixpath.join(base, spec))
        stem = re.sub(r"\.(js|jsx|ts|tsx|mjs|cjs)$", "", target)
        for cand in (target, stem + ".ts", stem + ".tsx", stem + ".js", stem + ".jsx", stem + "/index.ts", stem + "/index.tsx",
                     stem + "/index.js", stem + ".d.ts"):
            j = by_norm.get(_norm(cand))
            if j is not None and j != index:
                if j not in found:
                    found.append(j)
                break
    if re.search(r"""@prisma/client|\bprisma\.\w+\.(?:find|create|update|delete|upsert|count|aggregate)""", str(me.get("content") or "")) \
            or re.search(r"\b\w+(?:Include|WhereInput|Select|CreateInput|UpdateInput|OrderByWithRelationInput)\b", hint or ""):
        #: Prisma 를 쓰는 파일 — 모델 이름·관계 필드·enum 값은 schema.prisma 가 정한다(실기기: include: { order_items } 인데
        #: 스키마의 관계 이름은 items). 스키마를 함께 보여 줘야 어느 쪽을 맞출지 안다.
        for j, op in enumerate(ops):
            if j != index and str(op.get("file") or "").endswith(".prisma") and j not in found:
                found.insert(0, j)
    words = {w for w in re.findall(r"[A-Za-z_$][\w$]{3,}", hint or "")
             if not w.startswith("TS") and w not in {"Type", "type", "Property", "does", "exist", "Argument", "assignable",
                                                     "parameter", "Cannot", "find", "name", "Expected", "arguments", "only",
                                                     "specify", "known", "properties", "Object", "literal", "string", "number",
                                                     "boolean", "null", "undefined", "IntrinsicAttributes", "void"}}

    def score(j: int) -> int:
        text = str(ops[j].get("content") or "")
        return sum(1 for w in words if re.search(rf"\b(?:export\s+)?(?:declare\s+)?(?:async\s+)?(?:function|const|let|class|interface|type|enum)\s+{re.escape(w)}\b", text)) * 10 \
            + sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text))

    ranked = sorted(found, key=lambda j: (-score(j), found.index(j))) if words else found
    out, used = [], 0
    for j in ranked:
        size = len(str(ops[j].get("content") or ""))
        if used + size <= limit:
            out.append(j)
            used += size
    return sorted(out, key=found.index)


_TYPE_IN_MSG = re.compile(r"type '(?:IntrinsicAttributes & )?([A-Z][\w$]*)'")


def _definition_first(ops: list[dict], grouped: dict[int, list[str]]) -> dict[int, list[str]]:
    """여러 파일이 같은 정의(컴포넌트 props·훅 반환·Context 타입)와 어긋나면 정의 파일 하나를 먼저 고친다.

    실기기(카페 2.0.8): 화면 10개가 ErrorAlert 에 onClose 를 안 넘겨(필수) 10개 파일을 따로따로 고치려 했다 — 정의에서
    onClose 를 선택으로 바꾸면 한 번에 끝난다. 쓰는 쪽 파일의 그 오류 줄은 이번 회차에서 빼고(동시에 같은 정의를
    서로 다르게 고치지 않게) 다음 빌드에서 남았는지 다시 본다."""
    users: dict[str, dict[int, list[str]]] = {}
    for idx, problems in grouped.items():
        for problem in problems:
            for line in re.split(r" / (?=\d+행 )", problem):
                for name in set(_TYPE_IN_MSG.findall(line)):
                    users.setdefault(name, {}).setdefault(idx, []).append(line.split(" (해결:", 1)[0])
    out = {k: list(v) for k, v in grouped.items()}
    for name, by_file in users.items():
        if len(by_file) < 2:
            continue
        owners = [i for i, op in enumerate(ops) if re.search(
            rf"^\s*(?:export\s+)?(?:interface|type)\s+{re.escape(name)}\b", str(op.get("content") or ""), re.M)]
        if len(owners) != 1 or ops[owners[0]].get("fixed"):
            continue
        owner = owners[0]
        lines = [f"{ops[i]['file']}: {l.split(': ', 1)[-1][:220]}" for i, ls in by_file.items() for l in ls][:14]
        out.setdefault(owner, []).insert(0,
            f"다른 파일 {len(by_file)}개가 이 파일의 {name} 와(과) 다르게 씁니다 — 쓰는 쪽이 여럿이므로 정의를 그 쓰임에 맞추세요"
            f"(필수 항목은 선택(?)으로, 쓰는 쪽이 꺼내는 필드·함수는 더하기). 기존 이름·필드는 지우지 마세요.\n  "
            + "\n  ".join(lines) + " (해결: 정의 파일만 고치면 쓰는 파일들은 그대로 빌드됩니다)")
        for i in by_file:
            if i == owner:
                continue
            kept = []
            for problem in out.get(i, []):
                parts = [l for l in re.split(r" / (?=\d+행 )", problem) if name not in _TYPE_IN_MSG.findall(l)]
                if parts:
                    kept.append(" / ".join(parts))
            if kept:
                out[i] = kept
            else:
                out.pop(i, None)
    #: 정의 파일을 먼저(회차당 파일 수 한도 안에 들게)
    owners_first = sorted(out.items(), key=lambda kv: 0 if kv[1] and kv[1][0].startswith("다른 파일 ") else 1)
    return dict(owners_first)


def edit_fix_round(prompt: str, ops: list[dict], issues: list[dict], *, max_files: int = 8,
                   emit: Callable[[dict], None] | None = None, workers: int | None = None) -> list[dict] | None:
    """전체 점검에서 나온 문제를 **바뀔 부분만** 고친다(대규모 생성용).

    · 고칠 파일과 그 파일이 불러오는 파일(정의)을 함께 보여 주고, 어긋난 쪽이 정의 파일이면 그 파일도 고치게 한다 —
      한 파일만 보여 주면 "cart 가 없다" 같은 파일 사이 어긋남을 끝내 못 고쳤다(실기기 2.0.7: 10건이 6번 돌아도 남음).
    · 파일마다 동시에(기본 3명) 받고, 받은 고침은 차례로 적용한다(찾을 원문이 정확히 한 번 있을 때만).
    · ReCoder 고정 파일(검증 Dockerfile·결제 모듈)은 고치지 않는다. 고칠 수 있는 게 없으면 None.
    """
    from concurrent.futures import ThreadPoolExecutor
    ca = _ca()
    by_path = {_norm(op["file"]): i for i, op in enumerate(ops)}
    grouped: dict[int, list[str]] = {}
    for issue in issues:
        if issue.get("severity") != "error":
            continue
        idx = by_path.get(_norm(issue.get("file") or ""))
        if idx is None or ops[idx].get("fixed"):
            continue  # ReCoder 가 넣은 고정 파일(결제 모듈)은 AI 가 고치지 않는다
        grouped.setdefault(idx, []).append(f"{issue.get('message', '')} (해결: {issue.get('fix', '')})")
    if not grouped:
        return None
    grouped = _definition_first(ops, grouped)
    engine = LargeGeneration(prompt, emit=emit)
    listing = "\n".join(f"- {op['file']}" for op in ops)
    targets = list(grouped.items())[:max_files]

    def ask(item: tuple[int, list[str]]) -> tuple[int, list[dict]]:
        idx, problems = item
        op = ops[idx]
        if emit:
            n_err = sum(len(re.findall(r"\d+행 ", x)) or 1 for x in problems)
            emit({"step": "fixing", "agent": "review", "file": op["file"], "message": f"전체 점검 — {op['file']} 오류 {n_err}건 고치는 중"})
        related = _related(ops, idx, hint="\n".join(problems))
        context = "".join(f"\n[참고 — 이 파일이 불러오는 파일] {ops[j]['file']}\n```\n{ca._prompt_body(ops[j]['content'], 20_000)}\n```\n"
                          for j in related)
        p = _without_output_format(prompt) + f"""

[전체 점검 교정] 생성한 파일 목록:
{listing}
{op['file']} 에 아래 문제가 있습니다(컨테이너 빌드·점검의 실제 오류):
""" + "\n".join(f"- {x}" for x in problems) + f"""
{context}
고치는 방법:
- **파일 전체를 다시 쓰지 말고** 바꿀 부분만 edits 로 주세요. find 는 그 파일에 정확히 한 번 나오는 원문 그대로, replace 는 바꿀 내용.
- 기본은 {op['file']} 를 고칩니다(file 생략). 어긋난 원인이 위 참고 파일(이름·타입·함수 모양 정의)이면 그 파일을 고쳐도 됩니다 —
  그때는 edit 에 "file": "<그 파일 경로>" 를 넣으세요. 다른 파일이 쓰는 기존 이름은 지우지 말고 더하세요.
- 타입 검사를 끄거나(any 남발·@ts-ignore·as any) 기능을 지우지 마세요.
현재 파일 {op['file']}:
```
{ca._prompt_body(op['content'], 60_000)}
```"""
        try:
            resp = engine.call(p, FIX_SCHEMA, "generate_code_consistency", 4096, agent="review")
            data = ca._extract_json(resp.text)
        except (LLMError, ValueError, CodeOutputError, Paused):
            return idx, []
        edits = [e for e in (data.get("edits") or []) if isinstance(e, dict)
                 and isinstance(e.get("find"), str) and isinstance(e.get("replace"), str) and e["find"]]
        return idx, edits

    #: 생성과 같은 수의 에이전트가 동시에 고친다(설정한 팀 인원 — 예전엔 3명 고정이라 고칠 파일이 많으면 오래 걸렸다)
    workers = workers or engine.concurrency
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets))), thread_name_prefix="fix") as pool:
        answers = list(pool.map(ask, targets))
    out = [dict(op) for op in ops]
    changed = False
    for idx, edits in answers:
        for edit in edits:
            target_idx = idx
            if edit.get("file"):
                target_idx = by_path.get(_norm(edit["file"]), -1)
                if target_idx < 0 or out[target_idx].get("fixed"):
                    continue
            content = str(out[target_idx].get("content") or "")
            if content.count(edit["find"]) == 1:
                out[target_idx] = dict(out[target_idx], content=content.replace(edit["find"], edit["replace"], 1))
                changed = True
    return out if changed else None


# ── 설계·결과의 빈 곳 메우기 ─────────────────────────────────────────────
#: 실기기(2026-10-10 2.0.9 쇼핑몰, Haiku): 약속 문서가 길어 파일 목록을 나눠 받다가 AI 가 more=false 로 일찍 끝냈다.
#: 화면 30개는 다 있는데 서버는 유틸·헬스 등 10개뿐 — server.ts·DB 연결·인증·상품/주문 라우트·Prisma 스키마가
#: 목록에 없어서 아무도 만들지 않았고, 만든 파일은 없는 `../db/client`·`../middleware/auth` 를 불러 빌드가 실패했다.
#: 그래서 (1) 설계 직후 결정적으로 빈 곳을 채우고, (2) 검토 에이전트가 약속 대비 빠진 파일을 한 번 더 찾고,
#: (3) 다 만든 뒤에도 불러오는데 없는 파일이 있으면 그 파일을 마저 만든다.

_CODE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_LOCAL_IMPORT = (
    re.compile(r"""^\s*import\s+(?:type\s+)?([\w$*{}\s,]+?)\s+from\s*['"](\.{1,2}/[^'"]+)['"]""", re.MULTILINE),
    re.compile(r"""^\s*import\s*['"](\.{1,2}/[^'"]+)['"]""", re.MULTILINE),
    re.compile(r"""^\s*export\s+(?:type\s+)?([\w$*{}\s,]+?)\s+from\s*['"](\.{1,2}/[^'"]+)['"]""", re.MULTILINE),
    re.compile(r"""\b(?:const|let|var)\s+([\w$]+|\{[^}]*\})\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)"""),
)
#: 코드가 아닌 자원(스타일·그림·JSON)은 여기서 만들지 않는다 — 빈 스타일 파일 만들기는 배포 준비 점검이 따로 한다.
_ASSET_EXT = re.compile(r"\.(css|scss|sass|less|svg|png|jpe?g|gif|webp|ico|json|html|md|txt|woff2?|ttf|wasm)$", re.I)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\\'\"`])//[^\n]*", r"\1", text)


def _spec_candidates(base: str) -> list[str]:
    """`./x`·`./x.js`(NodeNext 식) 가 가리킬 수 있는 원본 파일들."""
    stem = re.sub(r"\.(js|jsx|mjs|cjs|ts|tsx)$", "", base)
    exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")
    return [base] + [stem + e for e in exts] + [f"{base}/index{e}" for e in exts[:4]]


def _imported_names(clause: str, text: str) -> list[str]:
    """`X, { a, b as c }` / `* as ns` → 그 파일이 내보내야 할 이름들(default 포함)."""
    clause = (clause or "").strip()
    names: list[str] = []
    m = re.match(r"\*\s+as\s+([\w$]+)", clause)
    if m:
        used = sorted(set(re.findall(rf"\b{re.escape(m.group(1))}\.([\w$]+)", text)))
        return used or ["(네임스페이스로 불러옴)"]
    head, _, rest = clause.partition("{")
    head = head.strip().rstrip(",").strip()
    if head and re.fullmatch(r"[\w$]+", head):
        names.append(f"default({head})")
    if rest:
        for part in rest.split("}")[0].split(","):
            part = re.sub(r"^type\s+", "", part.strip())
            name = part.split(" as ")[0].strip()
            if name:
                names.append(name)
    if clause.startswith("{") is False and not names and re.fullmatch(r"[\w$]+", clause):
        names.append(f"default({clause})")
    return names


def _target_for(importer: str, base: str) -> str:
    """없는 파일을 어떤 확장자로 만들지 — 불러오는 파일의 언어를 따른다(`./x.js` 를 TS 가 부르면 x.ts)."""
    m = re.search(r"\.(js|jsx|mjs|cjs|ts|tsx)$", base)
    ts = importer.endswith((".ts", ".tsx"))
    if m:
        if ts and m.group(1) in ("js", "mjs", "cjs"):
            return base[: m.start()] + ".ts"
        if ts and m.group(1) == "jsx":
            return base[: m.start()] + ".tsx"
        return base
    last = posixpath.basename(base)
    component = bool(re.match(r"[A-Z]", last)) or bool(re.search(r"/(components?|pages?|views?|layouts?)/", "/" + base))
    if importer.endswith(".tsx"):
        return base + (".tsx" if component else ".ts")
    if ts:
        return base + ".ts"
    if importer.endswith(".jsx"):
        return base + (".jsx" if component else ".js")
    return base + (".cjs" if importer.endswith(".cjs") else ".mjs" if importer.endswith(".mjs") else ".js")


def missing_local_imports(files: dict[str, str], exists: Callable[[str], bool] | None = None) -> dict[str, dict]:
    """생성한 코드가 불러오는데 어디에도 없는 프로젝트 파일 → {만들 경로: {"importers": [...], "names": [...]}}.

    files: {경로: 내용}(프로젝트 루트 기준). exists: 디스크에 이미 있는 파일인지(기존 프로젝트에 더할 때)."""
    norm = {_norm(p): p for p in files}
    have = lambda p: _norm(p) in norm or bool(exists and exists(p))  # noqa: E731
    out: dict[str, dict] = {}
    for path, text in files.items():
        if not path.endswith(_CODE_EXTS) or not isinstance(text, str):
            continue
        active = _strip_comments(text)
        found: list[tuple[str, str]] = []
        for i, pattern in enumerate(_LOCAL_IMPORT):
            for m in pattern.finditer(active):
                if i in (1, 4):
                    found.append(("", m.group(1)))
                else:
                    found.append((m.group(1), m.group(2)))
        for clause, spec in found:
            spec = spec.split("?")[0]
            if _ASSET_EXT.search(spec):
                continue
            base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
            if base.startswith("..") or base.startswith("/"):
                continue
            if any(have(c) for c in _spec_candidates(base)):
                continue
            target = _target_for(path, base)
            entry = out.setdefault(target, {"importers": [], "names": []})
            if path not in entry["importers"]:
                entry["importers"].append(path)
            for n in _imported_names(clause, active):
                if n not in entry["names"]:
                    entry["names"].append(n)
    return out


_SERVER_DIRS = ("backend", "server", "api")


def _pattern_examples(pattern: str) -> list[str]:
    """plan_gaps 의 진입점 정규식이 뜻하는 실제 경로들(디스크 확인용) — `a/(src/)?(x|y)\\.(ts|js)` 를 펼친다."""
    out = [""]
    for token in re.findall(r"\((?:[^()]|\([^()]*\))*\)\??|\\.|[^()\\]", pattern):
        if token.startswith("("):
            optional = token.endswith("?")
            body = token[1:-2] if optional else token[1:-1]
            alts = body.split("|") + ([""] if optional else [])
            out = [o + a for o in out for a in alts][:200]
        else:
            out = [o + (token[1] if token.startswith("\\") else token) for o in out]
    return out
_CLIENT_DIRS = ("frontend", "client", "web")
_ENTRY_STEMS = ("server", "index", "app", "main")


def plan_gaps(files: list[dict], manifest: dict, exists: Callable[[str], bool] | None = None) -> list[dict]:
    """설계 목록에서 결정적으로 알 수 있는 빈 곳 — 실행에 꼭 필요한데 목록에 없는 파일.

    · uses 에 적었는데 목록에 없는 프로젝트 파일
    · 서버 폴더에 코드가 있는데 진입 파일(src/server.ts 등)이 없음
    · 약속·요약에 Prisma 가 있는데 schema.prisma / Prisma 클라이언트 파일이 없음
    · 화면 폴더(React)에 진입 파일(index.html·src/main.tsx·src/App.tsx)이 없음"""
    paths = [str(f.get("file") or "") for f in files]
    keys = {_norm(p) for p in paths}
    add: list[dict] = []

    def have(p: str) -> bool:
        return any(_norm(c) in keys or bool(exists and exists(c)) for c in _spec_candidates(p))

    def present(pattern: str) -> bool:
        """목록이나 디스크(기존 프로젝트)에 이 정규식에 맞는 파일이 있는가."""
        return any(re.fullmatch(pattern, p) for p in paths) or bool(exists and any(
            exists(c) for c in _pattern_examples(pattern)))

    def push(path: str, purpose: str, layer: int, uses: list[str] | None = None) -> None:
        if _norm(path) in keys:
            return
        keys.add(_norm(path))
        add.append({"file": path, "purpose": purpose, "layer": layer, "uses": uses or [], "planned_by": "recoder"})

    for f in files:
        src = str(f.get("file") or "")
        for u in f.get("uses") or []:
            u = str(u).strip().replace("\\", "/")
            if u.startswith("./"):
                u = u[2:]
            if (not u or u.startswith(("..", "@", "~")) or "/" not in u or _ASSET_EXT.search(u) or have(u)
                    or not re.match(r"[\w.-]+/", u)):
                continue
            if not re.search(r"\.(ts|tsx|js|jsx|mjs|cjs|py|vue|prisma)$", u):
                u = _target_for(src, u) if src.endswith(_CODE_EXTS) else u
            push(u, f"{src} 가 불러오는 파일 — 설계 목록에서 빠져 추가함. {src} 가 쓰는 이름을 그대로 내보내세요.", _guess_layer(u))
    text = f"{manifest.get('summary') or ''}\n{manifest.get('contracts') or ''}".lower()
    #: 약속에 서버 API 가 있는데 서버 쪽 파일이 하나도 없다 — 실기기 2.0.13 쇼핑몰(job 4a06, Haiku): 설계 목록이 화면 30개와
    #: 모의 결제 서버 package.json 뿐이라 백엔드 없이 "완료" 됐고, 배포 점검은 스택을 몰라 엉뚱한 Dockerfile 을 제안했다.
    #: 서버의 뼈대(설정·DB·인증·오류 유틸)를 목록에 넣는다 — 라우트·진입점은 아래에서 약속의 /api 묶음대로 채운다.
    api_segments = sorted(set(re.findall(r"/api/([a-z][a-z0-9_-]{1,30})", text)) - {"v1", "v2", "health"})
    #: 화면 폴더·정적 폴더·AI 모의 결제 서버 밖의 코드(루트 src/routes 같은 서버 포함)가 하나라도 있으면 서버가 있다고 본다.
    server_code = any(
        p.endswith(_CODE_EXTS + (".py",)) and not p.endswith((".tsx", ".jsx", ".d.ts"))
        and not re.match(rf"({'|'.join(_CLIENT_DIRS)}|public|static|docs)/", p)
        and not re.search(r"(^|/)(vite|tailwind|postcss|eslint|jest|vitest)\.config\.", p)
        and not re.match(r"(?:[\w.-]+/)?(?:mock|fake)[-_]?(?:payment|stripe|pay)", p, re.I)
        for p in paths) or any(exists and exists(f"{side}/package.json") for side in _SERVER_DIRS)
    if api_segments and not server_code and not present(r"(src/)?(server|app|index|main)\.(ts|js|mjs|cjs|py)"):
        ts = any(p.endswith((".ts", ".tsx")) for p in paths) or "typescript" in text
        ext = ".ts" if ts else ".js"
        why = "약속(contracts)에 서버 API 가 있는데 설계 목록에 서버가 없어 추가함. "
        push("backend/package.json", why + "서버 의존성(코드가 불러오는 패키지만, 안정 버전)과 scripts: "
             + ("build=tsc, start=node dist/server.js, dev=tsx watch src/server.ts" if ts else "start=node src/server.js"), 0)
        if ts:
            push("backend/tsconfig.json", why + "Node 서버용 TypeScript 설정(strict, rootDir src, outDir dist, module NodeNext)", 0)
        if "prisma" not in text:
            db = ("PostgreSQL(pg Pool, DATABASE_URL)" if re.search(r"postgres|pg\b", text) else
                  "MongoDB(mongoose, MONGODB_URI)" if "mongo" in text else
                  "MySQL(mysql2, DATABASE_URL)" if "mysql" in text else "")
            if db:
                push(f"backend/src/db/connection{ext}", why + f"DB 연결 하나를 만들어 내보냄 — {db}, query 헬퍼", 0)
                if "mongo" not in text:
                    push("backend/src/db/schema.sql", why + "약속(contracts)의 모든 테이블·열·관계·인덱스를 만드는 CREATE TABLE IF NOT EXISTS", 0)
        if re.search(r"jwt|토큰|token|로그인|login|auth", text):
            push(f"backend/src/middleware/auth{ext}", why + "JWT 검증 미들웨어(Authorization: Bearer) — req.user 설정, 관리자 권한 확인", 1)
        push(f"backend/src/utils/errors{ext}", why + "공용 오류 클래스·오류 응답 형식(약속의 오류 형식 그대로)", 0)
        if re.search(r"샘플|시드|seed|sample|demo|데모", text):
            push(f"backend/src/scripts/seed{ext}", why + "샘플 데이터 넣기(이미 있으면 건너뜀, 여러 번 실행해도 중복 없음) — "
                 "package.json scripts.seed 로 실행", 2)
        paths = paths + [f["file"] for f in add]
    for side in _SERVER_DIRS:
        code = [p for p in paths if p.startswith(side + "/") and p.endswith(_CODE_EXTS)]
        if not code:
            continue
        ts = any(p.endswith((".ts", ".tsx")) for p in code)
        ext = ".ts" if ts else ".js"
        routes = [p for p in code if re.search(r"/(routes?|routers?|controllers?)/", p)]
        #: 약속에 적힌 API 묶음(/api/products …)마다 그 묶음을 다루는 서버 파일이 있어야 한다 — 없으면 라우트 파일을 더한다.
        lower_code = [p.lower() for p in code]
        for seg in sorted(set(re.findall(r"/api/([a-z][a-z0-9_-]{1,30})", text))):
            stem = seg[:-1] if seg.endswith("s") and len(seg) > 3 else seg
            handlers = [p for p in lower_code if not re.search(r"/(middlewares?|utils?|types?|lib|config|db|models?|schemas?|validators?)/", p)]
            if seg in ("v1", "v2", "health") or any(re.search(rf"/{re.escape(stem)}", p) for p in handlers):
                continue
            if any(have(f"{side}/src/{d}/{seg}") for d in ("routes", "routers", "controllers")):
                continue
            route = f"{side}/src/routes/{seg}{ext}"
            push(route, f"약속(contracts)의 /api/{seg} 경로 전부(메서드·요청·응답·인증·오류 형식 그대로)를 구현하는 라우터 — 기본 내보내기 router", 1)
            routes.append(route)
            code.append(route)
            lower_code.append(route.lower())
        if not present(rf"{side}/(src/)?({'|'.join(_ENTRY_STEMS)})\.(ts|js|mjs|cjs)"):
            #: uses 를 비워 둔다 — 같은 쪽의 모든 파일(나중에 더해지는 라우트 포함)이 끝난 뒤에 쓰게.
            push(f"{side}/src/server{ext}",
                 "서버 진입점: 환경 변수 읽기, express.json 등 미들웨어, 약속(contracts)의 모든 API 라우터를 그 경로에 연결"
                 f"({', '.join(routes[:20]) or '라우트 파일'}), /health, 오류 처리 미들웨어, 운영 모드에서 화면 빌드 결과(frontend/dist) 제공, PORT 로 listen",
                 2)
        if "prisma" in text:
            if not any(p.endswith(".prisma") for p in paths) and not have(f"{side}/prisma/schema.prisma"):
                push(f"{side}/prisma/schema.prisma",
                     "Prisma 스키마: 약속(contracts)의 모든 테이블·열·관계·enum, datasource postgresql(env DATABASE_URL), generator client", 0)
            if not any(re.search(r"/(db|lib|database|prisma)(/|\.)", p) and p.endswith(_CODE_EXTS) for p in code) and not any(
                    have(f"{side}/src/{c}") for c in ("db/client", "db/index", "db/prisma", "lib/prisma", "lib/db", "db", "prisma")):
                push(f"{side}/src/db/client{ext}",
                     "Prisma 클라이언트 하나를 만들어 `export const prisma` 로 내보냄(개발 중 재시작에도 하나만 쓰도록)", 0)
    for side in _CLIENT_DIRS:
        code = [p for p in paths if p.startswith(side + "/") and p.endswith(_CODE_EXTS)]
        if not any(p.endswith(".tsx") or p.endswith(".jsx") for p in code):
            continue
        x = "tsx" if any(p.endswith(".tsx") for p in code) else "jsx"
        if not have(f"{side}/index.html"):
            push(f"{side}/index.html", f"Vite 진입 HTML — <div id=\"root\"> 와 /src/main.{x} 스크립트", 0)
        if not present(rf"{side}/src/(main|index)\.(tsx|jsx|ts|js)"):
            push(f"{side}/src/main.{x}", "React 진입점 — App 을 #root 에 렌더(Router·Provider 감싸기 포함)", 2, [f"{side}/src/App.{x}"])
        if not present(rf"{side}/src/App\.(tsx|jsx)"):
            pages = [p for p in code if re.search(r"/pages?/", p)]
            push(f"{side}/src/App.{x}", "앱 셸 — 라우트 정의(모든 화면 연결)·공용 레이아웃", 2, pages[:30])
    return add


def fill_missing_files(prompt: str, ops: list[dict], *, summary: str = "", contracts: str = "", root: str = "",
                       target_folder: str = "", emit: Callable[[dict], None] | None = None,
                       workers: int | None = None) -> list[dict]:
    """전체 점검 중 — 코드가 불러오는데 없는 프로젝트 파일을 만들어 넣은 ops(없으면 그대로).

    부분 교정(edit_fix_round)은 있는 파일을 고칠 뿐 새 파일을 만들지 못해, 없는 `../db/client` 같은 문제가 몇 번을
    돌아도 그대로 남았다(실기기 2.0.9 쇼핑몰: "자동 교정으로 고치지 못했습니다")."""
    eng = LargeGeneration(prompt, target_folder=target_folder, concurrency=workers, emit=emit, root=root,
                          security_review=True)
    eng.state["manifest"] = {"summary": summary, "contracts": contracts}
    eng.state["files"] = [{"file": op["file"], "layer": _guess_layer(op["file"]), "uses": []}
                          for op in ops if op.get("action") != "delete"]
    eng.state["ops"] = {_norm(op["file"]): dict(op) for op in ops if op.get("action") != "delete"}
    before = len(eng.state["files"])
    eng._fill_missing_imports()
    if len(eng.state["files"]) == before:
        return ops
    made = [eng.state["ops"][_norm(f["file"])] for f in eng.state["files"][before:] if _norm(f["file"]) in eng.state["ops"]]
    return list(ops) + made
