"""로컬 Docker 배포의 '필요한 설정' — 빌드 전에 알리고, 키 없이 데모로, 값은 기록에 남기지 않는다."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import build_failure
import deploy_settings as ds

AUTH = """import jwt from 'jsonwebtoken';
const JWT_SECRET = process.env.JWT_SECRET;
if (!JWT_SECRET || JWT_SECRET.length < 32) {
  throw new Error('JWT_SECRET must be set and at least 32 characters long');
}
"""
PAYMENT = """const STRIPE_SECRET_KEY = process.env.STRIPE_SECRET_KEY;
const PAYMENT_MODE = process.env.PAYMENT_MODE;
const STRIPE_MOCK_HOST = process.env.STRIPE_MOCK_HOST;
const STRIPE_MOCK_PORT = process.env.STRIPE_MOCK_PORT;
if (!STRIPE_SECRET_KEY) {
  throw new Error('STRIPE_SECRET_KEY environment variable is required');
}
if (PAYMENT_MODE === 'mock') {
  if (!STRIPE_MOCK_HOST || !STRIPE_MOCK_PORT) {
    throw new Error('STRIPE_MOCK_HOST and STRIPE_MOCK_PORT are required when PAYMENT_MODE=mock');
  }
}
"""
SERVER = """if (!process.env.STRIPE_WEBHOOK_SECRET) {
  console.error('ERROR: STRIPE_WEBHOOK_SECRET must be set');
  process.exit(1);
}
const optional = process.env.FEATURE_FLAG || 'off';
"""


def shop(tmp_path: Path, demo: bool = True) -> Path:
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "backend/src/auth.js").write_text(AUTH)
    (tmp_path / "backend/src/payment.js").write_text(PAYMENT if demo else PAYMENT.split("if (PAYMENT_MODE")[0].replace(
        "const PAYMENT_MODE = process.env.PAYMENT_MODE;\n", ""))
    (tmp_path / "backend/src/server.js").write_text(SERVER)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/x.js").write_text("if (!process.env.VITE_ONLY) throw new Error('x')")
    return tmp_path


def test_시작할_때_없으면_종료하는_값만_필요한_설정으로_찾는다(tmp_path):
    req = ds.required_env(str(shop(tmp_path)))
    assert req == {"JWT_SECRET": 32, "STRIPE_SECRET_KEY": 0, "STRIPE_WEBHOOK_SECRET": 0}
    # 모의 결제 모드일 때만 필요한 값·선택값·프론트엔드 값은 묻지 않는다.


def test_내부_서명_키는_자동으로_만들고_외부_키는_입력을_기다린다(tmp_path):
    ws = str(shop(tmp_path))
    state = ds.evaluate("shop", ws, {"DATABASE_URL"})
    by = {s["name"]: s for s in state["settings"]}
    assert by["JWT_SECRET"]["source"] == "generated"
    assert state["missing"] == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]
    assert state["demo"]["available"] is True and state["demo"]["enabled"] is False
    secret = ds.load("shop")["generated"]["JWT_SECRET"]
    assert len(secret) >= 32
    # 다시 계획해도 같은 키를 쓴다(로그인 토큰이 배포마다 무효가 되지 않게).
    ds.evaluate("shop", ws, set())
    assert ds.load("shop")["generated"]["JWT_SECRET"] == secret
    assert secret not in json.dumps(state)  # 화면에 보내는 상태에는 값이 없다


def test_PC_env_에_있는_값은_묻지도_덮어쓰지도_않는다(tmp_path):
    ws = str(shop(tmp_path))
    state = ds.evaluate("shop", ws, {"STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "JWT_SECRET"})
    assert state["missing"] == [] and all(s["source"] == "provided" for s in state["settings"])
    ds.save_values("shop", {"STRIPE_SECRET_KEY": "sk_test_saved"})
    env = ds.runtime_env("shop", ws, {"STRIPE_SECRET_KEY"})
    assert "STRIPE_SECRET_KEY" not in env


def test_입력한_키와_데모_전환(tmp_path):
    ws = str(shop(tmp_path))
    ds.save_values("shop", {"STRIPE_SECRET_KEY": "sk_test_abc", "STRIPE_WEBHOOK_SECRET": "whsec_abc"})
    assert ds.evaluate("shop", ws, set())["missing"] == []
    env = ds.runtime_env("shop", ws, set())
    assert env["STRIPE_SECRET_KEY"] == "sk_test_abc" and "PAYMENT_MODE" not in env
    ds.save_values("shop", {"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": ""})
    assert ds.evaluate("shop", ws, set())["missing"]
    ds.set_demo("shop", True)
    state = ds.evaluate("shop", ws, set())
    assert state["missing"] == [] and state["demo"]["enabled"] is True
    env = ds.runtime_env("shop", ws, {"NODE_ENV"})
    assert env["NODE_ENV"] == "test" and env["PAYMENT_MODE"] == "mock"
    assert env["STRIPE_MOCK_HOST"] == "shop-payment-mock" and env["STRIPE_WEBHOOK_SECRET"].startswith("whsec_local_demo_")
    with pytest.raises(ValueError):
        ds.save_values("shop", {"STRIPE_SECRET_KEY": "a\nb"})
    with pytest.raises(ValueError):
        ds.save_values("shop", {"bad name": "x"})


def test_모의_결제를_지원하지_않는_앱은_데모를_내놓지_않는다(tmp_path):
    state = ds.evaluate("plain", str(shop(tmp_path, demo=False)), set())
    assert state["demo"]["available"] is False and state["missing"]


def test_모의_결제_서버는_앱_이미지의_node_로_띄우고_네트워크를_돌려준다(tmp_path):
    calls = []

    def run(args, timeout=0):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:3] == ["docker", "network", "inspect"] else 0, "", "")

    args = ds.ensure_demo("shop", "shop:latest", 3001, run=run)
    assert args == ["--network", "shop-net"] or args[0] == "--network"
    started = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--entrypoint" in started and started[started.index("--entrypoint") + 1] == "node"
    assert "shop:latest" in started and any(a.startswith("MOCK_WEBHOOK_URL=http://shop:3001/") for a in started)


def test_모의_결제_서버_코드는_문법이_맞다(tmp_path):
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("node 없음")
    script = tmp_path / "mock.js"
    script.write_text(ds.MOCK_PAYMENT_JS)
    assert subprocess.run([node, "--check", str(script)], capture_output=True).returncode == 0


def test_설정값이_없어_종료한_로그는_코드_수정이_아니라_설정_입력으로_안내한다():
    log = ("file:///app/backend/src/auth.js:8\n  throw new Error('JWT_SECRET must be set and at least 32 characters long');\n"
           "        ^\nError: JWT_SECRET must be set and at least 32 characters long\n    at file:///app/backend/src/auth.js:8:9\n")
    d = build_failure.diagnose(log, stage="run")
    assert d.code == "APP_MISSING_SETTING" and d.missing_env == ["JWT_SECRET"]
    assert "코드 오류가 아닙니다" in d.cause and "코드를 고친" not in d.fix
    other = build_failure.diagnose("TypeError: x is not a function\n    at /app/a.js:3:1\n", stage="run")
    assert other.code == "APP_START_ERROR"


# ── 배포 경로 ───────────────────────────────────────────────────────────────────
import asyncio

from fastapi import HTTPException

from api.routes import deploy as routes
from schemas import ActionType, DeployMethod, DeploymentPlan


def _plan(container="shop"):
    return DeploymentPlan(method=DeployMethod.LOCAL_DOCKER, action=ActionType.DOCKER_RUN, image=f"{container}:latest",
                          container_name=container, ports={"3001": "3001"}, env={"DATABASE_URL": "postgres://x"})


def test_계획에_필요한_설정이_값_없이_실린다(tmp_path):
    ws = str(shop(tmp_path))
    plan = _plan()
    routes._apply_settings_to_plan(plan, ws)
    assert plan.settings_missing == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]
    assert plan.demo and plan.demo["available"]
    assert any(r.startswith("필요한 설정 2개") for r in plan.risk_reasons)
    assert ds.load("shop")["generated"]["JWT_SECRET"] not in plan.model_dump_json()


def test_데모를_못_쓰는_앱은_계획에_이유를_싣고_데모는_켜지_않는다(tmp_path):
    ws = str(shop(tmp_path, demo=False))
    plan = _plan()
    routes._apply_settings_to_plan(plan, ws)
    assert plan.demo and plan.demo["available"] is False and plan.demo["enabled"] is False
    assert "모의 결제 모드가 없어" in plan.demo["unavailable_reason"]
    assert plan.settings_missing == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]


def test_비어_있으면_빌드하지_않고_멈춘다(tmp_path, monkeypatch):
    ws = str(shop(tmp_path))
    plan = _plan()
    routes._deployment_plans[plan.plan_id] = plan
    routes._plan_workspaces[plan.plan_id] = ws
    built = []
    monkeypatch.setattr(routes, "_build_local_image", lambda *a, **k: built.append(a))
    monkeypatch.setattr(routes, "_validate_local_plan", lambda p: None)
    try:
        out = asyncio.run(routes.execute_deployment(routes.ExecuteRequest(plan_id=plan.plan_id, approved=True)))
    finally:
        routes._deployment_plans.pop(plan.plan_id, None)
        routes._plan_workspaces.pop(plan.plan_id, None)
    assert out["status"] == "failed" and out["stage"] == "settings" and not built
    assert out["diagnosis"]["code"] == "APP_MISSING_SETTING"
    assert out["diagnosis"]["missing_env"] == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]


def test_설정_저장_엔드포인트는_값을_돌려주지_않고_검증한다(tmp_path):
    ws = str(shop(tmp_path))
    plan = _plan()
    routes._apply_settings_to_plan(plan, ws)
    routes._deployment_plans[plan.plan_id] = plan
    routes._plan_workspaces[plan.plan_id] = ws
    try:
        call = lambda **kw: asyncio.run(routes.save_deploy_settings(routes.DeploySettingsRequest(plan_id=plan.plan_id, **kw)))
        with pytest.raises(HTTPException):
            call(values={"OTHER": "x"})
        with pytest.raises(HTTPException):
            call(values={"JWT_SECRET": "short"})
        out = call(values={"STRIPE_SECRET_KEY": "sk_test_value", "STRIPE_WEBHOOK_SECRET": "whsec_value"})
        assert out["settings_missing"] == [] and "sk_test_value" not in json.dumps(out)
        out = call(values={"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": ""}, demo=True)
        assert out["settings_missing"] == [] and out["demo"]["enabled"] is True
        assert any(r.startswith("로컬 데모 모드") for r in out["risk_reasons"])
    finally:
        routes._deployment_plans.pop(plan.plan_id, None)
        routes._plan_workspaces.pop(plan.plan_id, None)


def test_TS_타입과_모아서_종료하는_검사도_필수_설정으로_찾는다(tmp_path):
    """실기기 TEMP: `const JWT_SECRET: string = process.env.JWT_SECRET ?? ''` 와 errors.push → 끝에서 process.exit(1)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.ts").write_text(
        "const JWT_SECRET: string = process.env.JWT_SECRET ?? '';\n"
        "if (!JWT_SECRET || JWT_SECRET.length < 32) {\n  console.error('bad');\n  process.exit(1);\n}\n")
    (tmp_path / "src" / "server.ts").write_text(
        "function validate(): void {\n  const errors: string[] = [];\n  if (!process.env.DATABASE_URL) {\n    errors.push('DATABASE_URL');\n  }\n"
        "  if (errors.length) {\n    process.exit(1);\n  }\n}\n")
    found = ds.required_env(str(tmp_path))
    assert found.get("JWT_SECRET") == 32 and "DATABASE_URL" in found
