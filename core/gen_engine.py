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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from llm.base import LLMError, LLMErrorType, LLMRequest
from code_output import CODE_OUTPUT_SCHEMA, CodeOutputError, parse_code_output
import generation_jobs as jobs

PAGE_FILES = 25          # 파일 목록 한 번에 받는 개수(응답 한도 안에 확실히 들어가는 크기)
BATCH = 2                # 작은 파일은 두 개씩 묶어 호출 수를 줄인다(넘치면 하나씩 → 이어 쓰기)
PART_LINES = 150         # 이어 쓰기 한 조각의 줄 수
#: 같은 파일이 실패할 때마다 쓰는 조각 크기(처음 → 두 번째 → 세 번째 시도). 응답이 잘리면 한 시도 안에서도 줄인다.
PART_LINES_STEPS = (150, 80, 40)
MAX_FILE_TRIES = 3       # 한 파일을 이번 실행에서 시도하는 횟수 — 넘으면 그 파일만 실패로 두고 나머지를 계속 만든다
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
                           "layer": {"type": "integer"}},
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
    return out


# ── 엔진 ───────────────────────────────────────────────────────────────

class LargeGeneration:
    def __init__(self, prompt: str, *, target_folder: str = "", new_project: bool = False,
                 job_id: str = "", fingerprint: str = "", concurrency: int | None = None,
                 emit: Callable[[dict], None] | None = None, limiter: RateLimiter | None = None,
                 security_review: bool = True, skip_failed: bool = False) -> None:
        self.prompt = prompt
        #: 응답 형식(ops JSON) 지시를 뺀 맥락 — 설계·조각·교정 호출은 각자 다른 형식으로 답해야 하므로
        #: 이 지시가 같이 들어가면 AI 가 조각 내용 자리에 ops JSON 을 통째로 넣는다(실기기 pages.css).
        self.context_prompt = _without_output_format(prompt)
        #: 이어 만들 때 실패한 파일을 빼고 결과를 받는다(사용자가 고른 경우만).
        self.skip_failed = skip_failed
        self.skipped: list[dict] = []
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
            lines = PART_LINES_STEPS[min(tries, len(PART_LINES_STEPS) - 1)]
            self._event("file_retry", agent=agent, file=path, kind=kind, attempt=tries + 1,
                        message=f"{path} — {ERROR_TEXT[kind]} → 처음부터 {lines}줄씩 다시 씁니다 ({tries + 1}/{MAX_FILE_TRIES})")
        self._save()
        return tries

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
    def call(self, prompt: str, schema: dict, operation: str, max_tokens: int, agent: str = "") -> Any:
        ca = _ca()
        attempt, waits = 0, (3, 8, 20, 45, 60)
        while True:
            self.limiter.acquire(lambda s: self._event("waiting", agent=agent, seconds=round(s),
                                                       message=f"AI 호출 한도 때문에 {round(s)}초 기다리는 중"))
            try:
                resp = ca.get_router().call(
                    LLMRequest(prompt=prompt, json_schema=schema, max_tokens=max_tokens, temperature=0.2),
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
아직 목록에 없는 파일을 의존성 순서로 최대 {PAGE_FILES}개 더 주세요(파일 내용은 쓰지 마세요). 각 파일에 layer(0/1/2)를 붙이고,
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
        self.state["manifest"] = manifest
        self.state["files"] = files
        self._save()
        waves = {layer: sum(1 for f in files if f["layer"] == layer) for layer in (0, 1, 2)}
        self._event("planned", message=f"설계 완료 — 파일 {len(files)}개 (기반 {waves[0]} · 기능 {waves[1]} · 화면 {waves[2]})",
                    summary=summary, files=[{"file": f["file"], "layer": f["layer"], "purpose": f.get("purpose", "")} for f in files])

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
                        "layer": item.get("layer") if isinstance(item.get("layer"), int) else None})
        return out

    def _rel(self, path: str) -> str:
        return _ca()._relative_to_target([{"file": path}], self.target_folder)[0]["file"]

    # ── 공통 프롬프트 ──
    def _context(self, files_done: list[dict]) -> str:
        m, files = self.state["manifest"], self.state["files"]
        listing = "\n".join(f"- {f['file']}: {f.get('purpose', '')}" for f in files)
        return f"""

[대규모 생성 2단계] 전체 파일 목록과 파일 사이의 약속은 아래와 같습니다. 다른 파일은 다른 에이전트가 같은 약속으로 동시에 작성합니다.
전체 요약: {m.get('summary', '')}
약속(반드시 지킬 것 — 이름·경로·필드·타입을 글자 그대로 맞추세요):
{m.get('contracts', '') or '(없음 — 파일 목록과 요청에서 일관되게 정하세요)'}
전체 파일 목록:
{listing}""" + _ca()._completed_files_context(files_done)

    # ── 3. 이어 쓰기 ──
    def write_in_parts(self, base_prompt: str, label: str, *, agent: str = "", operation: str = "generate_code_file_part",
                       path: str = "", lines: int = PART_LINES) -> str:
        """한 응답에 다 들어가지 않는 내용을 조각으로 이어 쓴다.

        · 조각은 저장 전에 검사한다(clean_content) — 응답 형식이 섞이면 실제 내용만, 꺼낼 수 없으면 버리고 다시 받는다.
        · 조각 응답이 길이 한도에서 잘리면 조각 크기를 줄여(150→80→40줄) 같은 조각을 다시 받는다.
        · 진행이 없거나 형식이 계속 틀리면 CodeOutputError — 부르는 쪽이 방법을 바꿔 다시 시도한다.
        """
        with self._lock:
            partial = self.state["partial"].get(path) if path else None
        written = (partial or {}).get("written", "")
        part = int((partial or {}).get("parts", 0))
        lines = int((partial or {}).get("lines", lines) or lines)
        bad = 0      # 형식이 맞지 않은 응답 연속 횟수
        empty = 0    # 빈 조각 연속 횟수
        name = path or label
        while part < PART_SAFETY:
            p = base_prompt + f"""

[이어 쓰기 — 응답 형식] {label} 은(는) 한 응답에 다 들어가지 않아 여러 번에 나눠 이어 씁니다. 지금은 {part + 1}번째 조각입니다.
이번 응답은 JSON {{"content": "...", "done": true|false}} 하나입니다.
- content 에는 {label} 의 {'처음' if not written else '바로 다음'} 부분을 **최대 {lines}줄** 파일에 들어갈 글자 그대로 쓰세요.
  content 안에 다시 JSON(summary·ops·file·rationale 등)으로 감싸지 마세요. 설명이나 마크다운 코드펜스도 넣지 마세요.
- 줄 중간에서 끊지 말고 함수·블록·규칙·문단 경계에서 끊으세요. 다음 응답에서 정확히 이어 씁니다.
- 이미 쓴 줄을 다시 쓰지 마세요.
- 이번 조각으로 끝나면 done 을 true, 아직 남았으면 false 로 주세요."""
            if written:
                p += f"\n\n지금까지 쓴 {label} (이 바로 뒤부터 이어서 쓰세요):\n```\n{_part_context(written)}\n```"
            try:
                resp = self.call(p, PART_SCHEMA, operation, 4096, agent=agent)
            except LLMError as exc:
                if not _is_truncation(exc):
                    raise
                smaller = next((n for n in PART_LINES_STEPS if n < lines), None)
                if smaller is None:
                    raise
                lines = smaller
                self._log(name, "truncation", f"{part + 1}번째 조각 응답이 잘려 {lines}줄씩으로 줄여 다시 받음")
                self._event("part_retry", agent=agent, file=path, kind="truncation", lines=lines,
                            message=f"{posixpath.basename(name)} — 응답이 잘려 {lines}줄씩으로 줄여 다시 씁니다")
                continue
            try:
                data = _ca()._extract_json(resp.text)
            except ValueError:
                data = None
            piece = data.get("content") if isinstance(data, dict) else None
            done = data.get("done") if isinstance(data, dict) else None
            cleaned = clean_content(name, piece) if isinstance(piece, str) else None
            if cleaned is None:
                bad += 1
                self._log(name, "format", f"{part + 1}번째 조각 응답 형식이 맞지 않아 버림({bad}/3)")
                if bad >= 3:
                    raise CodeOutputError(f"{label} 을(를) 이어 쓰는 응답이 계속 형식에 맞지 않습니다.")
                continue
            bad = 0
            if cleaned != piece and is_wrapped(name, piece):
                self._log(name, "format", f"{part + 1}번째 조각에 응답 형식이 섞여 실제 내용만 꺼냄")
            piece = _strip_overlap(written, cleaned)
            if written and piece and not written.endswith("\n"):
                written += "\n"
            written += piece
            part += 1
            if path:
                with self._lock:
                    self.state["partial"][path] = {"written": written, "parts": part, "lines": lines}
                self._save()
                self._event("file_part", agent=agent, file=path, part=part, lines=written.count("\n") + 1,
                            message=f"{path} 이어 쓰는 중 · {part}번째 조각 · {written.count(chr(10)) + 1}줄")
            if done is True:
                break
            empty = empty + 1 if not piece.strip() else 0
            if empty >= 3:
                raise CodeOutputError(f"{label} 을(를) 이어 쓰는 중 더 진행되지 않았습니다.")
        else:
            raise CodeOutputError(f"{label} 이(가) 비정상적으로 길어 멈췄습니다({PART_SAFETY}조각).")
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
        base = self.prompt + self._context(files_done)
        wanted = "\n".join(f"- {f['file']}" for f in batch)
        p = base + f"\n\n이번 응답의 ops 에는 **아래 파일만** 전체 내용으로 작성하세요(다른 파일은 넣지 마세요):\n{wanted}"
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
            if cleaned is None or not cleaned.strip():
                self._log(op["file"], "format", "받은 파일 내용에 응답 형식이 섞여 버리고 다시 받음")
                continue
            out.append(dict(op, file=target["file"], content=cleaned))
        return out

    def _gen_parts(self, target: dict, files_done: list[dict], agent: str, lines: int) -> dict:
        path = target["file"]
        resuming = path in self.state["partial"]
        self._event("file_split", agent=agent, file=path, lines=lines,
                    message=(f"{path} 이어 쓰기를 이어서 합니다" if resuming else f"{path} 이(가) 커서 {lines}줄씩 나눠 씁니다"))
        base = self.context_prompt + self._context(files_done) + f"\n\n지금은 {path} 파일 하나만 작성합니다."
        content = self.write_in_parts(base, f"{path} 파일", agent=agent, path=path, lines=lines)
        return {"action": "create", "file": path, "content": content, "language": "",
                "rationale": "출력 한도 때문에 나눠서 작성"}

    def _produce(self, target: dict, files_done: list[dict], agent: str) -> dict:
        """파일 하나를 지금 시도 횟수에 맞는 방법으로 만든다.
        첫 시도: 한 번에 → 넘치면 150줄 조각. 두 번째: 처음부터 80줄 조각. 세 번째: 처음부터 40줄 조각."""
        key = _norm(target["file"])
        tries = int((self.state["attempts"].get(key) or {}).get("tries", 0))
        lines = PART_LINES_STEPS[min(tries, len(PART_LINES_STEPS) - 1)]
        if tries == 0 and target["file"] not in self.state["partial"]:
            got = self._ask_files([target], files_done, agent)
            if got:
                return got[0]
        return self._gen_parts(target, files_done, agent, lines)

    def _run_single(self, target: dict, files_done: list[dict], agent: str) -> None:
        """파일 하나 — 실패하면 방법을 바꿔 다시, 정해진 횟수를 넘으면 그 파일만 실패로 두고 돌아간다."""
        key = _norm(target["file"])
        while True:
            if key in self.state["ops"]:
                return
            tries = int((self.state["attempts"].get(key) or {}).get("tries", 0))
            if tries >= MAX_FILE_TRIES:
                self._fail(target, agent)
                return
            try:
                op = self._produce(target, files_done, agent)
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
        p = self.context_prompt + self._context(files_done) + f"""

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
            files = self.state["files"]
            for layer in (0, 1, 2):
                wave = [f for f in files if f["layer"] == layer and _norm(f["file"]) not in self.state["ops"]
                        and not (self.skip_failed and _norm(f["file"]) in self.state["failed"])]
                if not wave:
                    continue
                # 기반 파일은 서로 의존하므로 순서대로(정확도), 기능·화면은 기반 코드를 보며 동시에(속도).
                workers = 1 if layer == 0 else self.concurrency
                done_before = [self.state["ops"][_norm(f["file"])] for f in files
                               if _norm(f["file"]) in self.state["ops"]]
                self._event("wave", layer=layer, agents=workers,
                            message=f"{['공통 기반', '기능', '화면·진입점'][layer]} {len(wave)}개 — 에이전트 {workers}명")
                batches = _batches(wave, BATCH)
                if workers == 1:
                    for batch in batches:
                        context = [self.state["ops"][_norm(f["file"])] for f in files if _norm(f["file"]) in self.state["ops"]]
                        self._run_batch(batch, context, "agent-1")
                else:
                    slots = [f"agent-{i + 1}" for i in range(workers)]
                    free = list(slots)
                    free_lock = threading.Lock()

                    def task(batch: list[dict]) -> None:
                        with free_lock:
                            slot = free.pop(0) if free else slots[0]
                        try:
                            self._run_batch(batch, done_before, slot)
                        finally:
                            with free_lock:
                                free.append(slot)

                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gen") as pool:
                        futures = [pool.submit(task, b) for b in batches]
                        first_error: BaseException | None = None
                        for fut in as_completed(futures):
                            exc = fut.exception()
                            if exc and first_error is None:
                                first_error = exc
                        if first_error:
                            raise first_error
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
        return {"summary": str(self.state["manifest"].get("summary") or ""), "skipped": self.skipped}, ops, self.last_resp

    def _run_batch(self, batch: list[dict], context: list[dict], agent: str) -> None:
        """작은 파일은 두 개씩 한 번에 받고, 받은 것은 하나씩 바로 저장한다. 빠진 파일·이어 쓰던 파일은 하나씩."""
        todo = [f for f in batch if _norm(f["file"]) not in self.state["ops"]]
        fresh = [f for f in todo if f["file"] not in self.state["partial"]
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
    if not written or not piece:
        return piece
    tail = written.rstrip("\n").split("\n")[-8:]
    lines = piece.split("\n")
    for n in range(min(len(tail), len(lines)), 0, -1):
        head = [line.rstrip() for line in lines[:n]]
        if head == [line.rstrip() for line in tail[-n:]] and any(h.strip() for h in head):
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


def edit_fix_round(prompt: str, ops: list[dict], issues: list[dict], *, max_files: int = 8,
                   emit: Callable[[dict], None] | None = None) -> list[dict] | None:
    """전체 점검에서 나온 문제를 **바뀔 부분만** 고친다(대규모 생성용).

    큰 결과를 통째로 다시 쓰게 하면 응답 한도에서 또 잘려 교정이 통째로 생략됐다.
    문제가 지목한 파일마다 edits(find→replace)만 받아 적용한다. 고칠 수 있는 게 없으면 None.
    """
    ca = _ca()
    by_path = {_norm(op["file"]): i for i, op in enumerate(ops)}
    grouped: dict[int, list[str]] = {}
    for issue in issues:
        if issue.get("severity") != "error":
            continue
        idx = by_path.get(_norm(issue.get("file") or ""))
        if idx is None:
            continue
        grouped.setdefault(idx, []).append(f"{issue.get('message', '')} (해결: {issue.get('fix', '')})")
    if not grouped:
        return None
    engine = LargeGeneration(prompt, emit=emit)
    listing = "\n".join(f"- {op['file']}" for op in ops)
    out = [dict(op) for op in ops]
    changed = False
    for idx, problems in list(grouped.items())[:max_files]:
        op = out[idx]
        if emit:
            emit({"step": "fixing", "agent": "review", "file": op["file"], "message": f"전체 점검 — {op['file']} 고치는 중"})
        p = _without_output_format(prompt) + f"""

[전체 점검 교정] 생성한 파일 목록:
{listing}
{op['file']} 에 아래 문제가 있습니다:
""" + "\n".join(f"- {x}" for x in problems) + f"""
**파일 전체를 다시 쓰지 말고** 바꿀 부분만 edits 로 주세요. find 는 현재 파일에 정확히 한 번 나오는 원문 그대로입니다.
현재 파일 {op['file']}:
```
{ca._prompt_body(op['content'], 60_000)}
```"""
        try:
            resp = engine.call(p, EDIT_SCHEMA, "generate_code_consistency", 4096, agent="review")
            data = ca._extract_json(resp.text)
        except (LLMError, ValueError, CodeOutputError, Paused):
            continue
        content = op["content"]
        for edit in data.get("edits") or []:
            if isinstance(edit, dict) and isinstance(edit.get("find"), str) and isinstance(edit.get("replace"), str) \
                    and edit["find"] and content.count(edit["find"]) == 1:
                content = content.replace(edit["find"], edit["replace"], 1)
        if content != op["content"]:
            out[idx] = dict(op, content=content)
            changed = True
    return out if changed else None
