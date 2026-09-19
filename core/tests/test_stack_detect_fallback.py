"""스택 감지 오분류 — 보드 이슈 「스택 감지가 순수 파이썬을 python-fastapi 로 분류」.

무엇이 사고였나
    라우트용 _detect_stack 은 파이썬 프로젝트에서 프레임워크를 못 찾으면
    PYTHON_FASTAPI 를 **기본값**으로 돌려줬고, InfraAgent 쪽 템플릿 선택도
    모르는 스택이면 fastapi 템플릿을 잡았다. 그 템플릿의 실행 명령은
    `uvicorn main:app ...` — uvicorn 이 없는 프로젝트는 컨테이너가 뜨자마자
    죽는다. 분류가 틀렸는데 화면에는 정상 생성된 Dockerfile 로 보였다.

여기서 고정하는 것 (DoD)
    1. requirements 에 fastapi/uvicorn 이 없는 파이썬 프로젝트는
       python-fastapi 로 분류되지 않는다.
    2. 모르는 스택은 fastapi 템플릿으로 위장되지 않고 명시적 안내로 실패한다.
    3. 분류 결과와 Dockerfile CMD 가 맞는다 — 템플릿별 실행 명령을 고정.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from api.routes import deploy  # noqa: E402
from agents.infra_agent import InfraAgent  # noqa: E402
from schemas import StackType  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 감지 — 기본값 fastapi 가 사라졌는지
# ---------------------------------------------------------------------------


def test_순수_파이썬은_fastapi_로_분류되지_않는다(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\npandas\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    assert deploy._detect_stack(str(tmp_path)) == StackType.UNKNOWN


def test_requirements_의_fastapi_선언만으로_분류된다(tmp_path) -> None:
    """소스에 import 가 아직 없어도 의존성 선언이 근거다."""
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    assert deploy._detect_stack(str(tmp_path)) == StackType.PYTHON_FASTAPI


def test_flask_프로젝트는_flask_로_분류된다(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")

    assert deploy._detect_stack(str(tmp_path)) == StackType.PYTHON_FLASK


# ---------------------------------------------------------------------------
# 2. 템플릿 선택 — 모르는 스택은 위장하지 않는다
# ---------------------------------------------------------------------------


def test_UNKNOWN_스택은_명시적_안내로_실패한다(tmp_path) -> None:
    with pytest.raises(deploy._UnsupportedDockerfileFallback) as exc:
        deploy._dockerfile_from_template(str(tmp_path), StackType.UNKNOWN, None)
    #: 사용자가 다음에 뭘 해야 하는지가 메시지에 있어야 한다.
    assert "requirements" in str(exc.value)


def test_에이전트_템플릿_선택도_기본값_fastapi_가_없다() -> None:
    for stack in (StackType.UNKNOWN, StackType.GO, StackType.JAVA_SPRING):
        with pytest.raises(ValueError):
            InfraAgent._pick_dockerfile_template(stack)


def test_음성대조_아는_스택은_그대로_매핑된다() -> None:
    assert InfraAgent._pick_dockerfile_template(StackType.PYTHON_FASTAPI) == "Dockerfile.python-fastapi"
    assert InfraAgent._pick_dockerfile_template(StackType.NODE_EXPRESS) == "Dockerfile.node-express"


# ---------------------------------------------------------------------------
# 3. 분류 ↔ 실행 명령 일치 고정
# ---------------------------------------------------------------------------


_TEMPLATES = _CORE / "registry" / "file_templates"

#: 템플릿이 이 실행 명령을 잃으면(또는 서로 뒤바뀌면) 분류가 맞아도 컨테이너가 죽는다.
_CMD_MARKERS = {
    "Dockerfile.python-fastapi": "uvicorn",
    "Dockerfile.python-flask": "flask",
    "Dockerfile.node-express": "node",
    "Dockerfile.node-next": "next",
}


@pytest.mark.parametrize("template_name,marker", sorted(_CMD_MARKERS.items()))
def test_템플릿_실행_명령이_스택과_일치한다(template_name: str, marker: str) -> None:
    path = _TEMPLATES / template_name
    assert path.exists(), f"템플릿이 없다: {template_name}"
    content = path.read_text(encoding="utf-8").lower()
    assert marker in content, f"{template_name} 의 실행 명령에 '{marker}' 가 없다"
