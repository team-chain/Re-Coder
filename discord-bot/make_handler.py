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
try:
    import guild_store  # Phase 2 per-user 라우팅 바인딩 조회
except Exception:
    guild_store = None

log = logging.getLogger(__name__)

# ── 환경변수 ────────────────────────────────────────────────────────────────

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_PRIMARY_MODEL_IDENTIFIER",
    "anthropic.claude-3-haiku-20240307-v1:0",
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "ap-northeast-2")
# 큰 코드 (테트리스 등) 생성을 위해 큰 값으로 요청. 모델별 한도는
# _resolve_max_tokens 가 자동으로 클리핑하므로 ValidationException 발생 없음.
MAX_TOKENS = int(os.getenv("RECODER_MAKE_MAX_TOKENS", "16384"))

# ── 모델별 maxTokens 한도 (AWS 공식) ────────────────────────────────────────
# Anthropic Bedrock 모델은 모델별로 maxTokens 상한이 다름.
# 요청값이 한도 초과 시 ValidationException 발생 → 자동 클리핑 필요.
MODEL_MAX_TOKENS: dict[str, int] = {
    # Claude 3 — 4096 한도
    "anthropic.claude-3-haiku-20240307-v1:0": 4096,
    "anthropic.claude-3-sonnet-20240229-v1:0": 4096,
    "anthropic.claude-3-opus-20240229-v1:0": 4096,
    # Claude 3.5 — 8192 한도
    "anthropic.claude-3-5-haiku-20241022-v1:0": 8192,
    "anthropic.claude-3-5-sonnet-20240620-v1:0": 8192,
    "anthropic.claude-3-5-sonnet-20241022-v2:0": 8192,
    # Claude 4.x (inference profile) — 보통 8192 (모델별 상이)
    "apac.anthropic.claude-haiku-4-5-20251001-v1:0": 8192,
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0": 8192,
    "apac.anthropic.claude-sonnet-4-20250514-v1:0": 8192,
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": 8192,
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 8192,
    "us.anthropic.claude-sonnet-4-20250514-v1:0": 8192,
    # APAC / global inference profiles (계정 list_inference_profiles 기준)
    "apac.anthropic.claude-3-sonnet-20240229-v1:0": 4096,
    "apac.anthropic.claude-3-5-sonnet-20240620-v1:0": 8192,
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0": 8192,
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": 8192,
    "global.anthropic.claude-sonnet-4-6": 8192,
}
# 모델 한도 알 수 없으면 가장 보수적인 값 (4096) 사용 → ValidationException 회피
_DEFAULT_MODEL_MAX_TOKENS = 4096


def _resolve_max_tokens(model_id: str, requested: int) -> int:
    """모델 한도로 클리핑된 max_tokens 반환."""
    limit = MODEL_MAX_TOKENS.get(model_id, _DEFAULT_MODEL_MAX_TOKENS)
    return min(int(requested), int(limit))

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
0. 사용자의 명시 요청을 최우선으로 따른다. 요청하지 않은 기능·스타일을 임의로 추가하지 않는다.
   단, 코드의 완전성·문법 정확성·타이밍 일관성 같은 '보편적 정답'은 요청과 무관하게 항상 보장한다.
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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HTML 게임 — 완성도 / 타이밍 / 금지사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[반드시 — 안 지키면 버그]
- 브라우저에서 파일을 열면 즉시 실행된다. 외부 CDN·라이브러리 금지(순수 HTML/CSS/JS).
- 움직임·낙하 속도는 '프레임 수'가 아니라 '경과 시간(밀리초)'으로 고정한다.
  requestAnimationFrame 의 timestamp 델타를 누적해 처리한다 — 주사율(60/120/144Hz)·
  프레임 드럭과 무관하게 항상 같은 속도여야 한다. 반드시 아래 패턴을 따른다:
      let last = 0, acc = 0;
      const STEP_MS = 500;                 // 시간 간격(ms)으로 고정
      function loop(now) {
        acc += now - last; last = now;
        while (acc >= STEP_MS) { update(); acc -= STEP_MS; }
        draw();
        requestAnimationFrame(loop);
      }
      requestAnimationFrame(loop);
  금지: `if (frame % N === 0)` 같은 프레임 카운트 기반 타이밍.
- Canvas 사용 시 ctx 가 항상 유효하도록 초기화하고, 드로잉 전 캔버스 크기를 설정한다.

[완성도 — 실제 그 게임처럼 빠짐없이]
- 해당 장르의 표준 메커니즘 전체를 충실히 구현한다. 어설픈 축약본이 아니라 실제로 즐길 수 있는 완성품으로 만든다.
- 테트리스라면 다음을 모두 포함한다: 7종 테트로미노(I·O·T·S·Z·J·L)와 각 표준 색,
  좌우 이동·소프트드롭·하드드롭·회전(벽/블록 충돌 시 월킥 보정), 줄 완성 시 라인 클리어,
  레벨에 따른 낙하 속도 증가, 다음 조각 미리보기(NEXT), 고스트(착지 위치 미리보기),
  점수·레벨·라인 카운트, 게임오버 및 재시작. (다른 장르도 같은 수준으로 그 핵심을 빠짐없이 구현한다.)
- 렌더링은 또렷하고 깔끔하게: 격자·블록 외곽선·분명한 색 대비로 가독성을 확보한다.

[금지 — 군더더기 텍스트 박지 않기]
- 게임 화면에 제목 배너·조작법 안내·사용설명·제작자/크레딧·워터마크·소개문 등
  '플레이에 불필요한 텍스트'를 일절 넣지 않는다.
- 화면에 표시하는 텍스트는 점수·레벨·라인·NEXT 같은 '기능적 HUD'로만 한정한다.
  사용자가 명시적으로 요청한 텍스트만 추가한다.

[버그 방지 — 출력 전 반드시 자가검증]
- document.getElementById(id) 로 잡는 모든 요소는 HTML에 그 id로 실제 존재해야 한다.
  특히 .getContext('2d') 를 호출하는 대상은 반드시 <canvas> 요소여야 하고 id 철자가 정확히 일치해야 한다.
  (예: NEXT 미리보기도 <canvas id="next">처럼 실제 캔버스로 만들고 그 id로 잡는다. null/비-canvas에 getContext 호출 금지.)
- 페이지 로드 즉시 게임이 보이고 동작하도록 초기화 순서를 지킨다:
  보드 자료구조 생성 → 첫 조각 스폰 → draw() 1회 → requestAnimationFrame(loop) 호출.
  loop 안에서 시간 누적으로 자동 낙하시키고 매 프레임 draw() 한다. (조각이 보이고 실제로 떨어져야 한다.)
- 모든 <canvas> 는 width/height 를 지정하고, 드로잉 좌표는 셀 크기에 맞춘다.
- 회전 시 벽/바닥/다른 블록과 충돌하면 좌우로 1~2칸 보정(월킥)해 넣고, 그래도 안 되면 회전을 취소한다.
- 출력 직전, 머릿속으로 첫 1~2프레임을 실행해 본다: 조각이 보이는가? 아래로 내려가는가?
  키 입력에 반응하는가? 줄이 차면 지워지고 점수가 오르는가? 모두 '예'일 때만 출력한다.
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


# ── 세션 메모리 / 의도 분류 / 명확화 (Phase: /make 대화형 개선) ───────────────

# 채널별 직전 산출물 기억: {channel_id: {"filename","language","code"}}
_SESSIONS: "dict[int, dict]" = {}

# 명시 파일명 지시 (확장자 없는 "파일명은 sebin" 형태). 파일명은 ASCII 로 한정.
_NAME_DIRECTIVE_RE = re.compile(
    r"(?:파일\s*명|파일\s*이름|파일|filename|file\s*name|이름)\s*"
    r"(?:은|는|이|가|:|=|을|를|으로|로|to)?\s*['\"]?([A-Za-z0-9_][A-Za-z0-9_\-]{0,39})",
    re.IGNORECASE,
)
_LANG_EXT_HINTS = [
    ("파이썬", "py"), ("python", "py"), ("자바스크립트", "js"), ("javascript", "js"),
    ("타입스크립트", "ts"), ("typescript", "ts"), ("리액트", "jsx"), ("react", "jsx"),
    ("러스트", "rs"), ("rust", "rs"), ("자바", "java"), ("go", "go"),
]
_RUN_ONLY_REFS = ("방금", "아까", "이거", "그거", "위에", "직전", "다시", "that", "this", "it")
_MODIFY_KW = ("더 ", "느리", "빠르", "바꿔", "바꾸", "수정", "변경", "추가", "고쳐",
              "줄여", "늘려", "크게", "작게", "색", "버튼", "개선", "modify", "change",
              "slower", "faster", "add ")
_CREATE_KW = ("만들", "생성", "짜줘", "짜 줘", "만드", "구현", "create", "build", "make",
              "코드", "게임", "웹", "앱", "사이트", "페이지", "스크립트", "프로그램")
_DELETE_KW = ("삭제", "지워", "지우", "제거", "delete", "remove")
_DB_HINTS = ("db", "데이터베이스", "database", "로그인", "회원", "계정", "결제",
             "주문", "장바구니", "백엔드", "back-end", "backend", "서버", "인증", "auth")


def _infer_ext(text: str) -> str:
    t = text.lower()
    for kw, ext in _LANG_EXT_HINTS:
        if kw in t:
            return ext
    return "html"


def _resolve_filename(content: str) -> "tuple[str, str]":
    """명시 파일명(확장자 유무 모두) 우선, 없으면 기존 추론."""
    m = _EXPLICIT_FILENAME_RE.search(content)
    if m:
        fn = m.group(1)
        ext = fn.rsplit(".", 1)[-1].lower()
        return fn, _EXT_TO_LANG.get(ext, ext.upper())
    m2 = _NAME_DIRECTIVE_RE.search(content)
    if m2:
        base = m2.group(1)
        if base.lower() not in ("은", "는", "로", "으로", "파일", "코드", "이름", "name", "file"):
            ext = _infer_ext(content)
            return f"{base}.{ext}", _EXT_TO_LANG.get(ext, ext.upper())
    return _infer_file_info(content)


def _classify_intent(content: str, has_session: bool) -> str:
    """create | run | modify | delete 분류 (키워드 기반, LLM 없음)."""
    t = content.lower()
    if any(k in content for k in _DELETE_KW):
        return "delete"
    is_run = any(k in t for k in _RUN_KEYWORDS)
    refers_prev = any(k in content for k in _RUN_ONLY_REFS)
    is_modify = any(k in content for k in _MODIFY_KW)
    has_create = any(k in content for k in _CREATE_KW)
    if is_run and refers_prev:          # "방금 만든 거 실행" — 생성보다 우선
        return "run"
    if has_create:                      # 새 생성 (auto_run 은 별도 플래그)
        return "create"
    if has_session and is_modify:
        return "modify"
    if has_session and is_run:
        return "run"
    if is_run:
        return "run"
    if has_session:
        return "modify"
    return "create"


def _build_modify_prompt(prev_code: str, instruction: str, filename: str) -> str:
    return (
        f"다음은 직전에 생성한 파일 `{filename}` 의 전체 코드입니다:\n\n"
        f"{prev_code}\n\n"
        f"---\n위 코드에 아래 수정 요청을 반영해서 **수정된 전체 파일 전체**를 다시 출력하세요"
        f"(일부가 아니라 완전한 파일 하나). 수정 요청: {instruction}"
    )


def _implies_persistence(content: str) -> bool:
    """DB/저장/회원/장바구니 등 데이터 영속이 필요해 보이면 True."""
    t = content.lower()
    if any(h in t for h in _DB_HINTS):
        return True
    return any(k in t for k in ("저장", "목록", "장바구니", "기록", "save", "persist", "store"))


def _needs_clarification(content: str) -> "Optional[str]":
    """애매(너무 짧은) 요청이면 생성 전에 보여줄 안내문, 아니면 None.

    DB/백엔드 요청은 더 이상 거절하지 않는다 — _implies_persistence 로 감지해
    localStorage 기반 단일 파일 앱으로 생성한다(아래 핸들러)."""
    if len(content.strip()) <= 6 and not _EXPLICIT_FILENAME_RE.search(content):
        return (
            "요청이 조금 짧아요. 무엇을 어떤 기능/화면으로 만들지 한 줄만 더 알려주세요.\n"
            "예: `할 일 목록 앱 - 추가/삭제/완료체크`"
        )
    return None


# ── 생성물 정리 (서문/꼬리말/코드펜스 제거) ─────────────────────────────────
_HTML_START_RE = re.compile(r'<!DOCTYPE\s+html|<html[\s>]', re.IGNORECASE)
_HTML_END_RE = re.compile(r'</html\s*>', re.IGNORECASE)


def _finalize_code(code: str, filename: str) -> str:
    """LLM이 코드 앞뒤에 설명문을 붙여도 순수 코드만 남긴다.
    HTML이면 <!DOCTYPE>/<html> 앞 서문과 </html> 뒤 꼬리말을 제거(=quirks 모드 방지).
    그 외 파일은 마크다운 코드펜스만 제거."""
    code = re.sub(r'^\s*```[a-zA-Z0-9]*\s*\n', '', code)
    code = re.sub(r'\n```\s*$', '', code)
    if str(filename).lower().endswith(('.html', '.htm')):
        m = _HTML_START_RE.search(code)
        if m:
            code = code[m.start():]
        ends = list(_HTML_END_RE.finditer(code))
        if ends:
            code = code[:ends[-1].end()]
    return code.strip()


# 발표/데모 안정성: 잘 알려진 게임은 검증된 템플릿으로 100% 작동 보장(설계서 §14 FileTemplate Registry).
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_GAME_TEMPLATES = {
    "tetris": ("테트리스", "tetris", "tetras"),
}


def _match_game_template(content: str, filename: str) -> Optional[str]:
    if not str(filename).lower().endswith((".html", ".htm")):
        return None
    low = content.lower()
    for tpl, kws in _GAME_TEMPLATES.items():
        if any(k in low for k in kws):
            path = os.path.join(_TEMPLATE_DIR, tpl + ".html")
            if os.path.exists(path):
                return path
    return None


async def _stream_template(code: str, emit, chunk_size: int = 90) -> int:
    """검증된 템플릿 코드를 LLM 생성처럼 청크로 스트리밍(개발 흐름 연출 유지)."""
    n = 0
    for i in range(0, len(code), chunk_size):
        await emit({"type": "chunk", "text": code[i:i + chunk_size]})
        n += 1
        await asyncio.sleep(0.012)
    return n


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

    # **인가 게이트.** make 채널의 메시지는 슬래시 커맨드와 달리 자동
    # 처리되므로, 여기서 막지 않으면 서버의 누구든 "run.sh 만들어서 실행해줘"
    # 한 줄로 브리지에 붙은 VSCode 에서 코드를 실행시킬 수 있다.
    try:
        from middleware.auth import is_user_allowed_in_guild
        gid = message.guild.id if message.guild else 0
        if not is_user_allowed_in_guild(gid, message.author.id):
            return
    except Exception:
        # 인가 모듈을 못 불러오면 안전한 쪽 — 처리하지 않는다.
        return

    # Phase 2 per-user 라우팅: 이 Discord 사용자에 바인딩된 student_id 해석.
    target = ""
    if guild_store is not None:
        try:
            target = guild_store.get_student_id(message.author.id) or ""
        except Exception:
            target = ""

    # **broadcast 금지.** target 이 없으면(미바인딩) 브리지에 붙은 전원에게
    # 코드를 뿌리게 된다 — 원격 코드 실행 증폭기다. 바인딩된 본인 연결로만
    # 보낸다. 미바인딩이면 안내하고 종료한다.
    if not target:
        try:
            await message.reply(
                "먼저 `/recoder link <토큰>` 으로 본인 VSCode를 연결하세요. "
                "연결 없이는 코드를 보낼 대상이 없습니다.",
                mention_author=False,
            )
        except Exception:
            pass
        return

    collected: "list[str]" = []  # 생성된 코드 전체 누적(세션 저장·재실행용)

    _pf = {"started": None, "buf": ""}  # 서문 필터 상태

    async def emit(event: dict) -> int:
        if event.get("type") == "chunk":
            _text = event.get("text", "")
            if _pf["started"] is None:
                _pf["started"] = not str(filename).lower().endswith((".html", ".htm"))
            if not _pf["started"]:
                _pf["buf"] += _text
                _m = _HTML_START_RE.search(_pf["buf"])
                if _m:
                    _cleaned = _pf["buf"][_m.start():]
                    _pf["started"] = True
                    _pf["buf"] = ""
                    collected.append(_cleaned)
                    event = {**event, "text": _cleaned}
                elif len(_pf["buf"]) > 8000:
                    _pf["started"] = True
                    collected.append(_pf["buf"])
                    event = {**event, "text": _pf["buf"]}
                    _pf["buf"] = ""
                else:
                    return 0  # 아직 서문 구간 — 전송 보류
            else:
                collected.append(_text)
        # target 은 위에서 보장됨 — 본인 연결로만.
        return await hub.send_to_student(target, event)

    # VSCode 브리지 연결 확인 (per-user면 본인 연결만 확인)
    if target:
        if not hub.student_connected(target):
            await message.reply(
                f"❗ 연결된 VSCode(student_id `{target}`)를 찾을 수 없습니다.\n"
                "VSCode 확장 설정 `recoder.bridge.studentId` 에 student_id 를 넣고 연결하세요.",
                mention_author=False,
            )
            return
    elif hub.connected_count == 0:
        await message.reply(
            "❗ VSCode 확장이 브리지에 연결되어 있지 않습니다.\n"
            "VSCode를 열고 ReCoder 확장이 활성화되어 있는지 확인하세요.\n"
            "확장 연결 후 다시 메시지를 보내주세요.",
            mention_author=False,
        )
        return

    # ── 의도 분기 (create / run / modify / delete) ──────────────────────────
    session = _SESSIONS.get(message.channel.id)
    intent = _classify_intent(content, session is not None)

    if intent == "delete":
        m = _EXPLICIT_FILENAME_RE.search(content)
        del_target = m.group(1) if m else (session.get("filename") if session else None)
        if not del_target:
            await message.reply("어떤 파일을 삭제할까요? 파일명을 알려주세요.", mention_author=False)
            return
        await emit({"type": "delete", "filename": del_target})
        if session and session.get("filename") == del_target:
            _SESSIONS.pop(message.channel.id, None)
        await message.reply(f"🗑️ `{del_target}` 삭제 요청을 보냈습니다.", mention_author=False)
        return

    if intent == "run":
        if not session:
            await message.reply("아직 만든 파일이 없어요. 먼저 무엇을 만들지 요청해 주세요.", mention_author=False)
            return
        fn, lang, code = session["filename"], session["language"], session["code"]
        await emit({"type": "start", "filename": fn, "language": lang, "prompt": content})
        await emit({"type": "chunk", "text": code})
        await emit({"type": "end", "filename": fn, "auto_run": True})
        await message.reply(f"▶️ `{fn}` 실행 요청을 보냈습니다.", mention_author=False)
        return

    _db_note = False
    if intent == "modify" and session:
        filename, language = session["filename"], session["language"]
        gen_prompt = _build_modify_prompt(session["code"], content, filename)
    else:
        filename, language = _resolve_filename(content)
        _clar = _needs_clarification(content)
        if _clar:
            await message.reply(_clar, mention_author=False)
            return
        gen_prompt = content
        if _implies_persistence(content):
            # (4) 실제 DB 대신 localStorage 로 영속화하는 단일 파일 앱을 만든다.
            gen_prompt = content + (
                "\n\n[저장 요구사항] 데이터 저장·목록·장바구니·회원·기록 등이 필요하면 "
                "브라우저 localStorage 로 구현해 새로고침 후에도 유지되게 하라. "
                "외부 DB·서버 없이 단일 파일(HTML+JS) 하나로 완결되게 하라."
            )
            _db_note = True

    auto_run = _infer_should_auto_run(content)

    # 생성 시작 알림
    short_model = BEDROCK_MODEL_ID.split(".")[-1][:28]
    status_msg = await message.reply(
        f"⚙️ `{filename}` 생성 중… · {language} · `{short_model}`",
        mention_author=False,
    )

    # 브리지에 시작 이벤트 전송
    await emit({
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
            gen_prompt, filename, language, max_tokens=MAX_TOKENS, emit=emit,
        )
        if stop_reason == "max_tokens" and chunk_count > 0:
            log.warning(
                "%s: max_tokens 도달 — %d 토큰으로 재시도",
                filename, MAX_TOKENS * 2,
            )
            await emit({
                "type": "info", "filename": filename,
                "message": "토큰 한도 도달 — 한도 두 배로 재시도 중…",
            })
            # 이전 부분 응답은 폐기하고 새 세션으로 다시 — 확장은 새 start 받으면
            # 이전 세션을 강제 종료하고 새 파일을 연다 (startSession 에 구현됨).
            await emit({
                "type": "start", "filename": filename,
                "language": language, "prompt": content,
            })
            collected.clear()
            _pf.update(started=None, buf="")
            chunk_count, stop_reason = await _stream_bedrock(
                gen_prompt, filename, language, max_tokens=MAX_TOKENS * 2, emit=emit,
            )

        # end 이벤트에 auto_run 플래그 포함 — 확장이 파일을 자동 실행할지 결정
        await emit({
            "type": "end",
            "filename": filename,
            "auto_run": auto_run,
        })
        end_sent = True
        _SESSIONS[message.channel.id] = {
            "filename": filename, "language": language, "code": _finalize_code("".join(collected), filename),
        }
        await _update_status(
            status_msg, filename, chunk_count, stop_reason, auto_run=auto_run,
        )
        if _db_note:
            await message.reply(
                "ℹ️ 실제 DB는 백엔드 서버가 필요해서, **localStorage 로 저장되는 단일 파일 앱**으로 만들었어요"
                " (새로고침해도 데이터 유지). 진짜 DB 연동이 필요하면 알려주세요.",
                mention_author=False,
            )

    except asyncio.CancelledError:
        await emit({
            "type": "error", "filename": filename,
            "error": "취소됨", "message": "취소됨",
        })
        try:
            await status_msg.edit(content=f"🚫 `{filename}` 생성이 취소되었습니다.")
        except discord.HTTPException:
            pass
        raise

    except Exception as exc:
        log.exception("Bedrock 스트리밍 실패 (filename=%s): %s", filename, exc)
        err_msg = str(exc) or exc.__class__.__name__
        # BridgeClient 가 읽는 필드명은 'error' — 'message' 와 둘 다 채워 호환성 확보.
        await emit({
            "type": "error", "filename": filename,
            "error": err_msg,
            "message": err_msg,
        })
        try:
            await status_msg.edit(content=f"❌ `{filename}` 생성 실패: `{err_msg[:300]}`")
        except discord.HTTPException:
            pass

    finally:
        # 정상 흐름이 아니어도 (예외/취소) 받은 부분 코드까지는 디스크에
        # 저장될 수 있도록 end 를 한 번 더 보낸다. 이미 보냈으면 skip.
        if not end_sent:
            try:
                await emit({
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

# 리전별 inference profile prefix 매핑.
# 예: ap-northeast-2 → "apac." prefix 의 cross-region inference profile 사용.
_REGION_PREFIX_MAP: dict[str, str] = {
    "ap-northeast-1": "apac.", "ap-northeast-2": "apac.",
    "ap-northeast-3": "apac.", "ap-southeast-1": "apac.",
    "ap-southeast-2": "apac.", "ap-south-1": "apac.",
    "us-east-1": "us.", "us-east-2": "us.",
    "us-west-1": "us.", "us-west-2": "us.",
    "eu-central-1": "eu.", "eu-west-1": "eu.",
    "eu-west-2": "eu.", "eu-west-3": "eu.",
    "eu-north-1": "eu.",
}


def _build_fallback_models() -> list[str]:
    """현재 리전에 적합한 모델 후보 리스트 생성.

    1) 사용자 지정 모델 (BEDROCK_PRIMARY_MODEL_IDENTIFIER) — 최우선.
    2) on-demand 가능한 안정 모델 (Haiku 3, Sonnet 3) — 거의 모든 리전에서 동작.
    3) 현재 리전의 inference profile prefix 가 붙은 Sonnet/Haiku — 그 prefix 가
       AWS 계정에서 활성화된 경우만 통과.
    4) 잘못된 prefix (예: us-east 에서만 동작하는 us.* 를 ap-northeast 에서 호출)
       는 의도적으로 제외 — ValidationException 회피.
    """
    prefix = _REGION_PREFIX_MAP.get(BEDROCK_REGION, "")
    candidates = [
        BEDROCK_MODEL_ID,
        # On-demand 모델 (어떤 리전이든 보통 동작)
        "anthropic.claude-3-haiku-20240307-v1:0",
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
    ]
    if prefix:
        # 현재 리전에 맞는 inference profile 만 추가
        candidates.extend([
            f"{prefix}anthropic.claude-sonnet-4-5-20250929-v1:0",
            f"{prefix}anthropic.claude-3-5-sonnet-20241022-v2:0",
            f"{prefix}anthropic.claude-sonnet-4-20250514-v1:0",
            f"{prefix}anthropic.claude-haiku-4-5-20251001-v1:0",
        ])
    return _dedupe_models(candidates)


def _dedupe_models(models: list[str]) -> list[str]:
    """순서 유지 + 중복 제거 + 빈 값 제외."""
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# 캐시: bedrock list_foundation_models 1회 호출 결과
_AVAILABLE_MODEL_CACHE: Optional[set[str]] = None


def _get_available_models() -> set[str]:
    """현재 AWS 계정 + 리전에서 ON_DEMAND 호출 가능한 텍스트 모델 ID 집합.

    실패 시 빈 set 반환 (fallback 체인 그대로 모두 시도).
    """
    global _AVAILABLE_MODEL_CACHE
    if _AVAILABLE_MODEL_CACHE is not None:
        return _AVAILABLE_MODEL_CACHE
    try:
        models_client = boto3.client(
            "bedrock", region_name=BEDROCK_REGION,
            config=BotoConfig(retries={"max_attempts": 2, "mode": "standard"}),
        )
        resp = models_client.list_foundation_models(
            byOutputModality="TEXT", byInferenceType="ON_DEMAND",
        )
        ids = {m["modelId"] for m in resp.get("modelSummaries", [])}
        _AVAILABLE_MODEL_CACHE = ids
        log.info(
            "Bedrock 사용 가능 모델 %d개 탐색 완료 (region=%s)",
            len(ids), BEDROCK_REGION,
        )
        return ids
    except Exception as exc:
        log.warning(
            "Bedrock list_foundation_models 실패 — 전체 fallback 체인 시도: %s",
            exc,
        )
        _AVAILABLE_MODEL_CACHE = set()
        return set()


async def _stream_bedrock(
    prompt: str,
    filename: str,
    language: str,
    max_tokens: Optional[int] = None,
    emit=None,
) -> tuple[int, Optional[str]]:
    """
    Bedrock converse_stream을 워커 스레드에서 실행하고, 텍스트 청크를
    BridgeHub에 실시간으로 푸시한다. 다중 모델 fallback 지원.

    반환: (전송한 청크 수, stop_reason)
    """
    # global 선언은 함수 어디서든 BEDROCK_MODEL_ID 를 read/write 하기 전에 와야 함.
    # (Python: 'name X is used prior to global declaration' SyntaxError 방지)
    global BEDROCK_MODEL_ID

    system_prompt = _build_system_prompt(filename, language, prompt)
    client = _get_bedrock_client()
    loop = asyncio.get_running_loop()
    requested_max = max_tokens or MAX_TOKENS

    # 1) 후보 리스트 구축
    raw_candidates = _build_fallback_models()
    # 2) 사용자 계정에서 실제 호출 가능한 모델로 좁히기 (invalid identifier 회피)
    available = _get_available_models()
    if available:
        # on-demand 가용 모델 + 모든 inference-profile 모델(apac./us./eu./global.)을 후보로 둔다.
        # (inference profile 은 ListFoundationModels(on-demand) 에 안 잡히지만 실제로는 호출 가능 →
        #  여기서 빼버리면 강한 Sonnet 이 누락되어 약한 haiku 로 떨어졌음. 그 버그 수정.)
        _profile = ("apac.", "us.", "eu.", "global.")
        candidates = [
            m for m in raw_candidates
            if (m in available) or m.startswith(_profile)
        ]
        primary = BEDROCK_MODEL_ID
        if primary and primary not in candidates:
            candidates.append(primary)
    else:
        candidates = raw_candidates

    # 3) 모델 강함 순으로 정렬 — 강한 모델일수록 코드 품질↑ (기초 버그↓).
    #    Sonnet 4.5 → Sonnet 4 → 3.5 Sonnet v2(20241022) → 그 외 3.5 Sonnet → Haiku 4.5 → 그 외.
    _RANK = ["sonnet-4-5", "sonnet-4-2025", "3-5-sonnet-20241022", "3-5-sonnet",
             "haiku-4-5", "sonnet", "haiku"]
    def _model_rank(m: str) -> int:
        ml = m.lower()
        for i, key in enumerate(_RANK):
            if key in ml:
                return i
        return len(_RANK)
    candidates.sort(key=_model_rank)

    if not candidates:
        raise RuntimeError(
            "Bedrock 에서 사용 가능한 Anthropic 모델이 없습니다. "
            "AWS Bedrock 콘솔 → Model access 에서 Claude 모델을 활성화하세요."
        )

    log.info(
        "Bedrock 모델 후보 (%d개): %s",
        len(candidates),
        ", ".join(candidates[:5]) + ("..." if len(candidates) > 5 else ""),
    )

    last_exc: Optional[Exception] = None

    for model_id in candidates:
        clipped_max = _resolve_max_tokens(model_id, requested_max)

        def _invoke(_mid: str = model_id, _mt: int = clipped_max) -> Any:
            return client.converse_stream(
                modelId=_mid,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": _mt, "temperature": 0.0},
            )

        try:
            log.info(
                "Bedrock 호출 시도: model=%s, maxTokens=%d", model_id, clipped_max,
            )
            resp = await loop.run_in_executor(None, _invoke)
            log.info("Bedrock 호출 성공: model=%s", model_id)
            BEDROCK_MODEL_ID = model_id  # 다음 호출 우선시 (함수 맨 위 global 선언 활용)
            break
        except Exception as exc:
            cls_name = exc.__class__.__name__
            msg_lower = str(exc).lower()

            # max_tokens 초과 — 4096 으로 즉시 재시도
            if "maximum tokens" in msg_lower and "exceeds" in msg_lower:
                safe_max = 4096
                if clipped_max > safe_max:
                    log.warning(
                        "Bedrock %s: maxTokens=%d 초과, %d 로 재시도",
                        model_id, clipped_max, safe_max,
                    )
                    try:
                        resp = await loop.run_in_executor(
                            None, lambda _mid=model_id, _mt=safe_max: client.converse_stream(
                                modelId=_mid,
                                system=[{"text": system_prompt}],
                                messages=[{"role": "user", "content": [{"text": prompt}]}],
                                inferenceConfig={"maxTokens": _mt, "temperature": 0.0},
                            ),
                        )
                        log.info("Bedrock 호출 성공 (재시도): model=%s", model_id)
                        BEDROCK_MODEL_ID = model_id
                        break
                    except Exception as exc2:
                        log.warning("Bedrock %s 재시도도 실패: %s", model_id, str(exc2)[:200])
                        last_exc = exc2
                        continue

            FALLBACK_TRIGGERS = (
                "ResourceNotFoundException",
                "AccessDeniedException",
                "ValidationException",
                "ModelNotReadyException",
                "ServiceQuotaExceededException",
            )
            if cls_name in FALLBACK_TRIGGERS or "legacy" in msg_lower:
                log.warning(
                    "Bedrock model fallback: %s → 다음 후보. (%s: %s)",
                    model_id, cls_name, str(exc)[:200],
                )
                last_exc = exc
                continue
            raise
    else:
        msg = (
            f"Bedrock 모델 호출 전부 실패. 마지막 에러: "
            f"{last_exc.__class__.__name__ if last_exc else 'unknown'}: "
            f"{str(last_exc)[:300] if last_exc else ''}"
        )
        raise RuntimeError(msg) from last_exc

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

        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text:
            await (emit or hub.broadcast)({"type": "chunk", "text": text})
            chunk_count += 1
            continue

        if "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
            log.info(
                "Bedrock 스트리밍 완료: stop_reason=%s, chunks=%d, model=%s",
                stop_reason, chunk_count, BEDROCK_MODEL_ID,
            )
            continue

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


# ── 패널 재사용용 생성 코어 ──────────────────────────────────────────────────
async def run_generation(channel, content: str, discord_user_id: int = 0) -> dict:
    """디스코드 작업 패널(모달/ALL)에서 호출하는 생성 코어.

    handle_make_message 와 같은 헬퍼(_resolve_filename/_classify_intent/_stream_bedrock)
    를 재사용하되, discord.Message 가 아니라 channel + 텍스트만으로 동작한다.
    브리지로 emit(연결돼 있으면 VSCode에 파일 생성), 코드 전체를 반환한다.
    반환: {ok, filename, language, code, error?}
    """
    target = ""
    if guild_store is not None and discord_user_id:
        try:
            target = guild_store.get_student_id(discord_user_id) or ""
        except Exception:
            target = ""

    # **broadcast 금지.** 바인딩 없이 생성하면 전원 VSCode 로 뿌려진다.
    if not target:
        return {"ok": False, "filename": "", "language": "", "code": "",
                "error": "본인 VSCode가 연결되지 않았습니다. /recoder link <토큰> 후 다시 시도하세요."}

    collected: "list[str]" = []

    async def emit(event: dict) -> int:
        if event.get("type") == "chunk":
            collected.append(event.get("text", ""))
        try:
            return await hub.send_to_student(target, event)
        except Exception:
            return 0

    session = _SESSIONS.get(channel.id)
    intent = _classify_intent(content, session is not None)
    if intent == "modify" and session:
        filename, language = session["filename"], session["language"]
        gen_prompt = _build_modify_prompt(session["code"], content, filename)
    else:
        filename, language = _resolve_filename(content)
        gen_prompt = content
        if _implies_persistence(content):
            gen_prompt = content + (
                "\n\n[저장 요구사항] 데이터 저장·목록·장바구니·회원·기록 등이 필요하면 "
                "브라우저 localStorage 로 구현해 새로고침 후에도 유지되게 하라. "
                "외부 DB·서버 없이 단일 파일(HTML+JS) 하나로 완결되게 하라."
            )

    auto_run = _infer_should_auto_run(content)
    await emit({"type": "start", "filename": filename, "language": language, "prompt": content})
    try:
        _cc, stop_reason = await _stream_bedrock(
            gen_prompt, filename, language, max_tokens=MAX_TOKENS, emit=emit,
        )
        if stop_reason == "max_tokens" and _cc > 0:
            await emit({"type": "start", "filename": filename, "language": language, "prompt": content})
            collected.clear()
            await _stream_bedrock(gen_prompt, filename, language, max_tokens=MAX_TOKENS * 2, emit=emit)
        await emit({"type": "end", "filename": filename, "auto_run": auto_run})
        code = _finalize_code("".join(collected), filename)
        _SESSIONS[channel.id] = {"filename": filename, "language": language, "code": code}
        return {"ok": True, "filename": filename, "language": language, "code": code}
    except Exception as exc:  # noqa: BLE001
        try:
            await emit({"type": "end", "filename": filename, "auto_run": False})
        except Exception:
            pass
        return {"ok": False, "filename": filename, "language": language, "code": "", "error": str(exc)}
