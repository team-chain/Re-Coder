"""4.0 진행 흐름 — 단계 기록과 화면용 상태."""
from __future__ import annotations

import asyncio

import pytest

import journey


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))


def _steps(state):
    return {s["key"]: s["status"] for s in state["steps"]}


def test_empty_folder_starts_at_build_and_locks_the_rest(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    st = journey.state(ws)
    assert st["available"] and st["next"] == "build"
    assert _steps(st) == {"build": "todo", "verify": "locked", "security": "locked", "local": "locked", "redteam": "locked", "release": "locked"}


def test_steps_advance_and_rebuild_marks_later_results_stale(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "server.js").write_text("x", encoding="utf-8")
    assert journey.state(ws)["next"] == "verify"  # 코드가 있으면 만들기는 끝난 것으로 본다
    journey.record(ws, "verify", "done", "통과")
    journey.record(ws, "security", "done", "점수 90")
    journey.record(ws, "local", "done", "http://localhost:3000")
    st = journey.state(ws)
    assert st["next"] == "redteam" and _steps(st)["local"] == "done"
    journey.record(ws, "build", "done", "다시 생성")
    st = journey.state(ws)
    assert _steps(st)["security"] == "stale" and _steps(st)["local"] == "stale" and st["next"] == "security"
    journey.record(ws, "nope", "done")  # 모르는 단계는 무시
    assert "nope" not in journey.load(ws)


def test_multi_service_project_blocks_aws_release_and_reads_stack_history(tmp_path, monkeypatch):
    import compose_deploy

    ws = tmp_path / "shop"
    ws.mkdir()
    (ws / "docker-compose.yml").write_text("services:\n  a:\n    build: ./a\n  b:\n    build: ./b\n", encoding="utf-8")
    (ws / "a.js").write_text("x", encoding="utf-8")
    st = journey.state(ws)
    assert st["multi"] and _steps(st)["release"] == "blocked" and "다음 버전" in st["steps"][-1]["summary"]
    monkeypatch.setattr(compose_deploy, "load", lambda w: {"services": [{"name": "a"}, {"name": "b"}], "databases": []})
    monkeypatch.setattr(compose_deploy, "history", lambda w: [{"status": "success", "services": ["a", "b"], "at": "t"}])
    monkeypatch.setattr("contract_check.check_workspace", lambda w: {"summary": {"error": 0}})
    st = journey.state(ws)
    assert _steps(st)["local"] == "done" and st["services"] == 2


def test_routes_read_state_and_accept_only_client_steps(tmp_path):
    from fastapi import HTTPException
    from api.routes import deploy as routes

    ws = tmp_path / "ws"
    ws.mkdir()
    assert asyncio.run(routes.project_journey(str(ws)))["next"] == "build"
    asyncio.run(routes.project_journey_record(routes.JourneyRecordRequest(workspace_path=str(ws), step="redteam", status="done", summary="공격 테스트 완료")))
    assert journey.load(ws)["redteam"]["status"] == "done"
    with pytest.raises(HTTPException):
        asyncio.run(routes.project_journey_record(routes.JourneyRecordRequest(workspace_path=str(ws), step="release", status="done")))


def test_code_scan_and_generation_record_their_steps(tmp_path):
    import code_agent
    from api.routes import security_checks

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.js").write_text("const q = 'SELECT * FROM t WHERE id=' + req.query.id; db.query(q);\n", encoding="utf-8")
    asyncio.run(security_checks.code_scan(security_checks.CodeScanRequest(workspace_path=str(ws))))
    assert journey.load(ws)["security"]["status"] in ("done", "failed")
    code_agent._record_journey(str(ws), "", {"ops": [{"file": "a"}], "verification": {"kind": "compose", "status": "failed", "output": "검사 3/5"}})
    data = journey.load(ws)
    assert data["build"]["status"] == "done" and data["verify"]["status"] == "failed" and "검사 3/5" in data["verify"]["summary"]
