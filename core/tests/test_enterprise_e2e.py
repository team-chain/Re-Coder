"""3.1 엔터프라이즈 하네스 — 요청 한 줄부터 다중 서비스 생성 → compose 검증 실패 → 교정 → 통과까지 한 번에(가짜 AI·가짜 Docker).

이 시험이 보여 주는 것
    · "마이크로서비스" 요청이면 설계 전에 아키텍처 계약을 받는다(plan_architecture).
    · AI 가 목록에 넣은 Dockerfile·compose 는 버리고, 계약에서 만든 고정 파일(compose·서비스별 Dockerfile·통합 테스트)을 넣는다.
    · 모든 생성·교정 프롬프트가 계약(이름·변수·포트)을 본다.
    · compose 검증이 "로그인 없이 열린 API" 를 잡아 그 라우트 파일만 고치게 하고, 다시 검증해 통과한다.
    · 고정 파일은 AI 교정이 바꾸지 않는다.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import code_agent as ca
import compose_check
import gen_engine
import generated_validation

ARCH = {
    "services": [
        {"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders"], "public": True},
        {"name": "orders", "kind": "api", "language": "python", "port": 8000,
         "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000", "databases": ["orders-db", "cache"]},
        {"name": "worker", "kind": "worker", "language": "python", "start": "python -m app.worker", "databases": ["orders-db", "cache"]},
    ],
    "databases": [{"name": "orders-db", "engine": "postgres", "tables": ["orders"]}, {"name": "cache", "engine": "redis"}],
    "endpoints": [{"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
                  {"service": "gateway", "method": "GET", "path": "/api/orders", "auth": True}],
}

GATEWAY = """import express from "express";
import jwt from "jsonwebtoken";

const app = express();
app.use(express.json());

function requireUser(req, res, next) {
  const token = (req.headers.authorization || "").replace(/^Bearer /, "");
  try {
    req.user = jwt.verify(token, process.env.JWT_SECRET);
    return next();
  } catch {
    return res.status(401).json({ error: "unauthorized" });
  }
}

app.get("/health", async (_req, res) => {
  try {
    const r = await fetch(process.env.ORDERS_URL + "/health");
    return res.status(r.ok ? 200 : 503).json({ status: r.ok ? "ok" : "degraded" });
  } catch {
    return res.status(503).json({ status: "degraded" });
  }
});

app.get("/api/orders", requireUser, async (req, res) => {
  const r = await fetch(process.env.ORDERS_URL + "/api/orders", { headers: { authorization: req.headers.authorization } });
  return res.status(r.status).json(await r.json());
});

app.listen(Number(process.env.PORT || 3000), "0.0.0.0");
"""

ORDERS_BUGGY = '''import os

import jwt
import psycopg
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()


def connect():
    return psycopg.connect(os.environ["ORDERS_DB_URL"])


@app.on_event("startup")
def migrate() -> None:
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, total INTEGER NOT NULL)")


@app.get("/health")
def health() -> dict:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders(authorization: str = Header(default="")) -> list:
    with connect() as conn:
        rows = conn.execute("SELECT id, user_id, total FROM orders").fetchall()
    return [{"id": r[0], "user_id": r[1], "total": r[2]} for r in rows]
'''

FIX_FIND = '''def list_orders(authorization: str = Header(default="")) -> list:
    with connect() as conn:'''
FIX_REPLACE = '''def list_orders(authorization: str = Header(default="")) -> list:
    user = require_user(authorization)
    with connect() as conn:'''
FIX_HELPER_FIND = '''@app.get("/health")'''
FIX_HELPER_REPLACE = '''def require_user(authorization: str) -> str:
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "), os.environ["JWT_SECRET"], algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    return str(claims.get("sub"))


@app.get("/health")'''

WORKER = '''import os
import time

import redis


def main() -> None:
    queue = redis.Redis.from_url(os.environ["CACHE_URL"])
    while True:
        try:
            queue.blpop(["orders:paid"], timeout=5)
        except redis.RedisError as exc:
            print(f"worker retry: {exc}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
'''

FILES = {
    "services/gateway/package.json": json.dumps({"name": "gateway", "version": "1.0.0", "type": "module",
                                                  "scripts": {"start": "node src/server.js"},
                                                  "dependencies": {"express": "^4.19.2", "jsonwebtoken": "^9.0.2"}}, indent=2) + "\n",
    "services/gateway/src/server.js": GATEWAY,
    "services/orders/requirements.txt": "fastapi==0.115.0\nuvicorn==0.30.6\npsycopg[binary]==3.2.1\nredis==5.0.8\npyjwt==2.9.0\n",
    "services/orders/app/__init__.py": "",
    "services/orders/app/main.py": ORDERS_BUGGY,
    "services/worker/requirements.txt": "redis==5.0.8\n",
    "services/worker/app/__init__.py": "",
    "services/worker/app/worker.py": WORKER,
    "README.md": "# 주문 플랫폼\n\n`cp .env.example .env` 후 `docker compose up -d --build --wait`.\n",
}
PLANNED = [
    {"file": "services/gateway/package.json", "layer": 0}, {"file": "services/orders/requirements.txt", "layer": 0},
    {"file": "services/worker/requirements.txt", "layer": 0}, {"file": "services/orders/app/__init__.py", "layer": 0},
    {"file": "services/worker/app/__init__.py", "layer": 0},
    {"file": "services/orders/app/main.py", "layer": 1}, {"file": "services/worker/app/worker.py", "layer": 1},
    {"file": "services/gateway/src/server.js", "layer": 1}, {"file": "README.md", "layer": 2},
    # AI 가 습관대로 넣은 인프라 파일 — ReCoder 가 버리고 계약에서 만든 것으로 바꿔야 한다.
    {"file": "services/orders/Dockerfile", "layer": 0}, {"file": "docker-compose.yml", "layer": 2},
]


DECISION = {"id": "stack", "question": "구성", "chosen_key": "node",
            "options": [{"key": "node", "label": "Node"}, {"key": "py", "label": "Python"}]}


def R(obj):
    return type("R", (), {"text": json.dumps(obj, ensure_ascii=False), "model_used": "m", "provider": "p"})()


class FakeAI:
    def __init__(self):
        self.lock = threading.Lock()
        self.ops: list[str] = []
        self.prompts: dict[str, list[str]] = {}

    def call(self, request, agent=None, operation=None):
        with self.lock:
            self.ops.append(operation)
            self.prompts.setdefault(operation, []).append(request.prompt)
        p = request.prompt
        if operation == "plan_architecture":
            return R(ARCH)
        if operation == "generate_code_manifest":
            if "이미 받은 파일" in p:
                return R({"files": [], "more": False})
            return R({"summary": "주문 플랫폼", "contracts": "주문 상태: pending → paid", "files": PLANNED, "more": False})
        if operation == "review_code_manifest":
            return R({"files": [], "more": False})
        if operation == "generate_code_part":
            wanted = p.rsplit("**아래 파일만**", 1)[1]
            names = [f for f in FILES if f"- {f}\n" in wanted + "\n"]
            assert names, wanted[:400]
            return R({"summary": "part", "ops": [{"action": "create", "file": n, "content": FILES[n]} for n in names]})
        if operation == "generate_code_file_part":
            target = p.split("지금은 ", 1)[1].split(" 파일 하나만", 1)[0]
            return R({"content": FILES[target], "done": True})
        if operation == "generate_code_consistency":
            if "services/orders/app/main.py 에 아래 문제" in p and "→ 200" in p:
                return R({"edits": [{"find": FIX_HELPER_FIND, "replace": FIX_HELPER_REPLACE},
                                    {"find": FIX_FIND, "replace": FIX_REPLACE}]})
            return R({"edits": []})
        raise AssertionError(f"예상하지 못한 AI 호출: {operation}")


class Docker:
    """compose 를 흉내 낸다 — 'up' 때 펼친 작업 폴더를 기억해 HTTP 응답이 실제 생성 코드를 따르게 한다."""

    def __init__(self):
        self.workspaces: list[Path] = []
        self.runs = 0

    def __call__(self, args, timeout):
        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(args, rc, out, err)
        if args[:2] == ["compose", "version"]:
            return done(out="v2")
        cmd = args[args.index("--env-file") + 2]
        if cmd == "up":
            self.runs += 1
            self.workspaces.append(Path(args[args.index("-f") + 1]).parent)
            return done()
        if cmd == "ps":
            names = ["gateway", "orders", "worker", "orders-db", "cache"]
            return done(out="\n".join(json.dumps({"Service": n, "State": "running", "Health": "" if n == "worker" else "healthy"})
                                      for n in names))
        if cmd == "port":
            return done(out="127.0.0.1:4" + args[args.index("port") + 2][:3] + "\n")
        if cmd == "exec":
            return done(out="orders\n")
        return done()

    def http(self, method, url, body):
        main = (self.workspaces[-1] / "services/orders/app/main.py").read_text(encoding="utf-8")
        path = "/" + url.split("/", 3)[3]
        if path == "/health":
            return 200, '{"status":"ok"}'
        if url.startswith("http://127.0.0.1:4800"):  # orders
            return (401 if "require_user(authorization)" in main else 200), "[]"
        return 401, "{}"


def test_요청_한_줄에서_다중_서비스를_만들고_compose_검증으로_교정해_통과한다(monkeypatch, tmp_path):
    ai, docker = FakeAI(), Docker()
    real = generated_validation.verify_compose
    monkeypatch.setattr(generated_validation, "verify_compose",
                        lambda root, ops, arch, **kw: real(root, ops, arch, docker=docker, http=docker.http))
    monkeypatch.setattr(ca, "get_router", lambda: ai)
    result = ca.generate_code("게이트웨이·주문 서비스·워커로 나뉜 마이크로서비스 주문 플랫폼, PostgreSQL 과 Redis 사용",
                              project_root=str(tmp_path), mode="team", test=False, decisions=[DECISION])

    ops = {op["file"]: op for op in result["ops"]}
    # ① 계약 → 고정 인프라 파일
    assert ai.ops[0] == "plan_architecture"
    for path in ("docker-compose.yml", "architecture.json", ".env.example", "contract-tests.json", "scripts/contract-test.mjs",
                 "services/gateway/Dockerfile", "services/orders/Dockerfile", "services/worker/Dockerfile"):
        assert path in ops and ops[path].get("fixed"), path
    assert ops["services/orders/Dockerfile"]["content"].startswith("# ReCoder 검증 Dockerfile")
    assert "ORDERS_DB_URL" in ops["docker-compose.yml"]["content"]
    assert "Dockerfile" not in ops  # 루트 단일 Dockerfile(docker_kit)을 끼워 넣지 않는다
    # 모든 파일 생성 프롬프트가 계약을 본다
    assert all("[아키텍처 계약" in p for p in ai.prompts["generate_code_part"])
    # ② compose 검증 → ③ 통합 테스트가 "로그인 없이 열린 API" 를 잡고 → 교정 → 다시 검증해 통과
    assert docker.runs == 2
    assert result["verification"]["kind"] == "compose" and result["verification"]["status"] == "passed"
    assert any(c["name"] == "test:orders-GET-/api/orders-noauth" and c["ok"] for c in result["verification"]["checks"])
    assert "require_user(authorization)" in ops["services/orders/app/main.py"]["content"]
    assert ops["services/gateway/src/server.js"]["content"] == GATEWAY  # 문제없는 서비스는 그대로
    fixes = ai.prompts["generate_code_consistency"]
    assert len(fixes) == 1 and "CONTRACT_TEST_FAILED" not in fixes[0] and "401" in fixes[0]
    contract = json.loads(ops["contract-tests.json"]["content"])
    assert any(c["id"] == "orders-GET-/api/orders-noauth" for c in contract["cases"])


def test_작은_요청은_아키텍처_단계를_거치지_않는다(monkeypatch, tmp_path):
    class Small(FakeAI):
        def call(self, request, agent=None, operation=None):
            assert operation != "plan_architecture"
            if operation == "generate_code_manifest" and "이미 받은 파일" not in request.prompt:
                return R({"summary": "메모", "contracts": "GET /api/notes",
                          "files": [{"file": "package.json", "layer": 0}, {"file": "src/server.js", "layer": 1}], "more": False})
            if operation == "generate_code_part":
                wanted = request.prompt.rsplit("**아래 파일만**", 1)[1]
                files = {"package.json": json.dumps({"name": "notes", "scripts": {"start": "node src/server.js"}}),
                         "src/server.js": "const http = require('http');\nhttp.createServer((q, s) => s.end('ok')).listen(process.env.PORT || 3000);\n"}
                return R({"summary": "p", "ops": [{"action": "create", "file": f, "content": c} for f, c in files.items()
                                                  if f"- {f}\n" in wanted + "\n"]})
            return super().call(request, agent, operation)

    monkeypatch.setattr(ca, "get_router", lambda: Small())
    result = ca.generate_code("간단한 메모 앱", project_root=str(tmp_path), mode="team", test=False, decisions=[DECISION])
    assert "docker-compose.yml" not in {op["file"] for op in result["ops"]}
    assert result["verification"]["kind"] != "compose"


def test_계약을_못_받으면_일반_설계로_계속_만든다(monkeypatch, tmp_path):
    from llm.base import LLMError, LLMErrorType

    class Broken(FakeAI):
        def call(self, request, agent=None, operation=None):
            if operation == "plan_architecture":
                raise LLMError("structured output invalid", LLMErrorType.STRUCTURED_OUTPUT)
            return super().call(request, agent, operation)

    engine = gen_engine.LargeGeneration("마이크로서비스 주문 플랫폼", new_project=True, architecture=True)
    monkeypatch.setattr(ca, "get_router", lambda: Broken())
    engine.plan()
    assert "architecture" not in engine.state["manifest"]
    assert any(f["file"] == "docker-compose.yml" for f in engine.state["files"])  # AI 목록 그대로(예전 흐름)
    assert compose_check  # 모듈이 import 된다
