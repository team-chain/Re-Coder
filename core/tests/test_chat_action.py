"""/api/chat 승인 카드 배선 — 구현 요청이면 action 을 싣고, 경로는 정규식으로 뽑는다.

9/16 테스트에서 채팅이 5턴 동안 질문만 반복하고, 말한 경로가 코드 생성에 전달되지
않아 파일이 워크스페이스 루트에 생겼다. 그 두 결함의 회귀 테스트.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from api.routes import analyze


# ── 경로 추출 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message, expected", [
    ("/Users/sebin/Desktop/te 여기 안에 만들어줘", "/Users/sebin/Desktop/te"),
    ("~/Desktop/te 폴더 만들고 거기에 테트리스 사이트 만들어줘", "~/Desktop/te"),
    ("~/Desktop/te에 만들어줘.", "~/Desktop/te"),
    ("./apps/web 에 추가해", "./apps/web"),
    ("C:\\work\\demo 에 만들어", "C:\\work\\demo"),
    ("테트리스 사이트 만들어줘", ""),
    ("회원 API 서버 만들어줘 (JWT)", ""),
])
def test_경로_추출(message: str, expected: str) -> None:
    assert analyze._extract_target_path(message) == expected


def test_이전_턴의_경로를_이어받는다(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, json.dumps({
        "reply": "진행할게요.", "action": {"instruction": "테트리스", "stack": "HTML", "files": ["index.html"], "summary": "테트리스"},
    }))
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(
        message="ㅇㅇ 진행해",
        history=[
            analyze.ChatHistoryMessage(role="user", content="~/Desktop/te 에 테트리스 만들어줘"),
            analyze.ChatHistoryMessage(role="assistant", content="스택은 어떻게 할까요?"),
        ],
    )))
    assert res["action"]["target_folder"] == "~/Desktop/te"
    assert res["action"]["target_source"] == "message"


# ── 응답 파싱 ────────────────────────────────────────────────────────────

def _stub_router(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    import llm.router as router_mod

    class _R:
        def __init__(self, t: str) -> None:
            self.text = t
            self.model_used = "stub-model"

    class _Router:
        def call(self, *args, **kwargs):
            return _R(text)

    monkeypatch.setattr(router_mod, "get_router", lambda: _Router())


def test_구현_요청이면_action_이_실린다(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, "```json\n" + json.dumps({
        "reply": "바닐라 JS로 만들게요. 아래에서 위치와 파일을 확인하고 승인해 주세요.",
        "action": {"instruction": "테트리스 웹 게임. 점수·난이도·게임오버 포함", "stack": "HTML / CSS / Vanilla JS",
                   "files": ["index.html", "style.css", "script.js"], "summary": "테트리스 웹 게임"},
    }, ensure_ascii=False) + "\n```")
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="~/Desktop/te 에 테트리스 사이트 만들어줘")))
    assert res["reply"].startswith("바닐라 JS")
    act = res["action"]
    assert act["type"] == "code.plan"
    assert act["target_folder"] == "~/Desktop/te"
    assert act["files"] == ["index.html", "style.css", "script.js"]
    assert act["stack"] == "HTML / CSS / Vanilla JS"
    #: reply 에 JSON 껍데기가 새면 안 된다 — 말풍선에 중괄호가 뜬다.
    assert "{" not in res["reply"]


def test_질문이면_action_은_없다(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, json.dumps({"reply": "배포는 Ship 탭에서 합니다.", "action": None}, ensure_ascii=False))
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="배포 어떻게 해?")))
    assert res["action"] is None
    assert res["reply"] == "배포는 Ship 탭에서 합니다."


def test_JSON_형식을_어겨도_구현_요청이면_action_을_만든다(monkeypatch: pytest.MonkeyPatch) -> None:
    """모델이 형식을 무시하고 평문으로 답한 경우 — 사용자 문장을 instruction 으로 쓴다."""
    _stub_router(monkeypatch, "네, 테트리스를 만들어 드릴게요! 먼저 폴더를 만들고...")
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="~/te 에 테트리스 만들어줘")))
    assert res["action"] is not None
    assert res["action"]["instruction"] == "~/te 에 테트리스 만들어줘"
    assert res["action"]["target_folder"] == "~/te"


def test_JSON_형식을_어기고_질문이면_평문_그대로(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, "README 는 프로젝트 소개 문서입니다.")
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="README 가 뭐야?")))
    assert res["action"] is None
    assert res["reply"] == "README 는 프로젝트 소개 문서입니다."


def test_경로_없는_구현_요청은_workspace_기준(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, json.dumps({"reply": "만들게요.", "action": {"instruction": "로그인 API", "stack": "FastAPI", "files": ["app.py"], "summary": "로그인 API"}}, ensure_ascii=False))
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="로그인 API 만들어줘", workspace_path="/tmp/proj")))
    assert res["action"]["target_folder"] == ""
    assert res["action"]["target_source"] == "workspace"


def test_action_에_instruction_이_없으면_무시(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_router(monkeypatch, json.dumps({"reply": "어떤 걸 원하세요?", "action": {"instruction": "  "}}))
    res = asyncio.run(analyze.chat_route(analyze.ChatRequest(message="뭐 좀 해줘")))
    assert res["action"] is None
