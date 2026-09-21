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
