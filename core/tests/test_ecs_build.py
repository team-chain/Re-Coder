"""
core/agents/ecs_build.py 테스트 — FR-05-04

여기서 지키려는 성질:

1. **실패마다 대처법이 다르게 나온다** (카드 DoD 3번 "사람이 읽을 수 있는
   에러 메시지"). docker 미설치, 데몬 꺼짐, Dockerfile 없음, 권한 거부,
   디스크 부족은 원인도 해결법도 다르다. 하나로 뭉뚱그리면 안 된다.

2. **비밀은 명령행에 실리지 않는다.** ECR 비밀번호를 argv 로 넘기면
   같은 머신의 다른 프로세스가 `ps` 로 읽을 수 있다. stdin 으로 가야 한다.

3. **Fargate 가 실행할 수 있는 아키텍처로 빌드한다.** Apple Silicon 에서
   그냥 빌드하면 arm64 가 나오고 태스크가 exec format error 로 죽는데,
   그 로그로는 원인을 알기 어렵다.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.agents import ecs_build  # noqa: E402


# ---------------------------------------------------------------------------
# 테스트용 러너 — 실행된 명령을 기록하고 정해진 결과를 돌려준다
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, results=None, default=(0, "", "")):
        self.calls: list[dict] = []
        self._results = dict(results or {})
        self._default = default

    def __call__(self, args, *, cwd=None, timeout=None, stdin_text=None):
        args = list(args)
        self.calls.append(
            {"args": args, "cwd": cwd, "timeout": timeout, "stdin_text": stdin_text}
        )
        for key, result in self._results.items():
            if key in " ".join(args):
                return result
        return self._default

    def command(self, needle: str) -> dict:
        for call in self.calls:
            if needle in " ".join(call["args"]):
                return call
        raise AssertionError(f"'{needle}' 를 실행한 적이 없다: {self.calls}")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c["args"]) for c in self.calls)


class FakeEcr:
    def __init__(self, token="AWS:sekrit", endpoint="https://123.dkr.ecr.us-east-1.amazonaws.com"):
        self._token = base64.b64encode(token.encode()).decode()
        self._endpoint = endpoint

    def get_authorization_token(self):
        return {
            "authorizationData": [
                {"authorizationToken": self._token, "proxyEndpoint": self._endpoint}
            ]
        }


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _docker_on_path(monkeypatch):
    """기본적으로 docker 가 설치된 것으로 둔다. 필요한 테스트만 뒤집는다."""
    monkeypatch.setattr(ecs_build.shutil, "which", lambda _: "/usr/bin/docker")


# ---------------------------------------------------------------------------
# 사전 점검 — 서로 다른 실패는 서로 다르게 말해야 한다
# ---------------------------------------------------------------------------


def test_missing_docker_says_install_it(monkeypatch):
    monkeypatch.setattr(ecs_build.shutil, "which", lambda _: None)
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ensure_docker_available(runner=FakeRunner())
    assert "찾지 못했습니다" in str(caught.value)
    assert "설치" in caught.value.remedy


def test_stopped_daemon_says_start_it_not_install_it():
    """부정 통제: 설치는 됐는데 데몬이 꺼진 경우를 '설치하세요'로 안내하면
    사용자는 이미 한 일을 또 하게 된다."""
    runner = FakeRunner(default=(1, "", "Cannot connect to the Docker daemon"))
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ensure_docker_available(runner=runner)
    assert "데몬에 연결하지 못했습니다" in str(caught.value)
    assert "실행" in caught.value.remedy
    assert "설치" not in caught.value.remedy


def test_missing_dockerfile_points_at_the_card(tmp_path):
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ensure_dockerfile(str(tmp_path))
    assert "Dockerfile 이 없습니다" in str(caught.value)
    assert caught.value.remedy


def test_missing_workspace_is_distinct_from_missing_dockerfile(tmp_path):
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ensure_dockerfile(str(tmp_path / "nope"))
    assert "작업 폴더를 찾을 수 없습니다" in str(caught.value)


# ---------------------------------------------------------------------------
# 빌드
# ---------------------------------------------------------------------------


def test_build_pins_the_platform_to_amd64(workspace):
    """Fargate 기본 아키텍처는 X86_64 다. arm64 로 빌드되면 태스크가
    exec format error 로 죽는데 그 로그로는 원인을 알기 어렵다."""
    runner = FakeRunner()
    ecs_build.build_image(workspace, "app:1", runner=runner)
    args = runner.command("docker build")["args"]
    assert "--platform" in args
    assert args[args.index("--platform") + 1] == "linux/amd64"


def test_build_runs_in_the_workspace(workspace):
    runner = FakeRunner()
    ecs_build.build_image(workspace, "app:1", runner=runner)
    assert runner.command("docker build")["cwd"] == workspace


def test_build_failure_surfaces_the_docker_output(workspace):
    runner = FakeRunner(
        results={"docker build": (1, "", "ERROR: failed to solve: pip install failed")}
    )
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.build_image(workspace, "app:1", runner=runner)
    assert "빌드에 실패했습니다" in str(caught.value)
    assert "pip install failed" in caught.value.detail


def test_build_checks_the_dockerfile_before_running_docker(tmp_path):
    """부정 통제: Dockerfile 이 없는데 docker 를 먼저 부르면 15분 타임아웃을
    기다린 끝에 알아보기 힘든 오류가 나온다."""
    runner = FakeRunner()
    with pytest.raises(ecs_build.BuildError):
        ecs_build.build_image(str(tmp_path), "app:1", runner=runner)
    assert not runner.ran("docker build"), "Dockerfile 없이 docker 를 실행했다"


# ---------------------------------------------------------------------------
# ECR 로그인
# ---------------------------------------------------------------------------


def test_ecr_login_decodes_the_token_and_uses_stdin_for_the_password():
    """비밀번호가 argv 에 실리면 같은 머신의 다른 프로세스가 ps 로 읽는다."""
    runner = FakeRunner()
    registry, username = ecs_build.ecr_login(FakeEcr(), runner=runner)

    assert registry == "123.dkr.ecr.us-east-1.amazonaws.com"
    assert username == "AWS"

    call = runner.command("docker login")
    assert call["stdin_text"] == "sekrit"
    assert "sekrit" not in " ".join(call["args"]), "비밀번호가 명령행에 노출됐다"
    assert "--password-stdin" in call["args"]


def test_ecr_login_uses_boto3_not_the_aws_cli():
    """부정 통제: `aws ecr get-login-password` 로 돌아가면 CLI 프로필과
    확장의 자격증명이 어긋날 수 있다."""
    runner = FakeRunner()
    ecs_build.ecr_login(FakeEcr(), runner=runner)
    assert not runner.ran("get-login-password"), "AWS CLI 로 토큰을 받고 있다"


def test_ecr_login_failure_names_the_missing_permission():
    class Denied:
        def get_authorization_token(self):
            raise RuntimeError("AccessDenied: ecr:GetAuthorizationToken")

    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ecr_login(Denied(), runner=FakeRunner())
    assert "ecr:GetAuthorizationToken" in caught.value.remedy


def test_empty_authorization_data_is_reported_not_crashed():
    class Empty:
        def get_authorization_token(self):
            return {"authorizationData": []}

    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.ecr_login(Empty(), runner=FakeRunner())
    assert "비어 있습니다" in str(caught.value)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_push_tags_then_pushes_and_reads_the_digest():
    runner = FakeRunner(
        results={
            "docker push": (
                0,
                "v1: digest: sha256:deadbeefcafe size: 1234",
                "",
            )
        }
    )
    result = ecs_build.push_image("app:v1", "123.dkr.ecr.x/app:v1", runner=runner)
    assert result.image_uri == "123.dkr.ecr.x/app:v1"
    assert result.digest == "sha256:deadbeefcafe"
    assert runner.ran("docker tag")


def test_push_without_a_digest_line_returns_none_rather_than_guessing():
    runner = FakeRunner(results={"docker push": (0, "Pushed", "")})
    assert ecs_build.push_image("app:v1", "r/app:v1", runner=runner).digest is None


def test_denied_push_mentions_credentials_and_the_four_hour_session():
    """학교 계정에서 가장 흔한 실패다 — 세션이 4시간마다 끊긴다."""
    runner = FakeRunner(
        results={"docker push": (1, "", "denied: Your authorization token has expired")}
    )
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.push_image("app:v1", "r/app:v1", runner=runner)
    assert "4시간" in caught.value.remedy


def test_disk_full_push_says_prune_not_check_permissions():
    """부정 통제: 디스크가 찬 걸 권한 문제로 안내하면 완전히 헛짚는다."""
    runner = FakeRunner(
        results={"docker push": (1, "", "write /var/lib/docker: no space left on device")}
    )
    with pytest.raises(ecs_build.BuildError) as caught:
        ecs_build.push_image("app:v1", "r/app:v1", runner=runner)
    assert "prune" in caught.value.remedy
    assert "권한" not in caught.value.remedy


def test_tag_failure_does_not_attempt_a_push():
    runner = FakeRunner(results={"docker tag": (1, "", "No such image: app:v1")})
    with pytest.raises(ecs_build.BuildError):
        ecs_build.push_image("app:v1", "r/app:v1", runner=runner)
    assert not runner.ran("docker push"), "태그가 실패했는데 push 를 시도했다"


# ---------------------------------------------------------------------------
# 전체 흐름
# ---------------------------------------------------------------------------


def test_build_and_push_runs_the_steps_in_order(workspace):
    runner = FakeRunner(
        results={"docker push": (0, "v1: digest: sha256:abc size: 1", "")}
    )
    result = ecs_build.build_and_push(
        FakeEcr(),
        workspace_path=workspace,
        repository_uri="123.dkr.ecr.us-east-1.amazonaws.com/recoder-app",
        tag="v1",
        runner=runner,
    )
    order = [
        " ".join(c["args"][:2]) for c in runner.calls if c["args"][0] == "docker"
    ]
    assert order == ["docker info", "docker build", "docker login", "docker tag",
                     "docker push"]
    assert result.image_uri == "123.dkr.ecr.us-east-1.amazonaws.com/recoder-app:v1"
    # 로컬 태그는 리포지토리 이름만 쓴다 — 레지스트리 호스트가 붙으면
    # docker 가 그 이름으로 push 를 시도해 혼선이 생긴다.
    assert result.local_tag == "recoder-app:v1"


@pytest.mark.parametrize("bad", ["", "v1:extra", "ns/v1"])
def test_bad_tags_are_rejected_before_anything_runs(workspace, bad):
    runner = FakeRunner()
    with pytest.raises(ecs_build.BuildError):
        ecs_build.build_and_push(
            FakeEcr(),
            workspace_path=workspace,
            repository_uri="r/app",
            tag=bad,
            runner=runner,
        )
    assert runner.calls == [], "잘못된 태그인데 명령을 실행했다"
