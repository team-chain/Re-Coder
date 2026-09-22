"""
릴레이 analyze 핸들러 회귀 — error_text 페이로드가 유실되지 않는지.

릴레이는 에러 본문을 `error_text` 로만 보낼 수 있는데, AnalyzeRequest 스키마엔
그 필드가 없어 조용히 버려졌다. 그 결과 analyzer 가 빈 문자열을 분석해
"에러 없음"을 성공으로 돌려줬다(Codex P1). 핸들러가 error_text 를
terminal_output 으로 넘겨주는지 검증한다.
"""
from __future__ import annotations

import asyncio

import pytest


def test_relay_analyze_maps_error_text_into_terminal_output(monkeypatch):
    import analyzer
    from relay import poller

    captured: dict = {}

    async def _fake_analyze(request, session_id=""):
        captured["terminal_output"] = request.terminal_output
        captured["session_id"] = session_id

        class _Ev:
            def to_dict(self):
                return {"ok": True}
        return _Ev()

    monkeypatch.setattr(analyzer, "analyze", _fake_analyze)

    result = asyncio.run(poller._run_handler("analyze", {
        "workspace_path": "/tmp/p",
        "error_text": "Traceback:\nValueError: boom",   # terminal_output 없음
        "session_id": "s1",
    }))

    assert result["status"] == "ok", result
    # error_text 가 버려지지 않고 분석 대상이 되어야 한다
    assert "ValueError: boom" in captured["terminal_output"], (
        "릴레이 error_text 가 유실됐다 — 빈 문자열을 분석하게 된다"
    )


def test_relay_analyze_prefers_terminal_output_when_both_present(monkeypatch):
    import analyzer
    from relay import poller

    captured: dict = {}

    async def _fake_analyze(request, session_id=""):
        captured["terminal_output"] = request.terminal_output

        class _Ev:
            def to_dict(self):
                return {"ok": True}
        return _Ev()

    monkeypatch.setattr(analyzer, "analyze", _fake_analyze)

    asyncio.run(poller._run_handler("analyze", {
        "workspace_path": "/tmp/p",
        "terminal_output": "real terminal output",
        "error_text": "fallback only",
        "session_id": "s1",
    }))

    assert captured["terminal_output"] == "real terminal output"
