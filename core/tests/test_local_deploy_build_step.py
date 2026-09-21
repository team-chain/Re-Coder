"""로컬 Docker 배포의 빌드 단계 (실기기 검증 C2 회귀).

허브·승인 화면은 `docker build && docker run` 이라고 했지만 코어는 run 만 했고,
이미지가 없어 "pull access denied for <이미지>" 로 죽었다. 이제 execute 는
임계 구역 밖에서 먼저 빌드하고, 실패하면 돌고 있던 컨테이너를 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from api.routes import deploy as d
from schemas import ActionType, DeployMethod, DeploymentPlan


def _plan(image="recoder-sample:latest"):
    return DeploymentPlan(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN,
                          image=image, container_name="recoder-sample", ports={"3000": "3000"})


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_Dockerfile_있으면_워크스페이스에서_docker_build_를_돈다(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    calls = []
    monkeypatch.setattr(d.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or _Proc(0))

    assert asyncio.run(d._build_local_image(_plan(), str(tmp_path))) is None
    cmd, kw = calls[0]
    assert cmd[:2] == ["docker", "build"] and "-t" in cmd and "recoder-sample:latest" in cmd
    assert kw["cwd"] == str(tmp_path) and kw["shell"] is False


def test_빌드_실패는_stderr_와_함께_실패로_끝난다_컨테이너는_안_건드림(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    monkeypatch.setattr(d.subprocess, "run", lambda cmd, **kw: _Proc(1, err="npm ERR! missing script"))

    r = asyncio.run(d._build_local_image(_plan(), str(tmp_path)))

    assert r["status"] == "failed" and r["stage"] == "build"
    assert "npm ERR!" in r["stderr"]
    assert "이전 컨테이너는 건드리지 않았습니다" in r["message"]


def test_Dockerfile_도_이미지도_없으면_빌드_불가를_말한다(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_local_image_exists", lambda image: False)
    r = asyncio.run(d._build_local_image(_plan(), str(tmp_path)))
    assert r["status"] == "failed" and "Dockerfile" in r["message"]


def test_Dockerfile_없어도_이미지가_있으면_그대로_쓴다(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_local_image_exists", lambda image: True)
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("빌드 금지")))
    assert asyncio.run(d._build_local_image(_plan(), str(tmp_path))) is None


def test_로컬_Docker_가_아니면_빌드_단계를_건너뛴다(monkeypatch, tmp_path):
    plan = DeploymentPlan(method=DeployMethod.SSH_DIRECT, action=ActionType.DOCKER_RUN, image=None)
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("빌드 금지")))
    assert asyncio.run(d._build_local_image(plan, str(tmp_path))) is None


def test_빌드_타임아웃은_예외_없이_실패_응답(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(d.subprocess, "run", boom)
    r = asyncio.run(d._build_local_image(_plan(), str(tmp_path)))
    assert r["status"] == "failed" and "끝나지 않았습니다" in r["message"]


# ── 포트 감지: Dockerfile EXPOSE > 소스 listen() > package.json 추측 ──
from agents.deploy_agent import DeployAgent  # noqa: E402


def test_포트는_Dockerfile_EXPOSE_를_최우선으로_본다(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node src/app.js"}}')
    (tmp_path / "Dockerfile").write_text("FROM node:20-alpine\nEXPOSE 3456\nCMD [\"node\",\"src/app.js\"]\n")
    assert DeployAgent._detect_port(str(tmp_path)) == (3456, 3456)


def test_Dockerfile_없으면_소스의_listen_포트를_본다(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node src/app.js"}}')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text('app.listen(3456, () => {});\n')
    assert DeployAgent._detect_port(str(tmp_path)) == (3456, 3456)


def test_아무_단서_없는_node_는_예전처럼_3000(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node index.js"}}')
    assert DeployAgent._detect_port(str(tmp_path)) == (3000, 3000)


# ── Dockerfile 생성용 Node 진입점·포트 감지 (index.js/3000 고정 추측 회귀) ──
import infra_agent  # noqa: E402


def test_node_진입점은_start_스크립트_main_순으로_읽고_포트는_소스에서_읽는다(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text('const PORT = process.env.PORT || 3456;\napp.listen(PORT);\n')
    (tmp_path / "package.json").write_text(
        '{"main":"src/app.js","scripts":{"start":"node src/app.js"},"dependencies":{"express":"^4"}}')
    stack, meta = infra_agent._detect_stack(str(tmp_path))
    assert stack == "node-express"
    assert meta["entrypoint"] == "src/app.js"
    assert meta["port"] == "3456"


def test_node_단서_없으면_예전_기본값_index_js_3000(tmp_path):
    (tmp_path / "index.js").write_text('app.listen(process.env.PORT);\n')
    (tmp_path / "package.json").write_text('{"dependencies":{"express":"^4"}}')
    stack, meta = infra_agent._detect_stack(str(tmp_path))
    assert meta["entrypoint"] == "index.js" and meta["port"] == "3000"


def test_플랜_포트는_PORT_기본값_패턴도_읽는다(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node src/app.js"}}')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text('const PORT = process.env.PORT || 3456;\napp.listen(PORT);\n')
    assert DeployAgent._detect_port(str(tmp_path)) == (3456, 3456)


# ── Dockerfile 생성 경로가 실제로 쓰는 project_scanner 도 같은 값을 내야 한다 ──
from project_scanner import ProjectScanner  # noqa: E402


def test_project_scanner_는_node_진입점과_포트를_프로젝트에서_읽는다(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text('const PORT = process.env.PORT || 3456;\napp.listen(PORT);\n')
    (tmp_path / "package.json").write_text(
        '{"main":"src/app.js","scripts":{"start":"node src/app.js"},"dependencies":{"express":"^4"}}')
    profile = ProjectScanner().scan(str(tmp_path))
    assert profile.default_port == 3456
    assert profile.default_run_command == "node src/app.js"


# ── Dockerfile 생성 프롬프트의 런타임 힌트 (EOL node 18 선택 회귀) ──
from agents import infra_agent as ia  # noqa: E402
from schemas import ProjectStack  # noqa: E402


def test_런타임_힌트는_nvmrc_engines_를_읽고_없으면_최신_LTS(tmp_path):
    (tmp_path / "package.json").write_text('{"engines":{"node":">=20"}}')
    assert "Node.js 20" in ia._runtime_hint(str(tmp_path), ProjectStack.NODE_EXPRESS)
    (tmp_path / ".nvmrc").write_text("v22.1.0\n")
    assert "Node.js 22.1.0" in ia._runtime_hint(str(tmp_path), ProjectStack.NODE_EXPRESS)
    (tmp_path / ".nvmrc").unlink(); (tmp_path / "package.json").write_text('{}')
    assert "Node.js 22" in ia._runtime_hint(str(tmp_path), ProjectStack.NODE_EXPRESS)


def test_프롬프트에_EOL_금지와_OS_패치_규칙이_있다():
    assert "end-of-life" in ia._DOCKERFILE_CUSTOMISE_PROMPT
    assert "{runtime_hint}" in ia._DOCKERFILE_CUSTOMISE_PROMPT
    assert "apk upgrade" in ia._DOCKERFILE_CUSTOMISE_PROMPT


def test_node_템플릿은_현재_LTS_와_OS_패치_단계를_쓴다():
    from registries import file_registry as fr
    assert "FROM node:22-slim" in fr._DOCKERFILE_NODE_EXPRESS
    assert "apt-get upgrade -y" in fr._DOCKERFILE_NODE_EXPRESS


def test_프롬프트와_템플릿이_번들_npm_tar_CVE_를_다룬다():
    from registries import file_registry as fr
    assert "npm@latest" in ia._DOCKERFILE_CUSTOMISE_PROMPT and "node_modules/npm" in ia._DOCKERFILE_CUSTOMISE_PROMPT
    assert "rm -rf /usr/local/lib/node_modules/npm" in fr._DOCKERFILE_NODE_EXPRESS
    assert "npm install -g npm@latest" in fr._DOCKERFILE_NODE_NEXT


def test_모델이_EOL_node_를_골라도_22_로_강제하고_포트_진입점은_프로필_값(tmp_path):
    from schemas import ProjectProfile
    profile = ProjectProfile(workspace_path=str(tmp_path), stack=ProjectStack.NODE_EXPRESS,
                             default_port=3456, default_run_command="node src/app.js")
    out = ia.InfraAgent._enforce_safe_customisations(
        {"NODE_VERSION": "18", "PORT": "3000", "START_SCRIPT": "index.js", "APP_NAME": "x"},
        ProjectStack.NODE_EXPRESS, profile)
    assert out["NODE_VERSION"] == "22" and out["PORT"] == "3456" and out["START_SCRIPT"] == "src/app.js"
    assert out["APP_NAME"] == "x"
    assert ia.InfraAgent._enforce_safe_customisations({"NODE_VERSION": "20.11"}, ProjectStack.NODE_EXPRESS, profile)["NODE_VERSION"] == "20.11"


def test_실제_node_템플릿에_OS_패치와_npm_제거가_있다():
    from pathlib import Path
    tpl = (Path(ia.__file__).resolve().parent.parent / "registry" / "file_templates" / "Dockerfile.node-express").read_text(encoding="utf-8")
    runtime = tpl[tpl.index("AS runtime"):]
    assert "apk upgrade --no-cache" in runtime
    assert "rm -rf /usr/local/lib/node_modules/npm" in runtime
