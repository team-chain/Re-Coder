"""배포 ADR 중복 생성 방지 — "ADR-001 · ADR-002" 이슈 카드의 수정.

무엇이 사고였나
    배포 대상 화면을 오가며 같은 대상을 다시 고르면, /api/deploy/decision 이
    호출마다 새 번호를 예약해 동일 내용의 ADR 이 계속 쌓였다. 번호 예약
    장부는 **덮어쓰기**(유실)를 막을 뿐, **내용 중복**은 판단하지 않는다.

여기서 검사하는 것
    1. 같은 대상 + 같은 근거 → 기존 ADR 파일을 그대로 재사용(새 번호 없음)
    2. [음성 대조] 대상이 바뀌면 → 반드시 **새** ADR (재사용이 과하면
       실제로 바뀐 결정 기록이 사라진다)
    3. 날짜만 달라진 기존 파일도 같은 결정으로 인식한다
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import deploy  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_reservation_store(tmp_path, monkeypatch):
    """번호 예약 장부를 테스트 전용 파일로 격리 — 실제 ~/.recoder 를 건드리지 않는다."""
    monkeypatch.setenv("RECODER_ADR_STORE", str(tmp_path / "reservations.json"))


def _write_adr(workspace: Path, op: dict) -> Path:
    """확장이 하는 일(반환된 op 를 워크스페이스에 기록)을 흉내 낸다."""
    target = workspace / op["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(op["content"], encoding="utf-8")
    return target


def test_같은_결정을_다시_보내면_기존_ADR을_재사용한다(tmp_path) -> None:
    first = deploy._build_deployment_decision_adr(str(tmp_path), "s3", ["package.json"])
    _write_adr(tmp_path, first)

    second = deploy._build_deployment_decision_adr(str(tmp_path), "s3", ["package.json"])

    assert second["file"] == first["file"], "같은 결정인데 새 파일이 생긴다 — 중복 그대로"
    assert second.get("reused") is True
    #: 재사용 op 를 다시 기록해도(확장은 구분하지 않는다) 파일은 하나여야 한다.
    _write_adr(tmp_path, second)
    adr_files = list((tmp_path / "docs" / "adr").glob("ADR-*.md"))
    assert len(adr_files) == 1


def test_음성대조_대상이_바뀌면_새_ADR을_만든다(tmp_path) -> None:
    """재사용 판정이 과하면 실제로 바뀐 결정이 기록되지 않는다."""
    first = deploy._build_deployment_decision_adr(str(tmp_path), "s3", ["package.json"])
    _write_adr(tmp_path, first)

    second = deploy._build_deployment_decision_adr(str(tmp_path), "ecs", ["package.json"])

    assert second["file"] != first["file"], "대상이 바뀌었는데 기존 ADR 을 재사용한다"
    assert second.get("reused") is not True
    _write_adr(tmp_path, second)
    adr_files = list((tmp_path / "docs" / "adr").glob("ADR-*.md"))
    assert len(adr_files) == 2


def test_근거가_바뀌면_새_ADR을_만든다(tmp_path) -> None:
    """감지 근거가 달라졌으면 같은 대상이라도 다른 결정 기록이다."""
    first = deploy._build_deployment_decision_adr(str(tmp_path), "s3", ["package.json"])
    _write_adr(tmp_path, first)

    second = deploy._build_deployment_decision_adr(
        str(tmp_path), "s3", ["package.json", "next.config.js"],
    )
    assert second["file"] != first["file"]


def test_날짜만_다른_기존_파일도_같은_결정으로_본다(tmp_path) -> None:
    """다음 날 같은 대상을 다시 골라도 중복이 생기면 안 된다."""
    first = deploy._build_deployment_decision_adr(str(tmp_path), "local", [])
    path = _write_adr(tmp_path, first)

    #: 어제 기록된 것처럼 날짜를 바꿔치기.
    aged = re.sub(r"- 날짜: \d{4}-\d{2}-\d{2}", "- 날짜: 2026-01-01", first["content"])
    assert aged != first["content"], "날짜 치환이 실패하면 이 테스트는 아무것도 검사하지 않는다"
    path.write_text(aged, encoding="utf-8")

    second = deploy._build_deployment_decision_adr(str(tmp_path), "local", [])
    assert second.get("reused") is True
    assert second["content"] == aged, "재사용 시 기존 기록(원래 날짜)을 보존해야 한다"
