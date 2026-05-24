"""
P0-12 smoke #2: infra_agent.generate_dockerfile / generate_docker_compose.

이 테스트는 RECODER_INFRA_AI_CUSTOMIZE=0 (conftest.py 에서 세팅) 으로
LLM 호출을 비활성화하고 FileRegistry 의 기본 템플릿이 그대로 반환되는 경로만 검증한다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from infra_agent import generate_dockerfile, generate_docker_compose
from schemas import InfraFileProposal, ProjectProfile, ProjectStack


@pytest.fixture
def fake_fastapi_project(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text(
        "fastapi\nuvicorn[standard]\n", encoding="utf-8"
    )
    return tmp_path


def test_generate_dockerfile_returns_proposal(fake_fastapi_project: Path, monkeypatch):
    monkeypatch.setenv("RECODER_PROJECT_ROOT", str(fake_fastapi_project))
    proposal = generate_dockerfile(
        request=None,
        project_profile=None,
        workspace_path=str(fake_fastapi_project),
    )
    assert isinstance(proposal, InfraFileProposal)
    assert proposal.proposal_id, "proposal_id 가 비어있음"
    assert proposal.file_type == "Dockerfile"
    assert proposal.target_path == "Dockerfile"
    assert proposal.content, "content 가 비어있음"
    assert "python" in proposal.content.lower(), proposal.content
    assert proposal.approval_level == 1


def test_generate_dockerfile_with_profile(fake_fastapi_project: Path):
    profile = ProjectProfile(
        project_id="test-proj",
        workspace_path=str(fake_fastapi_project),
        stack=ProjectStack.PYTHON_FASTAPI,
        package_manager="pip",
        default_run_command="uvicorn main:app",
        default_port=8000,
        health_check_path="/health",
    )
    proposal = generate_dockerfile(project_profile=profile)
    assert isinstance(proposal, InfraFileProposal)
    assert proposal.content.strip(), "content 가 비어있음"


def test_generate_docker_compose(fake_fastapi_project: Path):
    profile = ProjectProfile(
        project_id="test-proj",
        workspace_path=str(fake_fastapi_project),
        stack=ProjectStack.PYTHON_FASTAPI,
        package_manager="pip",
        default_run_command="uvicorn main:app",
        default_port=8000,
        health_check_path="/health",
    )
    proposal = generate_docker_compose(
        project_profile=profile,
        workspace_path=str(fake_fastapi_project),
    )
    assert isinstance(proposal, InfraFileProposal)
    assert "services:" in proposal.content
