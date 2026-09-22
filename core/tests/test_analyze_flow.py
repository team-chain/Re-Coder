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


def test_short_command_is_preserved_not_emptied(monkeypatch):
    """[Codex P2] 'ls'·'pwd' 같은 짧은(5자 미만) 명령이 게이트에 비워지지 않는다.

    run_gate 는 실제 것을 써서 짧은 입력을 비우는 동작을 재현하되, command 는
    _scrub 경로라 보존돼야 한다. 보존 안 되면 new_terminal_command 트리거 사유가
    통째로 사라진다.
    """
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    seen: list[str] = []
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing(seen))

    req = AnalyzeRequest(
        workspace_path="/tmp/p",
        terminal_output="Traceback:\nValueError: this is long enough to survive the gate",
        command="ls",
    )
    event = asyncio.run(analyzer.analyze(req, session_id="s"))

    reason_types = [r.get("type") for r in event.trigger_reasons]
    assert "new_terminal_command" in reason_types, (
        f"짧은 명령 'ls' 가 게이트에 비워져 트리거 사유가 사라졌다: {event.trigger_reasons}"
    )
    matched = [r.get("matched") for r in event.trigger_reasons
               if r.get("type") == "new_terminal_command"]
    assert matched and "ls" in matched[0]


# ---------------------------------------------------------------------------
# [Codex P2 3건] 초장문 1줄 캡 · event_id 유일성 · 릴레이 project_summary
# ---------------------------------------------------------------------------

def test_single_long_line_is_char_capped():
    """[P2①] 에러 패턴 없는 초장문 1줄도 문자 상한으로 잘린다."""
    import analyzer

    giant = "x" * 50_000  # 한 줄 50KB (줄 컷으로는 안 잘림)
    out = analyzer._extract_error_text(giant)
    assert len(out) <= analyzer._MAX_CHARS + 40, len(out)

    prompt = analyzer._build_prompt(
        AnalyzeRequest(workspace_path="/tmp/p", terminal_output=giant)
    )
    # 프롬프트 전체도 원문(50KB)보다 훨씬 작아야 한다
    assert len(prompt) < 20_000, len(prompt)


def test_event_ids_are_unique_within_same_session(monkeypatch):
    """[P2②] 같은 세션 1초 내 두 분석도 event_id 가 겹치지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _router_capturing([]))

    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    ev1 = asyncio.run(analyzer.analyze(AnalyzeRequest(**req.model_dump()), session_id="s"))
    ev2 = asyncio.run(analyzer.analyze(AnalyzeRequest(**req.model_dump()), session_id="s"))
    assert ev1.event_id != ev2.event_id, "같은 세션 이벤트가 같은 id 를 받았다"


def test_relay_forwards_project_files_summary(monkeypatch):
    """[P2③] 릴레이가 project_files_summary 를 analyzer 로 넘긴다."""
    import asyncio
    import analyzer
    from relay import poller

    captured: dict = {}

    async def _fake(request, session_id=""):
        captured["summary"] = request.project_files_summary

        class _Ev:
            def to_dict(self):
                return {}
        return _Ev()

    monkeypatch.setattr(analyzer, "analyze", _fake)
    asyncio.run(poller._run_handler("analyze", {
        "workspace_path": "/tmp/p",
        "terminal_output": "ValueError: boom",
        "project_files_summary": "FastAPI + SQLite 프로젝트",
    }))
    assert captured["summary"] == "FastAPI + SQLite 프로젝트"


# ---------------------------------------------------------------------------
# [Codex P2 4건] 폴백 event_type 유효화 · 정상출력을 에러로 직렬화 금지 ·
#                모든 컨텍스트 필드 캡 · 동시 LLM 호출 중복 제거
# ---------------------------------------------------------------------------

def test_llm_failure_fallback_returns_valid_event_no_error(monkeypatch):
    """[P2①] LLM 실패 + 에러 없는 컨텍스트에서도 크래시 없이 이벤트를 돌려준다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _BoomRouter:
        def call(self, *a, **kw):
            raise RuntimeError("provider down")

    monkeypatch.setattr(analyzer, "get_router", lambda: _BoomRouter())

    # 에러 패턴 없는 선택 코드 위주 요청
    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="", selected_text="x = 1")
    event = asyncio.run(analyzer.analyze(req, session_id="s"))
    assert event is not None
    # 예전엔 EventType.UNKNOWN 접근으로 AttributeError 가 났다
    assert event.event_type is not None


def test_invalid_event_type_from_llm_does_not_crash():
    """[P2①] LLM 이 EventType 에 없는 값을 줘도 _parse_event_type 이 크래시하지 않는다.

    예전 except 분기는 존재하지 않는 EventType.UNKNOWN 에 접근해 AttributeError
    로 죽었다. 그 분기를 직접 찌른다.
    """
    import analyzer
    from schemas import EventType

    assert not hasattr(EventType, "UNKNOWN")  # 이 전제가 깨지면 버그 자체가 없어진 것
    result = analyzer._parse_event_type("totally_bogus_value")
    assert isinstance(result, EventType)


def test_normal_output_not_serialized_as_error(monkeypatch):
    """[P2②] LLM 이 has_error=false 로 보면 정상 출력을 error_text/raw_errors 에 안 넣는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _NoErrResp:
        text = '{"has_error": false, "error_summary": "빌드 성공", "importance_score": 10, "event_type": "task_change"}'
        model_used = "fake"

    class _Router:
        def call(self, *a, **kw):
            return _NoErrResp()

    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    # 에러 패턴 없는 평범한 출력 → _extract_error_text 는 마지막 30줄을 주지만
    # LLM 이 에러 아님으로 분류하면 이벤트엔 안 실려야 한다
    req = AnalyzeRequest(workspace_path="/tmp/p",
                         terminal_output="\n".join(f"line {i}" for i in range(40)))
    event = asyncio.run(analyzer.analyze(req, session_id="s"))
    assert event.error_text == "", event.error_text
    assert event.raw_errors == [], event.raw_errors


def test_large_selected_text_is_bounded_in_prompt():
    """[P2③] 거대한 selected_text·project_files_summary 도 프롬프트에서 상한이 걸린다."""
    import analyzer

    huge = "A" * 200_000
    prompt = analyzer._build_prompt(AnalyzeRequest(
        workspace_path="/tmp/p",
        terminal_output="ValueError: boom",
        selected_text=huge,
        project_files_summary=huge,
    ))
    # 두 필드 합쳐 40만자였는데 프롬프트가 그 근처면 캡이 안 걸린 것
    assert len(prompt) < 20_000, len(prompt)


def test_concurrent_same_prompt_calls_llm_once(monkeypatch):
    """[P2④] 같은 프롬프트의 동시 요청은 provider 를 한 번만 부른다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    analyzer._llm_inflight.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    calls = [0]

    class _SlowResp:
        text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "error_detected"}'
        model_used = "fake"

    class _Router:
        def call(self, *a, **kw):
            calls[0] += 1
            return _SlowResp()

    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    async def _run():
        base = dict(workspace_path="/tmp/p", terminal_output="ValueError: boom")
        # 같은 프롬프트로 동시 3건
        return await asyncio.gather(*[
            analyzer.analyze(AnalyzeRequest(**base), session_id=f"s{i}")
            for i in range(3)
        ])

    events = asyncio.run(_run())
    assert len(events) == 3
    assert calls[0] == 1, f"동시 동일 프롬프트인데 provider 를 {calls[0]}회 불렀다"
    # 이벤트는 각각 고유해야 한다(캐시로 합쳐도 이벤트는 새로)
    assert len({e.event_id for e in events}) == 3


# ---------------------------------------------------------------------------
# [자체검수 P1/P2] dedup 취소 hang · 총량 캡 · 폴백 event_type
# ---------------------------------------------------------------------------

def test_dedup_leader_cancel_does_not_hang_waiter(monkeypatch):
    """[P1] dedup 리더가 취소돼도 같은 키 대기자가 hang 하지 않는다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    analyzer._llm_inflight.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    started = asyncio.Event() if False else None  # placeholder

    class _BlockingRouter:
        def call(self, *a, **kw):
            import time as _t
            _t.sleep(0.3)  # 리더를 to_thread 안에 붙잡아 둔다
            class _R:
                text = '{"has_error": true, "error_summary": "x", "importance_score": 10, "event_type": "error_detected"}'
                model_used = "fake"
            return _R()

    monkeypatch.setattr(analyzer, "get_router", lambda: _BlockingRouter())

    async def _run():
        base = dict(workspace_path="/tmp/p", terminal_output="ValueError: boom")
        leader = asyncio.create_task(analyzer.analyze(AnalyzeRequest(**base), session_id="L"))
        await asyncio.sleep(0.05)  # 리더가 future 등록 + to_thread 진입하도록
        waiter = asyncio.create_task(analyzer.analyze(AnalyzeRequest(**base), session_id="W"))
        await asyncio.sleep(0.05)  # 대기자가 inflight future 를 await 하도록
        leader.cancel()            # 리더 취소
        # 대기자는 hang 하지 않고 **종결**돼야 한다. wait_for 가 타임아웃하면
        # = 대기자가 orphan future 에 걸려 hang 한 것 → 실패로 본다.
        try:
            await asyncio.wait_for(waiter, timeout=2.0)
        except asyncio.TimeoutError:
            waiter.cancel()
            raise AssertionError("대기자가 hang 했다 — dedup future 가 고아가 됐다")
        except (asyncio.CancelledError, Exception):
            pass  # 취소 전파 또는 자체 완료 = hang 아님 (성공)
        return True

    assert asyncio.run(_run()) is True


def test_total_prompt_capped_across_fields():
    """[P2] 필드가 여럿이어도 프롬프트 컨텍스트 총량이 상한을 넘지 않는다."""
    import analyzer

    # 에러 패턴이 없는 거대 blob — 추출로 짧아지지 않아 필드별 4000 캡이 걸린다.
    # 그런 필드 4개(에러텍스트/선택/프로젝트/터미널)면 총량 캡 없이는 ~16000자.
    huge = "B" * 100_000
    prompt = analyzer._build_prompt(AnalyzeRequest(
        workspace_path="/tmp/p",
        terminal_output=huge,
        selected_text=huge,
        project_files_summary=huge,
    ))
    # 총량 캡(_MAX_PROMPT_CHARS=8000) + 스키마·라벨 여유. 캡 없으면 16000+ 이라 실패.
    assert len(prompt) < analyzer._MAX_PROMPT_CHARS + 3000, len(prompt)
    # 스키마는 잘리지 않고 온전히 남아야 한다
    assert "[응답 형식]" in prompt
    assert "has_error" in prompt


def test_invalid_event_type_with_error_falls_back_to_error_detected(monkeypatch):
    """[낮음] has_error=true 인데 event_type 이 enum 밖이면 ERROR_DETECTED 로 떨어진다."""
    import asyncio
    import analyzer
    from schemas import EventType

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _Resp:
        text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "nonsense"}'
        model_used = "fake"

    monkeypatch.setattr(analyzer, "get_router", lambda: type("R", (), {"call": lambda self, *a, **k: _Resp()})())

    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    event = asyncio.run(analyzer.analyze(req, session_id="s"))
    assert event.event_type == EventType.ERROR_DETECTED, event.event_type


def test_non_dict_llm_response_does_not_crash(monkeypatch):
    """[Codex P2 크리티컬] provider 가 객체 아닌 JSON(null/[])을 줘도 크래시 안 함."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    for bad in ("null", "[]", "123", '"just a string"'):
        class _Resp:
            text = bad
            model_used = "fake"
        monkeypatch.setattr(analyzer, "get_router",
                            lambda r=_Resp: type("R", (), {"call": lambda self, *a, **k: r()})())
        analyzer._llm_cache.clear()
        req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
        event = asyncio.run(analyzer.analyze(req, session_id="s"))
        # 크래시 대신 폴백 이벤트가 나와야 한다
        assert event is not None, bad
        assert isinstance(event.to_dict(), dict), bad


def test_waiter_cancellation_does_not_kill_other_waiters(monkeypatch):
    """[Codex P2] 대기자 하나가 취소돼도 다른 대기자는 정상 완료한다(shield)."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    analyzer._llm_inflight.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _SlowRouter:
        def call(self, *a, **kw):
            import time as _t
            _t.sleep(0.3)
            class _R:
                text = '{"has_error": true, "error_summary": "x", "importance_score": 10, "event_type": "error_detected"}'
                model_used = "fake"
            return _R()

    monkeypatch.setattr(analyzer, "get_router", lambda: _SlowRouter())

    async def _run():
        base = dict(workspace_path="/tmp/p", terminal_output="ValueError: boom")
        leader = asyncio.create_task(analyzer.analyze(AnalyzeRequest(**base), session_id="L"))
        await asyncio.sleep(0.05)
        w1 = asyncio.create_task(analyzer.analyze(AnalyzeRequest(**base), session_id="W1"))
        w2 = asyncio.create_task(analyzer.analyze(AnalyzeRequest(**base), session_id="W2"))
        await asyncio.sleep(0.05)
        w1.cancel()  # 대기자 하나만 취소
        # 리더와 남은 대기자는 정상 완료해야 한다
        results = await asyncio.gather(leader, w2, return_exceptions=True)
        return results

    results = asyncio.run(_run())
    assert all(not isinstance(r, Exception) for r in results), (
        f"대기자 취소가 다른 요청까지 죽였다: {results}"
    )
    assert results[0].event_id != results[1].event_id


def test_scalar_suggested_actions_does_not_crash(monkeypatch):
    """[Codex P2 크리티컬] suggested_actions 가 스칼라(정수)여도 analyze() 안 터짐."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    class _Resp:
        # 스키마 위반: suggested_actions 가 리스트가 아니라 정수
        text = '{"has_error": true, "error_summary": "boom", "importance_score": 50, "event_type": "error_detected", "suggested_actions": 1}'
        model_used = "fake"

    monkeypatch.setattr(analyzer, "get_router",
                        lambda: type("R", (), {"call": lambda self, *a, **k: _Resp()})())

    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    event = asyncio.run(analyzer.analyze(req, session_id="s"))  # 예전엔 TypeError
    assert event is not None
    assert event.suggested_actions == []


# ── [회귀] analyzer 후속 품질 P2 3건 ──────────────────────────────────
#
# 카드: 「analyzer 후속 품질 P2 3건 (캐싱·has_error·최신 에러 선택)」
# PR #18 리뷰에서 나왔으나 크리티컬이 아니라 머지 후로 미뤄둔 것들.


def _failing_router():
    """provider 가 죽은 상황."""
    class _Router:
        def call(self, req, *a, **kw):
            raise RuntimeError("provider 일시 장애")
    return _Router()


def test_llm_failure_fallback_is_not_cached(monkeypatch):
    """[①] 폴백 응답을 캐싱하면 일시 장애가 **지속 장애로 굳는다.**

    provider 가 잠깐 흔들린 대가로 TTL(60초) 내내 모든 요청이 "분석 불가능"을
    돌려받는다. 사용자는 다시 눌러도 같은 답만 보고 제품이 고장 났다고
    판단한다.
    """
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())

    # 1) provider 장애 → 폴백
    monkeypatch.setattr(analyzer, "get_router", _failing_router)
    req = AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom")
    asyncio.run(analyzer.analyze(req, session_id="s1"))
    assert not analyzer._llm_cache, f"폴백이 캐시에 들어갔다: {analyzer._llm_cache}"

    # 2) provider 회복 → 곧바로 정상 분석이 나와야 한다
    calls = [0]
    monkeypatch.setattr(analyzer, "get_router", lambda: _counting_router(calls))
    asyncio.run(analyzer.analyze(req, session_id="s2"))
    assert calls[0] == 1, "회복 후에도 폴백 캐시를 재사용했다"
    assert analyzer._llm_cache, "정상 응답은 캐싱돼야 한다"


def test_fallback_marker_never_leaks_to_the_caller(monkeypatch):
    """폴백 표식은 내부용이다 — 이벤트나 응답에 실려 나가면 안 된다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()
    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", _failing_router)

    ev = asyncio.run(analyzer.analyze(
        AnalyzeRequest(workspace_path="/tmp/p", terminal_output="ValueError: boom"),
        session_id="s1",
    ))
    assert "_fallback" not in json.dumps(ev.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False), ("FALSE", False),
    ("no", False), ("0", False), ("", False),
    (1, True), (0, False), (None, False),
])
def test_has_error_string_is_interpreted_not_truthy(raw, expected):
    """[②] 모델이 `"false"` 문자열을 주면 파이썬에서는 **참**이다.

    그대로 쓰면 정상 출력이 에러로 분류돼, 멀쩡한 실행 결과에 빨간 에러
    카드가 뜬다.
    """
    import analyzer
    assert analyzer._as_bool(raw) is expected


def test_normal_output_is_not_flagged_when_model_returns_string_false(monkeypatch):
    """[②] 통합 경로에서도 확인한다."""
    import asyncio
    import analyzer

    analyzer._llm_cache.clear()

    class _StringFalseResp:
        text = json.dumps({
            "has_error": "false",          # ← 문자열
            "error_summary": "정상 종료",
            "importance_score": 10,
            "event_type": "task_change",
            "suggested_actions": [],
        })
        model_used = "test"

    class _Router:
        def call(self, req, *a, **kw):
            return _StringFalseResp()

    monkeypatch.setattr(analyzer, "run_gate", _fake_gate_factory())
    monkeypatch.setattr(analyzer, "get_router", lambda: _Router())

    ev = asyncio.run(analyzer.analyze(
        AnalyzeRequest(workspace_path="/tmp/p", terminal_output="build finished"),
        session_id="s1",
    ))
    from schemas import EventType as _EventType

    payload = ev.to_dict()
    assert ev.event_type != _EventType.ERROR_DETECTED, payload
    # **여기가 실제로 갈라지는 지점이다.** `"false"` 를 참으로 읽으면 정상
    # 출력이 에러로 직렬화돼 `error_text`·`raw_errors` 가 채워지고, 사용자
    # 화면에 빨간 에러 카드가 뜬다. event_type 만 보면 모델이 준 값
    # ("task_change")에 가려 차이가 드러나지 않는다.
    assert payload["error_text"] == "", payload
    assert payload["raw_errors"] == [], payload


def test_latest_error_wins_over_the_first_one():
    """[③] 사용자가 방금 겪은 것은 **마지막** 에러다.

    앞의 에러는 이미 고쳤거나 무관한 경고인 경우가 많다 — 재시도하는 빌드
    로그가 정확히 그 형태다. 예전에는 패턴 목록 순서가 로그 순서를 이겨서,
    앞쪽 Traceback 이 뒤쪽 최신 `npm ERR!` 를 항상 눌렀다.
    """
    import analyzer

    log = (
        "$ npm run build\n"
        "Traceback (most recent call last):\n"
        '  File "old.py", line 1, in <module>\n'
        "    import gone\n"
        "ModuleNotFoundError: No module named 'gone'\n"
        "(위 에러는 고쳤음)\n"
        "$ npm run build\n"
        "npm ERR! code ELIFECYCLE\n"
        "npm ERR! Failed at the build script."
    )
    out = analyzer._extract_error_text(log)
    assert "npm ERR!" in out, out
    assert "ModuleNotFoundError" not in out, out


def test_traceback_frames_are_not_lost_to_the_inner_exception_line():
    """[③ 반대 방향] Traceback 안의 예외 줄도 두 번째 패턴에 걸린다.

    끝 위치가 같으므로, 시작이 이른 쪽(=전체 블록)을 골라야 프레임이
    살아남는다. 여기서 틀리면 "가장 최신"을 고르려다 **가장 유용한 정보**를
    버리게 된다.
    """
    import analyzer

    tb = (
        "Traceback (most recent call last):\n"
        '  File "a.py", line 3, in <module>\n'
        '    raise ValueError("boom")\n'
        "ValueError: boom"
    )
    out = analyzer._extract_error_text(tb)
    assert "Traceback" in out and 'File "a.py"' in out and "ValueError: boom" in out, out


def test_context_follows_the_latest_occurrence_of_a_repeated_error():
    """[③] 같은 에러가 두 번 나오면 앞 맥락도 **최신 쪽** 것이어야 한다.

    본문 첫 줄을 원문에서 다시 찾는 방식이면 첫 등장을 집어, 최신 에러에
    엉뚱한 옛 맥락이 붙는다.
    """
    import analyzer

    log = "\n".join([
        "$ 1차 시도",
        "무관한 줄",
        "ValueError: same",
        "중간 줄 A",
        "중간 줄 B",
        "$ 2차 시도 (최신)",
        "ValueError: same",
    ])
    trimmed = analyzer._trim_terminal_output(log, context_lines=2)
    assert "2차 시도" in trimmed, trimmed
    assert "1차 시도" not in trimmed, trimmed


def test_npm_error_block_is_grouped_not_reduced_to_its_last_line():
    """[Codex P2] npm 진단의 **마지막 줄은 거의 항상 로그 파일 경로 안내**다.

    한 줄씩 매치하면 "가장 나중"이 그 안내문이 되어, 에러 카드에 원인 대신
    `npm ERR!  C:\\...\\_logs\\...-debug.log` 가 뜬다. 「최신 에러를 고른다」로
    바꾸면서 만든 회귀다 — 그 전에는 첫 매치라 `code ELIFECYCLE` 이 나왔다.
    """
    import analyzer

    log = (
        "> build\n> next build\n\n"
        "Failed to compile.\n"
        "./src/app/page.tsx\n"
        "Type error: Property 'foo' does not exist on type 'Bar'.\n\n"
        "npm ERR! code ELIFECYCLE\n"
        "npm ERR! errno 1\n"
        "npm ERR! my-app@0.1.0 build: `next build`\n"
        "npm ERR! Exit status 1\n"
        "npm ERR!\n"
        "npm ERR! Failed at the my-app@0.1.0 build script.\n\n"
        "npm ERR! A complete log of this run can be found in:\n"
        "npm ERR!     /home/u/.npm/_logs/2026-08-12T02_11_43_120Z-debug.log"
    )

    out = analyzer._extract_error_text(log)
    # 로그 경로 한 줄만 남으면 안 된다.
    assert out.count("\n") > 0, f"한 줄만 잡혔다: {out!r}"
    assert "code ELIFECYCLE" in out, out
    assert "Failed at the my-app@0.1.0 build script." in out, out

    # 진짜 원인은 블록 앞에 있으므로 맥락에 들어와야 한다.
    trimmed = analyzer._trim_terminal_output(log)
    assert "Type error: Property 'foo'" in trimmed, trimmed


def test_a_later_traceback_still_beats_an_earlier_npm_block():
    """블록으로 묶었다고 '최신 우선' 규칙이 깨지면 안 된다."""
    import analyzer

    log = (
        "npm ERR! code ELIFECYCLE\n"
        "npm ERR! Exit status 1\n"
        "(위 에러는 고쳤음)\n"
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in <module>\n'
        "    import gone\n"
        "ModuleNotFoundError: No module named 'gone'"
    )
    out = analyzer._extract_error_text(log)
    assert "ModuleNotFoundError" in out, out
    assert "ELIFECYCLE" not in out, out
