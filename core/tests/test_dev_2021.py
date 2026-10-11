"""2.0.21 — 실기기 ECS: 태스크는 떴는데 주소가 응답하지 않던 문제(TEMP 데일리데스크 쇼핑몰).

원인 셋을 sandbox 의 실제 Docker 로 ECS 태스크 정의를 그대로 띄워 재현했다:
1) 시드 컨테이너와 앱이 동시에 떠 둘 다 데모 계정을 넣다 앱이 unique 위반으로 종료(태스크 계속 교체 → IP 무응답)
2) 실행 단계 작업 폴더가 root 소유라 앱이 uploads 폴더를 만들다 EACCES 로 종료
3) `app.use('/health', healthRouter)` 를 헬스 경로로 못 읽어 ECS 헬스체크 없이 "성공" 처리
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_infra  # noqa: E402
import node_fixups  # noqa: E402
from agents import ecs_runtime  # noqa: E402
from agents.ecs_health import _declared_health_path  # noqa: E402


def test_app_waits_for_seed_to_finish():
    plan = ecs_runtime.RuntimePlan()
    plan.env = {"DATABASE_URL": "postgres://x"}
    plan.depends_on = [{"containerName": "recoder-postgres", "condition": "HEALTHY"}]
    plan.sidecars = [
        {"name": "recoder-postgres", "image": "pg", "essential": True},
        ecs_runtime._seed_container("/app/backend", "dist/db/init.js", plan.depends_on),
    ]
    td = {"containerDefinitions": [{"name": "app", "image": "img", "environment": []}]}
    out = ecs_runtime.apply(td, "app", plan, "img:1")
    app = out["containerDefinitions"][0]
    assert {"containerName": "recoder-seed", "condition": "COMPLETE"} in app["dependsOn"]
    assert {"containerName": "recoder-postgres", "condition": "HEALTHY"} in app["dependsOn"]
    seed = next(c for c in out["containerDefinitions"] if c["name"] == "recoder-seed")
    # COMPLETE 의존 대상은 essential=false 여야 ECS 가 받는다. 끝나지 않는 시드는 시간 제한으로 끊는다.
    assert seed["essential"] is False
    assert seed["entryPoint"][:2] == ["node", "-e"] and seed["command"] == ["dist/db/init.js"]
    assert str(ecs_runtime.SEED_TIMEOUT_S * 1000) in seed["entryPoint"][2]


def test_no_seed_dependency_when_seed_dropped():
    plan = ecs_runtime.RuntimePlan()
    plan.env = {"A": "1"}
    plan.sidecars = [{"name": "recoder-postgres", "image": "pg", "essential": True}]
    out = ecs_runtime.apply({"containerDefinitions": [{"name": "app", "environment": []}]}, "app", plan, "img")
    assert not any(d.get("containerName") == "recoder-seed" for d in out["containerDefinitions"][0].get("dependsOn", []))


def test_health_route_mounted_via_router(tmp_path):
    (tmp_path / "src/routes").mkdir(parents=True)
    (tmp_path / "src/server.ts").write_text(
        "import express, { Express } from 'express';\n"
        "import healthRouter, { configRouter } from './routes/health.js';\n"
        "function createApp(): Express {\n  const app = express();\n  app.use('/health', healthRouter);\n  return app;\n}\n",
        encoding="utf-8")
    (tmp_path / "src/routes/health.ts").write_text(
        "import { Router } from 'express';\nconst router = Router();\nrouter.get('/', (_q, r) => r.json({ok:1}));\nexport default router;\n",
        encoding="utf-8")
    assert _declared_health_path(tmp_path, "node", "/health") == "/health"
    # 라우터에 '/' 가 없으면 근거가 아니다
    (tmp_path / "src/routes/health.ts").write_text("import { Router } from 'express';\nconst r = Router();\nr.get('/x', f);\n", encoding="utf-8")
    assert _declared_health_path(tmp_path, "node", "/health") is None


DOCKERFILE = """FROM node:22-alpine AS build
WORKDIR /app
COPY . .
FROM node:22-alpine
ENV NODE_ENV=production PORT=3000
WORKDIR /app/backend
COPY --from=build --chown=1000:1000 /app/backend /app/backend
COPY --from=build --chown=1000:1000 /app/frontend/dist /app/frontend/dist
# 공식 node 이미지의 node 사용자(숫자 ID)
USER 1000
CMD ["node", "dist/index.js"]
"""


def test_workdir_owned_by_root_is_detected_and_fixed():
    fixed = node_fixups.runtime_workdir_not_writable(DOCKERFILE)
    assert fixed and fixed.index("RUN chown 1000:1000 /app /app/backend") < fixed.index("USER 1000")
    assert node_fixups.runtime_workdir_not_writable(fixed) is None
    # root 로 뜨거나 distroless 면 손대지 않는다
    assert node_fixups.runtime_workdir_not_writable(DOCKERFILE.replace("USER 1000", "USER root")) is None
    assert node_fixups.runtime_workdir_not_writable(
        DOCKERFILE.replace("FROM node:22-alpine\nENV", "FROM gcr.io/distroless/nodejs22\nENV")) is None


def test_readiness_flags_root_workdir(tmp_path):
    import build_readiness as br
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/package.json").write_text('{"name":"b","scripts":{"start":"node dist/index.js"},"dependencies":{"express":"4"}}', encoding="utf-8")
    (tmp_path / "backend/src/index.js").write_text("const express=require('express');const app=express();app.get('/health',(q,r)=>r.send('ok'));app.listen(3000);\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    codes = {i.code for i in br.analyze(tmp_path).issues}
    assert "DOCKERFILE_WORKDIR_NOT_WRITABLE" in codes
    br.apply_fix(tmp_path, "DOCKERFILE_WORKDIR_NOT_WRITABLE")
    assert "DOCKERFILE_WORKDIR_NOT_WRITABLE" not in {i.code for i in br.analyze(tmp_path).issues}


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class _ECS:
    def __init__(self, tasks):
        self.tasks = tasks

    def list_tasks(self, **kw):
        want = kw.get("desiredStatus")
        return {"taskArns": [t["taskArn"] for t in self.tasks if t.get("desired", "RUNNING") == want]}

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [t for t in self.tasks if t["taskArn"] in tasks]}


class _Logs:
    def __init__(self, events=None, deny=False):
        self.events, self.deny, self.asked = events or [], deny, []

    def get_log_events(self, **kw):
        self.asked.append(kw["logStreamName"])
        if self.deny:
            raise _Err("AccessDeniedException")
        return {"events": [{"message": m} for m in self.events]} if kw["logStreamName"].startswith("ecs/") else {"events": []}


STOPPED = {
    "taskArn": "arn:aws:ecs:us-east-1:1:task/c/abc123", "desired": "STOPPED", "taskDefinitionArn": "td:7",
    "stoppedAt": "2026-10-11T15:00:00", "stoppedReason": "Essential container in task exited",
    "containers": [{"name": "recoder-app", "exitCode": 1}, {"name": "recoder-postgres", "exitCode": 0}],
}


def test_diagnosis_shows_reason_exit_code_and_app_log():
    logs = _Logs(["INFO start", "ERROR Failed to start server EACCES: permission denied, mkdir '/app/backend/uploads'",
                  "JWT_SECRET=supersecretvalue1234567890abcdef"])
    lines = aws_infra.diagnose_stopped_tasks(_ECS([STOPPED]), logs, cluster="c", service="s",
                                             log_group="/ecs/recoder-task", app_container="recoder-app",
                                             task_definition_arn="td:7")
    text = "\n".join(lines)
    assert "Essential container in task exited" in text and "recoder-app: 종료 코드 1" in text
    assert "EACCES" in text and logs.asked[0] == "ecs/recoder-app/abc123"
    assert "recoder-postgres" not in text


def test_diagnosis_without_log_permission_says_so():
    lines = aws_infra.diagnose_stopped_tasks(_ECS([STOPPED]), _Logs(deny=True), cluster="c", service="s",
                                             log_group="/ecs/x", app_container="recoder-app")
    assert any("logs:GetLogEvents" in line for line in lines)
    assert aws_infra.diagnose_stopped_tasks(_ECS([]), _Logs(), cluster="c", service="s",
                                            log_group="/ecs/x", app_container="recoder-app") == []


def test_public_ip_prefers_this_revision():
    old = {"taskArn": "t/old", "taskDefinitionArn": "td:6", "lastStatus": "RUNNING",
           "attachments": [{"type": "ElasticNetworkInterface", "details": [{"name": "networkInterfaceId", "value": "eni-old"}]}]}
    new = {"taskArn": "t/new", "taskDefinitionArn": "td:7", "lastStatus": "RUNNING",
           "attachments": [{"type": "ElasticNetworkInterface", "details": [{"name": "networkInterfaceId", "value": "eni-new"}]}]}

    class EC2:
        def describe_network_interfaces(self, NetworkInterfaceIds):
            ip = {"eni-old": "1.1.1.1", "eni-new": "2.2.2.2"}
            return {"NetworkInterfaces": [{"Association": {"PublicIp": ip[i]}} for i in NetworkInterfaceIds]}

    probe = aws_infra._probe_public_ip(_ECS([old, new]), EC2(), cluster="c", service="s", task_definition_arn="td:7")
    assert probe.ip == "2.2.2.2"
