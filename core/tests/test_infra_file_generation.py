"""
회귀: 인프라 파일 생성이 평문 500 으로 죽지 않고 초안을 돌려주는가

배경 — 데모에서 무슨 일이 있었나
    배포 센터 「인프라 파일 생성」에서 빨간 배너로 `Error: Internal Server
    Error` 만 떴다. 그 이상 아무 정보가 없었다.

    원인은 세 겹이었다.

    (1) `/api/deploy/dockerfile` 이 InfraAgent 호출을 `except TypeError` 로만
        감쌌다. 실제로 터진 건 LLM 제공자의 `RuntimeError`(자격증명 만료·키
        없음·rate limit)라서 그대로 라우트를 뚫고 나갔고, Starlette 이
        **평문** `Internal Server Error` 를 반환했다. 확장은 응답 본문을
        그대로 배너에 띄우므로 사용자가 본 게 정확히 그 문자열이다.

    (2) `/api/deploy/compose` 라우트가 **아예 없었다**(404). 확장에는 탭이
        Dockerfile · Compose · GitHub Actions 셋인데 Compose 만 대응 API 가
        없어서 동작할 수가 없었다. 생성기(infra_agent.generate_docker_compose)
        는 이미 있었고 라우트만 빠져 있었다.

    (3) 처리되지 않은 예외 전반이 평문으로 나갔다 — 라우트 하나를 고쳐도
        다른 경로에서 같은 증상이 재발할 수 있는 구조였다.

DoD 근거: 칸반 「인프라 파일 생성이 500 Internal Server Error」(P1)
    "Dockerfile · Compose · GitHub Actions 세 탭 모두에서 파일 초안이
     생성된다. 실패하는 경우 500 대신 원인과 다음 행동이 담긴 메시지가
     나온다. 실패 경로 테스트 1건 추가."
"""
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import main


TOKEN = "t" * 32


@pytest.fixture()
def client():
    app = main.create_app()
    app.state.session_token = TOKEN
    #: Core 는 127.0.0.1 만 바인딩하고 미들웨어가 localhost 를 면제한다.
    #: 실제 확장과 같은 조건으로 부르기 위해 클라이언트 주소를 맞춘다.
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 5555))


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    return str(tmp_path)


def _post(client, path, body):
    return client.post(path, json=body, headers={"X-Session-Token": TOKEN})


# ---------------------------------------------------------------------------
# (1) LLM 이 죽어도 초안은 나온다
# ---------------------------------------------------------------------------


def test_dockerfile_LLM이_터져도_500이_아니라_초안을_준다(client, workspace, monkeypatch):
    """데모에서 죽은 그 경로. RuntimeError 는 TypeError 절에 안 걸렸다."""
    import api.routes.deploy as deploy_routes

    class _AgentThatFails:
        async def generate_dockerfile(self, *_args, **_kwargs):
            raise RuntimeError(
                "Gemini SDK not initialised. Set the 'GEMINI_API_KEY' environment variable."
            )

    monkeypatch.setattr(deploy_routes, "_get_infra_agent", lambda: _AgentThatFails())

    resp = _post(client, "/api/deploy/dockerfile", {"workspace_path": workspace})

    assert resp.status_code == 200, f"여전히 터진다: {resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert body["content"].strip(), "초안 내용이 비었다 — 폴백이 아무것도 못 만들었다"
    assert body["target_path"] == "Dockerfile"


def test_dockerfile_폴백이면_이유와_다음행동을_함께_알려준다(client, workspace, monkeypatch):
    """
    DoD: "원인과 다음 행동이 담긴 메시지". 조용히 템플릿으로 바꿔치기하면
    사용자는 AI 가 돈 줄 알고 초안을 그대로 믿는다.
    """
    import api.routes.deploy as deploy_routes

    class _AgentThatFails:
        async def generate_dockerfile(self, *_args, **_kwargs):
            raise RuntimeError("rate limit exceeded")

    monkeypatch.setattr(deploy_routes, "_get_infra_agent", lambda: _AgentThatFails())

    body = _post(client, "/api/deploy/dockerfile", {"workspace_path": workspace}).json()
    notes = " ".join(body.get("risk_reasons") or [])

    assert notes, "AI 를 왜 못 썼는지 알려주지 않는다"
    assert "rate limit exceeded" in notes, "원인(원문)이 빠졌다"
    assert "다시 생성" in notes, "다음에 뭘 하면 되는지가 없다"


def test_음성대조_AI가_정상이면_폴백_안내가_붙지_않는다(client, workspace, monkeypatch):
    """
    위 두 테스트가 의미 있으려면, 정상일 때는 안내가 **없어야** 한다.
    항상 붙는다면 저 검사는 아무것도 증명하지 못한다.
    """
    import api.routes.deploy as deploy_routes
    from schemas import ApprovalLevel, FileType, InfraFileProposal, RiskLevel

    class _AgentThatWorks:
        async def generate_dockerfile(self, *_args, **_kwargs):
            return InfraFileProposal(
                file_type=FileType.DOCKERFILE,
                target_path="Dockerfile",
                content="FROM node:20-slim\n",
                base_template="ai",
                risk_level=RiskLevel.LOW,
                approval_level=ApprovalLevel.CONFIRM,
            )

    monkeypatch.setattr(deploy_routes, "_get_infra_agent", lambda: _AgentThatWorks())

    body = _post(client, "/api/deploy/dockerfile", {"workspace_path": workspace}).json()
    assert body["content"] == "FROM node:20-slim\n"
    assert not (body.get("risk_reasons") or []), "정상인데 폴백 안내가 붙었다"


# ---------------------------------------------------------------------------
# (2) 세 탭 모두 초안이 나온다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_type, expected_target",
    [
        ("/api/deploy/dockerfile", "dockerfile", "Dockerfile"),
        ("/api/deploy/compose", "docker_compose", "docker-compose.yml"),
        ("/api/deploy/github-actions", "github_actions", ".github/workflows/deploy.yml"),
    ],
)
def test_세_탭_모두_초안이_생성된다(client, workspace, path, expected_type, expected_target):
    """compose 는 라우트 자체가 없어 404 였다."""
    resp = _post(client, path, {"workspace_path": workspace})

    assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert body["file_type"] == expected_type
    assert body["target_path"] == expected_target
    assert body["content"].strip(), "초안이 비었다"


# ---------------------------------------------------------------------------
# (3) 실패해도 사람이 읽을 수 있는 메시지로
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/deploy/compose", "/api/deploy/github-actions"])
def test_없는_워크스페이스는_404와_경로를_알려준다(client, path):
    resp = _post(client, path, {"workspace_path": "/tmp/__그런_폴더_없음__"})
    assert resp.status_code == 404
    assert "__그런_폴더_없음__" in resp.json()["detail"], "어느 경로가 문제인지 안 알려준다"


@pytest.mark.parametrize("path", ["/api/deploy/compose", "/api/deploy/github-actions"])
def test_빈_워크스페이스_경로는_400(client, path):
    resp = _post(client, path, {"workspace_path": ""})
    assert resp.status_code == 400
    assert "workspace_path" in resp.json()["detail"]


def test_처리되지_않은_예외도_평문이_아니라_JSON_이유로_나온다(client, workspace, monkeypatch):
    """
    마지막 그물. 라우트를 하나 고쳐도 다른 경로에서 같은 증상이 나면 소용없다.

    예전 동작: 본문이 정확히 `Internal Server Error` (평문) → 확장이 그대로
    배너에 띄움 → 사용자는 원인도 다음 행동도 모름.
    """
    import api.routes.deploy as deploy_routes

    class _AgentThatExplodesOutsideHandledPath:
        async def generate_dockerfile(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    def _boom():
        raise MemoryError("의도적으로 라우트 밖에서 터뜨림")

    monkeypatch.setattr(deploy_routes, "_get_infra_agent", _boom)

    resp = _post(client, "/api/deploy/dockerfile", {"workspace_path": workspace})

    assert resp.status_code == 500
    assert resp.text.strip() != "Internal Server Error", (
        "여전히 평문 Internal Server Error 다 — 사용자에게 아무 정보가 없다"
    )
    detail = resp.json()["detail"]
    assert "MemoryError" in detail, "예외 종류가 없다"
    assert "의도적으로 라우트 밖에서 터뜨림" in detail, "원인 메시지가 없다"
    assert "/api/deploy/dockerfile" in detail, "어느 요청에서 났는지가 없다"


def test_음성대조_정상_요청은_전역_핸들러를_타지_않는다(client, workspace):
    """전역 핸들러가 정상 응답까지 500 으로 바꿔버리면 위 테스트는 무의미하다."""
    resp = _post(client, "/api/deploy/compose", {"workspace_path": workspace})
    assert resp.status_code == 200
    assert "detail" not in resp.json()


# ---------------------------------------------------------------------------
# 생성 → 승인 → 실제 파일 저장까지
# ---------------------------------------------------------------------------


def test_actions_초안이_승인시_워크스페이스에_저장된다(client, workspace):
    """이미 deploy.yml 이 있어도 덮어쓰기가 성공해야 한다(카드의 가설 검증)."""
    gh = pathlib.Path(workspace, ".github", "workflows")
    gh.mkdir(parents=True)
    (gh / "deploy.yml").write_text("name: 기존 워크플로\n", encoding="utf-8")

    proposal_id = _post(
        client, "/api/deploy/github-actions", {"workspace_path": workspace},
    ).json()["proposal_id"]

    resp = client.post(
        f"/api/deploy/github-actions/approve?proposal_id={proposal_id}&approved=true",
        headers={"X-Session-Token": TOKEN},
    )

    assert resp.status_code == 200, resp.text[:200]
    written = (gh / "deploy.yml").read_text(encoding="utf-8")
    assert "기존 워크플로" not in written, "덮어쓰기가 안 됐다"
    assert "name: CI" in written


def test_생성한_초안은_proposal_id로_다시_찾을_수_있다(client, workspace):
    """승인 단계가 in-memory 저장소에서 찾지 못하면 404 가 된다."""
    import api.routes.deploy as deploy_routes

    pid = _post(client, "/api/deploy/compose", {"workspace_path": workspace}).json()["proposal_id"]
    assert pid in deploy_routes._infra_proposals


def test_compose_초안이_유효한_YAML_구조를_갖는다(client, workspace):
    body = _post(client, "/api/deploy/compose", {"workspace_path": workspace}).json()
    content = body["content"]
    assert "services:" in content, f"compose 형태가 아니다: {content[:120]}"
    #: JSON 으로 파싱되면 안 된다 — 템플릿이 통째로 잘못 들어간 경우를 잡는다.
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)
