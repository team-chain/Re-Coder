"""3.1 엔터프라이즈 하네스 실환경 검증 — 사용자 PC 에서 실제 Docker(와 선택하면 실제 AI)로 돌린다.

    python scripts/verify-enterprise-live.py fixture
        AI 없이(요금 없음). 서비스 3개(gateway·orders·worker) + PostgreSQL + Redis 프로젝트를 아키텍처 계약에서 만들고
        실제 컨테이너로 띄워 compose_check 를 돌린다. 이어서 일부러 "로그인 없이 열린 API" 를 넣은 판을 돌려 잡아내는지 본다.
        필요: Docker Desktop 실행 중, 인터넷(이미지·npm·pip 받기). 첫 실행은 이미지 받기로 몇 분 걸린다.

    python scripts/verify-enterprise-live.py deploy
        AI 없이(요금 없음). 같은 프로젝트를 ReCoder 로컬 다중 서비스 배포로 올린다 — 실제 보안 게이트(Gitleaks·Hadolint·Trivy),
        빌드, 기동, 검증, 두 번째 배포, 승인 롤백까지 돌리고 끝나면 컨테이너·볼륨·이미지를 지운다.

    python scripts/verify-enterprise-live.py generate --budget 3
        실제 AI 로 요청 한 줄에서 다중 서비스 앱을 만들고(팀 모드) compose 검증·교정까지 돈다. AI 요금이 든다.
        --budget 은 시작 전에 확인하는 USD 상한(한 번 실행의 비용은 결과에 표시). --out 폴더에 결과 프로젝트를 남긴다.

결과는 .test-artifacts/enterprise-live-<시각>/report.json 에 남는다(비밀값 없음).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

import architecture  # noqa: E402
import compose_check  # noqa: E402
import compose_kit  # noqa: E402

ARCH_RAW = {
    "services": [
        {"name": "gateway", "kind": "api", "language": "node", "port": 3000, "depends_on": ["orders"], "public": True},
        {"name": "orders", "kind": "api", "language": "python", "port": 8000,
         "start": "uvicorn app.main:app --host 0.0.0.0 --port 8000", "databases": ["orders-db", "cache"]},
        {"name": "worker", "kind": "worker", "language": "python", "start": "python -m app.worker", "databases": ["orders-db", "cache"]},
    ],
    "databases": [{"name": "orders-db", "engine": "postgres", "tables": ["orders"]}, {"name": "cache", "engine": "redis"}],
    "endpoints": [
        {"service": "gateway", "method": "POST", "path": "/api/auth/login", "body": {"email": "demo@example.com", "password": "demo"}},
        {"service": "gateway", "method": "GET", "path": "/api/orders", "auth": True},
        {"service": "orders", "method": "GET", "path": "/api/orders", "auth": True},
        {"service": "orders", "method": "POST", "path": "/api/orders", "auth": True, "body": {"total": 1200}},
    ],
}

GATEWAY = r"""import express from "express";
import jwt from "jsonwebtoken";

const app = express();
app.use(express.json());
const ORDERS = process.env.ORDERS_URL;

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
    const r = await fetch(ORDERS + "/health");
    return res.status(r.ok ? 200 : 503).json({ status: r.ok ? "ok" : "degraded" });
  } catch {
    return res.status(503).json({ status: "degraded" });
  }
});

app.post("/api/auth/login", (req, res) => {
  const { email, password } = req.body || {};
  if (!email || !password) return res.status(400).json({ error: "email and password required" });
  const token = jwt.sign({ sub: String(email) }, process.env.JWT_SECRET, { expiresIn: "1h" });
  return res.json({ token });
});

app.get("/api/orders", requireUser, async (req, res) => {
  const r = await fetch(ORDERS + "/api/orders", { headers: { authorization: req.headers.authorization } });
  return res.status(r.status).json(await r.json());
});

app.listen(Number(process.env.PORT || 3000), "0.0.0.0", () => console.log("gateway up"));
"""

ORDERS = r'''import json
import os
import time
from contextlib import asynccontextmanager

import jwt
import psycopg
import redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


def connect():
    return psycopg.connect(os.environ["ORDERS_DB_URL"])


def queue():
    return redis.Redis.from_url(os.environ["CACHE_URL"])


@asynccontextmanager
async def lifespan(_app):
    for attempt in range(30):
        try:
            with connect() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS orders (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, "
                             "total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    yield


app = FastAPI(lifespan=lifespan)


class NewOrder(BaseModel):
    total: int


def require_user(authorization: str) -> str:
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "), os.environ["JWT_SECRET"], algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    return str(claims.get("sub"))


@app.get("/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        queue().ping()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="degraded") from exc
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders(authorization: str = Header(default="")):
    user = require_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT id, total, status FROM orders WHERE user_id = %s ORDER BY id", (user,)).fetchall()
    return [{"id": r[0], "total": r[1], "status": r[2]} for r in rows]


@app.post("/api/orders", status_code=201)
def create_order(body: NewOrder, authorization: str = Header(default="")):
    user = require_user(authorization)
    with connect() as conn:
        order_id = conn.execute("INSERT INTO orders (user_id, total) VALUES (%s, %s) RETURNING id", (user, body.total)).fetchone()[0]
    queue().rpush("orders:new", json.dumps({"id": order_id}))
    return {"id": order_id, "status": "pending"}
'''

WORKER = r'''import json
import os
import time

import psycopg
import redis


def main() -> None:
    while True:
        try:
            q = redis.Redis.from_url(os.environ["CACHE_URL"])
            item = q.blpop(["orders:new"], timeout=5)
            if item:
                order = json.loads(item[1])
                with psycopg.connect(os.environ["ORDERS_DB_URL"]) as conn:
                    conn.execute("UPDATE orders SET status = 'confirmed' WHERE id = %s", (order["id"],))
        except Exception as exc:  # noqa: BLE001 — 워커는 멈추지 않고 다시 시도한다
            print(f"worker retry: {exc}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
'''

FILES = {
    "services/gateway/package.json": json.dumps({"name": "gateway", "version": "1.0.0", "type": "module", "private": True,
                                                  "scripts": {"start": "node src/server.js"},
                                                  "dependencies": {"express": "4.21.2", "jsonwebtoken": "9.0.2"}}, indent=2) + "\n",
    "services/gateway/src/server.js": GATEWAY,
    "services/orders/requirements.txt": "fastapi==0.115.0\nuvicorn==0.30.6\npsycopg[binary]==3.2.10\nredis==5.0.8\nPyJWT==2.9.0\n",
    "services/orders/app/__init__.py": "",
    "services/orders/app/main.py": ORDERS,
    "services/worker/requirements.txt": "psycopg[binary]==3.2.10\nredis==5.0.8\n",
    "services/worker/app/__init__.py": "",
    "services/worker/app/worker.py": WORKER,
}


def _write(project: Path, arch: dict, files: dict[str, str]) -> None:
    for op in compose_kit.fixed_ops(arch):
        p = project / op["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(op["content"], encoding="utf-8", newline="")
    for rel, text in files.items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")


def _summary(res: dict) -> dict:
    return {"status": res.get("status"), "seconds": res.get("seconds"), "output": res.get("output"),
            "checks": [f"{'✓' if c['ok'] else '✗'} {c['name']}" for c in res.get("checks") or []],
            "failures": [{k: f.get(k) for k in ("kind", "service", "title", "detail")} for f in res.get("failures") or []],
            "cleanup": res.get("cleanup", "ok")}


def fixture(work: Path) -> dict:
    arch, notes = architecture.normalize(ARCH_RAW)
    assert not notes, notes
    report: dict = {"mode": "fixture", "architecture": {"services": len(arch["services"]), "databases": len(arch["databases"]),
                                                       "tests": len(architecture.test_cases(arch))}}
    good = work / "good"
    _write(good, arch, FILES)
    print("1/2 정상 프로젝트를 띄워 검증합니다(첫 실행은 이미지를 받느라 몇 분 걸립니다)…", flush=True)
    res = compose_check.run(good, arch, timeout=1500)
    report["good"] = _summary(res)
    print(json.dumps(report["good"], ensure_ascii=False, indent=1), flush=True)
    broken = work / "broken"
    bad_files = dict(FILES)
    bad_files["services/orders/app/main.py"] = ORDERS.replace(
        'def list_orders(authorization: str = Header(default="")):\n    user = require_user(authorization)',
        'def list_orders(authorization: str = Header(default="")):\n    user = "anyone"')
    assert bad_files["services/orders/app/main.py"] != ORDERS
    _write(broken, arch, bad_files)
    print("2/2 일부러 '로그인 없이 열린 API' 를 넣은 판을 검증합니다…", flush=True)
    res2 = compose_check.run(broken, arch, timeout=1500)
    report["broken"] = _summary(res2)
    issues = compose_check.issues(res2, arch, bad_files)
    report["broken"]["issues"] = [{k: i[k] for k in ("code", "file")} for i in issues]
    print(json.dumps(report["broken"], ensure_ascii=False, indent=1), flush=True)
    caught = any(i["code"] == "CONTRACT_TEST_FAILED" and i["file"] == "services/orders/app/main.py" for i in issues)
    report["verdict"] = {"good_passed": res.get("status") == "passed", "broken_caught": caught,
                         "ok": res.get("status") == "passed" and res2.get("status") == "failed" and caught}
    return report


def deploy_cycle(work: Path) -> dict:
    """compose_deploy 를 실제 Docker·실제 스캐너로 — 배포 → 다시 배포 → 롤백 → 정리."""
    import asyncio

    import compose_deploy
    from api.routes import deploy as routes

    arch, _ = architecture.normalize(ARCH_RAW)
    ws = work / "stack"
    _write(ws, arch, FILES)
    report: dict = {"mode": "deploy", "project": compose_deploy.project_name(ws)}
    say = lambda stage, msg: print(f"  [{stage}] {msg}", flush=True)  # noqa: E731
    try:
        print("1/3 첫 배포(보안 게이트·빌드·기동·검증)…", flush=True)
        first = asyncio.run(compose_deploy.deploy(ws, scan=routes._execute_scan, progress=say))
        report["first"] = {k: first.get(k) for k in ("status", "stage", "message", "url")} | {"gate": first.get("gate", {}).get("unverified")}
        print(json.dumps(report["first"], ensure_ascii=False), flush=True)
        print("2/3 코드를 바꿔 다시 배포…", flush=True)
        (ws / "services/gateway/src/server.js").write_text(GATEWAY.replace('"gateway up"', '"gateway v2 up"'), encoding="utf-8")
        second = asyncio.run(compose_deploy.deploy(ws, scan=routes._execute_scan, progress=say))
        report["second"] = {k: second.get(k) for k in ("status", "stage", "message")}
        print("3/3 승인 롤백(직전 버전으로)…", flush=True)
        back = asyncio.run(compose_deploy.rollback(ws))
        report["rollback"] = back
        report["history"] = [{k: r.get(k) for k in ("status", "at")} for r in compose_deploy.history(ws)]
        report["verdict"] = {"ok": first.get("status") == "success" and second.get("status") == "success" and back.get("status") == "success"}
    finally:
        env, _ = compose_deploy.env_file(ws, arch)
        subprocess.run(["docker", *compose_deploy._base(ws, env), "down", "-v", "--remove-orphans", "--rmi", "local"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        for rec in compose_deploy.history(ws):
            for tag in (rec.get("pins") or {}).values():
                if tag:
                    subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=60)
    return report


def generate(work: Path, args) -> dict:
    import code_agent
    from llm import cost_ledger, task_tier

    if not 0 < args.budget <= 50:
        raise SystemExit("--budget 는 0 보다 크고 50 이하(USD)")
    out = Path(args.out) if args.out else work / "project"
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit(f"{out} 가 비어 있지 않습니다 — 빈 폴더를 주세요.")
    decision = {"id": "harness", "question": "구성", "chosen_key": "go", "options": [{"key": "go", "label": "진행"}]}
    tag = f"enterprise-live-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    error = ""
    result: dict = {}
    print(f"실제 AI 로 만듭니다 — 요청: {args.prompt}", flush=True)
    try:
        with task_tier.tagged(tag):
            result = code_agent.generate_code(args.prompt, project_root=str(out), mode="team", decisions=[decision], test=False,
                                              agents=args.agents)
    except Exception as exc:  # noqa: BLE001
        error = (str(exc) or exc.__class__.__name__)[:500]
    cost = sum(float(r.get("cost_usd") or 0) for r in cost_ledger.all_records() if r.get("run") == tag)
    for op in result.get("ops") or []:
        target = (out / op["file"]).resolve()
        if out.resolve() in target.parents:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op.get("content") or "", encoding="utf-8", newline="")
    v = result.get("verification") or {}
    arch = None
    if (out / "architecture.json").is_file():
        arch = json.loads((out / "architecture.json").read_text(encoding="utf-8"))
    return {"mode": "generate", "prompt": args.prompt, "seconds": round(time.monotonic() - started, 1), "cost_usd": round(cost, 4),
            "over_budget": cost > args.budget, "error": error, "project": str(out),
            "architecture": {"services": [s["name"] for s in (arch or {}).get("services", [])],
                             "databases": [d["name"] for d in (arch or {}).get("databases", [])]},
            "files": len(result.get("ops") or []), "verification": _summary(v) if v.get("kind") == "compose" else v,
            "remaining_issues": [{k: i.get(k) for k in ("code", "file", "severity")} for i in result.get("consistency_issues") or []][:30]}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fixture", help="AI 없이 실제 컨테이너로 하네스 검증")
    sub.add_parser("deploy", help="AI 없이 다중 서비스 로컬 배포·롤백 검증")
    g = sub.add_parser("generate", help="실제 AI 로 생성 → compose 검증(요금 발생)")
    g.add_argument("--budget", type=float, required=True)
    g.add_argument("--agents", type=int, default=3)
    g.add_argument("--out", default="")
    g.add_argument("--prompt", default="API 게이트웨이, 주문 서비스(Python), 상품 서비스(Node), 결제 후 처리 워커로 나뉜 마이크로서비스 "
                                         "쇼핑몰. 주문은 PostgreSQL, 상품은 MongoDB, 대기열·캐시는 Redis. 로그인(JWT) 필요.")
    args = p.parse_args()
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise SystemExit("Docker Desktop 이 실행 중이어야 합니다.")
    work = ROOT / ".test-artifacts" / ("enterprise-live-" + time.strftime("%Y%m%d-%H%M%S"))
    work.mkdir(parents=True, exist_ok=False)
    os.environ.pop("RECODER_TEST_MODE", None)
    os.environ.setdefault("RECODER_HOME", str(work / "recoder-home"))  # 사용자의 배포 기록·비밀값과 섞지 않는다
    report = fixture(work) if args.cmd == "fixture" else deploy_cycle(work) if args.cmd == "deploy" else generate(work, args)
    (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n결과: {work / 'report.json'}")
    if args.cmd in ("fixture", "deploy"):
        print("판정:", "통과" if report.get("verdict", {}).get("ok") else f"실패 — {report.get('verdict')}")
        raise SystemExit(0 if report.get("verdict", {}).get("ok") else 1)


if __name__ == "__main__":
    main()
