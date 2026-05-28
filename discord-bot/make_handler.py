"""
discord-bot/make_handler.py — 지정 채널 자연어 메시지 → Bedrock → VSCode 브리지

[처리 흐름]
  1. on_message → 지정 채널인지 확인
  2. 메시지에서 파일명 추출 — 명시되지 않았으면 자연어 의도 추론으로 자동 결정
     예) "테트리스 만들어줘"     → tetris.html
         "스네이크 게임"         → snake.html
         "Flask 서버 app.py"     → app.py  (명시됨)
         "할 일 목록 웹 앱"      → todo.html
         "정렬 알고리즘 파이썬"  → sort.py
  3. 파일 타입에 맞는 시스템 프롬프트 구성
  4. AWS Bedrock converse_stream으로 전체 텍스트 수집 (스트리밍 없음)
  5. 완성된 코드 전체를 BridgeHub.broadcast({type:"content", ...}) 로 한 번에 전송

환경변수:
  BEDROCK_PRIMARY_MODEL_IDENTIFIER  — 사용할 Bedrock 모델 ID
  BEDROCK_REGION                    — Bedrock 리전 (기본: us-east-1)
  RECODER_MAKE_MAX_TOKENS           — 최대 생성 토큰 (기본: 32768)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

import boto3
from botocore.config import Config as BotoConfig
import discord

from recoder_bridge import hub
from bridge_settings import get_make_channel_id

log = logging.getLogger(__name__)

# ── 환경변수 ────────────────────────────────────────────────────────────────

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_PRIMARY_MODEL_IDENTIFIER",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
MAX_TOKENS = int(os.getenv("RECODER_MAKE_MAX_TOKENS", "32768"))

# ── 상수 ────────────────────────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    "py":   "Python",
    "js":   "JavaScript",
    "ts":   "TypeScript",
    "tsx":  "TypeScript + React",
    "jsx":  "JavaScript + React",
    "html": "HTML (CSS와 JavaScript 모두 인라인 포함, 단일 파일)",
    "css":  "CSS",
    "go":   "Go",
    "rs":   "Rust",
    "java": "Java",
    "cpp":  "C++",
    "c":    "C",
    "rb":   "Ruby",
    "php":  "PHP",
    "swift":"Swift",
    "kt":   "Kotlin",
    "sh":   "Bash Shell Script",
    "sql":  "SQL",
    "md":   "Markdown",
    "json": "JSON",
    "yaml": "YAML",
    "yml":  "YAML",
    "toml": "TOML",
    "env":  "환경변수 파일 (.env 형식)",
}

# 명시된 파일명 감지 (예: tetris.html, app.py, main.go)
_EXPLICIT_FILENAME_RE = re.compile(r"\b([\w][\w\-]*\.[a-zA-Z0-9]{1,8})\b")

# "실행" 의도 — 사용자가 결과물을 바로 실행/열어보고 싶다는 신호
_RUN_KEYWORDS = (
    "실행", "돌려", "열어", "열어줘", "열어봐", "보여줘", "켜줘",
    "run", "open", "execute", "launch", "start it", "play",
)


def _infer_should_auto_run(text: str) -> bool:
    """
    사용자 메시지에 '실행해줘' / 'run' / '열어줘' 같은 키워드가 있으면 True.

    이 플래그는 end 이벤트와 함께 BridgeClient 로 전달되어, 확장이
    파일 종류에 맞는 방법으로 자동 실행한다:
      - .html → 외부 기본 브라우저로 open
      - .py   → 통합 터미널에서 `python file.py`
      - .sh   → 통합 터미널에서 `bash file.sh`
      - 그 외 → 에디터만 열고 알림
    """
    lower = text.lower()
    return any(kw in lower for kw in _RUN_KEYWORDS)

# ── 게임 키워드 → (파일명, 영문 게임명) ─────────────────────────────────────

_GAME_KEYWORDS: list[tuple[list[str], str, str]] = [
    # (한/영 키워드 목록, 파일명, 영문 설명)
    (["테트리스", "tetris"],                      "tetris.html",    "Tetris"),
    (["스네이크", "snake", "뱀"],                 "snake.html",     "Snake"),
    (["2048"],                                    "2048.html",      "2048"),
    (["지뢰찾기", "minesweeper", "지뢰"],         "minesweeper.html","Minesweeper"),
    (["벽돌깨기", "breakout", "arkanoid", "벽돌"],"breakout.html",  "Breakout"),
    (["팩맨", "pacman", "pac-man"],               "pacman.html",    "Pac-Man"),
    (["체스", "chess"],                           "chess.html",     "Chess"),
    (["퐁", "pong"],                              "pong.html",      "Pong"),
    (["플래피", "flappy", "새"],                  "flappy.html",    "Flappy Bird"),
    (["소코반", "sokoban"],                       "sokoban.html",   "Sokoban"),
    (["퀴즈", "quiz"],                            "quiz.html",      "Quiz"),
    (["슈팅", "shooting", "스페이스 인베이더",
      "spaceinvader", "space invader"],           "shooter.html",   "Space Shooter"),
    (["계산기", "calculator"],                    "calculator.html","Calculator"),
    (["시계", "clock", "타이머", "timer",
      "스톱워치", "stopwatch"],                   "clock.html",     "Clock/Timer"),
    # "메모리 카드"를 "메모장/노트(todo)"보다 먼저 매칭 — 부분 문자열 충돌 방지
    (["카드", "card", "메모리 카드", "memory card",
      "짝맞추기", "matching"],                   "memory.html",    "Memory Card Game"),
    (["todo", "할일", "할 일", "노트", "메모장"], "todo.html",      "Todo App"),
    (["달력", "캘린더", "calendar"],              "calendar.html",  "Calendar"),
    (["날씨", "weather"],                         "weather.html",   "Weather App"),
    (["채팅", "chat"],                            "chat.html",      "Chat UI"),
    (["사이먼", "simon says", "simon"],           "simon.html",     "Simon Says"),
    (["다이노", "dino", "공룡", "달리기"],        "dino.html",      "Dino Run"),
]

# 일반 카테고리 키워드 (게임 아닌 경우)
_CATEGORY_RULES: list[tuple[list[str], str, str]] = [
    # (키워드 목록, 기본 파일명, 카테고리명)
    (["flask", "django", "fastapi", "api", "서버", "server",
      "백엔드", "backend", "rest"],               "app.py",     "Python 백엔드"),
    (["react", "리액트", "컴포넌트", "component"],
                                                  "App.tsx",    "React 컴포넌트"),
    (["vue", "뷰"],                               "App.vue",    "Vue 컴포넌트"),
    (["크롤", "crawler", "scraper", "스크래핑",
      "크롤링"],                                  "crawler.py", "Python 웹 크롤러"),
    (["bash", "shell", "쉘"],                     "run.sh",     "Bash 스크립트"),
    (["자동화", "automation", "스크립트", "script",
      "batch"],                                   "script.py",  "Python 스크립트"),
    (["sql", "쿼리", "query", "데이터베이스"],    "query.sql",  "SQL 쿼리"),
    (["dockerfile", "docker"],                    "Dockerfile", "Dockerfile"),
    (["web", "웹", "홈페이지", "사이트", "랜딩",
      "landing page", "포트폴리오", "portfolio"],
                                                  "index.html", "웹 페이지"),
    (["게임", "game"],                            "game.html",  "HTML 게임"),
]


# ── Bedrock 클라이언트 (싱글턴) ──────────────────────────────────────────────

_bedrock_client: Optional[Any] = None


def _get_bedrock_client() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=BotoConfig(
                read_timeout=600,
                connect_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _bedrock_client


# ── 파일명/언어 추론 ─────────────────────────────────────────────────────────

def _infer_file_info(text: str) -> tuple[str, str]:
    """
    자연어 메시지에서 (파일명, 언어 설명) 을 추론한다.

    우선순위:
    1. 메시지에 명시적 파일명(확장자 포함)이 있으면 그것을 사용
    2. 게임 키워드 매칭
    3. 카테고리 키워드 매칭
    4. 최후 fallback → code.html
    """
    lower = text.lower()

    # 1. 명시적 파일명
    m = _EXPLICIT_FILENAME_RE.search(text)
    if m:
        filename = m.group(1)
        ext = filename.rsplit(".", 1)[-1].lower()
        lang = _EXT_TO_LANG.get(ext, ext.upper())
        return filename, lang

    # 2. 게임 키워드 우선 검색
    for keywords, filename, game_name in _GAME_KEYWORDS:
        if any(kw in lower for kw in keywords):
            lang = _EXT_TO_LANG.get("html", "HTML")
            return filename, lang

    # 3. 일반 카테고리
    for keywords, filename, _ in _CATEGORY_RULES:
        if any(kw in lower for kw in keywords):
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "sh"
            lang = _EXT_TO_LANG.get(ext, ext.upper())
            return filename, lang

    # 4. Fallback
    return "code.html", _EXT_TO_LANG["html"]


# ── 시스템 프롬프트 ───────────────────────────────────────────────────────────

# 모든 파일 타입에 공통으로 적용되는 품질/출력 규칙
_BASE_RULES = """\

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
출력 규칙 (반드시 준수)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 코드만 출력한다. 설명문, 인사말, 꼬리말 일절 금지.
2. 마크다운 코드 펜스(``` 또는 ~~~)를 절대 사용하지 않는다.
3. 주석은 해당 언어의 주석 문법으로 코드 내부에만 작성한다.
4. 단일 파일로 완전히 동작해야 한다. 외부 파일 의존 없음.
5. 코드를 출력하기 전, 머릿속으로 반드시 다음을 점검한다:
   - 모든 여는 괄호·태그에 대응하는 닫는 괄호·태그가 있는가?
   - 모든 함수·변수가 사용 전에 선언되어 있는가?
   - 의도하지 않은 코드 잘림이 없도록 충분히 완성된 구조인가?
   점검 통과 후에만 코드를 출력한다.
"""

_HTML_STRUCTURE_GUIDE = """\

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HTML 필수 구조 (이 순서를 반드시 지킨다)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>제목</title>
  <style>
    /* 모든 CSS를 여기에 */
    * { margin:0; padding:0; box-sizing:border-box; }
    body { ... }         ← 반드시 셀렉터 뒤에 { } 쌍으로 작성
    .클래스 { ... }
  </style>
</head>
<body>
  <!-- HTML 내용 -->
  <script>
    // 모든 JavaScript를 여기에
    // 변수 선언 → 함수 정의 → 이벤트 등록 → 초기화 실행 순서로 작성
  </script>
</body>
</html>

CSS 작성 금지 패턴:
  margin: 0;          ← 셀렉터 없이 프로퍼티만 쓰는 것 절대 금지
  body               ← { } 없이 셀렉터만 쓰는 것 절대 금지
  ${{변수}}           ← CSS 안에 JavaScript 템플릿 리터럴 절대 금지
"""

_HTML_GAME_GUIDE = """\

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HTML 게임 품질 기준
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 브라우저에서 HTML 파일을 바로 열면 즉시 실행 가능해야 한다.
- 외부 CDN·라이브러리 금지. 순수 HTML/CSS/JS만 사용한다.
- 필수 기능: 키보드 조작, 점수 표시, 게임오버 감지, 재시작 버튼.
- 고스트 피스(낙하 예측) 등 게임성을 높이는 UX를 포함한다.
- requestAnimationFrame 기반 게임 루프를 사용한다.
- 모든 Canvas 드로잉 함수는 ctx를 null 체크 없이 안전하게 호출 가능해야 한다.
- 한국어 UI 텍스트를 사용한다.
"""


def _build_system_prompt(filename: str, language: str, user_request: str) -> str:
    """
    파일 타입에 맞는 시스템 프롬프트를 반환한다.

    핵심 전략: LLM에게 코드를 출력하기 *전* 머릿속으로 구조를 점검하게 유도
    (자기검증 체크리스트) → 문법 에러 / 잘린 코드 / 셀렉터 누락 방지.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    header = (
        f"당신은 ReCoder 코드 생성기입니다.\n"
        f"사용자 요청: \"{user_request}\"\n"
        f"출력 대상 파일: `{filename}` ({language})\n"
    )

    if ext == "html":
        is_game = any(
            kw in user_request.lower()
            for kw in ["게임", "game", "테트리스", "스네이크", "퍼즐", "슈팅",
                        "팩맨", "벽돌", "플래피", "체스", "2048", "지뢰"]
        )
        return header + _HTML_STRUCTURE_GUIDE + (
            _HTML_GAME_GUIDE if is_game else
            "\n게임이 아닌 웹 앱이라면: 반응형 디자인, 직관적인 UI, 즉시 사용 가능한 기능을 구현한다.\n"
        ) + _BASE_RULES

    if ext == "py":
        return header + """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Python 코드 품질 기준
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 타입 힌트(type hints)를 모든 함수 인자·반환값에 사용한다.
- 외부 패키지가 필요하면 파일 맨 위 주석에 pip install 명령을 명시한다.
- if __name__ == '__main__': 블록으로 진입점을 명확히 한다.
- try/except로 예측 가능한 에러를 처리한다.
- 들여쓰기는 스페이스 4칸으로 일관되게 사용한다.
""" + _BASE_RULES

    if ext in ("ts", "tsx", "jsx", "js"):
        return header + """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JavaScript/TypeScript 코드 품질 기준
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- ES2022+ 최신 문법(optional chaining, nullish coalescing 등)을 사용한다.
- TypeScript라면 any 타입을 피하고 명확한 인터페이스/타입을 정의한다.
- 비동기 처리는 async/await를 사용한다.
- 모든 변수는 const/let으로 선언하고 var는 사용하지 않는다.
""" + _BASE_RULES

    if ext == "sh":
        return header + """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bash 스크립트 품질 기준
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 첫 줄은 반드시 #!/usr/bin/env bash 로 시작한다.
- set -euo pipefail 을 두 번째 줄에 넣어 에러 시 즉시 중단한다.
- 변수는 "${{VAR}}" 형식으로 항상 따옴표로 감싼다.
""" + _BASE_RULES

    # 기타 — 범용 프롬프트
    return header + _BASE_RULES


# ── 메시지 핸들러 (진입점) ────────────────────────────────────────────────────

async def handle_make_message(bot: discord.Client, message: discord.Message) -> None:
    """지정 채널의 메시지를 처리하고 Bedrock 응답을 브리지로 스트리밍한다."""

    # 봇 자신 / 다른 봇 무시
    if message.author.bot:
        return

    # 채널 ID 동적 조회 (Workbench UI 변경 즉시 반영)
    make_channel_id = get_make_channel_id()
    if make_channel_id == 0:
        return
    if message.channel.id != make_channel_id:
        return

    content = (message.content or "").strip()
    if not content:
        return

    # 파일명 / 언어 자동 추론
    filename, language = _infer_file_info(content)

    # "실행" 의도 감지 — VSCode 확장이 파일을 자동으로 열어줘야 하는지
    auto_run = _infer_should_auto_run(content)

    # VSCode 브리지 연결 확인
    if hub.connected_count == 0:
        await message.reply(
            "❗ VSCode 확장이 브리지에 연결되어 있지 않습니다.\n"
            "VSCode를 열고 ReCoder 확장이 활성화되어 있는지 확인하세요.\n"
            "확장 연결 후 다시 메시지를 보내주세요.",
            mention_author=False,
        )
        return

    # 생성 시작 알림
    short_model = BEDROCK_MODEL_ID.split(".")[-1][:28]
    status_msg = await message.reply(
        f"⚙️ `{filename}` 생성 중… · {language} · `{short_model}`",
        mention_author=False,
    )

    # 브리지에 시작 이벤트 전송
    await hub.broadcast({
        "type": "start",
        "filename": filename,
        "language": language,
        "prompt": content,
    })

    # NOTE: 예외 경로에서도 반드시 `end` 를 보낸다.
    # VSCode 의 BridgeClient 는 `end` 가 와야 세션을 닫고 무결성 검증/파일
    # 저장을 수행한다. error 만 보내고 end 를 누락하면 다음 start 가 올 때까지
    # 파일이 잠긴 상태로 남는다. 따라서 finally 에서 end 를 강제 발송.
    chunk_count = 0
    stop_reason: Optional[str] = None
    end_sent = False

    try:
        # 자동 재시도 안전망:
        # max_tokens 로 잘리거나 응답이 비정상적으로 짧으면 한 번까지
        # 토큰 한도를 두 배로 늘려 재시도. 무한 루프 방지 위해 1회만.
        chunk_count, stop_reason = await _stream_bedrock(
            content, filename, language, max_tokens=MAX_TOKENS,
        )
        if stop_reason == "max_tokens" and chunk_count > 0:
            log.warning(
                "%s: max_tokens 도달 — %d 토큰으로 재시도",
                filename, MAX_TOKENS * 2,
            )
            await hub.broadcast({
                "type": "info", "filename": filename,
                "message": "토큰 한도 도달 — 한도 두 배로 재시도 중…",
            })
            # 이전 부분 응답은 폐기하고 새 세션으로 다시 — 확장은 새 start 받으면
            # 이전 세션을 강제 종료하고 새 파일을 연다 (startSession 에 구현됨).
            await hub.broadcast({
                "type": "start", "filename": filename,
                "language": language, "prompt": content,
            })
            chunk_count, stop_reason = await _stream_bedrock(
                content, filename, language, max_tokens=MAX_TOKENS * 2,
            )

        # end 이벤트에 auto_run 플래그 포함 — 확장이 파일을 자동 실행할지 결정
        await hub.broadcast({
            "type": "end",
            "filename": filename,
            "auto_run": auto_run,
        })
        end_sent = True
        await _update_status(
            status_msg, filename, chunk_count, stop_reason, auto_run=auto_run,
        )

    except asyncio.CancelledError:
        await hub.broadcast({
            "type": "error", "filename": filename, "message": "취소됨"
        })
        try:
            await status_msg.edit(content=f"🚫 `{filename}` 생성이 취소되었습니다.")
        except discord.HTTPException:
            pass
        raise

    except Exception as exc:
        log.exception("Bedrock 스트리밍 실패 (filename=%s): %s", filename, exc)
        await hub.broadcast({
            "type": "error", "filename": filename, "message": str(exc),
        })
        try:
            await status_msg.edit(content=f"❌ `{filename}` 생성 실패: `{exc}`")
        except discord.HTTPException:
            pass

    finally:
        # 정상 흐름이 아니어도 (예외/취소) 받은 부분 코드까지는 디스크에
        # 저장될 수 있도록 end 를 한 번 더 보낸다. 이미 보냈으면 skip.
        if not end_sent:
            try:
                await hub.broadcast({
                    "type": "end", "filename": filename, "auto_run": False,
                })
            except Exception:
                pass


async def _update_status(
    status_msg: discord.Message,
    filename: str,
    chunk_count: int,
    stop_reason: Optional[str],
    auto_run: bool = False,
) -> None:
    """완료 후 상태 메시지를 결과에 맞게 편집한다."""
    run_note = " · 🚀 자동 실행 요청됨" if auto_run else ""
    try:
        if stop_reason == "max_tokens":
            text = (
                f"⚠️ `{filename}` — 토큰 한도({MAX_TOKENS:,} tokens)에 도달해 코드가 잘렸습니다.\n"
                f"`{chunk_count:,}` 청크 전송 완료. "
                f"더 길게 생성하려면 `.env`에 `RECODER_MAKE_MAX_TOKENS=65536` 설정 후 재시작."
            )
        elif stop_reason and stop_reason != "end_turn":
            text = (
                f"⚠️ `{filename}` — 비정상 종료 "
                f"(stop_reason=`{stop_reason}`, {chunk_count:,} chunks){run_note}"
            )
        else:
            text = (
                f"✅ `{filename}` 생성 완료 · {chunk_count:,} chunks · "
                f"VSCode에서 확인하세요!{run_note}"
            )
        await status_msg.edit(content=text)
    except discord.HTTPException as exc:
        log.warning("상태 메시지 편집 실패: %s", exc)


# ── Bedrock 스트리밍 ──────────────────────────────────────────────────────────

async def _stream_bedrock(
    prompt: str,
    filename: str,
    language: str,
    max_tokens: Optional[int] = None,
) -> tuple[int, Optional[str]]:
    """
    Bedrock converse_stream을 워커 스레드에서 실행하고,
    텍스트 청크를 BridgeHub에 실시간으로 푸시한다.

    반환: (전송한 청크 수, stop_reason)
    """
    system_prompt = _build_system_prompt(filename, language, prompt)
    client = _get_bedrock_client()
    loop = asyncio.get_running_loop()
    effective_max_tokens = max_tokens or MAX_TOKENS

    def _invoke() -> Any:
        return client.converse_stream(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={
                "maxTokens": effective_max_tokens,
                "temperature": 0.2,   # 코드 생성은 낮은 온도로 결정론적으로
            },
        )

    resp = await loop.run_in_executor(None, _invoke)

    stream = resp.get("stream")
    if stream is None:
        raise RuntimeError("converse_stream 응답에 stream 필드가 없습니다.")

    it = iter(stream)
    chunk_count = 0
    stop_reason: Optional[str] = None

    while True:
        event = await loop.run_in_executor(None, _safe_next, it)
        if event is None:
            break

        # 텍스트 청크 처리
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text:
            await hub.broadcast({"type": "chunk", "text": text})
            chunk_count += 1
            continue

        # 종료 이벤트
        if "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
            log.info(
                "Bedrock 스트리밍 완료: stop_reason=%s, chunks=%d, model=%s",
                stop_reason, chunk_count, BEDROCK_MODEL_ID,
            )
            continue

        # 에러 이벤트 처리
        _check_stream_error(event)

    return chunk_count, stop_reason


def _safe_next(it) -> Optional[dict]:
    """이터레이터에서 다음 항목을 가져온다. 소진되면 None 반환."""
    try:
        return next(it)
    except StopIteration:
        return None


def _check_stream_error(event: dict) -> None:
    """Bedrock 스트림 에러 이벤트를 감지해 RuntimeError를 발생시킨다."""
    error_keys = (
        "internalServerException",
        "modelStreamErrorException",
        "throttlingException",
        "validationException",
        "modelTimeoutException",
        "serviceUnavailableException",
    )
    for key in error_keys:
        if key in event:
            payload = event[key]
            msg = payload.get("message", str(payload)) if isinstance(payload, dict) else str(payload)
            raise RuntimeError(f"Bedrock 스트림 오류 [{key}]: {msg}")
