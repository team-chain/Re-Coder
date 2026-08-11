"""
P0-12 smoke #3: /api/analyze flow (핵심 결선 검증).

주의: 에러 추출 테스트는 예전에 `server.py` 의 함수를 가져다 썼다. 그런데
`server.py` 는 아무도 import 하지 않는 죽은 파일이라, **살아있는 경로에는
없는 기능을 테스트가 통과시키고 있었다.** 지금은 정식 위치인 `analyzer` 를
검사한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemas import AnalyzeRequest, FilePatch, PatchProposal, RiskLevel


def test_extract_error_text_traceback():
    from analyzer import _extract_error_text
    log = (
        "running uvicorn ...\n"
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
        "    import fastapi\n"
        "ModuleNotFoundError: No module named 'fastapi'\n"
    )
    out = _extract_error_text(log)
    assert "Traceback" in out, out
    # 예전 판은 들여쓰기가 끝나는 지점에서 끊어 **예외 줄을 통째로 잘라냈다.**
    # 기존 단언이 `or` 라서 앞 조건으로 통과해 이 결함을 못 잡았다.
    assert "ModuleNotFoundError: No module named 'fastapi'" in out, (
        "에러의 핵심(예외 타입·메시지)이 빠졌다 — 진단에 가장 필요한 줄이다"
    )


def test_extract_error_text_ts_compile():
    from analyzer import _extract_error_text
    log = "src/index.ts:10:5\nerror TS2322: Type 'string' is not assignable to type 'number'.\n"
    out = _extract_error_text(log)
    assert "TS2322" in out


def test_extract_error_text_fallback_to_last_30_lines():
    from analyzer import _extract_error_text
    # 패턴이 매치되지 않는 일반 출력 → 마지막 30 줄 폴백 (50 lines, last 30 = 20..49)
    log = "\n".join(f"info {i}" for i in range(50))
    out = _extract_error_text(log)
    assert "info 49" in out
    assert "info 20" in out
    assert "info 0\n" not in out


# ---------------------------------------------------------------------------
# [회귀] analyzer 가 사라진 스키마 필드를 읽어 즉사하던 문제 + 원문 통째 전송
#
# `AnalyzeRequest` 에서 error_text·file_context 가 제거됐는데 analyzer 는 계속
# 그 필드를 읽었다. 그것도 LLM 호출 try 블록 **밖**이라 폴백조차 안 걸렸다.
# 살아있는 호출처(MCP 도구 recoder_analyze, 릴레이 analyze 명령)가 전부
# 에러 응답만 뱉고 있었다.
# ---------------------------------------------------------------------------

def _long_build_log(error_tail: str) -> str:
    return "\n".join(f"info {i}" for i in range(300)) + "\n$ pytest -q\n" + error_tail


def test_build_prompt_does_not_crash_on_current_schema():
    """[회귀] 현재 스키마의 요청으로 프롬프트를 만들 수 있어야 한다."""
    import analyzer

    req = AnalyzeRequest(
        workspace_path="/tmp/proj",
        terminal_output="Traceback (most recent call last):\nValueError: boom",
        selected_text="x = 1\n",
        command="pytest",
    )
    # 예전 코드는 여기서 AttributeError 로 죽었다.
    prompt = analyzer._build_prompt(req)
    assert "ValueError: boom" in prompt
    assert "[선택한 코드]" in prompt and "x = 1" in prompt


def test_build_prompt_shrinks_long_terminal_output():
    """[회귀] 긴 빌드 로그는 에러 중심으로 줄어든다 (원문 통째 금지)."""
    import analyzer

    log = _long_build_log(
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
        "    import fastapi\n"
        "ModuleNotFoundError: No module named 'fastapi'\n"
    )
    req = AnalyzeRequest(workspace_path="/tmp/proj", terminal_output=log)
    prompt = analyzer._build_prompt(req)

    # ① 에러 본문은 살아 있다
    assert "ModuleNotFoundError" in prompt

    # ② 노이즈(로그 앞부분)는 실리지 않는다
    assert "info 0" not in prompt, "로그 앞부분이 그대로 프롬프트에 실렸다"
    assert "info 100" not in prompt

    # ③ 직전 맥락(실행한 명령)은 남는다 — 진단에 필요하다
    assert "$ pytest -q" in prompt, "에러 직전 맥락까지 잘려나갔다"

    # ④ 전체적으로 원문보다 확실히 짧다
    assert len(prompt) < len(log), (len(prompt), len(log))


def test_build_prompt_keeps_context_lines_before_error():
    """에러 본문 앞 N줄이 함께 실린다."""
    import analyzer

    log = "\n".join([f"step {i}" for i in range(40)]) + "\nValueError: boom\n"
    req = AnalyzeRequest(workspace_path="/tmp/proj", terminal_output=log)
    prompt = analyzer._build_prompt(req)

    assert "ValueError: boom" in prompt
    assert "step 39" in prompt, "에러 직전 줄이 빠졌다"
    assert "step 0" not in prompt, "앞부분까지 다 실렸다"


def test_analyze_uses_terminal_output_for_fingerprint(monkeypatch):
    """[회귀] analyze() 가 사라진 error_text 필드를 읽지 않는다.

    예전 코드는 `request.error_text` 를 읽어 **호출 즉시** AttributeError 로
    죽었다. 여기서는 LLM 을 가짜로 바꿔 그 지점을 지나가는지만 본다.
    """
    import asyncio

    import analyzer

    class _Resp:
        text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "error_detected"}'
        model_used = "fake"

    class _Router:
        def call(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    req = AnalyzeRequest(
        workspace_path="/tmp/proj",
        terminal_output="Traceback (most recent call last):\nValueError: boom",
    )
    event = asyncio.run(analyzer.analyze(req, session_id="s1"))
    assert event is not None
    # 에러 본문이 terminal_output 에서 뽑혀 이벤트에 담긴다
    assert "ValueError" in event.error_text
    assert event.raw_errors and "ValueError" in event.raw_errors[0]
    assert "boom" in event.summary
    # 직렬화까지 되어야 MCP·릴레이가 결과를 돌려줄 수 있다
    assert isinstance(event.to_dict(), dict)


def test_analyze_applies_context_gate_masking(monkeypatch):
    """[회귀] Context Gate 의 마스킹 결과가 실제로 반영된다.

    `run_gate` 는 async 인데 예전 코드는 await 없이 불러 코루틴을 받았고,
    바로 다음 줄에서 터져 except 로 빠졌다. 그 결과 **마스킹이 통째로
    스킵**돼 시크릿·PII 가 그대로 LLM 으로 갔다. 로그에만 남고 결과에는
    흔적이 없어 알아채기 어려웠다 — 그래서 여기서 못 박는다.
    """
    import asyncio

    import analyzer

    secret = "AKIAIOSFODNN7EXAMPLE"
    raw = f"export AWS_ACCESS_KEY_ID={secret}\nValueError: boom"
    masked = "export AWS_ACCESS_KEY_ID=[REDACTED]\nValueError: boom"

    class _Gate:
        text = masked
        quality_score = 0.9

    async def _fake_run_gate(_text, *a, **kw):
        return _Gate()

    seen_prompts: list[str] = []

    class _Resp:
        text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "error_detected"}'
        model_used = "fake"

    class _Router:
        def call(self, req, *a, **kw):
            seen_prompts.append(req.prompt)
            return _Resp()

    monkeypatch.setattr(analyzer, "run_gate", _fake_run_gate)
    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    req = AnalyzeRequest(workspace_path="/tmp/proj", terminal_output=raw)
    event = asyncio.run(analyzer.analyze(req, session_id="s2"))

    assert seen_prompts, "LLM 이 호출되지 않았다"
    assert secret not in seen_prompts[0], (
        "마스킹 전 원문이 LLM 프롬프트에 실렸다 — Context Gate 가 무시됐다"
    )
    assert "[REDACTED]" in seen_prompts[0]
    # 이벤트에 남는 컨텍스트도 마스킹된 본문이어야 한다
    assert all(secret not in c for c in event.contexts), event.contexts


def test_analyze_request_dataclass_round_trip():
    # `AnalyzeRequest` 는 확장이 보내는 분석 페이로드다. 예전 테스트는
    # error_text·related_files·file_context 를 검사했는데, 그 필드들은 지금
    # 모델에 없다(pydantic 이 조용히 무시). 현재 모델의 실제 필드로 왕복한다.
    req = AnalyzeRequest(
        workspace_path="/tmp/proj",
        terminal_output="ModuleNotFoundError: No module named 'foo'",
        selected_text="import foo\n",
        command="pytest",
    )
    d = req.to_dict()
    assert d["workspace_path"] == "/tmp/proj"
    assert d["terminal_output"].startswith("ModuleNotFoundError")
    assert d["selected_text"] == "import foo\n"
    assert d["command"] == "pytest"


def test_patch_proposal_to_dict_shape():
    fp = FilePatch(
        file="main.py",
        base_sha256="0" * 64,
        unified_diff="--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-import foo\n+import bar\n",
        reason="rename",
    )
    proposal = PatchProposal(
        proposal_id="abc",
        summary="rename foo to bar",
        risk_level=RiskLevel.LOW,
        test_command="pytest -q",
        patches=[fp],
        approval_level=1,
    )
    d = proposal.to_dict()
    assert d["proposal_id"] == "abc"
    assert d["approval_level"] == 1
    assert isinstance(d["patches"], list) and len(d["patches"]) == 1
    assert d["patches"][0]["file"] == "main.py"
    assert d["risk_level"] == "low"


# ---------------------------------------------------------------------------
# [Codex P1] 마스킹·캐시 3건 — analyzer 가 LLM 에 보내는 모든 텍스트 필드를
# 가리고, 게이트 실패 시 fail-closed 하며, 캐시가 요청 범위로 좁혀지는지.
# ---------------------------------------------------------------------------

_SECRET = "AKIAIOSFODNN7EXAMPLE"


def _fake_gate_factory():
    """입력에 든 AKIA 키를 실제로 가리는 가짜 run_gate (필드별 검증용)."""
    async def _fake_run_gate(text, *a, **kw):
        class _G:
            pass
        g = _G()
        g.text = (text or "").replace(_SECRET, "[MASKED_AWS_KEY]")
        g.quality_score = 0.9
        return g
    return _fake_run_gate


class _AnalyzeResp:
    text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "error_detected"}'
    model_used = "fake"


def _router_capturing(seen_prompts):
    class _Router:
        def call(self, req, *a, **kw):
            seen_prompts.append(req.prompt)
            return _AnalyzeResp()
    return _Router()


def test_analyze_masks_selected_text_and_project_summary(monkeypatch):
    """[Codex P1-①] terminal_output 뿐 아니라 selected_text·project_files_summary
    도 마스킹돼야 한다. 자격증명 파일을 '선택'해 보내면 그 내용이 새면 안 된다."""
    import asyncio
    import analyzer

    seen: list[str] = []
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing(seen))

    req = AnalyzeRequest(
        workspace_path="/tmp/proj",
        terminal_output="ValueError: boom",
        selected_text=f"aws_access_key_id = {_SECRET}",
        project_files_summary=f"config: {_SECRET}",
    )
    event = asyncio.run(analyzer.analyze(req, session_id="s1"))

    assert seen, "LLM 이 호출되지 않았다"
    prompt = seen[0]
    assert _SECRET not in prompt, "selected_text/project_files_summary 로 시크릿이 샜다"
    assert "[MASKED_AWS_KEY]" in prompt
    # 이벤트에 남는 컨텍스트에도 원문 시크릿이 없어야 한다
    assert all(_SECRET not in c for c in event.contexts), event.contexts


def test_analyze_fails_closed_when_gate_raises(monkeypatch):
    """[Codex P1-②] run_gate 가 터지면 원문을 흘리지 않고 정규식 폴백으로 가린다."""
    import asyncio
    import analyzer

    async def _boom(*a, **kw):
        raise RuntimeError("executor down")

    seen: list[str] = []
    monkeypatch.setattr(analyzer, "run_gate", _boom)
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing(seen))

    req = AnalyzeRequest(
        workspace_path="/tmp/proj",
        terminal_output=f"export AWS_ACCESS_KEY_ID={_SECRET}\nValueError: boom",
        selected_text=f"key={_SECRET}",
    )
    asyncio.run(analyzer.analyze(req, session_id="s1"))

    assert seen, "LLM 이 호출되지 않았다"
    prompt = seen[0]
    # 게이트가 죽었어도 원문 시크릿이 프롬프트에 실리면 안 된다 (fail-closed)
    assert _SECRET not in prompt, (
        "게이트 실패 시 마스킹 안 된 원문이 LLM 으로 갔다 — fail-open 회귀"
    )
    assert "[MASKED_AWS_KEY]" in prompt


def _counting_router(counter):
    class _Router:
        def call(self, req, *a, **kw):
            counter[0] += 1
            return _AnalyzeResp()
    return _Router()


def test_different_context_does_not_share_llm_cache(monkeypatch):
    """[Codex P1-③] 프롬프트가 다르면(선택 코드 등) LLM 캐시를 공유하지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    calls = [0]
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _counting_router(calls))

    req_a = AnalyzeRequest(workspace_path="/home/alice/p", terminal_output="ValueError: boom",
                           selected_text="alice_secret_context")
    req_b = AnalyzeRequest(workspace_path="/home/bob/p", terminal_output="ValueError: boom",
                           selected_text="bob_secret_context")
    ev_a = asyncio.run(analyzer.analyze(req_a, session_id="a"))
    ev_b = asyncio.run(analyzer.analyze(req_b, session_id="b"))

    assert ev_a is not ev_b, "이벤트는 매 호출 새로 만들어져야 한다"
    assert calls[0] == 2, "프롬프트가 다른데 LLM 캐시를 공유했다"
    assert all("alice_secret_context" not in c for c in ev_b.contexts), ev_b.contexts
    assert analyzer._llm_cache_key(req_a) != analyzer._llm_cache_key(req_b)


def test_identical_prompt_reuses_llm_but_rebuilds_event(monkeypatch):
    """[Codex P2] 프롬프트가 같으면 LLM 은 1회만 부르되, event_id·트리거는
    세션/명령에 맞춰 매 호출 새로 만든다 — 남의 세션/명령 메타를 물려받지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    calls = [0]
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _counting_router(calls))

    # 같은 터미널(=같은 프롬프트), 다른 세션 + 다른 command
    base = dict(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    ev1 = asyncio.run(analyzer.analyze(AnalyzeRequest(**base, command="ls"), session_id="sess-1"))
    ev2 = asyncio.run(analyzer.analyze(AnalyzeRequest(**base, command="curl x"), session_id="sess-2"))

    # LLM 은 한 번만 (프롬프트 동일 → 분석 재사용)
    assert calls[0] == 1, "동일 프롬프트인데 LLM 을 두 번 불렀다 — dedup 이 깨졌다"
    # 하지만 이벤트는 서로 달라야 한다 (event_id 가 세션·시각 기반)
    assert ev1.event_id != ev2.event_id, "다른 세션인데 event_id 를 물려받았다"
    assert ev1.event_id.startswith("sess-1_") and ev2.event_id.startswith("sess-2_")
    # command 가 다르면 트리거 사유도 달라야 한다 (stale 메타 금지)
    reasons2 = [r.get("type") for r in ev2.trigger_reasons]
    assert "new_terminal_command" in reasons2, ev2.trigger_reasons


def test_command_is_masked_in_trigger_reasons(monkeypatch):
    """[Codex P1-①] command 에 든 자격증명이 트리거 사유(matched)로 새지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing([]))

    req = AnalyzeRequest(
        workspace_path="/tmp/p",
        terminal_output="ValueError: boom",
        command=f"curl -H 'Authorization: {_SECRET}' https://api",
    )
    event = asyncio.run(analyzer.analyze(req, session_id="s"))
    dumped = json.dumps(event.to_dict(), ensure_ascii=False)
    assert _SECRET not in dumped, (
        "command 의 자격증명이 트리거 사유를 통해 to_dict() 로 샜다"
    )


def test_gate_fallback_anonymizes_paths(monkeypatch):
    """[Codex P1-②] 게이트 실패 폴백도 절대경로를 익명화한다 (시크릿만이 아니라)."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()

    async def _boom(*a, **kw):
        raise RuntimeError("executor down")

    seen: list[str] = []
    monkeypatch.setattr(analyzer, "run_gate", _boom)
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing(seen))

    # 절대 경로가 든 로그 — 정상 게이트라면 anonymize_paths 로 지운다
    req = AnalyzeRequest(
        workspace_path="/tmp/p",
        terminal_output="Traceback:\n  File \"/home/alice/secret_project/main.py\", line 3\nValueError: boom",
    )
    asyncio.run(analyzer.analyze(req, session_id="s"))

    assert seen, "LLM 이 호출되지 않았다"
    assert "/home/alice/secret_project" not in seen[0], (
        "폴백이 경로를 익명화하지 않아 사용자 절대경로가 LLM 으로 갔다"
    )


def test_analyze_survives_malformed_importance_score(monkeypatch):
    """[P2] LLM 이 importance_score 를 null/문자열로 줘도 크래시하지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _BadResp:
        text = '{"has_error": true, "error_summary": "boom", "importance_score": "high", "event_type": "error_detected"}'
        model_used = "fake"

    class _Router:
        def call(self, *a, **kw):
            return _BadResp()

    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    event = asyncio.run(analyzer.analyze(req, session_id="s"))
    # 크래시 대신 기본값으로 떨어진다
    assert 0 <= event.importance_score <= 100
