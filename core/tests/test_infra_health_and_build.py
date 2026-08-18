"""
PR #24 코드리뷰 — compose 헬스체크 · build 참조 · Next 헬스 경로

세 건 모두 **틀려도 예외가 안 나는** 종류다. 파일은 정상적으로 생성되고,
제안은 "성공" 으로 저장되고, 테스트는 초록이다. 문제는 사용자가
`docker compose up` 을 한 다음에야 드러난다 — 그래서 여기서 잡는다.

1. compose 의 앱 헬스체크가 `wget || curl` 이었다.
   우리가 만들어 주는 이미지(python:slim · node:slim)에 그 둘이 **모두 없다.**
   앱이 멀쩡히 떠 있어도 compose 는 영구히 unhealthy 로 보고하고,
   `depends_on: condition: service_healthy` 가 걸린 쪽은 아예 못 뜬다.

2. compose 의 `build:` 가 같은 폴더의 Dockerfile 을 가리키는데, Compose 탭은
   Dockerfile 탭보다 먼저 눌릴 수 있고 이 경로는 Dockerfile 을 만들지 않는다.

3. Dockerfile.node-next 의 헬스 경로가 하드코딩 `/api/health` 에서
   `{{HEALTH_CHECK_PATH}}` 로 바뀌었는데, 스캐너는 **항상** `/health` 를 넣는다.
   Next.js 는 `/api/` 아래에 라우트를 두므로 없는 경로를 찌르게 된다.
   이 PR 이 만든 회귀다.
"""
import pathlib

import pytest
import yaml

import infra_agent
from registries.file_registry import get_file_registry
from schemas import ProjectProfile, StackType

try:
    from api.routes import deploy
except ImportError:  # pragma: no cover - 저장소 루트에서 실행할 때
    from core.api.routes import deploy


# ---------------------------------------------------------------------------
# 1. compose 헬스체크 — 이미지에 실제로 있는 명령을 쓰는가
# ---------------------------------------------------------------------------


def _workspace(tmp_path: pathlib.Path, **files: str) -> pathlib.Path:
    for name, content in files.items():
        target = tmp_path / name.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def _app_service(content: str) -> dict:
    """생성된 compose 를 **실제로 YAML 파싱**해서 app 서비스를 꺼낸다.

    문자열 검사만 하면 인용이 깨진 YAML 도 통과한다. 헬스체크 명령에는
    따옴표가 섞이므로 파싱 자체가 검사의 일부다.
    """
    return yaml.safe_load(content)["services"]["app"]


def test_python_스택은_python으로_헬스체크한다(tmp_path):
    """python:slim 에는 wget 도 curl 도 없다. python 은 반드시 있다."""
    ws = _workspace(tmp_path, **{"requirements.txt": "fastapi\nuvicorn\n"})
    proposal = infra_agent.generate_docker_compose(None, str(ws))

    test = _app_service(proposal.content)["healthcheck"]["test"]
    assert test[0] == "CMD", "CMD-SHELL 은 따옴표 이스케이프가 깨지기 쉽다"
    assert test[1] == "python"
    assert "urllib.request" in test[3]


def test_node_스택은_node로_헬스체크한다(tmp_path):
    ws = _workspace(tmp_path, **{"package.json": '{"name":"x","dependencies":{"express":"^4"}}'})
    proposal = infra_agent.generate_docker_compose(None, str(ws))

    test = _app_service(proposal.content)["healthcheck"]["test"]
    assert test[1] == "node"
    assert "require('http')" in test[3]


@pytest.mark.parametrize("deps,label", [
    ("fastapi\npsycopg2-binary\n", "postgres"),
    ("fastapi\npymysql\n", "mysql"),
])
def test_DB_변형도_같은_규칙을_따른다(tmp_path, deps, label):
    """DB 템플릿에도 같은 헬스체크가 복사돼 있었다 — 한 곳만 고치면 남는다."""
    ws = _workspace(tmp_path / label, **{"requirements.txt": deps})
    proposal = infra_agent.generate_docker_compose(None, str(ws))

    assert proposal.base_template == "db-multi"
    assert _app_service(proposal.content)["healthcheck"]["test"][1] == "python"


def test_어떤_compose_템플릿도_앱을_wget이나_curl로_검사하지_않는다():
    """**이 검사가 없어서 놓쳤다.**

    Dockerfile 템플릿 쪽은 이미 python urllib 로 고쳤는데 compose 만 남았다.
    템플릿이 셋(단일·postgres·mysql)이라 한 곳만 고치면 조용히 살아남는다.
    """
    registry = get_file_registry()
    for template_id in ("docker-compose", "docker-compose-db", "docker-compose-mysql"):
        body = registry.get(template_id).base_content
        assert "wget" not in body, f"{template_id} 가 아직 wget 을 쓴다"
        assert "curl" not in body, f"{template_id} 가 아직 curl 을 쓴다"


def test_음성대조_DB_서비스의_헬스체크는_그대로_남아야_한다(tmp_path):
    """앱 헬스체크만 손대야 한다. DB 것까지 지우면 depends_on 이 무의미해진다."""
    ws = _workspace(tmp_path, **{"requirements.txt": "fastapi\npsycopg2-binary\n"})
    doc = yaml.safe_load(infra_agent.generate_docker_compose(None, str(ws)).content)

    db_test = doc["services"]["db"]["healthcheck"]["test"]
    assert "pg_isready" in " ".join(db_test)
    assert doc["services"]["app"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_모르는_스택이면_헬스체크를_아예_넣지_않는다():
    """**항상 실패하는 헬스체크는 없는 것보다 나쁘다** — 재시작 루프를 만든다."""
    assert infra_agent.compose_health_check_block("custom", "8000", "/health") == ""
    assert infra_agent.compose_health_check_block("", "8000", "/health") == ""


def test_헬스체크가_빠져도_compose_는_유효한_YAML_이다(tmp_path):
    """빈 블록을 끼워 넣다가 들여쓰기가 깨지면 파일 전체가 못 쓰게 된다."""
    ws = _workspace(tmp_path, **{"go.mod": "module x\n"})
    #: Go 는 자동 감지 대상이 아니라 프로필로만 들어온다.
    profile = _profile(ws, StackType.GO)
    doc = yaml.safe_load(infra_agent.generate_docker_compose(profile, str(ws)).content)

    assert "healthcheck" not in doc["services"]["app"]
    assert doc["services"]["app"]["build"]["dockerfile"] == "Dockerfile"


# ---------------------------------------------------------------------------
# 2. build 가 가리키는 Dockerfile
# ---------------------------------------------------------------------------


def test_Dockerfile_이_없으면_먼저_만들라고_알려준다(tmp_path):
    """제안은 '성공' 으로 저장되지만 docker compose up 은 빌드에서 실패한다.

    사용자에게는 이유가 안 보인다 — 그래서 제안에 그대로 담아 보낸다.
    """
    ws = _workspace(tmp_path, **{"requirements.txt": "fastapi\n"})
    proposal = infra_agent.generate_docker_compose(None, str(ws))

    assert proposal.risk_reasons, "Dockerfile 이 없는데 아무 말도 없다"
    joined = " ".join(proposal.risk_reasons)
    assert "Dockerfile" in joined
    assert "탭" in joined, "무엇을 해야 하는지 안 알려준다"


def test_자동생성이_안_되는_스택에는_그_탭으로_보내지_않는다(tmp_path):
    """안내대로 따라갔더니 422 가 나오면, 사용자는 할 수 있는 게 없다."""
    ws = _workspace(tmp_path, **{"go.mod": "module x\n"})
    proposal = infra_agent.generate_docker_compose(_profile(ws, StackType.GO), str(ws))

    joined = " ".join(proposal.risk_reasons)
    assert joined, "Dockerfile 이 없는데 아무 말도 없다"
    assert "탭" not in joined, "만들어 주지 못하는 탭으로 보낸다"
    assert "직접 추가" in joined


def test_pyproject만_있어도_헬스체크가_들어간다(tmp_path):
    """Poetry 만 쓰는 FastAPI 는 스캐너가 custom 으로 분류한다.

    스택 이름만 보면 헬스체크가 통째로 빠져서, Dockerfile 에는 있고
    compose 에는 없는 어긋난 상태가 된다.
    """
    ws = _workspace(tmp_path, **{"pyproject.toml": "[project]\nname='x'\n"})
    proposal = infra_agent.generate_docker_compose(_profile(ws, StackType.CUSTOM), str(ws))

    test = _app_service(proposal.content)["healthcheck"]["test"]
    assert test[1] == "python"


def test_두_파일이_같은_헬스_경로를_찌른다(tmp_path):
    """**판단이 두 군데로 갈리면 아무도 눈치채지 못한다.**

    헬스체크가 틀려도 예외가 아니라 "영원히 unhealthy" 로 나타난다.
    """
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "app" / "health").mkdir(parents=True)
    (tmp_path / "app" / "health" / "route.ts").write_text("export function GET(){}", encoding="utf-8")
    profile = _profile(tmp_path, StackType.NODE_NEXT)

    dockerfile, _ = deploy._dockerfile_from_template(str(tmp_path), StackType.NODE_NEXT, profile)
    compose = infra_agent.generate_docker_compose(profile, str(tmp_path)).content
    compose_test = " ".join(yaml.safe_load(compose)["services"]["app"]["healthcheck"]["test"])

    assert "/health" in compose_test and "/api/health" not in compose_test
    assert "localhost:3000/health" in dockerfile, "Dockerfile 이 다른 경로를 찌른다"


def test_음성대조_Dockerfile_이_있으면_그_경고는_없다(tmp_path):
    """항상 경고하면 위 테스트는 아무것도 증명하지 못한다."""
    ws = _workspace(
        tmp_path,
        **{"requirements.txt": "fastapi\n", "Dockerfile": "FROM python:3.11-slim\n"},
    )
    proposal = infra_agent.generate_docker_compose(None, str(ws))

    assert not [r for r in proposal.risk_reasons if "Dockerfile" in r]


# ---------------------------------------------------------------------------
# 3. Next.js 헬스 경로
# ---------------------------------------------------------------------------


def _profile(ws: pathlib.Path, stack: StackType, health: str = "/health") -> ProjectProfile:
    return ProjectProfile(
        workspace_path=str(ws), stack=stack, default_port=3000, health_check_path=health,
    )


def test_Next_는_기본적으로_api_health_를_찌른다(tmp_path):
    """스캐너는 항상 /health 를 넣는다 — Next 에서 그건 거의 확실히 404 다."""
    profile = _profile(tmp_path, StackType.NODE_NEXT)
    assert deploy._discover_health_path(str(tmp_path), StackType.NODE_NEXT, profile) == "/api/health"


@pytest.mark.parametrize("route_file,expected", [
    ("app/api/health/route.ts", "/api/health"),
    ("src/app/api/health/route.js", "/api/health"),
    ("pages/api/health.ts", "/api/health"),
    ("app/health/route.ts", "/health"),
])
def test_Next_의_실제_라우트를_찾아_쓴다(tmp_path, route_file, expected):
    target = tmp_path / route_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export function GET(){}", encoding="utf-8")

    profile = _profile(tmp_path, StackType.NODE_NEXT)
    assert deploy._discover_health_path(str(tmp_path), StackType.NODE_NEXT, profile) == expected


def test_사람이_직접_정한_경로는_존중한다(tmp_path):
    """탐지가 사람 설정을 덮어쓰면, 고쳐도 계속 틀린 곳을 찌른다."""
    profile = _profile(tmp_path, StackType.NODE_NEXT, health="/healthz")
    assert deploy._discover_health_path(str(tmp_path), StackType.NODE_NEXT, profile) == "/healthz"


def test_음성대조_Next_가_아닌_스택은_health_그대로(tmp_path):
    """Next 규약을 전 스택에 퍼뜨리면 FastAPI 쪽이 반대로 깨진다."""
    profile = _profile(tmp_path, StackType.PYTHON_FASTAPI)
    got = deploy._discover_health_path(str(tmp_path), StackType.PYTHON_FASTAPI, profile)
    assert got == "/health"


def test_렌더된_Next_Dockerfile_이_api_health_를_찌른다(tmp_path):
    """단위 함수만 고치고 템플릿에 안 이어지면 실제 파일은 그대로다."""
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    profile = _profile(tmp_path, StackType.NODE_NEXT)

    content, template_id = deploy._dockerfile_from_template(
        str(tmp_path), StackType.NODE_NEXT, profile,
    )
    assert template_id == "Dockerfile.node-next"
    assert "/api/health" in content
    assert "localhost:3000/health" not in content


def test_음성대조_Express_Dockerfile_은_health_를_유지한다(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    profile = _profile(tmp_path, StackType.NODE_EXPRESS)

    content, _ = deploy._dockerfile_from_template(
        str(tmp_path), StackType.NODE_EXPRESS, profile,
    )
    assert "/api/health" not in content
