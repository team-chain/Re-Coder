"""2.0.12 — ECS 배포가 실제로 앱을 띄우게(실기기 2.0.11 쇼핑몰 0ce6 를 ECS 로 배포 → 8단계 실패, 고쳐도 앱이 시작하자마자 종료).

· 설치본(exe)에서 태스크 정의 틀을 못 찾던 문제 — test_dev_2010 의 resource 테스트.
· 앱이 시작할 때 요구하는 DB·서명 키·결제 키를 ECS 가 하나도 넘기지 않던 문제 — ecs_runtime.
· 실제 0ce6 결과물을 띄워 보니 드러난 런타임 결함 — ESM require/__dirname, 확장자 없는 import, 데모에서 화면 미제공, 통화 대소문자.
"""
import json

import botocore.session
import pytest
from botocore.validate import validate_parameters

import build_readiness as br
import node_fixups as nf
from build_readiness import ProjectFiles, analyze
from core.agents import ecs_runtime
from core.agents.ecs_agent import ECSAgent
from core.schemas import ECSDeployRequest

ARN = "arn:aws:iam::123456789012:role/ecsTaskExecutionRole"

SERVER = """import 'dotenv/config';
import express from 'express';
const app = express();
if (!process.env.DATABASE_URL) { console.error('DATABASE_URL required'); process.exit(1); }
if (!process.env.JWT_SECRET) { console.error('JWT_SECRET required'); process.exit(1); }
if (process.env.JWT_SECRET.length < 32) { process.exit(1); }
if (!process.env.STRIPE_SECRET_KEY) { throw new Error('STRIPE_SECRET_KEY required'); }
const mode = process.env.PAYMENT_MODE, host = process.env.STRIPE_MOCK_HOST, port = process.env.STRIPE_MOCK_PORT;
app.get('/health', (_req, res) => res.json({ ok: true }));
app.listen(process.env.PORT || 3000);
"""


def _shop(tmp_path, server=SERVER, deps=None):
    files = {
        "package.json": json.dumps({"name": "shop", "type": "module",
                                    "dependencies": deps or {"express": "^4.19.2", "pg": "^8.11.0", "stripe": "^14.25.0"}}),
        "src/server.js": server,
        "schema.sql": "CREATE TABLE IF NOT EXISTS products (id SERIAL PRIMARY KEY, name TEXT);\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _plan(tmp_path, environment="staging", provided=(), **kw):
    return ecs_runtime.plan(str(tmp_path), key="ap-northeast-2-c-s", environment=environment,
                            provided=set(provided), container_port=3000, **kw)


# ── ECS 실행 설정 ────────────────────────────────────────────────────────────

def test_staging_prepares_db_signing_key_and_mock_payment(tmp_path):
    plan = _plan(_shop(tmp_path))
    assert not plan.blocked
    assert plan.services == ["postgres"] and plan.demo
    assert plan.env["DATABASE_URL"].startswith("postgresql://recoder:") and "@127.0.0.1:5432/app" in plan.env["DATABASE_URL"]
    assert len(plan.env["JWT_SECRET"]) >= 32 and plan.generated == ["JWT_SECRET"]
    assert plan.env["NODE_ENV"] == "test" and plan.env["PAYMENT_MODE"] == "mock" and plan.env["STRIPE_MOCK_HOST"] == "127.0.0.1"
    names = [c["name"] for c in plan.sidecars]
    assert names == ["recoder-postgres", "recoder-payment-mock"]
    pg = plan.sidecars[0]
    pg_env = {e["name"]: e["value"] for e in pg["environment"]}
    assert "listen_addresses=127.0.0.1" in pg["command"][0] and pg_env["RECODER_INIT_SQL_B64"]
    assert ".listen(PORT,'127.0.0.1')" in plan.sidecars[1]["entryPoint"][2]
    assert plan.min_memory == 1024
    #: 화면·기록용 요약에는 값이 없다.
    summary = json.dumps(plan.summary())
    assert plan.env["JWT_SECRET"] not in summary and "postgresql://" not in summary


def test_generated_signing_key_is_stable_across_deploys(tmp_path):
    root = _shop(tmp_path)
    first, second = _plan(root), _plan(root)
    assert first.env["JWT_SECRET"] == second.env["JWT_SECRET"]
    assert first.env["DATABASE_URL"] == second.env["DATABASE_URL"]


def test_preview_does_not_create_keys(tmp_path, monkeypatch):
    root = _shop(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setenv("RECODER_ECS_RUNTIME_DIR", str(store))
    plan = _plan(root, create=False)
    assert not plan.blocked and not store.exists()


def test_production_blocks_instead_of_temporary_db_or_plaintext_keys(tmp_path):
    plan = _plan(_shop(tmp_path), environment="production")
    assert plan.blocked and not plan.sidecars and not plan.env
    assert [m["name"] for m in plan.missing] == ["DATABASE_URL", "JWT_SECRET", "STRIPE_SECRET_KEY"]
    message, remedy = plan.block_message()
    assert "DATABASE_URL" in message and "Secrets Manager" in remedy and "아직 아무것도" in remedy


def test_production_passes_with_secret_references(tmp_path):
    plan = _plan(_shop(tmp_path), environment="production",
                 provided={"DATABASE_URL", "JWT_SECRET", "STRIPE_SECRET_KEY"})
    assert not plan.blocked and not plan.sidecars and not plan.env


def test_user_database_url_is_kept_and_no_sidecar_db(tmp_path):
    plan = _plan(_shop(tmp_path), provided={"DATABASE_URL"})
    assert "recoder-postgres" not in [c["name"] for c in plan.sidecars]
    assert "DATABASE_URL" not in plan.env


def test_external_key_without_mock_support_is_blocked(tmp_path):
    server = SERVER.replace("const mode = process.env.PAYMENT_MODE, host = process.env.STRIPE_MOCK_HOST, port = process.env.STRIPE_MOCK_PORT;\n", "")
    plan = _plan(_shop(tmp_path, server))
    assert [m["name"] for m in plan.missing] == ["STRIPE_SECRET_KEY"]
    assert "모의 결제를 지원하지 않습니다" in plan.missing[0]["reason"]


def test_user_node_env_disables_mock_payment(tmp_path):
    plan = _plan(_shop(tmp_path), provided={"NODE_ENV"})
    assert not plan.demo and [m["name"] for m in plan.missing] == ["STRIPE_SECRET_KEY"]


def _request(tmp_path, **kw):
    fields = dict(project_id="x", cluster="recoder-cluster", service="shop", image="", region="ap-northeast-2",
                  workspace_path=str(tmp_path), container_port=3000, container_name="app", environment="staging",
                  env_vars={"ENVIRONMENT": "staging"}, task_definition_family="recoder-task")
    fields.update(kw)
    return ECSDeployRequest(**fields)


def test_task_definition_runs_the_app_with_its_sidecars_and_is_valid_for_ecs(tmp_path):
    req = _request(_shop(tmp_path), env_vars={"ENVIRONMENT": "staging", "FRONTEND_URL": "https://x"})
    agent = ECSAgent()
    image = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/shop:v1"
    td = agent._render_task_definition(req, image=image, execution_role_arn=ARN, task_role_arn="",
                                       runtime=ECSAgent._plan_runtime(req))
    containers = {c["name"]: c for c in td["containerDefinitions"]}
    assert list(containers) == ["app", "recoder-postgres", "recoder-payment-mock"]
    app = containers["app"]
    env = {e["name"]: e["value"] for e in app["environment"]}
    assert env["FRONTEND_URL"] == "https://x" and env["PORT"] == "3000" and "JWT_SECRET" in env
    assert app["dependsOn"] == [{"containerName": "recoder-postgres", "condition": "HEALTHY"},
                                {"containerName": "recoder-payment-mock", "condition": "START"}]
    assert containers["recoder-payment-mock"]["image"] == image
    for name in ("recoder-postgres", "recoder-payment-mock"):
        log = containers[name]["logConfiguration"]["options"]
        assert log["awslogs-group"] == app["logConfiguration"]["options"]["awslogs-group"]
        assert log["awslogs-stream-prefix"] == name
    assert td["memory"] == "1024" and td["cpu"] == "256"
    model = botocore.session.get_session().get_service_model("ecs").operation_model("RegisterTaskDefinition")
    validate_parameters(td, model.input_shape)
    assert len(json.dumps(td)) < 64 * 1024


def test_task_definition_without_workspace_is_unchanged(tmp_path):
    req = _request(tmp_path, workspace_path=None)
    td = ECSAgent()._render_task_definition(req, image="img", execution_role_arn=ARN, task_role_arn="", runtime=None)
    assert [c["name"] for c in td["containerDefinitions"]] == ["app"]


def test_pipeline_stops_before_any_aws_call_when_settings_are_missing(tmp_path, monkeypatch):
    #: pytest-asyncio 없이 돈다 — 테스트 의존성(requirements-dev.txt)에 없는 플러그인에 기대면 CI·다른 PC 에서만 실패한다.
    import asyncio
    req = _request(_shop(tmp_path), environment="production")
    agent = ECSAgent()
    touched = []
    monkeypatch.setattr(agent, "_clients", lambda region: touched.append(region) or {})
    record = asyncio.run(agent._deploy_pipeline(req))
    assert record.status.value.lower() == "failed"
    assert "DATABASE_URL" in record.error_message and "Secrets Manager" in (record.error_remedy or "")
    assert touched == []


# ── 실제 결과물에서 드러난 런타임 결함 ────────────────────────────────────────

APP_TS = """import express, { Request, Response } from 'express';
import path from 'path';
const app = express();
app.use('/api/products', products);
// Serve static frontend files in production
if (process.env.NODE_ENV === 'production') {
  const frontendPath = path.join(__dirname, '../../frontend/dist');
  app.use(express.static(frontendPath));
  app.get('*', (req: Request, res: Response) => {
    if (!req.path.startsWith('/api')) { res.sendFile(path.join(frontendPath, 'index.html')); }
  });
}
const origin = process.env.NODE_ENV === 'production' ? 'x' : '*';
export default app;
"""


def test_demo_app_serves_the_ui_outside_production(tmp_path):
    for rel, text in {"backend/src/app.ts": APP_TS,
                      "backend/src/pay.ts": "export const m = [process.env.PAYMENT_MODE, process.env.STRIPE_MOCK_HOST, process.env.STRIPE_MOCK_PORT];\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    notes, writes = nf.frontend_production_gate(ProjectFiles(tmp_path))
    new = writes["backend/src/app.ts"]
    assert "if (process.env.NODE_ENV !== 'development') {" in new
    assert "process.env.NODE_ENV === 'production' ? 'x' : '*'" in new   # 화면 제공과 무관한 조건은 그대로
    (tmp_path / "backend/src/app.ts").write_text(new, encoding="utf-8")
    assert nf.frontend_production_gate(ProjectFiles(tmp_path)) == ([], {})


def test_production_gate_is_not_flagged_for_apps_without_mock_payment(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.ts").write_text(APP_TS, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"a","dependencies":{"express":"^4.19.2"}}', encoding="utf-8")
    assert "NODE_FRONTEND_PRODUCTION_ONLY" not in [i.code for i in analyze(str(tmp_path), online=False).issues]


def test_currency_comparison_ignores_case_only_for_currency(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/pay.ts").write_text(
        "if (order.currency !== currency) { throw 1; }\nif (order.total_cents !== amount_cents) { throw 2; }\n"
        "if (payload.currency === order.currency) ok();\n", encoding="utf-8")
    _notes, writes = nf.currency_case_compare(ProjectFiles(tmp_path))
    new = writes["src/pay.ts"]
    assert "String(order.currency).toLowerCase() !== String(currency).toLowerCase()" in new
    assert "order.total_cents !== amount_cents" in new
    assert "String(payload.currency).toLowerCase() === String(order.currency).toLowerCase()" in new
    (tmp_path / "src/pay.ts").write_text(new, encoding="utf-8")
    assert nf.currency_case_compare(ProjectFiles(tmp_path)) == ([], {})


def test_esm_require_and_dirname_become_imports(tmp_path):
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/package.json").write_text('{"type":"module"}', encoding="utf-8")
    (tmp_path / "backend/src/app.ts").write_text(
        "import express from 'express';\nconst app = express();\nconst path = require('path');\n"
        "const { readFileSync } = require('fs');\napp.use(express.static(path.join(__dirname, 'public')));\n", encoding="utf-8")
    _notes, writes = nf.esm_commonjs_globals(ProjectFiles(tmp_path))
    new = writes["backend/src/app.ts"]
    assert "require(" not in new and "import path from 'path';" in new and "import { readFileSync } from 'fs';" in new
    assert "const __dirname = recoderDirname(__filename);" in new


def test_esm_extensions_are_added_for_node_resolution(tmp_path):
    (tmp_path / "backend/src/db").mkdir(parents=True)
    (tmp_path / "backend/package.json").write_text('{"type":"module"}', encoding="utf-8")
    (tmp_path / "backend/tsconfig.json").write_text('{"compilerOptions":{"module":"ESNext","moduleResolution":"node"}}', encoding="utf-8")
    (tmp_path / "backend/src/server.ts").write_text("import app from './app';\nimport { q } from './db';\n", encoding="utf-8")
    (tmp_path / "backend/src/app.ts").write_text("export default 1;\n", encoding="utf-8")
    (tmp_path / "backend/src/db/index.ts").write_text("export const q = 1;\n", encoding="utf-8")
    (tmp_path / "frontend/src").mkdir(parents=True)
    (tmp_path / "frontend/package.json").write_text('{"type":"module","devDependencies":{"vite":"^5.0.0"}}', encoding="utf-8")
    (tmp_path / "frontend/src/main.ts").write_text("import App from './App';\n", encoding="utf-8")
    _notes, writes = nf.esm_import_extensions(ProjectFiles(tmp_path))
    assert "import app from './app.js';" in writes["backend/src/server.ts"]
    assert "from './db/index.js'" in writes["backend/src/server.ts"]
    assert not any(rel.startswith("frontend/") for rel in writes)   # 번들러(vite) 화면은 건드리지 않는다


def test_esm_server_gets_the_built_ui_once(tmp_path):
    text = "import express from 'express';\nconst app = express();\napp.use('/api/x', r);\napp.listen(3000);\n"
    once = br.serve_frontend_module(text, "backend", "frontend/dist")
    assert once and "recoderUiDir" in once and "express.static(" in once
    assert once.index("recoderUiDir") < once.index("app.listen")
    assert br.serve_frontend_module(once, "backend", "frontend/dist") is None
