"""
P0-12 smoke #2: infra_agent.generate_dockerfile / generate_docker_compose.

이 테스트는 RECODER_INFRA_AI_CUSTOMIZE=0 (conftest.py 에서 세팅) 으로
LLM 호출을 비활성화하고 FileRegistry 의 기본 템플릿이 그대로 반환되는 경로만 검증한다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from infra_agent import generate_dockerfile, generate_docker_compose
from schemas import FileType, InfraFileProposal, ProjectProfile, ProjectStack


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
    # file_type 은 **타입**(enum 값 "dockerfile"), target_path 는 **파일명**
    # ("Dockerfile"). 예전 테스트는 둘을 혼동해 file_type 을 "Dockerfile" 과
    # 비교했다 — enum 값이 소문자라 항상 실패했다.
    assert proposal.file_type == FileType.DOCKERFILE
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


def test_generate_docker_compose_uses_db_template_when_driver_is_detected(
    fake_fastapi_project: Path,
):
    with (fake_fastapi_project / "requirements.txt").open("a", encoding="utf-8") as f:
        f.write("asyncpg\n")

    proposal = generate_docker_compose(workspace_path=str(fake_fastapi_project))

    assert proposal.base_template == "db-multi"
    assert "  db:" in proposal.content
    assert "postgres:16-alpine" in proposal.content
    assert "DATABASE_URL: postgresql://app:app@db:5432/app" in proposal.content


def test_generate_docker_compose_without_driver_stays_single_service(
    fake_fastapi_project: Path,
):
    proposal = generate_docker_compose(workspace_path=str(fake_fastapi_project))

    assert proposal.base_template == "single"
    assert "  db:" not in proposal.content
    assert "DATABASE_URL:" not in proposal.content


def test_docker_compose_env_file_only_when_env_exists(fake_fastapi_project: Path):
    """[회귀] `.env` 가 실제로 있을 때만 env_file 을 참조한다.

    `.env.example` 만 있고 `.env` 는 없는 상태(갓 클론한 저장소의 흔한 모습)
    에서 `env_file: - .env` 를 넣으면, docker compose 가 없는 `.env` 를 필수로
    찾아 생성된 compose 가 `docker compose up` 에서 안 뜬다.
    """
    def _compose(root: Path) -> str:
        return generate_docker_compose(workspace_path=str(root)).content

    # ① `.env.example` 만 있고 `.env` 는 없음 → env_file 없어야 한다
    (fake_fastapi_project / ".env.example").write_text("KEY=\n", encoding="utf-8")
    content = _compose(fake_fastapi_project)
    assert "env_file" not in content, (
        ".env 가 없는데 env_file 을 참조한다 — compose 가 안 뜬다"
    )

    # ② 실제 `.env` 가 있으면 env_file 을 넣는다
    (fake_fastapi_project / ".env").write_text("KEY=v\n", encoding="utf-8")
    content = _compose(fake_fastapi_project)
    assert "env_file" in content and ".env" in content
