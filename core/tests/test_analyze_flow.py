"""
P0-12 smoke #3: /api/analyze flow (server.py 의 핵심 결선 검증).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from schemas import AnalyzeRequest, FilePatch, PatchProposal, RiskLevel


def test_extract_error_text_traceback():
    from server import _extract_error_text  # type: ignore
    log = (
        "running uvicorn ...\n"
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
        "    import fastapi\n"
        "ModuleNotFoundError: No module named 'fastapi'\n"
    )
    out = _extract_error_text(log)
    assert "Traceback" in out or "ModuleNotFoundError" in out, out


def test_extract_error_text_ts_compile():
    from server import _extract_error_text  # type: ignore
    log = "src/index.ts:10:5\nerror TS2322: Type 'string' is not assignable to type 'number'.\n"
    out = _extract_error_text(log)
    assert "TS2322" in out


def test_extract_error_text_fallback_to_last_30_lines():
    from server import _extract_error_text  # type: ignore
    # 패턴이 매치되지 않는 일반 출력 → 마지막 30 줄 폴백 (50 lines, last 30 = 20..49)
    log = "\n".join(f"info {i}" for i in range(50))
    out = _extract_error_text(log)
    assert "info 49" in out
    assert "info 20" in out
    assert "info 0\n" not in out


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
