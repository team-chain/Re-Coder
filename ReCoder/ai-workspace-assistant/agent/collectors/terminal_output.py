"""터미널 출력 로그 파일 감시 유틸리티."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Iterable


DEFAULT_TERMINAL_LOG_PATH = '~/.ai_assistant/terminal.log'
TERMINAL_LOG_POLL_INTERVAL = 1.0
_LOOKBACK_CHARS = 4096

ERROR_PATTERNS = [
    re.compile(r'Traceback \(most recent call last\)', re.IGNORECASE),
    re.compile(r'\b\w+Error:', re.IGNORECASE),
    re.compile(r'\b\w+Exception:', re.IGNORECASE),
    re.compile(r'\bFATAL\b', re.IGNORECASE),
    re.compile(r'\bFAILED\b', re.IGNORECASE),
    re.compile(r'HTTP/\d(?:\.\d)?\s+[45]\d{2}', re.IGNORECASE),
    re.compile(r'(?:status|code|error|response)\s*:?\s*[45]\d{2}\b', re.IGNORECASE),
    re.compile(r'(?:GET|POST|PUT|DELETE|PATCH)\s+\S+\s+[45]\d{2}\b', re.IGNORECASE),
    re.compile(r'\bUnhandledPromiseRejection\b', re.IGNORECASE),
    re.compile(r'\bERR_\w+\b', re.IGNORECASE),
    re.compile(r'에러', re.IGNORECASE),
    re.compile(r'오류', re.IGNORECASE),
    re.compile(r'실패', re.IGNORECASE),
    re.compile(r'\berror\s*:', re.IGNORECASE),
    re.compile(r'\bcompilation failed\b', re.IGNORECASE),
]

RESOLVE_PATTERNS = [
    re.compile(r'\b(?:BUILD\s+)?SUCCESS(?:FUL)?\b', re.IGNORECASE),
    re.compile(r'\bDONE\b', re.IGNORECASE),
    re.compile(r'\bCOMPLETED\b', re.IGNORECASE),
    re.compile(r'\bPASSED\b', re.IGNORECASE),
    re.compile(r'\b200\s+OK\b', re.IGNORECASE),
    re.compile(r'성공', re.IGNORECASE),
    re.compile(r'완료', re.IGNORECASE),
]


def get_terminal_log_path() -> Path:
    raw_path = os.getenv('TERMINAL_LOG_PATH', DEFAULT_TERMINAL_LOG_PATH).strip()
    return Path(raw_path or DEFAULT_TERMINAL_LOG_PATH).expanduser()


def match_patterns(texts: Iterable[str], patterns: list[re.Pattern[str]]) -> list[str]:
    """texts에서 patterns에 매칭되는 문자열을 중복 없이 반환한다."""
    matched: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = match.group()
                if value not in seen:
                    seen.add(value)
                    matched.append(value)
    return matched


def _match_patterns_since(
    text: str,
    patterns: list[re.Pattern[str]],
    new_content_start: int,
) -> list[str]:
    matched: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            if match.end() <= new_content_start:
                continue
            value = match.group()
            if value not in seen:
                seen.add(value)
                matched.append(value)
    return matched


def _read_new_output(path: Path, offset: int) -> tuple[str, int]:
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        f.seek(offset)
        text = f.read()
        return text, f.tell()


async def _invoke_callback(callback: Callable[..., object], *args) -> None:
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        print(f'[terminal_output] 콜백 실행 실패: {e}')


async def watch_terminal_output(
    on_new_output: Callable[[str], None | Awaitable[None]],
    on_error_detected: Callable[[str, list[str]], None | Awaitable[None]],
) -> None:
    """
    터미널 로그 파일을 감시하여 새 출력과 에러를 콜백으로 전달한다.

    Args:
        on_new_output: 새로운 터미널 출력이 감지되면 호출. 인자는 출력 텍스트.
        on_error_detected: 에러 패턴이 감지되면 호출.
            첫 번째 인자는 에러가 포함된 출력 블록,
            두 번째 인자는 매칭된 에러 패턴 리스트.
    """
    path = get_terminal_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    offset = 0
    lookback = ''

    while True:
        try:
            if not path.exists() or not path.is_file():
                offset = 0
                lookback = ''
                await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
                continue

            current_size = path.stat().st_size
            if current_size < offset:
                offset = 0
                lookback = ''

            if current_size > offset:
                new_text, offset = await asyncio.to_thread(_read_new_output, path, offset)
                if new_text:
                    await _invoke_callback(on_new_output, new_text)

                    block = lookback + new_text
                    matches = _match_patterns_since(block, ERROR_PATTERNS, len(lookback))
                    if matches:
                        await _invoke_callback(on_error_detected, block, matches)
                    lookback = block[-_LOOKBACK_CHARS:]

            await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f'[terminal_output] 로그 감시 실패: {e}')
            await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
