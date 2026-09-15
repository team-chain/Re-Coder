"""에이전트 로드 실패가 성공처럼 보이던 문제 — 보드 이슈 「실패가 200 OK 로 나가는 Placeholder 응답」.

무엇이 사고였나
    에이전트 import 가 실패하면 세 라우트가 **그럴듯한 가짜 응답을 200 OK 로**
    돌려줬다. 화면에서는 정상 응답과 구분되지 않아, 핵심 기능이 죽어 있는데
    아무도 모르는 상태가 됐다.

    · /api/analyze     → "[Placeholder] Orchestrator not yet available."
    · /api/deploy/plan → image=app:latest / port 8080 **고정값 플랜**
    · /api/ops/analyze → risk_reasons=["[Placeholder] OpsAgent not yet loaded."]

여기서 고정하는 것
    1. analyze·deploy 는 **503 으로 실패**하고, 원인과 다음 행동을 담는다.
    2. ops 는 규칙 기반 축소 모드를 유지하되(장애 대응 중 도구가 막히는 게 더
       나쁘다) **AI 분석인 척하지 않는다** — 사람이 승인하기 전에 축소 모드임을
       화면에서 읽을 수 있어야 한다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))


# ---------------------------------------------------------------------------
# /api/analyze — 분석 엔진 없음
# ---------------------------------------------------------------------------


def test_오케스트레이터가_없으면_503_으로_실패한다(monkeypatch) -> None:
    from api.routes import analyze

    monkeypatch.setattr(analyze, "_get_orchestrator", lambda: None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(analyze._delegate_to_orchestrator(
            analyze.AnalyzeRequest(workspace_path="/tmp/x", terminal_output="boom"), 1.0, 1.0,
        ))

    assert exc.value.status_code == 503
    detail = str(exc.value.detail)
    #: 원인 + 다음 행동이 둘 다 있어야 한다. 원인만 있으면 사용자는 재시도밖에 못 한다.
    assert "Orchestrator" in detail or "분석 엔진" in detail
    assert "다시 시작" in detail or "로그" in detail


def test_음성대조_오케스트레이터가_있으면_그대로_위임한다(monkeypatch) -> None:
    """실패 처리가 과해서 정상 경로까지 막으면 안 된다."""
    from api.routes import analyze
    from schemas import ApprovalLevel, PatchProposal, RiskLevel

    expected = PatchProposal(
        summary="정상 분석 결과",
        risk_level=RiskLevel.LOW,
        risk_reasons=[],
        approval_level=ApprovalLevel.AUTO,
        patches=[],
        test_command=None,
    )

    class _Orch:
        async def process_analyze_request(self, request):
            return expected

    monkeypatch.setattr(analyze, "_get_orchestrator", lambda: _Orch())

    result = asyncio.run(analyze._delegate_to_orchestrator(
        analyze.AnalyzeRequest(workspace_path="/tmp/x", terminal_output="boom"), 1.0, 1.0,
    ))
    assert result.summary == "정상 분석 결과"


def test_플레이스홀더_문구가_코드에서_사라졌다() -> None:
    """[음성 대조] 문구만 지우고 200 을 유지하면 이 테스트만 통과하고 사고는 남는다.

    그래서 위 503 검사와 **함께** 본다. 여기서는 내부 용어가 사용자 응답으로
    새어 나가는 경로가 되살아나지 않았는지만 확인한다.
    """
    for rel in ("api/routes/analyze.py", "api/routes/ops.py", "api/routes/deploy.py"):
        text = (_CORE / rel).read_text(encoding="utf-8")
        #: 주석에서 과거 사고를 설명하는 건 허용 — 실제 응답 값으로 쓰이면 안 된다.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("·"):
                continue
            assert "[Placeholder]" not in line, f"{rel}: 플레이스홀더 응답이 되살아났다 — {line.strip()[:60]}"


# ---------------------------------------------------------------------------
# /api/deploy/plan — 배포 플랜 생성기 없음
# ---------------------------------------------------------------------------


def test_배포_플랜_생성기가_없으면_추측_플랜_대신_503() -> None:
    """고정값 플랜(app:latest / 8080)을 200 으로 주면 요청과 다른 설정으로 배포된다."""
    source = (_CORE / "api/routes/deploy.py").read_text(encoding="utf-8")
    anchor = source.index("deploy_agent = _get_deploy_agent()")
    block = source[anchor:anchor + 1200]

    assert "503" in block, "DeployAgent 부재 시 여전히 플랜을 만들어 돌려준다"
    #: 예전 고정값이 되살아나지 않았는지.
    assert 'image=request.image or "app:latest"' not in block, "추측 플랜이 되살아났다"


# ---------------------------------------------------------------------------
# /api/ops/analyze — 축소 모드는 유지하되 정직하게
# ---------------------------------------------------------------------------


def test_운영_분석은_축소_모드를_유지하되_그렇다고_밝힌다() -> None:
    """장애 대응 중 도구가 통째로 막히는 것보다, 축소 모드를 밝히는 게 낫다.

    단 사람이 HIGH 위험 작업을 승인하기 **전에** 축소 모드임을 읽을 수 있어야 한다.
    """
    source = (_CORE / "api/routes/ops.py").read_text(encoding="utf-8")
    anchor = source.index("ops_agent = _get_ops_agent()")
    block = source[anchor:anchor + 2000]

    #: 폴백 자체는 살아 있어야 한다 (503 으로 바꾸지 않았다).
    assert "ResponseProposal(" in block, "운영 폴백이 사라졌다 — 장애 중 도구가 막힌다"
    #: 사용자가 읽을 수 있는 한국어 경고여야 한다.
    assert "축소 모드" in block, "축소 모드임이 화면 문구로 드러나지 않는다"
    assert "승인 전에" in block, "승인 전에 확인하라는 안내가 없다"
    #: 사람 승인 단계는 그대로 유지.
    assert "DOUBLE_CONFIRM" in block
