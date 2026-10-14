"""로컬 Docker 배포: PC 포트 충돌을 계획 단계에서 피하고, 실행 단계 실패는 원인을 보여 준다."""
from types import SimpleNamespace

import build_failure
from api.routes import deploy


def _plan(port="3000", name="test-temp"):
    return SimpleNamespace(ports={port: "3000"}, container_name=name)


def test_다른_컨테이너가_쓰는_포트는_다음_빈_포트로(monkeypatch):
    users = {3000: ["temp"], 3001: []}
    monkeypatch.setattr(deploy, "_docker_port_publishers", lambda p: users.get(p, []))
    monkeypatch.setattr(deploy, "_host_port_listening", lambda p: p == 3000)
    plan = _plan()
    notes = deploy._resolve_local_host_ports(plan)
    assert plan.ports == {"3001": "3000"}
    assert "'temp'" in notes[0] and "3001" in notes[0] and "http://localhost:3001" in notes[0]


def test_교체할_자기_컨테이너가_쓰는_포트는_그대로(monkeypatch):
    monkeypatch.setattr(deploy, "_docker_port_publishers", lambda p: ["test-temp"])
    monkeypatch.setattr(deploy, "_host_port_listening", lambda p: True)
    plan = _plan()
    assert deploy._resolve_local_host_ports(plan) == []
    assert plan.ports == {"3000": "3000"}


def test_컨테이너가_아닌_프로그램이_쓰는_포트도_피한다(monkeypatch):
    monkeypatch.setattr(deploy, "_docker_port_publishers", lambda p: [])
    monkeypatch.setattr(deploy, "_host_port_listening", lambda p: p in (3000, 3001))
    plan = _plan()
    notes = deploy._resolve_local_host_ports(plan)
    assert plan.ports == {"3002": "3000"} and "다른 프로그램" in notes[0]


def test_docker_상태를_모르면_건드리지_않는다(monkeypatch):
    monkeypatch.setattr(deploy, "_docker_port_publishers", lambda p: None)
    monkeypatch.setattr(deploy, "_host_port_listening", lambda p: True)
    plan = _plan()
    assert deploy._resolve_local_host_ports(plan) == [] and plan.ports == {"3000": "3000"}


def test_docker_run_포트_충돌은_원인과_해결책으로():
    raw = ("docker: Error response from daemon: failed to set up container networking: driver failed programming "
           "external connectivity on endpoint test-temp (b23e): Bind for 0.0.0.0:3000 failed: port is already allocated")
    d = build_failure.diagnose(raw, stage="run")
    assert d.code == "HOST_PORT_IN_USE" and "3000" in d.cause and "docker ps --filter publish=3000" in d.fix
