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
import time
from datetime import datetime, timedelta

from llm.base import LLMRequest
from llm.router import get_router
from schemas import (
    AgentEvent, AnalyzeRequest, EventType, UserAction,
)
from context_gate import run_gate
from trigger_detector import should_trigger_with_reasons, _error_fingerprint

# 에러 중복 체크용 캐시 (60초 내 동일 에러 → 기존 이벤트 재사용)
_error_cache: dict[str, tuple[float, AgentEvent]] = {}
_CACHE_TTL_SECONDS = 60


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
    """분석 요청을 LLM 프롬프트로 변환한다."""
    lines = [
        "다음 컨텍스트를 분석하고 JSON으로 응답하세요.",
        "",
        "[에러 텍스트]",
        request.error_text or "(없음)",
        "",
    ]

    if request.file_context:
        lines.extend([
            "[파일 컨텍스트]",
            request.file_context,
            "",
        ])

    if request.terminal_output:
        lines.extend([
            "[터미널 출력]",
            request.terminal_output,
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

    # 2. error_fingerprint 중복 체크 (gate 전에 error_text 확보)
    error_text = request.error_text or ""
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

    # 1. Context Gate 통과 — run_gate(text: str) 는 async 함수
    quality_score = 0.5
    try:
        gate_result = await run_gate(error_text or (request.terminal_output or ""))
        # GateResult 객체 또는 dict 형태 모두 처리
        if hasattr(gate_result, "quality_score"):
            quality_score = gate_result.quality_score
        elif isinstance(gate_result, dict):
            quality_score = gate_result.get("quality_score", 0.5)
        print(f"[analyzer] Context Gate 완료 | quality_score={quality_score:.2f}")
    except Exception as e:
        print(f"[analyzer] Context Gate 에러 (무시하고 계속): {e}")

    # 3. trigger_score 계산 — should_trigger_with_reasons(errors, new_commands, text_changed, window_switched, uia_failure)
    try:
        trig_result = should_trigger_with_reasons(
            [error_text] if error_text else [],  # errors
            [],                                  # new_commands
            bool(error_text),                    # text_changed
            False,                               # window_switched
            False,                               # uia_failure
        )
        # 반환값: (trigger, score, need_capture, reasons)
        should_trigger_flag = trig_result[0]
        reasons = trig_result[3] if len(trig_result) > 3 else []
    except Exception as e:
        print(f"[analyzer] trigger_score 계산 에러 (무시하고 계속): {e}")
        should_trigger_flag = bool(error_text)
        reasons = []
    trigger_score = 80 if should_trigger_flag else 30

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

    # 5. AgentEvent 생성 (schemas.AgentEvent 필드와 정확히 일치)
    error_summary = response_data.get("error_summary", "")
    suggested_actions = response_data.get("suggested_actions", [])
    importance_score = int(response_data.get("importance_score", 50))
    event_type_str = response_data.get("event_type", "unknown")
    llm_model = llm_resp.model_used if 'llm_resp' in dir() or 'llm_resp' in locals() else "unknown"

    event = AgentEvent(
        event_id=f"{session_id}_{int(now)}",
        event_type=_parse_event_type(event_type_str),
        summary=error_summary,
        contexts=[],
        importance_score=min(100, max(0, importance_score)),
        suggested_actions=_parse_user_actions(suggested_actions),
        created_at=datetime.now().isoformat(),
        raw_errors=[error_text] if error_text else [],
        error_text=error_text,
        trigger_score=trigger_score,
        trigger_reasons=reasons,
    )

    print(f"[analyzer] 분석 완료 | event_type={event.event_type.value} | "
          f"importance={importance_score} | model={llm_model}")

    # 캐시 저장
    _error_cache[fingerprint] = (now, event)

    return event
