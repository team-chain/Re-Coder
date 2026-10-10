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


# ── 엔진 ───────────────────────────────────────────────────────────────

class LargeGeneration:
    def __init__(self, prompt: str, *, target_folder: str = "", new_project: bool = False,
                 job_id: str = "", fingerprint: str = "", concurrency: int | None = None,
                 emit: Callable[[dict], None] | None = None, limiter: RateLimiter | None = None,
                 security_review: bool = True) -> None:
        self.prompt = prompt
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
                                      "ops": {}, "partial": {}}
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

    def resume_from(self, saved: dict | None) -> bool:
        if not saved or not saved.get("manifest") or not saved.get("files"):
            return False
        self.state = {"fingerprint": self.fp, "manifest": saved["manifest"], "files": saved["files"],
                      "ops": dict(saved.get("ops") or {}), "partial": dict(saved.get("partial") or {})}
        self._event("resumed", message=f"멈춘 지점부터 이어서 만듭니다 — {len(self.state['ops'])}/{len(self.state['files'])}개 완료")
        return True

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
        header = self.prompt + f"""

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
            resp = self.call(self.prompt + "\n\n이 요청이 무엇을 만드는지 한국어 한 줄로만 JSON {\"summary\": \"...\"} 으로 답하세요.",
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
            page_prompt = self.prompt + f"""

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
                       path: str = "") -> str:
        partial = self.state["partial"].get(path) if path else None
        written = (partial or {}).get("written", "")
        part = int((partial or {}).get("parts", 0))
        stalls = 0
        while part < PART_SAFETY:
            p = base_prompt + f"""

{label} 은(는) 한 응답에 다 들어가지 않아 여러 번에 나눠 이어 씁니다. 지금은 {part + 1}번째 조각입니다.
이번 응답의 content 에는 {'처음' if not written else '바로 다음'} 부분을 **최대 {PART_LINES}줄**만 쓰세요.
- 줄 중간에서 끊지 말고 함수·블록·문단 경계에서 끊으세요. 다음 응답에서 정확히 이어 씁니다.
- 이미 쓴 줄을 다시 쓰지 말고, 설명이나 마크다운 코드펜스 없이 내용만 쓰세요.
- 이번 조각으로 끝나면 done 을 true, 아직 남았으면 false 로 주세요."""
            if written:
                p += f"\n\n지금까지 쓴 {label} (이 바로 뒤부터 이어서 쓰세요):\n```\n{_part_context(written)}\n```"
            resp = self.call(p, PART_SCHEMA, operation, 4096, agent=agent)
            try:
                data = _ca()._extract_json(resp.text)
            except ValueError:
                stalls += 1
                if stalls >= 3:
                    raise CodeOutputError(f"{label} 을(를) 이어 쓰는 응답이 계속 형식에 맞지 않습니다.")
                continue
            piece, done = data.get("content"), data.get("done")
            if not isinstance(piece, str):
                piece = ""
            if piece.lstrip().startswith("```"):
                piece = re.sub(r"^\s*```[\w.+-]*\s*\n?", "", piece)
                piece = re.sub(r"\n?```\s*$", "", piece)
            piece = _strip_overlap(written, piece)
            if written and piece and not written.endswith("\n"):
                written += "\n"
            written += piece
            part += 1
            if path:
                self.state["partial"][path] = {"written": written, "parts": part}
                self._save()
                self._event("file_part", agent=agent, file=path, part=part, lines=written.count("\n") + 1,
                            message=f"{path} 이어 쓰는 중 · {part}번째 조각 · {written.count(chr(10)) + 1}줄")
            if done is True:
                break
            stalls = stalls + 1 if not piece.strip() else 0
            if stalls >= 3:
                raise CodeOutputError(f"{label} 을(를) 이어 쓰는 중 더 진행되지 않았습니다.")
        else:
            raise CodeOutputError(f"{label} 이(가) 비정상적으로 길어 멈췄습니다({PART_SAFETY}조각).")
        if not written.strip():
            #: 내용 없이 "끝"이라고 하면 빈 파일이 된다 — 조용히 넘기지 않는다(이어서 만들기로 다시 시도).
            raise CodeOutputError(f"{label} 의 내용을 받지 못했습니다.")
        if path:
            self.state["partial"].pop(path, None)
        return written if written.endswith("\n") else written + "\n"

    # ── 2. 파일 생성 ──
    def _gen_batch(self, batch: list[dict], files_done: list[dict], agent: str) -> list[dict]:
        ca = _ca()
        base = self.prompt + self._context(files_done)
        if len(batch) == 1 and batch[0]["file"] in self.state["partial"]:
            return [self._gen_parts(batch[0], base, agent)]
        wanted = "\n".join(f"- {f['file']}" for f in batch)
        p = base + f"\n\n이번 응답의 ops 에는 **아래 파일만** 전체 내용으로 작성하세요(다른 파일은 넣지 마세요):\n{wanted}"
        for f in batch:
            self._event("file_start", agent=agent, file=f["file"], message=f"{f['file']} 작성 중")
        try:
            resp = self.call(p, CODE_OUTPUT_SCHEMA, "generate_code_part", ca._CODE_AGENT_MAX_TOKENS, agent=agent)
            _data, ops = parse_code_output(resp.text)
            ops = ca._relative_to_target(ops, self.target_folder)
            keys = {_norm(f["file"]): f for f in batch}
            ops = [op for op in ops if _norm(op["file"]) in keys]
            got = {_norm(op["file"]) for op in ops}
            for f in batch:
                if _norm(f["file"]) not in got:
                    ops.extend(self._gen_batch([f], files_done, agent))
            return ops
        except (LLMError, CodeOutputError) as exc:
            if not (_is_truncation(exc) or isinstance(exc, CodeOutputError)):
                raise
            if len(batch) > 1:
                out: list[dict] = []
                for f in batch:
                    out.extend(self._gen_batch([f], files_done, agent))
                return out
            return [self._gen_parts(batch[0], base, agent)]

    def _gen_parts(self, target: dict, base: str, agent: str) -> dict:
        path = target["file"]
        self._event("file_split", agent=agent, file=path, message=f"{path} 이(가) 커서 나눠 이어 씁니다")
        content = self.write_in_parts(base + f"\n\n지금은 {path} 파일 하나만 작성합니다.", f"{path} 파일",
                                      agent=agent, path=path)
        return {"action": "create", "file": path, "content": content, "language": "",
                "rationale": "출력 한도 때문에 나눠서 작성"}

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
        p = self.prompt + self._context(files_done) + f"""

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
                wave = [f for f in files if f["layer"] == layer and _norm(f["file"]) not in self.state["ops"]]
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
            raise jobs.GenerationPaused(
                f"{exc} — 지금까지 만든 {len(self.state['ops'])}/{len(self.state['files'])}개는 저장했습니다. "
                "[이어서 만들기]로 멈춘 지점부터 계속할 수 있습니다.",
                self.job_id, len(self.state["ops"]), len(self.state["files"]), reason=str(exc)) from exc
        except (LLMError, CodeOutputError, RuntimeError, OSError) as exc:
            self._save()
            if self.state.get("files"):
                raise jobs.GenerationPaused(
                    f"생성이 중간에 멈췄습니다({exc}). 지금까지 만든 {len(self.state['ops'])}/{len(self.state['files'])}개는 "
                    "저장했습니다. [이어서 만들기]로 멈춘 지점부터 계속할 수 있습니다.",
                    self.job_id, len(self.state["ops"]), len(self.state["files"]), reason=str(exc)) from exc
            raise
        files = self.state["files"]
        missing = [f["file"] for f in files if _norm(f["file"]) not in self.state["ops"]]
        if missing:
            raise CodeOutputError(f"일부 파일을 만들지 못했습니다: {', '.join(missing[:5])}")
        ops = [self.state["ops"][_norm(f["file"])] for f in files]
        self._event("generated", message=f"파일 {len(ops)}개 작성 완료 — 전체 점검 중")
        return {"summary": str(self.state["manifest"].get("summary") or "")}, ops, self.last_resp

    def _run_batch(self, batch: list[dict], context: list[dict], agent: str) -> None:
        ops = self._gen_batch(batch, context, agent)
        for op in ops:
            op = self._verify_and_fix(op, context, agent)
            with self._lock:
                self.state["ops"][_norm(op["file"])] = op
            self._save()
            self._event("file_done", agent=agent, file=op["file"], lines=op["content"].count("\n") + 1,
                        message=f"{op['file']} 완료")


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
        p = prompt + f"""

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
