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
    # ── Python ───────────────────────────────────────────────────────
    re.compile(r'Traceback \(most recent call last\)', re.IGNORECASE),
    re.compile(r'\b\w+Error:', re.IGNORECASE),
    re.compile(r'\b\w+Exception:', re.IGNORECASE),
    re.compile(r'No module named', re.IGNORECASE),
    re.compile(r'IndentationError', re.IGNORECASE),
    re.compile(r'DeprecationWarning.*error', re.IGNORECASE),
    # ── JavaScript / Node.js / TypeScript ────────────────────────────
    re.compile(r'\bReferenceError\b', re.IGNORECASE),
    re.compile(r'\bUnhandledPromiseRejection\b', re.IGNORECASE),
    re.compile(r'\bERR_\w+\b'),                           # Node ERR_ 코드
    re.compile(r'npm ERR!'),
    re.compile(r'yarn error', re.IGNORECASE),
    re.compile(r'error TS\d+:', re.IGNORECASE),           # TypeScript
    re.compile(r'Module not found: Error', re.IGNORECASE),
    re.compile(r'Cannot find module', re.IGNORECASE),
    re.compile(r'\bcompilation failed\b', re.IGNORECASE),
    re.compile(r'SyntaxError: Unexpected', re.IGNORECASE),
    # ── HTTP 4xx / 5xx ───────────────────────────────────────────────
    re.compile(r'HTTP/\d(?:\.\d)?\s+[45]\d{2}', re.IGNORECASE),
    re.compile(r'(?:status|code|error|response)\s*:?\s*[45]\d{2}\b', re.IGNORECASE),
    re.compile(r'(?:GET|POST|PUT|DELETE|PATCH)\s+\S+\s+[45]\d{2}\b', re.IGNORECASE),
    # ── Java / Kotlin / Gradle ────────────────────────────────────────
    re.compile(r'\bNullPointerException\b', re.IGNORECASE),
    re.compile(r'\bClassNotFoundException\b', re.IGNORECASE),
    re.compile(r'\bStackOverflowError\b', re.IGNORECASE),
    re.compile(r'BUILD FAILED', re.IGNORECASE),
    re.compile(r'Caused by:', re.IGNORECASE),
    re.compile(r'COMPILATION ERROR', re.IGNORECASE),
    # ── Rust ──────────────────────────────────────────────────────────
    re.compile(r'error\[E\d+\]'),
    re.compile(r'^error:', re.MULTILINE),
    # ── Go ────────────────────────────────────────────────────────────
    re.compile(r'panic:', re.IGNORECASE),
    re.compile(r'undefined:', re.IGNORECASE),
    re.compile(r'goroutine \d+ \[running\]', re.IGNORECASE),
    # ── Docker / Kubernetes ───────────────────────────────────────────
    re.compile(r'Error response from daemon', re.IGNORECASE),
    re.compile(r'\bImagePullBackOff\b', re.IGNORECASE),
    re.compile(r'\bCrashLoopBackOff\b', re.IGNORECASE),
    re.compile(r'ErrImagePull', re.IGNORECASE),
    re.compile(r'OOMKilled', re.IGNORECASE),
    # ── Database ──────────────────────────────────────────────────────
    re.compile(r'OperationalError', re.IGNORECASE),
    re.compile(r'IntegrityError', re.IGNORECASE),
    re.compile(r'FATAL:\s+role', re.IGNORECASE),
    re.compile(r'Connection refused', re.IGNORECASE),
    re.compile(r'SQLSTATE\[\w+\]', re.IGNORECASE),
    # ── 공통 / 한국어 ────────────────────────────────────────────────
    re.compile(r'\bFATAL\b', re.IGNORECASE),
    re.compile(r'\bFAILED\b', re.IGNORECASE),
    re.compile(r'\berror\s*:', re.IGNORECASE),
    re.compile(r'command not found', re.IGNORECASE),
    re.compile(r'Permission denied', re.IGNORECASE),
    re.compile(r'No such file or directory', re.IGNORECASE),
    re.compile(r'에러'),
    re.compile(r'오류'),
    re.compile(r'실패'),
    re.compile(r'Segmentation fault', re.IGNORECASE),
]

RESOLVE_PATTERNS = [
    # 빌드 성공
    re.compile(r'\b(?:BUILD\s+)?SUCCESS(?:FUL)?\b', re.IGNORECASE),
    re.compile(r'\bDONE\b', re.IGNORECASE),
    re.compile(r'\bCOMPLETED\b', re.IGNORECASE),
    re.compile(r'\bPASSED\b', re.IGNORECASE),
    # HTTP 성공
    re.compile(r'\b200\s+OK\b', re.IGNORECASE),
    re.compile(r'\b2\d{2}\s+\w+\b', re.IGNORECASE),      # 201, 204 등
    # 테스트 성공
    re.compile(r'\d+\s+passed', re.IGNORECASE),            # pytest: 5 passed
    re.compile(r'All tests passed', re.IGNORECASE),
    re.compile(r'Tests:\s+\d+\s+passed', re.IGNORECASE),  # Jest
    re.compile(r'ok\s+\d+\s+\S+', re.IGNORECASE),         # Go test ok
    # 배포/서버 시작 성공
    re.compile(r'Server\s+(?:is\s+)?(?:running|started|listening)', re.IGNORECASE),
    re.compile(r'Application\s+started', re.IGNORECASE),
    re.compile(r'Listening on', re.IGNORECASE),
    re.compile(r'Ready in', re.IGNORECASE),                # Next.js, Vite
    re.compile(r'compiled\s+successfully', re.IGNORECASE),
    # 한국어
    re.compile(r'성공'),
    re.compile(r'완료'),
    re.compile(r'정상'),
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
    on_resolved: Callable[[str, list[str]], None | Awaitable[None]] | None = None,
) -> None:
    """
    터미널 로그 파일을 감시하여 새 출력, 에러, 해결을 콜백으로 전달한다.

    Args:
        on_new_output:    새로운 터미널 출력이 감지되면 호출. 인자는 출력 텍스트.
        on_error_detected: 에러 패턴이 감지되면 호출.
            첫 번째 인자는 에러가 포함된 출력 블록,
            두 번째 인자는 매칭된 에러 패턴 리스트.
        on_resolved:      에러 해결 패턴이 감지되면 호출 (선택적).
            첫 번째 인자는 출력 블록,
            두 번째 인자는 매칭된 해결 패턴 리스트.
    """
    path = get_terminal_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 시작 시 파일 끝 위치로 건너뜀 — 과거 로그의 에러를 재감지하지 않음
    try:
        offset = path.stat().st_size if path.exists() else 0
    except Exception:
        offset = 0
    lookback = ''
    # 이전 에러 상태 추적 (연속 에러 → 해결 전환 감지)
    _pending_errors: list[str] = []

    while True:
        try:
            if not path.exists() or not path.is_file():
                offset = 0
                lookback = ''
                await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
                continue

            current_size = path.stat().st_size
            if current_size < offset:
                # 로그 파일이 교체/초기화된 경우 처음부터 다시 읽음
                offset = 0
                lookback = ''

            if current_size > offset:
                new_text, offset = await asyncio.to_thread(_read_new_output, path, offset)
                if new_text:
                    await _invoke_callback(on_new_output, new_text)

                    block = lookback + new_text
                    new_start = len(lookback)

                    # 에러 감지
                    error_matches = _match_patterns_since(block, ERROR_PATTERNS, new_start)
                    if error_matches:
                        _pending_errors = error_matches
                        await _invoke_callback(on_error_detected, block, error_matches)

                    # 해결 감지 — 이전에 에러가 있었던 경우에만 처리
                    elif _pending_errors and on_resolved is not None:
                        resolve_matches = _match_patterns_since(block, RESOLVE_PATTERNS, new_start)
                        if resolve_matches:
                            await _invoke_callback(on_resolved, block, _pending_errors)
                            _pending_errors = []

                    lookback = block[-_LOOKBACK_CHARS:]

            await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f'[terminal_output] 로그 감시 실패: {e}')
            await asyncio.sleep(TERMINAL_LOG_POLL_INTERVAL)
