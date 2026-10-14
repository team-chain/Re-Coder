"""설계 결정(plan)은 구조화 출력(json_schema)으로 받는다 — 자유 텍스트 JSON 이 따옴표로 깨지던 문제."""
import json

import code_agent as ca
from llm.base import LLMError, LLMErrorType

PLAN = {"decisions": [{"id": "architecture", "question": "쇼핑몰의 \"기술\" 스택은?",
                       "options": [{"key": "node", "label": "Node.js + React", "pros": ["언어 통일"], "recommended": True},
                                   {"key": "python", "label": "Django"}], "impact": "구조"}]}


class Router:
    def __init__(self):
        self.requests = []

    def call(self, request, agent=None, operation=None, prefer="primary"):
        self.requests.append(request)
        text = json.dumps(PLAN, ensure_ascii=False)
        return type("R", (), {"text": text, "parsed": PLAN, "model_used": "m", "provider": "p"})()


def test_plan_requests_a_schema_and_parses_quotes_in_korean(monkeypatch, tmp_path):
    router = Router()
    monkeypatch.setattr(ca, "get_router", lambda: router)
    result = ca.generate_plan("쇼핑몰 사이트 하나 만들어줘", project_root=str(tmp_path))
    #: 새 앱은 주제(topics)를 먼저 받고, 카드는 같은 PLAN_SCHEMA 로 3개씩 받는다
    plan_requests = [r for r in router.requests if r.json_schema is ca.PLAN_SCHEMA]
    assert plan_requests and plan_requests[0].json_schema["properties"]["decisions"]["maxItems"] == 3
    keys = [d["id"] for d in result["decisions"]]
    assert "architecture" in keys
    assert any('"기술"' in d["question"] or "기술" in d["question"] for d in result["decisions"])


def test_truncated_structured_plan_retries_with_shorter_instruction(monkeypatch, tmp_path):
    calls = []

    class Cut(Router):
        def call(self, request, agent=None, operation=None, prefer="primary"):
            calls.append(request.prompt)
            if len(calls) == 1:
                raise LLMError("모델 출력이 응답 길이 제한에서 잘렸습니다.", LLMErrorType.STRUCTURED_OUTPUT)
            return super().call(request, agent, operation, prefer)

    monkeypatch.setattr(ca, "get_router", lambda: Cut())
    (tmp_path / "index.js").write_text("x")  # 기존 프로젝트 — 한 번에 받는 경로
    result = ca.generate_plan("쇼핑몰", project_root=str(tmp_path))
    assert len(calls) == 2 and "재시도" in calls[1] and "2개 이하" in calls[1]
    assert result["decisions"]
