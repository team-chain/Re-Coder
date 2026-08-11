"""
/api/code/plan DoD 검증 (AI-DLC 1단계 — 코드 대신 "설계 결정" 제시).

노션 태스크(§ /api/code/plan 구현) 완료 기준:
  1. "만들어줘" 요청을 보내면 코드가 아니라 결정 목록이 온다.
  2. 응답이 항상 JSON 스키마에 맞는다 (LLM 이 흔들려도 _extract_json 으로 파싱됨).
  3. 대표 요청 3종(할일앱/API서버/랜딩페이지)으로 테스트 통과.

이 테스트는 core/code_agent.py 의 generate_plan() (핵심 로직)과
core/api/routes/analyze.py 에 라우트가 실제로 등록됐는지를 검증한다.
LLM 호출은 code_agent.get_router 를 가짜 라우터로 monkeypatch 해서 대체한다
(다른 테스트들의 관례와 동일 — 실제 Bedrock 호출 없음).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import code_agent


class _FakeLLMResponse:
    def __init__(self, text: str, model_used: str = "fake-model"):
        self.text = text
        self.model_used = model_used


class _FakeRouter:
    """호출될 때마다 미리 정해둔 raw 텍스트를 순서대로 반환하는 가짜 라우터."""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.calls: list[dict] = []

    def call(self, request, agent=None, operation=None):
        self.calls.append({"agent": agent, "operation": operation, "prompt": request.prompt})
        text = self._texts.pop(0) if self._texts else self._texts_default()
        return _FakeLLMResponse(text)

    def _texts_default(self) -> str:
        return json.dumps({"decisions": []})


def _decisions_json(*decisions: dict) -> str:
    return json.dumps({"decisions": list(decisions)})


_TODO_APP_DECISION = {
    "id": "storage",
    "question": "데이터를 어디에 저장할까요?",
    "options": [
        {"key": "local", "label": "브라우저 로컬 저장", "summary": "서버 없이 바로 동작",
         "pros": ["간단"], "cons": ["기기 간 공유 불가"], "recommended": True},
        {"key": "file", "label": "파일(JSON)", "summary": "간단하지만 기기 간 공유 안 됨",
         "pros": [], "cons": [], "recommended": False},
        {"key": "db", "label": "DB(SQLite)", "summary": "확장 좋지만 배포 무거움",
         "pros": [], "cons": [], "recommended": False},
    ],
    "impact": "앱 구조에 영향",
}

_API_SERVER_DECISION = {
    "id": "auth",
    "question": "인증 방식을 무엇으로 할까요?",
    "options": [
        {"key": "jwt", "label": "JWT", "summary": "토큰 기반, stateless",
         "pros": ["확장 용이"], "cons": ["토큰 폐기 어려움"], "recommended": True},
        {"key": "session", "label": "세션", "summary": "서버 세션 저장소 필요",
         "pros": ["폐기 쉬움"], "cons": ["스케일 아웃 시 세션 공유 필요"], "recommended": False},
    ],
    "impact": "API 서버 구조에 영향",
}

_LANDING_PAGE_DECISION = {
    "id": "framework",
    "question": "어떤 방식으로 랜딩 페이지를 만들까요?",
    "options": [
        {"key": "static_html", "label": "정적 HTML/CSS", "summary": "빌드 도구 없이 즉시 배포",
         "pros": ["단순"], "cons": ["컴포넌트 재사용 어려움"], "recommended": True},
        {"key": "react", "label": "React", "summary": "컴포넌트 기반, 빌드 필요",
         "pros": ["재사용성"], "cons": ["빌드 설정 필요"], "recommended": False},
    ],
    "impact": "정적 배포(S3) 경로와 궁합",
}


def _assert_decision_shape(decision: dict) -> None:
    assert set(["id", "question", "options", "impact"]).issubset(decision.keys())
    assert decision["id"] and decision["question"]
    assert isinstance(decision["options"], list) and decision["options"]
    for opt in decision["options"]:
        assert set(["key", "label", "summary", "recommended"]).issubset(opt.keys())
        assert isinstance(opt["recommended"], bool)
    assert any(opt["recommended"] for opt in decision["options"]), "추천 옵션이 하나는 있어야 함"


# ---------------------------------------------------------------------------
# 1) "만들어줘" 요청 -> 코드(ops)가 아니라 결정 목록(decisions)이 온다
# ---------------------------------------------------------------------------

def test_generate_plan_returns_decisions_not_code(monkeypatch):
    fake = _FakeRouter([_decisions_json(_TODO_APP_DECISION)])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("할 일 목록 웹앱 만들어줘", session_id="t-todo")

    assert "decisions" in result
    assert "ops" not in result, "plan 단계는 코드(ops)를 반환하면 안 됨"
    assert "code" not in result
    assert fake.calls[0]["operation"] == "generate_plan"
    assert len(result["decisions"]) == 1
    _assert_decision_shape(result["decisions"][0])
    assert result["decisions"][0]["id"] == "storage"


# ---------------------------------------------------------------------------
# 2) 응답이 항상 JSON 스키마에 맞는다 — LLM 이 흔들려도 _extract_json 으로 파싱
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_wrap",
    [
        lambda body: body,  # 정상: 순수 JSON
        lambda body: f"```json\n{body}\n```",  # 마크다운 코드펜스
        lambda body: f"물론이죠! 아래 JSON을 확인하세요:\n{body}\n감사합니다.",  # 앞뒤 설명 텍스트
        lambda body: f"```\n{body}\n```\n",  # 언어 태그 없는 펜스
    ],
)
def test_generate_plan_survives_wobbly_llm_output(monkeypatch, raw_wrap):
    body = _decisions_json(_API_SERVER_DECISION)
    fake = _FakeRouter([raw_wrap(body)])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("REST API 서버 만들어줘")

    assert isinstance(result["decisions"], list)
    assert len(result["decisions"]) == 1
    _assert_decision_shape(result["decisions"][0])


def test_generate_plan_drops_malformed_decisions_but_keeps_valid_ones(monkeypatch):
    # LLM 이 일부 decision 에 id/question 을 빼먹거나 options 를 비워도
    # 전체 응답이 깨지지 않고, 유효한 decision 만 필터링돼야 한다.
    body = json.dumps({
        "decisions": [
            _TODO_APP_DECISION,
            {"id": "", "question": "빈 id", "options": [{"key": "x", "label": "x"}]},
            {"id": "empty_options", "question": "옵션 없음", "options": []},
        ]
    })
    fake = _FakeRouter([body])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("할 일 앱 만들어줘")

    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["id"] == "storage"


def test_generate_plan_recommends_first_option_when_llm_forgets(monkeypatch):
    # recommended 를 아무 옵션도 표시하지 않아도, 항상 하나는 추천으로 표시돼야
    # "응답이 항상 스키마에 맞는다"는 조건(추천 옵션 존재)을 만족한다.
    body = json.dumps({"decisions": [{
        "id": "auth", "question": "인증 방식은?",
        "options": [{"key": "jwt", "label": "JWT"}, {"key": "session", "label": "세션"}],
        "impact": "",
    }]})
    fake = _FakeRouter([body])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("로그인 API 만들어줘")
    _assert_decision_shape(result["decisions"][0])


# ---------------------------------------------------------------------------
# 3) 대표 요청 3종(할일앱/API서버/랜딩페이지) 통과
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "instruction,decision",
    [
        ("할 일 목록 웹앱 만들어줘", _TODO_APP_DECISION),
        ("회원 관리 REST API 서버 만들어줘", _API_SERVER_DECISION),
        ("제품 소개 랜딩 페이지 만들어줘", _LANDING_PAGE_DECISION),
    ],
    ids=["todo-app", "api-server", "landing-page"],
)
def test_generate_plan_representative_requests(monkeypatch, instruction, decision):
    fake = _FakeRouter([_decisions_json(decision)])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan(instruction)

    assert result["decisions"], f"'{instruction}' 요청에 decisions 가 비어있음"
    for d in result["decisions"]:
        _assert_decision_shape(d)


def test_generate_plan_trivial_request_falls_back_to_confirm_card(monkeypatch):
    # 오타 수정처럼 설계상 갈림길이 없는 요청이라도 decisions 는 비면 안 된다.
    # FR-02-05(항상 선택지·사람 승인): 결정이 없다고 승인 단계를 건너뛰면
    # AI 가 혼자 판단해 코드를 만든 셈이 되어 AI-DLC 전제가 깨진다.
    # 그래서 LLM 이 빈 배열을 줘도 서버가 "진행/취소" 확인 카드로 대체한다.
    from adr import CONFIRM_DECISION_ID

    fake = _FakeRouter([json.dumps({"decisions": []})])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)

    result = code_agent.generate_plan("변수명 오타 고쳐줘")

    assert len(result["decisions"]) == 1, "빈 결정은 확인 카드로 대체돼야 함"
    card = result["decisions"][0]
    assert card["id"] == CONFIRM_DECISION_ID
    _assert_decision_shape(card)  # 확인 카드도 plan 스키마를 그대로 따른다
    assert [o["key"] for o in card["options"]] == ["proceed", "cancel"]
    assert "변수명 오타 고쳐줘" in card["question"], "무엇을 승인하는지 질문에 보여야 함"


def test_generate_plan_includes_attached_context_files_in_prompt(monkeypatch):
    """첨부한 아키텍처 제약이 plan 단계의 LLM 프롬프트까지 전달돼야 한다."""
    fake = _FakeRouter([json.dumps({"decisions": []})])
    monkeypatch.setattr(code_agent, "get_router", lambda: fake)
    context_files = [{
        "path": "docs/architecture.md",
        "content": "데이터 저장소는 PostgreSQL만 사용한다. SQLite는 사용 금지.",
    }]

    code_agent.generate_plan("사용자 저장 기능을 추가해줘", context_files=context_files)

    prompt = fake.calls[0]["prompt"]
    assert "사용자가 첨부한 참고 파일" in prompt
    assert "docs/architecture.md" in prompt
    assert "PostgreSQL만 사용한다" in prompt
    assert "모순되는 선택지는 추천하지 마세요" in prompt


def test_generate_plan_empty_instruction_raises():
    with pytest.raises(ValueError):
        code_agent.generate_plan("   ")


# ---------------------------------------------------------------------------
# 라우트 결선 확인 — core/api/routes/ 에 POST /api/code/plan 이 실제로 등록됐는가
# ---------------------------------------------------------------------------

def test_code_plan_route_registered():
    from api.routes import analyze as analyze_routes

    matches = [
        r for r in analyze_routes.router.routes
        if getattr(r, "path", None) == "/api/code/plan"
    ]
    assert matches, "POST /api/code/plan 라우트가 등록되어 있지 않음"
    assert "POST" in matches[0].methods


def test_code_plan_request_accepts_context_files():
    from api.routes.analyze import CodePlanRequest

    request = CodePlanRequest(
        instruction="계획 세워줘",
        context_files=[{"path": "docs/architecture.md", "content": "PostgreSQL 사용"}],
    )

    assert request.context_files == [{"path": "docs/architecture.md", "content": "PostgreSQL 사용"}]


def test_code_plan_route_forwards_context_files(monkeypatch):
    from api.routes import analyze as analyze_routes

    captured: dict = {}

    def fake_generate_plan(**kwargs):
        captured.update(kwargs)
        return {"decisions": [], "model": "fake-model"}

    monkeypatch.setattr(code_agent, "generate_plan", fake_generate_plan)
    context_files = [{"path": "docs/architecture.md", "content": "PostgreSQL 사용"}]
    result = asyncio.run(analyze_routes.code_plan_route(analyze_routes.CodePlanRequest(
        instruction="계획 세워줘",
        context_files=context_files,
    )))

    assert result["decisions"] == []
    assert captured["context_files"] == context_files


# ---------------------------------------------------------------------------
# [회귀] 워크스페이스 경로를 프로세스 전역 env 로 흘리지 않는다
#
# 회차1 에서 교차 오염(P1, 커밋 489e3be)을 잡을 때 `project_root` 인자 전달만
# 추가하고 `os.environ["RECODER_PROJECT_ROOT"] = ...` 쓰기는 지우지 않아
# 사고 경로가 남아 있었다. 라우트가 to_thread 로 넘어가며 실행을 양보하는
# 사이 다른 창의 요청이 전역을 덮어쓰거나, 경로를 안 보낸 요청이
# code_agent._project_root() 폴백으로 그 전역을 읽으면 **남의 워크스페이스**
# 를 기준으로 코드·ADR 이 만들어진다.
# ---------------------------------------------------------------------------

def _run_route_and_capture_env(monkeypatch, tmp_path, *, route: str) -> dict:
    """라우트를 한 번 호출하고, 넘어간 인자와 전역 env 변화를 함께 돌려준다."""
    import os

    from api.routes import analyze as analyze_routes

    captured: dict = {}

    def _fake(**kwargs):
        # 라우트가 스레드로 넘긴 시점의 전역 상태를 그대로 기록한다.
        captured.update(kwargs)
        captured["_env_seen"] = os.environ.get("RECODER_PROJECT_ROOT", "")
        return {"decisions": [], "ops": [], "model": "fake-model"}

    workspace = tmp_path / "my-workspace"
    workspace.mkdir()

    # 전역이 원래 비어 있는 상태에서 시작 (다른 테스트 잔재 차단)
    monkeypatch.delenv("RECODER_PROJECT_ROOT", raising=False)

    if route == "plan":
        monkeypatch.setattr(code_agent, "generate_plan", _fake)
        asyncio.run(analyze_routes.code_plan_route(analyze_routes.CodePlanRequest(
            instruction="계획 세워줘",
            workspace_path=str(workspace),
        )))
    else:
        monkeypatch.setattr(code_agent, "generate_code", _fake)
        asyncio.run(analyze_routes.generate_code_route(analyze_routes.CodeGenerateRequest(
            instruction="만들어줘",
            workspace_path=str(workspace),
        )))

    captured["_env_after"] = os.environ.get("RECODER_PROJECT_ROOT", "")
    captured["_workspace"] = str(workspace)
    return captured


@pytest.mark.parametrize("route", ["generate", "plan"])
def test_route_does_not_leak_workspace_into_global_env(monkeypatch, tmp_path, route):
    """워크스페이스는 **인자로만** 전달되고 전역 env 는 건드리지 않는다."""
    captured = _run_route_and_capture_env(monkeypatch, tmp_path, route=route)

    # ① 경로는 인자로 제대로 넘어갔다
    assert captured["project_root"] == captured["_workspace"], (
        "워크스페이스 경로가 인자로 전달되지 않았다 — 폴백에 의존하게 된다"
    )

    # ② 전역 env 는 호출 중에도, 후에도 오염되지 않았다
    assert captured["_env_seen"] == "", (
        "라우트가 RECODER_PROJECT_ROOT 전역에 워크스페이스를 심었다 — "
        "동시 요청이 서로의 워크스페이스를 덮어쓰는 교차 오염 경로가 부활했다"
    )
    assert captured["_env_after"] == "", (
        "라우트 종료 후에도 RECODER_PROJECT_ROOT 전역이 남아 있다 — "
        "경로를 안 보낸 다음 요청이 이 값을 폴백으로 읽는다"
    )


def test_request_without_workspace_does_not_inherit_previous_workspace(monkeypatch, tmp_path):
    """[핵심] A 창 요청 뒤에 온 경로 없는 B 요청이 A 의 워크스페이스를 잡지 않는다.

    이것이 실제 사고 시나리오다. 전역 쓰기가 있으면 B 의 `project_root` 는
    비어 있고, code_agent._project_root() 가 A 가 심어둔 전역을 읽어
    **B 가 A 의 워크스페이스에 파일을 쓴다.**
    """
    import os

    from api.routes import analyze as analyze_routes

    monkeypatch.delenv("RECODER_PROJECT_ROOT", raising=False)

    workspace_a = tmp_path / "window-a"
    workspace_a.mkdir()

    seen: list[dict] = []

    def _fake(**kwargs):
        seen.append({
            "project_root": kwargs.get("project_root", ""),
            "env": os.environ.get("RECODER_PROJECT_ROOT", ""),
        })
        return {"decisions": [], "ops": [], "model": "fake-model"}

    monkeypatch.setattr(code_agent, "generate_code", _fake)

    # A 창: 워크스페이스를 명시해 요청
    asyncio.run(analyze_routes.generate_code_route(analyze_routes.CodeGenerateRequest(
        instruction="A 창 요청",
        workspace_path=str(workspace_a),
    )))

    # B 창: 워크스페이스 없이 요청 (폴더를 안 연 창)
    asyncio.run(analyze_routes.generate_code_route(analyze_routes.CodeGenerateRequest(
        instruction="B 창 요청",
    )))

    assert len(seen) == 2
    assert seen[0]["project_root"] == str(workspace_a)
    # B 는 A 의 경로를 인자로도, 전역으로도 물려받으면 안 된다
    assert seen[1]["project_root"] == "", "B 요청이 A 의 경로를 인자로 물려받았다"
    assert str(workspace_a) not in seen[1]["env"], (
        "B 요청 시점에 전역 env 가 A 의 워크스페이스를 가리킨다 — "
        "code_agent._project_root() 폴백이 이걸 읽어 B 가 A 의 폴더에 파일을 쓴다"
    )
