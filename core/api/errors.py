"""5xx 응답에서 내부 예외 문장을 숨기는 공통 도우미.

내부 예외 메시지에는 경로·자격증명·AWS 계정 정보 등이 섞일 수 있다. 응답에는 짧은 한국어 안내와
추적용 오류 ID 만 담고, 예외와 스택 트레이스는 코어 로그에만 남긴다(main.py 의 전역 처리기와 같은 방식).
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import HTTPException

_LOG = logging.getLogger("recoder.core")

DEFAULT_MESSAGE = "코어에서 처리되지 않은 내부 오류가 발생했습니다."


def new_error_id() -> str:
    return secrets.token_hex(8)


def public_detail(message: str, error_id: str) -> str:
    """사용자에게 보이는 문장 — 원인 문장 대신 오류 ID 로 로그를 찾게 한다."""
    message = (message or DEFAULT_MESSAGE).rstrip()
    return f"{message} 오류 ID: {error_id} — 코어 로그를 확인한 뒤 다시 시도해 주십시오."


def internal_error(
    exc: BaseException,
    *,
    status_code: int = 500,
    message: str = DEFAULT_MESSAGE,
    logger: Optional[logging.Logger] = None,
) -> HTTPException:
    """예외를 로그에 남기고, 원문 없이 오류 ID 만 담은 HTTPException 을 돌려준다(호출자가 raise)."""
    error_id = new_error_id()
    (logger or _LOG).error("내부 오류 [%s] (%s): %s", error_id, status_code, message,
                           exc_info=(type(exc), exc, exc.__traceback__))
    return HTTPException(status_code=status_code, detail=public_detail(message, error_id))
