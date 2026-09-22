"""인프라 파일 덮어쓰기 가드 — 실기기 검증 D4 에서 드러난 "보이기만 하는 승인".

손으로 고친 Dockerfile 이 "Dockerfile 생성 → 저장 Level 1" 한 번에 조용히 원래대로
돌아갔다. 생성 결과가 예전과 같아 화면엔 변화가 없었고, 사용자는 자기 수정이
사라진 줄도 몰랐다. 같은 경로에 **내용이 다른 파일**이 있으면 묻지 않고 덮어쓰지
않는다 — diff 를 돌려주고, 덮어쓸 때는 백업을 남긴다.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import api.routes.deploy as deploy_route


def _proposal(tmp_path, content: str, target: str = "Dockerfile"):
    return SimpleNamespace(
        target_path=target,
        workspace_path=str(tmp_path),
        content=content,
        file_type="dockerfile",
    )


def test_파일이_없으면_그냥_쓴다(tmp_path):
    result = deploy_route._write_proposal_to_workspace(_proposal(tmp_path, "FROM a\n"), "", "p1")

    assert result["status"] == "saved"
    assert result["overwritten"] is False
    assert (tmp_path / "Dockerfile").read_text() == "FROM a\n"


def test_같은_내용이면_충돌이_아니다(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM a\n")

    result = deploy_route._write_proposal_to_workspace(_proposal(tmp_path, "FROM a\n"), "", "p1")

    assert result["status"] == "saved"
    assert result["overwritten"] is False
    assert not (tmp_path / "Dockerfile.recoder-prev").exists()


def test_내용이_다르면_쓰지_않고_diff_를_돌려준다(tmp_path):
    (tmp_path / "Dockerfile").write_text('FROM a\nENTRYPOINT ["nope-init", "--"]\n')

    result = deploy_route._write_proposal_to_workspace(
        _proposal(tmp_path, 'FROM a\nENTRYPOINT ["dumb-init", "--"]\n'), "", "p1",
    )

    assert result["status"] == "exists"
    assert (tmp_path / "Dockerfile").read_text().startswith('FROM a\nENTRYPOINT ["nope-init"'), "묻기 전에 덮어썼다"
    assert "-ENTRYPOINT [\"nope-init\"" in result["diff"]
    assert "+ENTRYPOINT [\"dumb-init\"" in result["diff"]
    assert result["existing_content"].startswith("FROM a")


def test_overwrite_면_백업을_남기고_덮어쓴다(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM old\n")

    result = deploy_route._write_proposal_to_workspace(
        _proposal(tmp_path, "FROM new\n"), "", "p1", overwrite=True,
    )

    assert result["status"] == "saved"
    assert result["overwritten"] is True
    assert (tmp_path / "Dockerfile").read_text() == "FROM new\n"
    assert (tmp_path / "Dockerfile.recoder-prev").read_text() == "FROM old\n"
    assert result["backup_path"].endswith("Dockerfile.recoder-prev")


def test_승인_라우트는_exists_뒤에도_제안을_남긴다(tmp_path):
    """사용자가 diff 를 보고 '덮어쓰기' 를 고르면 같은 제안을 다시 승인한다 — 지웠으면 404."""
    (tmp_path / "Dockerfile").write_text("FROM old\n")
    deploy_route._infra_proposals["pX"] = _proposal(tmp_path, "FROM new\n")
    try:
        first = asyncio.run(deploy_route.approve_dockerfile("pX", True))
        assert first["status"] == "exists"
        assert "pX" in deploy_route._infra_proposals, "exists 응답 뒤에 제안이 사라졌다"

        second = asyncio.run(deploy_route.approve_dockerfile("pX", True, overwrite=True))
        assert second["status"] == "saved"
        assert "pX" not in deploy_route._infra_proposals
        assert (tmp_path / "Dockerfile").read_text() == "FROM new\n"
    finally:
        deploy_route._infra_proposals.pop("pX", None)


def test_거절하면_기존_파일은_그대로다(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM mine\n")
    deploy_route._infra_proposals["pR"] = _proposal(tmp_path, "FROM ai\n")

    result = asyncio.run(deploy_route.approve_dockerfile("pR", False))

    assert result["status"] == "rejected"
    assert (tmp_path / "Dockerfile").read_text() == "FROM mine\n"
    assert "pR" not in deploy_route._infra_proposals
