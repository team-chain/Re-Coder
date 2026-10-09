"""승인 화면의 '실행할 명령' — 실행 경로와 같은 인자 조립 함수로 만들고, 설정값은 가린다."""
from __future__ import annotations

from pathlib import Path

import pytest

import deploy_settings as ds
import local_services as ls
from api.routes import deploy as routes
from schemas import ActionType, ApprovalLevel, DeploymentPlan, DeployMethod, RiskLevel
from tests.test_deploy_settings import shop


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "_HOME", tmp_path / "ls")
    monkeypatch.setattr(ls, "_docker_name_exists", lambda name: False)


def make_plan(**kw) -> DeploymentPlan:
    base = dict(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN, image="shop:latest",
                container_name="shop", ports={"3002": "3001"}, env={}, risk_level=RiskLevel.MEDIUM,
                risk_reasons=["Local Docker run — container will be exposed on localhost"],
                approval_level=ApprovalLevel.CONFIRM)
    base.update(kw)
    return DeploymentPlan(**base)


def test_데모_모드는_네트워크_DB_모의결제_상품_빌드_교체_실행_순서로_보이고_값은_가린다(tmp_path):
    ws = shop(tmp_path / "ws")
    (ws / "Dockerfile").write_text("FROM node:22-alpine\n")
    (ws / "backend/init-db.js").write_text("// seed")
    env, _ = ls.plan("shop", ["postgres"], ["DATABASE_URL"])
    ds.set_demo("shop", True)
    plan = make_plan(env=env, companions=["postgres"])
    routes._apply_settings_to_plan(plan, str(ws))
    cmds = [s["command"] for s in plan.command_steps]
    assert cmds[0] == "docker build -f Dockerfile -t shop:latest ."
    assert cmds[1] == "docker network create recoder-shop"
    assert "--name shop-postgres-demo" in cmds[2] and "POSTGRES_PASSWORD=***" in cmds[2] and "POSTGRES_USER=recoder" in cmds[2]
    assert "--name shop-payment-mock" in cmds[3] and "<모의 결제 서버 코드>" in cmds[3] and "MOCK_WEBHOOK_SECRET=***" in cmds[3]
    assert cmds[4].endswith("shop:latest backend/init-db.js")
    assert cmds[5] == "docker stop shop && docker rm shop"
    run = cmds[6]
    assert run.startswith("docker run -d --name shop --label ") and "-p 3002:3001" in run
    assert "-e DATABASE_URL=***" in run and "-e JWT_SECRET=***" in run and "-e PAYMENT_MODE=mock" in run
    assert "--network recoder-shop" in run and run.endswith("--restart unless-stopped shop:latest")
    # 값이 화면으로 새지 않는다
    secret = ds.load("shop")["generated"]["JWT_SECRET"]
    password = ls._load("shop")["passwords"]["postgres"]
    joined = "\n".join(cmds)
    assert secret not in joined and password not in joined
    assert all(s["note"] for s in plan.command_steps)


def test_위험_사유는_한글로_지금_포트를_말한다(tmp_path):
    ws = shop(tmp_path / "ws")
    plan = make_plan()
    routes._apply_settings_to_plan(plan, str(ws))
    assert plan.risk_reasons[0].startswith("이 PC 의 포트 3002 로 열립니다 — http://localhost:3002")
    assert not any("Local Docker run" in r for r in plan.risk_reasons)
    plan.ports = {"3005": "3001"}
    routes._apply_settings_to_plan(plan, str(ws))
    assert plan.risk_reasons[0].startswith("이 PC 의 포트 3005") and sum("이 PC 의 포트" in r for r in plan.risk_reasons) == 1


def test_DB_없고_데모_아니면_빌드_교체_실행만(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}")
    (ws / ".env").write_text("API_KEY=abcdef123456\n")
    plan = make_plan(env_files=[".env"])
    routes._apply_settings_to_plan(plan, str(ws))
    cmds = [s["command"] for s in plan.command_steps]
    assert cmds[0].startswith("docker build -f Dockerfile") and len(cmds) == 3
    assert "-e API_KEY=***" in cmds[2] and "abcdef123456" not in cmds[2] and "--network" not in cmds[2]


def test_실행과_미리보기가_같은_조립_함수를_쓴다():
    src = Path(ls.__file__).read_text(encoding="utf-8")
    assert "run(service_run_args(container, kind" in src
    assert "run(mock_run_args(container, image, app_port, secret)" in Path(ds.__file__).read_text(encoding="utf-8")
