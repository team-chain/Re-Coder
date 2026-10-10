"""새 앱은 설계 결정을 넉넉히(주제 → 카드 3개씩) 받는다 — 사용자 지적 2.0.6: "설계가 왜 이렇게 적게 나와"."""
import json
import re
import threading
from types import SimpleNamespace

import code_agent as ca
import payment_contract as pc

TOPICS = [{"id": f"t{i}", "question": f"질문 {i}", "impact": "영향"} for i in range(1, 8)]


class Router:
    def __init__(self, topics=TOPICS, fail_topics=False):
        self.topics, self.fail_topics, self.calls, self.lock = topics, fail_topics, [], threading.Lock()

    def call(self, req, agent=None, operation=None):
        with self.lock:
            self.calls.append(operation)
        if operation == "generate_plan_topics":
            if self.fail_topics:
                raise RuntimeError("down")
            return SimpleNamespace(text=json.dumps({"topics": self.topics}), model_used="m", provider="p")
        ids = re.findall(r"- id=(t\d+):", req.prompt)
        if not ids:  # 예전 방식(한 번에)
            ids = ["one"]
        return SimpleNamespace(text=json.dumps({"decisions": [
            {"id": i, "question": f"{i}?", "options": [{"key": "a", "label": "A", "recommended": True}, {"key": "b", "label": "B"}]}
            for i in ids]}), model_used="m", provider="p")


def test_새_앱은_주제를_먼저_받고_카드를_3개씩_나눠_받는다(tmp_path, monkeypatch):
    r = Router()
    monkeypatch.setattr(ca, "get_router", lambda: r)
    plan = ca.generate_plan("할 일 관리 앱을 만들어줘", project_root=str(tmp_path))
    ids = [d["id"] for d in plan["decisions"]]
    assert ids == [f"t{i}" for i in range(1, 8)]
    assert r.calls.count("generate_plan_topics") == 1 and r.calls.count("generate_plan") == 3


def test_결제_앱은_결제_카드_자리를_남긴다(tmp_path, monkeypatch):
    r = Router(topics=[{"id": f"t{i}", "question": f"q{i}"} for i in range(1, 12)])
    monkeypatch.setattr(ca, "get_router", lambda: r)
    plan = ca.generate_plan("실제 운영 가능한 쇼핑몰을 만들어줘", project_root=str(tmp_path), after_starter="custom")
    ids = [d["id"] for d in plan["decisions"]]
    assert ids[-1] == pc.PAYMENT_ID and len(ids) == ca.FULL_DESIGN_MAX + 1 and len(ids) <= ca.MAX_DECISIONS


def test_주제를_못_받으면_예전처럼_한_번에_받는다(tmp_path, monkeypatch):
    r = Router(fail_topics=True)
    monkeypatch.setattr(ca, "get_router", lambda: r)
    assert [d["id"] for d in ca.generate_plan("메모 앱", project_root=str(tmp_path))["decisions"]] == ["one"]


def test_기존_프로젝트_수정은_예전처럼(tmp_path, monkeypatch):
    (tmp_path / "index.js").write_text("x")
    r = Router()
    monkeypatch.setattr(ca, "get_router", lambda: r)
    ca.generate_plan("로그인 붙여줘", project_root=str(tmp_path))
    assert "generate_plan_topics" not in r.calls
