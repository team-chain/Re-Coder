"""2.0.9 — 개발 단계에서 파일 사이가 어긋나던 문제(카페·쇼핑몰 실기기 결과로 재현)와 공통 기반 동시 생성."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import code_agent as ca
import docker_kit
import gen_engine as ge
import local_services
import node_fixups as nf
from build_readiness import ProjectFiles, analyze
from security_scan import scan_text_for_secrets


def _files(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ProjectFiles(tmp_path)


# ── 생성 순서: 의존 순서대로, 서로 상관없는 파일은 동시에 ─────────────────────────────

def test_rank_and_side():
    sides = ge._sides_of([{"file": "frontend/package.json"}, {"file": "backend/package.json"}])
    assert ge._side("frontend/src/pages/Cart.tsx", sides) == "frontend"
    assert ge._side("public/app.js", sides) == ""
    order = ["frontend/package.json", "frontend/src/types/index.ts", "frontend/src/context/CartContext.tsx",
             "frontend/src/hooks/useCart.ts", "frontend/src/components/ErrorAlert.tsx", "frontend/src/pages/Cart.tsx",
             "frontend/src/App.tsx"]
    assert [ge._rank(p) for p in order] == sorted(ge._rank(p) for p in order)
    assert ge._rank("README.md") == 7 and ge._rank("backend/src/routes/menu.ts") == 3


def _engine(files, contents, concurrency=4, delay=0.02):
    log, lock, prompts = [], threading.Lock(), {}

    def call(self, prompt, schema, operation, max_tokens, agent="", raw=False):
        if operation == "generate_code_manifest":
            return SimpleNamespace(text=json.dumps({"summary": "s", "contracts": "", "files": files, "more": False}))
        wanted = prompt.rsplit("**아래 파일만**", 1)[1]
        fs = [f["file"] for f in files if f"- {f['file']}\n" in wanted + "\n"]
        with lock:
            for f in fs:
                log.append(("start", f, time.monotonic()))
        time.sleep(delay)
        with lock:
            for f in fs:
                log.append(("end", f, time.monotonic()))
                prompts[f] = prompt
        return SimpleNamespace(text=json.dumps({"summary": "", "ops": [{"action": "create", "file": f, "content": contents[f]} for f in fs]}))

    eng = ge.LargeGeneration("요청", concurrency=concurrency, security_review=False)
    eng.call = call.__get__(eng)
    return eng, log, prompts


def test_pages_are_written_after_the_components_they_use_and_see_their_props(monkeypatch):
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    contents = {
        "frontend/package.json": '{"name":"f"}',
        "frontend/src/components/ErrorAlert.tsx": "interface ErrorAlertProps {\n  message: string;\n  onClose: () => void;\n}\nexport function ErrorAlert({ message, onClose }: ErrorAlertProps) { return null; }\n",
        "frontend/src/hooks/useCart.ts": "export interface UseCartReturn {\n  items: string[];\n  total: number;\n}\nexport function useCart(): UseCartReturn { return { items: [], total: 0 }; }\n",
        "frontend/src/pages/Cart.tsx": "import { ErrorAlert } from '../components/ErrorAlert';\nexport default function Cart() { return null; }\n",
        "frontend/src/pages/Home.tsx": "export default function Home() { return null; }\n",
    }
    files = [{"file": f, "purpose": ""} for f in reversed(list(contents))]  # 화면을 목록 앞에 둬도
    files[0]["uses"] = []
    eng, log, prompts = _engine(files, contents)
    eng.run()
    end = {f: t for k, f, t in log if k == "end"}
    start = {f: t for k, f, t in log if k == "start"}
    for page in ("frontend/src/pages/Cart.tsx", "frontend/src/pages/Home.tsx"):
        assert start[page] >= end["frontend/src/components/ErrorAlert.tsx"]
        assert start[page] >= end["frontend/src/hooks/useCart.ts"]
    assert "onClose: () => void" in prompts["frontend/src/pages/Cart.tsx"]
    assert "UseCartReturn" in prompts["frontend/src/pages/Cart.tsx"]


def test_foundation_files_are_generated_in_parallel(monkeypatch):
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    contents = {f"backend/src/utils/u{i}.ts": f"export const u{i} = {i};\n" for i in range(6)}
    contents["backend/package.json"] = '{"name":"b"}'
    files = [{"file": f, "purpose": "", "layer": 0} for f in contents]
    eng, log, _ = _engine(files, contents, concurrency=4, delay=0.15)
    t0 = time.monotonic()
    eng.run()
    assert time.monotonic() - t0 < 0.15 * 4 * 0.75  # 한 명이 순서대로(둘씩 4번)면 0.6초 이상
    running = max(sum(1 for k, f, t in log if k == "start" and t <= at) - sum(1 for k, f, t in log if k == "end" and t <= at)
                  for _k, _f, at in log)
    assert running >= 3


def test_explicit_uses_wait_and_cycles_do_not_deadlock(monkeypatch):
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    contents = {"src/utils/api.ts": "export const a = 1;\n", "src/api/client.ts": "export const c = 1;\n"}
    files = [{"file": "src/utils/api.ts", "purpose": "", "uses": ["src/api/client.ts"]},
             {"file": "src/api/client.ts", "purpose": ""}]
    eng, log, _ = _engine(files, contents)
    _data, ops, _ = eng.run()
    assert {o["file"] for o in ops} == set(contents)


def test_export_summary_shows_props_and_signatures():
    text = ("import x from 'y';\ninterface ErrorAlertProps {\n  message: string | null;\n  onClose: () => void;\n}\n"
            "export function ErrorAlert({ message }: ErrorAlertProps) {\n  return null;\n}\nexport default ErrorAlert;\n")
    out = ge._export_summary("a/ErrorAlert.tsx", text)
    assert "onClose: () => void;" in out and "export function ErrorAlert" in out and "return null" not in out


# ── 고치기: 여러 파일이 같은 정의와 어긋나면 정의를 한 번 ─────────────────────────────

def test_definition_first_groups_callers():
    ops = [{"file": "src/components/ErrorAlert.tsx", "content": "interface ErrorAlertProps {\n  onClose: () => void;\n}\n"}] + [
        {"file": f"src/pages/P{i}.tsx", "content": "x"} for i in range(3)]
    grouped = {i: [f"컨테이너 빌드에서 src/pages/P{i - 1}.tsx 의 오류 1건: 10행 TS2741: Property 'onClose' is missing in type "
                   f"'{{ message: string; }}' but required in type 'ErrorAlertProps'. ← `<ErrorAlert />` (해결: x)"] for i in (1, 2, 3)}
    out = ge._definition_first(ops, grouped)
    assert list(out) == [0] and "다른 파일 3개" in out[0][0]


def test_fix_round_uses_team_size(monkeypatch):
    seen = set()

    def call(self, p, schema, op, max_tokens, agent=""):
        seen.add(threading.current_thread().name)
        time.sleep(0.05)
        return SimpleNamespace(text=json.dumps({"edits": []}))
    monkeypatch.setattr(ge.LargeGeneration, "call", call)
    ops = [{"file": f"a{i}.ts", "content": "x"} for i in range(6)]
    issues = [{"severity": "error", "file": f"a{i}.ts", "message": "m", "fix": ""} for i in range(6)]
    ge.edit_fix_round("p", ops, issues, workers=6)
    assert len(seen) >= 4


# ── 결정적 교정 ─────────────────────────────────────────────────────────────

def test_ts_safe_rewrites(tmp_path):
    files = _files(tmp_path, {
        "b/src/jwt.ts": "import jwt from 'jsonwebtoken';\nconst JWT_SECRET = process.env.JWT_SECRET;\nconst EXP = process.env.EXP || '7d';\n"
                        "if (!JWT_SECRET) {\n  throw new Error('x');\n}\nexport const t = () => jwt.sign({}, JWT_SECRET, { expiresIn: EXP });\n",
        "b/src/opt.ts": "const KEY = process.env.KEY;\nexport const k = KEY;\n",
        "b/src/log.ts": "export const e = (data?: unknown) => ({ a: 1, ...(data && { data }) });\n",
    })
    notes, writes = nf.ts_safe_rewrites(files)
    assert "const JWT_SECRET = process.env.JWT_SECRET as string;" in writes["b/src/jwt.ts"]
    assert "expiresIn: EXP as jwt.SignOptions['expiresIn']" in writes["b/src/jwt.ts"]
    assert "b/src/opt.ts" not in writes  # 확인이 없으면 정말 없을 수 있다
    assert "...(data ? { data } : {})" in writes["b/src/log.ts"]


def test_tailwind_self_apply(tmp_path):
    files = _files(tmp_path, {"f/src/a.css": "@tailwind base;\n.text-center {\n  @apply text-center;\n}\n.btn {\n  @apply px-2 btn;\n}\n.x { @apply px-1; }\n"})
    _notes, writes = nf.tailwind_self_apply(files)
    out = writes["f/src/a.css"]
    assert ".text-center" not in out and "@apply px-2;" in out and ".x { @apply px-1; }" in out


def test_hoisted_client_and_namespace_import(tmp_path):
    files = _files(tmp_path, {
        "b/src/db.ts": "export function getDb() { return {}; }\n",
        "b/src/services/orderService.ts": "export async function getOrderById(id: string) { return id; }\n",
        "b/src/svc.ts": "import { getDb } from './db.js';\n"
                        "export async function a() {\n  const db = getDb();\n  return db.x;\n}\n"
                        "export async function b(id: string): Promise<{ ok: boolean }> {\n  if (id) {\n    return db.y;\n  }\n  return { ok: true };\n}\n",
        "b/src/routes.ts": "import { x } from './db.js';\nexport const r = () => orderService.getOrderById('1');\n",
    })
    _p, writes = nf.hoisted_clients(files)
    text = writes["b/src/svc.ts"]
    assert text.count("const db = getDb();") == 2 and "  if (id) {\n    const db" not in text
    _p2, w2 = nf.namespace_imports(files)
    assert "import * as orderService from './services/orderService.js';" in w2["b/src/routes.ts"]


def test_constant_names_are_not_secrets():
    assert scan_text_for_secrets("  INVALID_PASSWORD: 'INVALID_PASSWORD',") == []
    assert scan_text_for_secrets("const password = 'hunter2hunter2hunter2'")


# ── Prisma·검증 빌드·시작 데이터 ─────────────────────────────────────────────

def _prisma_app(tmp_path):
    return _files(tmp_path, {
        "package.json": json.dumps({"name": "shop", "private": True, "workspaces": ["backend", "frontend"],
                                    "scripts": {"build": "npm run build --workspaces", "start": "npm start --workspace=backend"}}),
        "backend/package.json": json.dumps({"type": "module", "main": "dist/index.js", "scripts": {"build": "tsc", "start": "node dist/index.js", "db:seed": "tsx src/db/seed.ts"},
                                            "dependencies": {"@prisma/client": "^5.8.0", "express": "^4"}, "devDependencies": {"typescript": "^5"}}),
        "backend/prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n',
        "backend/src/index.ts": "import express from 'express';\nimport { PrismaClient } from '@prisma/client';\nconst prisma = new PrismaClient();\n"
                                "express().get('/health', (q, s) => s.send('ok'));\nexpress().listen(process.env.PORT || 3000);\n",
        "backend/tsconfig.json": '{"compilerOptions": {"outDir": "./dist", "rootDir": "./src"}}',
        "frontend/package.json": json.dumps({"scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}),
    })


def test_prisma_kit_generates_client_and_tables(tmp_path):
    files = _prisma_app(tmp_path)
    info = docker_kit.layout(files)
    assert info["prisma"] == "prisma/schema.prisma"
    text = docker_kit.render(info)
    assert "npx prisma generate --schema prisma/schema.prisma" in text and "--to-schema-datamodel" in text
    assert "apk add --no-cache openssl" in text and '"recoder-start.cjs", "dist/index.js"' in text
    assert "ENV NODE_ENV=production" in text
    assert nf.prisma_cli_missing(files)["backend/package.json"].count('"prisma": "^5.8.0"') == 1
    assert analyze(tmp_path).services[:1] == ["postgres"]
    verify = docker_kit.render(dict(info, verify=True))
    assert "/tmp/recoder-build-frontend.out" in verify and 'cat "${f%.rc}.out"' in verify


def test_non_prisma_kit_is_unchanged_except_production_env(tmp_path):
    files = _files(tmp_path, {"server/package.json": json.dumps({"scripts": {"start": "node index.js"}}), "server/index.js": "x"})
    text = docker_kit.render(docker_kit.layout(files))
    assert "prisma" not in text and "openssl" not in text and 'CMD ["node", "index.js"]' in text


def test_verification_build_uses_collecting_dockerfile(tmp_path):
    _prisma_app(tmp_path)
    kit = docker_kit.for_workspace(tmp_path)
    ops = [{"file": "Dockerfile", "content": kit}]
    out = ca._verification_ops(tmp_path, ops)
    assert "recoder-build-backend.rc" in out[0]["content"] and ops[0]["content"] == kit


def test_build_error_keys_ignore_line_numbers():
    a = [{"file": "f.ts", "code": "GENERATED_BUILD_FAILED", "message": "x 의 오류 1건: 10행 TS2339: Property 'a' missing ← `a`"}]
    b = [{"file": "f.ts", "code": "GENERATED_BUILD_FAILED", "message": "x 의 오류 1건: 12행 TS2339: Property 'a' missing ← `a`"}]
    assert ca._build_error_keys(a) == ca._build_error_keys(b)


def test_seed_command_maps_ts_seed_to_built_file(tmp_path):
    _prisma_app(tmp_path)
    (tmp_path / "Dockerfile").write_text(docker_kit.for_workspace(tmp_path))
    assert local_services.seed_command(str(tmp_path)) == {"workdir": "/app/backend", "script": "dist/db/seed.js", "name": "db:seed"}
    (tmp_path / "Dockerfile").write_text("FROM node:22\n")
    assert local_services.seed_command(str(tmp_path)) is None


def test_prisma_start_helper_is_valid_javascript(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("node"):
        return
    path = tmp_path / "s.cjs"
    path.write_text(docker_kit.PRISMA_START)
    assert subprocess.run(["node", "--check", str(path)]).returncode == 0
