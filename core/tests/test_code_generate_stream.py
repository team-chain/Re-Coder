"""/api/code/generate/stream — 시간 상한 없이 진행 이벤트, 멈추면 이어서 만들 수 있는 정보."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import code_agent as ca
from api.routes import analyze
from llm.base import LLMError, LLMErrorType
from tests.test_gen_engine import DECISION, Fake, files_for


def _app():
    app = FastAPI()
    app.include_router(analyze.router)
    return TestClient(app)


def _events(text):
    out = []
    for frame in text.split("\n\n"):
        data = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def _body(tmp_path, **kw):
    return {"instruction": "결제까지 되는 대형 쇼핑몰", "workspace_path": str(tmp_path), "decisions": [DECISION],
            "mode": "team", **kw}


def test_스트림은_진행_이벤트를_보내고_결과로_끝난다(monkeypatch, tmp_path):
    fake = Fake(files_for(6))
    monkeypatch.setattr(ca, "get_router", lambda: fake)
    res = _app().post("/api/code/generate/stream", json=_body(tmp_path, agents=2))
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    events = _events(res.text)
    steps = [e["step"] for e in events]
    assert steps[0] == "planning" and steps[-1] == "done"
    assert "planned" in steps and steps.count("file_done") == len(files_for(6))
    planned = next(e for e in events if e["step"] == "planned")
    assert len(planned["files"]) == len(files_for(6))
    agents = {e.get("agent") for e in events if e["step"] == "file_done"}
    assert {"agent-1", "agent-2"} <= agents  # 동시에 일한 에이전트가 보인다
    result = events[-1]["result"]
    assert {f["file"] for f in files_for(6)} <= {op["file"] for op in result["ops"]}
    assert all(e.get("job_id") == events[0]["job_id"] for e in events)


def test_멈추면_이어서_만들_정보를_주고_같은_작업으로_끝낸다(monkeypatch, tmp_path):
    files = files_for(6)
    quota = LLMError("gateway 429: 일일 토큰 한도를 초과했습니다.", LLMErrorType.QUOTA_EXCEEDED)
    monkeypatch.setattr(ca, "get_router", lambda f=Fake(files, fail_after=3, fail_exc=quota): f)
    events = _events(_app().post("/api/code/generate/stream", json=_body(tmp_path)).text)
    err = events[-1]
    assert err["step"] == "error" and err["resumable"] is True and err["resume_job"]
    assert 0 < err["done_count"] < err["total"] == len(files)
    second = Fake(files)
    monkeypatch.setattr(ca, "get_router", lambda: second)
    events = _events(_app().post("/api/code/generate/stream", json=_body(tmp_path, resume_job=err["resume_job"])).text)
    assert events[-1]["step"] == "done"
    assert any(e["step"] == "resumed" for e in events)
    assert "generate_code_manifest" not in second.ops


def test_예전_경로는_409로_이어서_만들_정보를_준다(monkeypatch, tmp_path):
    quota = LLMError("gateway 429: 일일 토큰 한도를 초과했습니다.", LLMErrorType.QUOTA_EXCEEDED)
    monkeypatch.setattr(ca, "get_router", lambda f=Fake(files_for(4), fail_after=2, fail_exc=quota): f)
    res = _app().post("/api/code/generate", json=_body(tmp_path))
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["resumable"] is True and detail["resume_job"] and "이어서 만들기" in detail["message"]
