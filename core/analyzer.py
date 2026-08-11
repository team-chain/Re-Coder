"""
컨텍스트 분석 모듈 (v6.4 §8).

터미널 출력 + 파일 컨텍스트를 LLM으로 분석해 AgentEvent를 반환한다.
- PIL/RapidOCR/화면 캡처 관련 코드 제거
- Context Gate 통과 (마스킹 + quality_score)
- trigger_score 계산
- error_fingerprint 중복 체크 (60초 내 동일 → 기존 이벤트 재사용)
- LLM 호출 (router.get_router().call())
- AgentEvent 생성 반환
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Optional

from llm.base import LLMRequest
from llm.router import get_router
from schemas import AgentEvent, AnalyzeRequest, EventType, UserAction
from context_gate import run_gate
from trigger_detector import should_trigger_with_reasons, _error_fingerprint

# 에러 중복 체크용 캐시 (60초 내 동일 에러 → 기존 이벤트 재사용)
_error_cache: dict[str, tuple[float, AgentEvent]] = {}
_CACHE_TTL_SECONDS = 60


# ── 터미널 출력에서 에러 본문 추출 ────────────────────────────────────
#
# 이 함수들은 원래 구(舊) `server.py` 에만 있었다. 모듈식으로 재작성하면서
# **살아있는 경로에 안 붙어** 기능이 조용히 사라졌고, 테스트는 죽은 함수를
# 계속 검사하고 있어서 "있다"고 착각하기 쉬운 상태였다.
#
# 동시에 `AnalyzeRequest` 에서 `error_text` 필드가 제거됐는데 이 모듈은 계속
# `request.error_text` 를 읽고 있었다 — 즉 `analyze()` 는 호출되는 즉시
# AttributeError 로 죽었다(LLM 호출 try 블록 **밖**이라 폴백도 안 걸린다).
# 에러 본문을 `terminal_output` 에서 뽑도록 바꾸면 두 문제가 같이 해결된다.

_ERROR_PATTERNS = [
    # 파이썬 트레이스백: 헤더 + 들여쓴 프레임들 + **마지막 예외 줄**.
    #
    # 구 server.py 판은 `(?=\n\S|\Z)` 로 끊어서, 들여쓰기가 끝나는 순간
    # 멈췄다 — 즉 `ModuleNotFoundError: No module named 'fastapi'` 라는
    # **가장 중요한 줄을 잘라내고** 프레임 목록만 넘겼다. 기존 테스트는
    # `"Traceback" in out or "ModuleNotFoundError" in out` 이라 앞 조건으로
    # 통과해 이걸 못 잡았다.
    #
    # (한계) 연쇄 예외("During handling of the above exception ...")는 첫
    # 블록까지만 잡는다. 진단에는 대개 충분하다.
    re.compile(
        r"^(Traceback \(most recent call last\):\n"
        r"(?:[ \t]+.*\n)*"      # 들여쓴 프레임 줄들 (0개 이상)
        r"(?:\S.*)?)",          # 예외 줄 (Type: message)
        re.M,
    ),
    re.compile(r"^(\w+(?:Error|Exception):.*?)$", re.M),
    re.compile(r"^(error\s+TS\d+:.*?)$", re.M | re.I),
    re.compile(r"^(npm ERR!.*?)$", re.M),
    re.compile(r"^(yarn run.*?ERROR.*?)$", re.M | re.I),
]

#: 에러 본문 앞에 함께 보낼 줄 수. 에러만 남기면 "무슨 명령을 실행했는가",
#: "직전에 무슨 일이 있었는가" 가 사라져 진단이 어려워진다.
_CONTEXT_LINES_BEFORE = 10

#: 패턴이 하나도 안 맞을 때 남길 꼬리 줄 수.
_FALLBACK_TAIL_LINES = 30


def _match_error_block(terminal_output: str) -> Optional[str]:
    """알려진 에러 패턴에 맞는 본문을 돌려준다. 못 찾으면 None."""
    for pat in _ERROR_PATTERNS:
        m = pat.search(terminal_output)
        if m:
            return m.group(1).strip()
    return None


def _extract_error_text(terminal_output: str) -> str:
    """터미널 출력에서 에러 본문만 추출. 못 찾으면 마지막 30줄을 반환."""
    if not terminal_output:
        return ""
    body = _match_error_block(terminal_output)
    if body is not None:
        return body
    lines = terminal_output.strip().splitlines()
    return "\n".join(lines[-_FALLBACK_TAIL_LINES:])


def _trim_terminal_output(
    terminal_output: str,
    context_lines: int = _CONTEXT_LINES_BEFORE,
) -> str:
    """프롬프트에 실을 터미널 출력을 **에러 중심으로** 줄인다.

    빌드 로그는 수백 줄이 보통이고, 그중 쓸모있는 건 에러 몇 줄이다. 전부
    넣으면 (1) 토큰을 낭비하고 (2) 진짜 신호가 노이즈에 묻혀 분석 품질이
    떨어지며 (3) 아주 긴 로그는 입력 한도에 걸려 요청 자체가 실패한다.

    다만 에러 본문만 남기면 맥락이 함께 잘린다. 그래서 **에러 본문 + 그 앞
    `context_lines` 줄**을 남긴다(실행한 명령·직전 출력이 여기 들어온다).
    패턴이 하나도 안 맞으면 마지막 몇 줄로 폴백한다.
    """
    text = terminal_output or ""
    if not text.strip():
        return ""

    body = _match_error_block(text)
    if body is None:
        # 에러 형태를 못 찾음 → 꼬리만 남긴다 (앞에 맥락을 더 붙이지 않는다)
        lines = text.strip().splitlines()
        return "\n".join(lines[-_FALLBACK_TAIL_LINES:])

    body_lines = body.splitlines()
    if not body_lines:
        return body

    lines = text.splitlines()
    first = body_lines[0].strip()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == first),
        None,
    )
    if start is None:
        # 본문 첫 줄을 원문에서 못 찾음(정규식이 다듬은 경우) → 본문만
        return body

    head_from = max(0, start - context_lines)
    kept = lines[head_from:start] + body_lines
    out = "\n".join(kept)
    if head_from > 0:
        # 잘라냈다는 사실을 남긴다. 없으면 LLM 이 이게 로그 전부라고 읽는다.
        out = "… (앞부분 생략)\n" + out
    return out


_SYSTEM_INSTRUCTION = """당신은 DevOps 특화 AI 에이전트입니다.
터미널 에러 출력을 분석해 원인과 해결 방법을 제안합니다.
에러가 없으면 현재 작업 컨텍스트를 요약합니다.
마크다운 코드펜스 없이 순수 JSON만 출력하세요."""


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_error": {
            "type": "boolean",
            "description": "에러 존재 여부"
        },
        "error_type": {
            "type": "string",
            "description": "에러 타입 (ModuleNotFoundError, SyntaxError 등)"
        },
        "error_summary": {
            "type": "string",
            "description": "한국어 요약"
        },
        "suggested_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "제안 액션 목록 (fix_code, install_dependency, run_test 등)"
        },
        "importance_score": {
            "type": "integer",
            "description": "중요도 0~100"
        },
        "event_type": {
            "type": "string",
            "description": "EventType 값 (error_detected, task_change, resolved 등)"
        }
    },
    "required": ["has_error", "error_summary", "importance_score", "event_type"]
}


def _build_prompt(request: AnalyzeRequest) -> str:
    """분석 요청을 LLM 프롬프트로 변환한다.

    `error_text` · `file_context` 는 `AnalyzeRequest` 에서 없어진 필드다.
    에러 본문은 `terminal_output` 에서 뽑고, 파일 맥락은 현재 스키마에 있는
    `selected_text` · `project_files_summary` 로 대신한다.
    """
    lines = [
        "다음 컨텍스트를 분석하고 JSON으로 응답하세요.",
        "",
        "[에러 텍스트]",
        _extract_error_text(request.terminal_output or "") or "(없음)",
        "",
    ]

    if request.selected_text:
        lines.extend([
            "[선택한 코드]",
            request.selected_text,
            "",
        ])

    if request.project_files_summary:
        lines.extend([
            "[프로젝트 파일]",
            request.project_files_summary,
            "",
        ])

    # 원문을 통째로 넣지 않는다 — 에러 본문 + 앞 맥락만.
    trimmed = _trim_terminal_output(request.terminal_output or "")
    if trimmed:
        lines.extend([
            "[터미널 출력]",
            trimmed,
            "",
        ])

    lines.extend([
        "[응답 형식]",
        json.dumps(_RESPONSE_SCHEMA, ensure_ascii=False),
    ])

    return "\n".join(lines)


def _extract_json_response(raw: str) -> dict:
    """LLM 응답에서 JSON을 추출한다."""
    text = raw.strip()
    # 코드펜스 제거
    text = text.lstrip('`').rstrip('`').strip()
    if text.startswith('json'):
        text = text[4:].lstrip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 첫 번째 { ... } 블록 추출 시도
        start = text.find('{')
        if start == -1:
            raise ValueError(f"JSON을 찾을 수 없습니다: {raw[:200]}")

        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        chunk = text[start:i + 1]
                        return json.loads(chunk)
        raise ValueError(f"닫히지 않은 JSON: {raw[:200]}")


def _parse_event_type(value: str) -> EventType:
    """문자열을 EventType으로 변환."""
    try:
        return EventType(value)
    except (ValueError, AttributeError):
        return EventType.UNKNOWN


def _parse_user_actions(values: list[str] | str | None) -> list[UserAction]:
    """문자열 또는 리스트를 UserAction 리스트로 변환."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]

    actions = []
    for v in values:
        try:
            actions.append(UserAction(v))
        except (ValueError, AttributeError):
            pass
    return actions


async def analyze(request: AnalyzeRequest, session_id: str = "") -> AgentEvent:
    """
    AnalyzeRequest를 받아 AgentEvent를 생성 및 반환한다.

    프로세스:
    1. Context Gate 통과 (마스킹 + quality_score)
    2. error_fingerprint 중복 체크 (60초 내 동일 → 기존 이벤트 재사용)
    3. trigger_score 계산
    4. LLM 호출 (router.get_router().call())
    5. AgentEvent 생성 반환

    인자:
        request: AnalyzeRequest 객체
        session_id: 세션 ID (로깅용)

    반환:
        AgentEvent 객체
    """
    print(f"[analyzer] 분석 시작 | 세션: {session_id}")

    # 1. Context Gate 통과 (마스킹 + quality_score)
    #    `run_gate` 는 **async 이고 문자열을 받아 GateResult 를 돌려준다.**
    #    예전 코드는 request 객체를 넘기고 await 도 안 해서 코루틴이 반환됐고,
    #    바로 아래 `.get(...)` 이 터지면서 except 로 빠졌다. 그 결과
    #    **마스킹이 통째로 스킵**돼 시크릿·PII 가 그대로 LLM 으로 갔다.
    #    로그에만 남고 결과에는 흔적이 없어서 알아채기 어려웠다.
    quality_score = 0.3
    try:
        gate = await run_gate(request.terminal_output or "")
        quality_score = gate.quality_score
        # 마스킹된 본문을 이후 단계가 쓰도록 요청을 갈아끼운다.
        request = request.model_copy(update={"terminal_output": gate.text})
        print(f"[analyzer] Context Gate 완료 | quality_score={quality_score:.2f}")
    except Exception as e:
        # 게이트가 죽어도 분석 자체는 계속한다. 다만 **마스킹이 안 된 상태**
        # 이므로 그 사실을 로그에 분명히 남긴다.
        print(f"[analyzer] Context Gate 에러(마스킹 미적용): {e}")

    # 2. error_fingerprint 중복 체크
    #    (예전엔 request.error_text 를 읽었는데 그 필드는 스키마에서 사라졌다
    #     — 여기서 AttributeError 로 죽고 있었다. 터미널 출력에서 뽑는다.)
    error_text = _extract_error_text(request.terminal_output or "")
    fingerprint = _error_fingerprint([error_text])
    now = time.time()

    if fingerprint in _error_cache:
        cached_time, cached_event = _error_cache[fingerprint]
        if now - cached_time < _CACHE_TTL_SECONDS:
            print(f"[analyzer] 캐시 히트: {fingerprint} (경과: {now - cached_time:.1f}초)")
            return cached_event

    # 캐시 정리: 만료된 항목 제거
    expired = [k for k, (t, _) in _error_cache.items() if now - t >= _CACHE_TTL_SECONDS]
    for k in expired:
        del _error_cache[k]

    # 3. trigger_score 계산
    #    시그니처는 (errors, new_commands, text_changed, window_switched,
    #    uia_failure) 5개이고 (trigger, score, need_capture, reasons) 4개를
    #    돌려준다. 예전 코드는 인자 2개만 넘기고 2개만 받아 **TypeError 로
    #    죽었다** — 이 지점은 try 로 감싸여 있지도 않아 요청 전체가 실패했다.
    should_trigger, raw_score, _need_capture, reasons = should_trigger_with_reasons(
        [error_text] if error_text else [],
        [request.command] if request.command else [],
        bool(request.selected_text),   # text_changed
        False,                          # window_switched (이 경로엔 창 전환 신호 없음)
        False,                          # uia_failure (화면 접근 실패 — 해당 없음)
    )
    trigger_score = int(raw_score)

    # 4. LLM 호출
    prompt = _build_prompt(request)
    try:
        llm_resp = get_router().call(
            LLMRequest(
                prompt=prompt,
                system=_SYSTEM_INSTRUCTION,
                json_schema=_RESPONSE_SCHEMA,
                max_tokens=1024,
                temperature=0.3,
            ),
            agent="analyzer",
            operation="analyze_context",
        )
        raw_response = llm_resp.text.strip()
        print(f"[analyzer] LLM 응답 길이: {len(raw_response)}자 (model={llm_resp.model_used})")

        response_data = _extract_json_response(raw_response)
    except Exception as e:
        print(f"[analyzer] LLM 호출 실패: {e}")
        # 에러 시 기본 응답 생성
        response_data = {
            "has_error": bool(error_text),
            "error_type": "unknown",
            "error_summary": error_text[:100] if error_text else "분석 불가능",
            "suggested_actions": [],
            "importance_score": 20,
            "event_type": "error_detected" if error_text else "unknown",
        }

    # 5. AgentEvent 생성
    has_error = response_data.get("has_error", False)
    error_type = response_data.get("error_type", "")
    error_summary = response_data.get("error_summary", "")
    suggested_actions = response_data.get("suggested_actions", [])
    importance_score = int(response_data.get("importance_score", 50))
    event_type_str = response_data.get("event_type", "unknown")

    # AgentEvent 는 dataclass 이고 필드가 아래가 전부다. 예전 코드는
    # timestamp·session_id·has_error·source·quality_score·metadata 처럼
    # **존재하지 않는 인자**를 넘겨 TypeError 로 죽었다. 스키마에 맞춰 담되,
    # 없어진 항목 중 살릴 가치가 있는 것(에러 타입·모델명)은 summary 와
    # contexts 로 옮긴다.
    summary = error_summary or ("에러 감지" if has_error else "컨텍스트 요약")
    if has_error and error_type:
        summary = f"[{error_type}] {summary}"

    contexts: list[str] = []
    trimmed = _trim_terminal_output(request.terminal_output or "")
    if trimmed:
        contexts.append(trimmed)
    if request.selected_text:
        contexts.append(request.selected_text)

    event = AgentEvent(
        event_id=f"{session_id}_{int(now)}",
        event_type=_parse_event_type(event_type_str),
        summary=summary,
        contexts=contexts,
        importance_score=min(100, max(0, importance_score)),
        suggested_actions=_parse_user_actions(suggested_actions),
        created_at=datetime.now().isoformat(),
        raw_errors=[error_text] if error_text else [],
        error_text=error_text,
        trigger_score=trigger_score,
        trigger_reasons=reasons,
    )

    print(f"[analyzer] 분석 완료 | event_type={event.event_type.value} | "
          f"has_error={has_error} | importance={importance_score}")

    # 캐시 저장
    _error_cache[fingerprint] = (now, event)

    return event
