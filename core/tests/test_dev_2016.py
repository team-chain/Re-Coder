"""2.0.16 — 출시 전 독립 검토(코드 리뷰 에이전트 2명)가 실제로 재현한 결함 19건의 회귀 테스트."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import build_readiness as br
import docker_kit
import gen_engine as ge
import node_fixups as nf
from build_readiness import ProjectFiles, analyze

_TS = next((p for p in ["/tmp/e2e_83ed6660aaef4209/app/backend/node_modules/typescript/lib/typescript.js",
                        str(Path(__file__).resolve().parents[2] / "extension/node_modules/typescript/lib/typescript.js")]
            if os.path.isfile(p)), None)


def _syntax_ok(text: str, name: str = "x.tsx") -> bool:
    """TypeScript 문법 검사(타입은 보지 않음)."""
    if not _TS or not shutil.which("node"):
        pytest.skip("typescript 없음")
    js = ("const ts=require(%r);const src=require('fs').readFileSync(0,'utf8');"
          "const out=ts.transpileModule(src,{fileName:%r,reportDiagnostics:true,compilerOptions:{jsx:'react-jsx',module:'esnext',target:'es2020'}});"
          "process.exit(out.diagnostics.length?1:0);") % (_TS, name)
    return subprocess.run(["node", "-e", js], input=text, text=True, capture_output=True).returncode == 0


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# 1 ─ 타입 전용 판정은 확실할 때만
def test_component_or_value_import_is_not_type_only():
    assert not br._type_only_use("import { ProductCard } from './P';\nexport default () => <ProductCard name='x' />;\n", "ProductCard")
    assert not br._type_only_use("import { DEFAULTS } from './c';\nconst o = { opts: DEFAULTS };\n", "DEFAULTS")
    assert not br._type_only_use("import { Fmt } from './c';\nfoo(a, Fmt);\n", "Fmt")
    assert br._type_only_use("import { CreateOrderRequest } from './t';\nconst b = req.body as CreateOrderRequest;\n"
                             "function f(x: CreateOrderRequest): CreateOrderRequest { return x; }\n", "CreateOrderRequest")


def test_missing_component_export_is_still_an_error(tmp_path):
    _write(tmp_path, {"package.json": '{"name":"ui","dependencies":{"react":"^18.2.0"},"devDependencies":{"vite":"^5.0.0"}}',
                      "src/components/ProductCard.tsx": "export default function ProductCard() { return null; }\n",
                      "src/App.tsx": "import { ProductCard } from './components/ProductCard';\nexport default () => <ProductCard />;\n"})
    codes = [i.code for i in analyze(tmp_path, online=False).issues]
    assert "NODE_IMPORT_NAME_MISSING" in codes and "NODE_TYPE_NAME_MISSING" not in codes


# 2·3 ─ 상태 훅: 여러 줄 대입, 주소 인자
USE_API = """import { useCallback, useRef } from 'react';
import client from '../api/client';
export function useApi<T>(url: string, options?: { onSuccess?: (d: unknown) => void }) {
  const stateRef = useRef<{ data: T | null; error: string | null }>({ data: null, error: null });
  const execute = useCallback(async () => {
    try {
      const res = await client.get<T>(url);
      stateRef.current.data = {
        ...(res.data as object),
      } as T;
      options?.onSuccess?.(res.data);
    } catch (err) {
      stateRef.current.error =
        err instanceof Error ? err.message : 'x';
    }
  }, [url, options]);
  return { data: stateRef.current.data, error: stateRef.current.error, execute };
}
"""


def test_ref_state_fix_keeps_multiline_assignments_and_url_dependency(tmp_path):
    _write(tmp_path, {"src/hooks/useApi.ts": USE_API})
    _notes, writes = nf.ref_state_hooks(ProjectFiles(tmp_path))
    new = writes["src/hooks/useApi.ts"]
    assert _syntax_ok(new, "useApi.ts")
    assert "} as T;\n      recoderRerender();" in new
    assert "err.message : 'x';\n      recoderRerender();" in new
    assert "[url]" in new and "recoder_options.current?.onSuccess" in new


# 4 ─ 응답 봉투: 여러 줄 import · async 가 아닌 함수
def test_envelope_fix_is_valid_with_multiline_imports_and_sync_functions(tmp_path):
    client = ("import {\n  API_BASE,\n} from '../config'\n\nexport function handle(res) {\n  return res.json();\n}\n"
              "export async function get(url: string) {\n  const r = await fetch(API_BASE + url);\n  const data = await r.json();\n  return data;\n}\n")
    _write(tmp_path, {"frontend/src/api/client.ts": client,
                      "backend/src/r.ts": "res.status(200).json({ status: 'success', message: 'ok', data });\n"})
    _notes, writes = nf.api_envelope_unwrap(ProjectFiles(tmp_path))
    new = writes["frontend/src/api/client.ts"]
    assert _syntax_ok(new, "client.ts")
    assert "return res.json().then(recoderUnwrap);" in new and "return recoderUnwrap(data);" in new
    assert new.index("function recoderUnwrap") > new.index("} from '../config'")


# 5 ─ react import 의 끝 쉼표
def test_react_import_with_trailing_comma_stays_valid(tmp_path):
    cart = ("import React, {\n  createContext,\n  useState,\n} from 'react';\nconst C = createContext(null);\n"
            "export const CartProvider = () => {\n  const [cart, setCart] = useState<any[]>([]);\n  return null;\n};\n")
    _write(tmp_path, {"frontend/src/context/CartContext.tsx": cart})
    _notes, writes = nf.cart_persist(ProjectFiles(tmp_path))
    assert _syntax_ok(writes["frontend/src/context/CartContext.tsx"])
    assert ",," not in writes["frontend/src/context/CartContext.tsx"]


# 6 ─ 상태 확인 별칭은 Express 만
def test_health_alias_skips_fastify(tmp_path):
    _write(tmp_path, {"server/index.js": "import Fastify from 'fastify';\nconst app = Fastify();\napp.get('/health', async () => ({ ok: 1 }));\n",
                      "frontend/src/Pay.tsx": "fetch('/api/health');\n"})
    assert nf.health_alias(ProjectFiles(tmp_path)) == ([], {})


# 7 ─ 감싼 함수 이름은 그 모듈을 쓰는 파일에서만
def test_wrapper_rewrite_does_not_touch_other_modules(tmp_path):
    _write(tmp_path, {
        "package.json": '{"name":"ui","dependencies":{"axios":"^1.6.0"},"devDependencies":{"vite":"^5.0.0"}}',
        "src/api/client.ts": ("import axios from 'axios';\nconst client = axios.create({ baseURL: '/api' });\n"
                              "export async function get(url: string) { return (await client.get(url)).data; }\nexport default client;\n"),
        "src/services/auth.ts": "async function get(url: string) { return (await fetch(url)).json(); }\nexport const me = () => get('/api/auth/me');\n",
        "src/pages/P.tsx": "import { get } from '../api/client';\nget('/api/products');\n",
    })
    found = analyze(tmp_path, online=False).fix_data.get("api_double_prefix") or {}
    assert "src/services/auth.ts" not in found and "src/pages/P.tsx" in found


# 8 ─ AI 모의 결제 서버: 여러 줄 호출은 통째로, 식 안이면 손대지 않음
def test_mock_server_calls_removed_as_statements_or_left_alone(tmp_path):
    _write(tmp_path, {"backend/src/mockPaymentServer.ts": "import express from 'express';\nexpress().listen(1);\n",
                      "backend/src/server.ts": ("import express from 'express';\nimport { startMockPaymentServer } from './mockPaymentServer.js';\n"
                                                "if (x) {\n  startMockPaymentServer({\n    port: 4010,\n  });\n}\n")})
    _notes, writes = nf.drop_ai_mock_server_imports(ProjectFiles(tmp_path))
    new = writes["backend/src/server.ts"]
    assert "startMockPaymentServer" not in new and "port: 4010" not in new and _syntax_ok(new, "server.ts")
    assert "띄우지 않습니다.\n}" in new  # 호출 문장 전체(닫는 괄호·세미콜론 포함)를 뺐다
    _write(tmp_path, {"backend/src/server.ts": ("import express from 'express';\nimport { startMockPaymentServer } from './mockPaymentServer.js';\n"
                                                "const s = startMockPaymentServer(1);\n")})
    assert nf.drop_ai_mock_server_imports(ProjectFiles(tmp_path)) == ([], {})


# 9 ─ Tailwind 오탐
def _tw_project(tmp_path, deps, extra=None):
    page = '<div className="container mt-4 mb-3 p-3 text-center shadow-sm rounded border w-100 mx-auto d-flex">x</div>' * 3
    return _write(tmp_path, {"frontend/package.json": json.dumps({"name": "ui", "devDependencies": {"vite": "^5.0.0", **deps}}),
                             "frontend/index.html": '<script type="module" src="/src/main.tsx"></script>',
                             "frontend/src/main.tsx": "import './index.css';\n", "frontend/src/index.css": "",
                             "frontend/src/App.tsx": page, **(extra or {})})


def test_tailwind_setup_ignores_bootstrap_and_wired_v4_and_locked(tmp_path):
    assert nf.tailwind_setup(ProjectFiles(_tw_project(tmp_path / "bs", {"bootstrap": "^5.3.0"}))) == ([], {})
    tw = '<div className="min-h-screen bg-gray-50 px-4 py-2 text-gray-700 font-semibold items-center justify-between rounded-lg shadow-md">x</div>' * 3
    root = _tw_project(tmp_path / "v4", {"@tailwindcss/vite": "^4.0.0", "tailwindcss": "^4.0.0"}, {"frontend/src/App.tsx": tw})
    assert nf.tailwind_setup(ProjectFiles(root)) == ([], {})
    root = _tw_project(tmp_path / "lock", {}, {"frontend/src/App.tsx": tw, "frontend/package-lock.json": "{}"})
    assert nf.tailwind_setup(ProjectFiles(root)) == ([], {})
    root = _tw_project(tmp_path / "latest", {"tailwindcss": "latest"}, {"frontend/src/App.tsx": tw})
    _notes, writes = nf.tailwind_setup(ProjectFiles(root))
    assert "@tailwindcss/postcss" in writes["frontend/postcss.config.js"]


# 10 ─ 시드 옮기기: rootDir 없음 · rootDir 밖 import
def test_seed_move_skips_when_unsafe(tmp_path):
    base = {"backend/package.json": json.dumps({"name": "api", "scripts": {"seed": "tsx scripts/seed.ts"}}),
            "backend/scripts/seed.ts": "import products from '../data/products.json';\n", "backend/data/products.json": "[]"}
    _write(tmp_path / "a", {**base, "backend/tsconfig.json": '{"compilerOptions":{"outDir":"dist"},"include":["src"]}'})
    assert nf.seed_script_into_build(ProjectFiles(tmp_path / "a")) == ([], {})
    _write(tmp_path / "b", {**base, "backend/tsconfig.json": '{"compilerOptions":{"rootDir":"src","outDir":"dist"}}'})
    assert nf.seed_script_into_build(ProjectFiles(tmp_path / "b")) == ([], {})


# 11 ─ Go·Java 서버는 "서버 없음" 아님
def test_go_backend_is_a_server(tmp_path):
    _write(tmp_path, {"frontend/package.json": "{}", "frontend/src/api.ts": "fetch('/api/items');\n",
                      "backend/go.mod": "module x\n", "backend/main.go": "package main\n"})
    assert "APP_SERVER_MISSING" not in [i.code for i in analyze(tmp_path, online=False).issues]


# 12 ─ 배포 빌드: 타입 오류는 넘어가고 문법·모듈 오류는 멈춤
@pytest.mark.parametrize("log,rc,ok", [("ok", 0, True), ("src/a.ts(1,1): error TS2769: No overload", 2, True),
                                       ("src/a.ts(1,1): error TS1005: ',' expected", 2, False),
                                       ("src/a.ts(1,1): error TS2307: Cannot find module './x'", 2, False)])
def test_server_build_fails_only_on_fatal_errors(tmp_path, log, rc, ok):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/server.js").write_text("console.log(1)\n")
    script = f"npm() {{ echo {json.dumps(log)}; return {rc}; }}\n" + docker_kit.server_build("dist/server.js")
    done = subprocess.run(["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert (done.returncode == 0) is ok


# 13 ─ 대상 폴더가 있으면 디스크 확인도 그 폴더 기준
def test_engine_disk_root_includes_target_folder(tmp_path):
    (tmp_path / "web/frontend/src").mkdir(parents=True)
    (tmp_path / "web/frontend/src/App.tsx").write_text("x")
    eng = ge.LargeGeneration("요청", target_folder="web", root=str(tmp_path), emit=lambda e: None, security_review=False)
    assert eng._exists_on_disk("frontend/src/App.tsx")


def test_existing_project_gets_no_contract_route_duplicates():
    files = [{"file": "backend/src/routes/reviews.ts", "layer": 1, "uses": []}]
    manifest = {"summary": "리뷰 추가", "contracts": "GET /api/products · POST /api/orders · POST /api/reviews"}
    gaps = {g["file"] for g in ge.plan_gaps(files, manifest, lambda p: p == "backend/src/server.ts", new_project=False)}
    assert not {"backend/src/routes/products.ts", "backend/src/routes/orders.ts"} & gaps


# 14·15·16 ─ ECS 실행 설정
@pytest.mark.asyncio
async def test_runtime_plan_is_dropped_when_deploy_fails_early(tmp_path, monkeypatch):
    from core.agents.ecs_agent import ECSAgent
    from core.schemas import ECSDeployRequest
    req = ECSDeployRequest(project_id="x", cluster="c", service="s", image="", region="ap-northeast-2",
                           workspace_path=str(tmp_path), container_port=3000, environment="staging")
    agent = ECSAgent()
    monkeypatch.setattr(ECSAgent, "_plan_runtime", staticmethod(lambda r: __import__("core.agents.ecs_runtime", fromlist=["x"]).RuntimePlan(env={"JWT_SECRET": "s" * 40})))

    def boom(region):
        raise RuntimeError("no creds")
    monkeypatch.setattr(agent, "_clients", boom)
    await agent._deploy_pipeline(req)
    assert agent._runtime == {}


def test_task_definition_stays_under_the_aws_limit():
    from core.agents import ecs_runtime
    plan = ecs_runtime.RuntimePlan(env={f"V{i}": "x" * 300 for i in range(20)})
    plan.sidecars = [{"name": "recoder-seed", "image": ecs_runtime.APP_IMAGE, "essential": False, "entryPoint": ["node"], "command": ["s.js"],
                      "dependsOn": [], "logConfiguration": {"logDriver": "awslogs", "options": {}}},
                     {"name": "recoder-postgres", "image": "pg", "essential": True,
                      "environment": [{"name": "RECODER_INIT_SQL_B64", "value": "A" * 32000}]}]
    td = {"family": "f", "memory": "512", "containerDefinitions": [
        {"name": "app", "image": "i", "environment": [{"name": f"U{i}", "value": "y" * 300} for i in range(40)],
         "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "g"}}}]}
    out = ecs_runtime.apply(td, "app", plan, "img")
    assert len(json.dumps(out)) < 64 * 1024


def test_optional_redis_does_not_block_production(tmp_path):
    from core.agents import ecs_runtime
    _write(tmp_path, {"package.json": json.dumps({"name": "a", "type": "module", "dependencies": {"express": "^4.19.2", "pg": "^8.11.0", "redis": "^4.6.0"}}),
                      "src/server.js": ("import express from 'express';\nimport pg from 'pg';\nimport { createClient } from 'redis';\n"
                                        "if (!process.env.DATABASE_URL) { process.exit(1); }\nconst r = process.env.REDIS_URL ? createClient() : null;\n"
                                        "express().listen(3000);\n")})
    plan = ecs_runtime.plan(str(tmp_path), key="k", environment="production", provided={"DATABASE_URL"}, container_port=3000)
    assert [m["name"] for m in plan.missing] == []


# 17 ─ 템플릿 문자열의 {{name}} 은 그대로
def test_undouble_keeps_template_text():
    text = "const f = () => {{\n  return `Hi {{name}} ${{id}}`;\n}}\nconst g = () => {{\n  return 1;\n}}\n"
    out = ge.undouble_braces("a.ts", text)
    assert "`Hi {{name}} ${id}`" in out and "() => {\n" in out


# ── 재검토(2차)에서 나온 것 ───────────────────────────────────────────────────

def test_class_extends_and_ternary_values_are_not_type_only():
    assert not br._type_only_use("import { BaseService } from './b';\nclass P extends BaseService {}\n", "BaseService")
    assert not br._type_only_use("import { UserPanel } from './p';\nconst C = a ? AdminPanel : UserPanel;\n", "UserPanel")
    assert br._type_only_use("import { Req } from './t';\ninterface X extends Req {}\nconst y = z as Req;\n", "Req")


def test_ref_state_options_object_is_read_through_ref_and_dropped_from_deps(tmp_path):
    hook = ("import { useCallback, useRef } from 'react';\nimport client from './client';\n"
            "export function useApi(url, options = {}) {\n  const stateRef = useRef({ data: null });\n"
            "  const execute = useCallback(async () => {\n    const res = await client.get(url, options);\n"
            "    stateRef.current.data = res.data;\n  }, [url, options]);\n  return { data: stateRef.current.data, execute };\n}\n")
    _write(tmp_path, {"src/useApi.js": hook})
    _notes, writes = nf.ref_state_hooks(ProjectFiles(tmp_path))
    new = writes["src/useApi.js"]
    assert "client.get(url, recoder_options.current)" in new and "}, [url]);" in new and _syntax_ok(new, "useApi.js")


def test_ref_state_skips_expression_assignments_and_regex_braces(tmp_path):
    hook = ("import { useCallback, useRef } from 'react';\nexport function useApi(url) {\n  const ref = useRef({ data: null });\n"
            "  const run = useCallback(() => {\n    client.get(url).then((res) =>\n      ref.current.data = res.data\n    );\n  }, [url]);\n"
            "  return { data: ref.current.data, run };\n}\n")
    _write(tmp_path, {"src/a.js": hook})
    assert nf.ref_state_hooks(ProjectFiles(tmp_path)) == ([], {})
    hook2 = ("import { useCallback, useRef } from 'react';\nexport function useApi(url) {\n  const ref = useRef({ data: null });\n"
             "  const run = useCallback(async () => {\n    const res = await client.get(url);\n"
             "    ref.current.data = res.data.replace(/[{]/g, '');\n  }, [url]);\n  return { data: ref.current.data, run };\n}\n")
    _write(tmp_path, {"src/a.js": hook2})
    _notes, writes = nf.ref_state_hooks(ProjectFiles(tmp_path))
    assert _syntax_ok(writes["src/a.js"], "a.js")


def test_mock_call_in_braceless_if_keeps_next_statement_outside(tmp_path):
    _write(tmp_path, {"backend/src/mockPaymentServer.ts": "import express from 'express';\nexpress().listen(1);\n",
                      "backend/src/server.ts": ("import express from 'express';\nimport { startMockPaymentServer } from './mockPaymentServer.js';\n"
                                                "const app = express();\nif (process.env.PAYMENT_MODE === 'mock')\n  startMockPaymentServer();\napp.listen(3000);\n")})
    _notes, writes = nf.drop_ai_mock_server_imports(ProjectFiles(tmp_path))
    assert "if (process.env.PAYMENT_MODE === 'mock')\n  ;//" in writes["backend/src/server.ts"]


def test_tailwind_declared_but_plugins_missing_with_lock_is_left_alone(tmp_path):
    tw = '<div className="min-h-screen bg-gray-50 px-4 py-2 text-gray-700 font-semibold items-center justify-between rounded-lg shadow-md">x</div>' * 3
    root = _tw_project(tmp_path, {"tailwindcss": "^3.4.0"}, {"frontend/src/App.tsx": tw, "frontend/package-lock.json": "{}"})
    assert nf.tailwind_setup(ProjectFiles(root)) == ([], {})


def test_undouble_nested_object_in_template_expression(tmp_path):
    text = ("export function A(x) {{\n  const f = (x) => {{\n    return `총 ${{ x.toLocaleString('ko-KR', {{ style: 'currency', currency: 'KRW' }}) }}`;\n"
            "  }};\n  return f(x);\n}}\nconsole.log(A(1000));\n")
    out = ge.undouble_braces("a.mjs", text)
    (tmp_path / "a.mjs").write_text(out)
    assert subprocess.run(["node", str(tmp_path / "a.mjs")], capture_output=True, text=True).stdout.strip() == "총 ₩1,000"


def test_pretty_tsc_syntax_errors_still_fail_and_mixed_builds_are_strict(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/server.js").write_text("console.log(1)\n")
    log = "src/a.ts:1:1 - error\x1b[0m\x1b[90m TS1005: ',' expected"
    script = f"npm() {{ printf '%s\\n' {json.dumps(log)}; return 2; }}\n" + docker_kit.server_build("dist/server.js")
    assert subprocess.run(["sh", "-c", script], cwd=tmp_path, capture_output=True).returncode != 0
    info = {"server": "backend", "entry": "dist/server.js", "ui": None, "server_build": True, "port": 3000, "health": "/",
            "server_tsc_only": False}
    assert "test -f dist/server.js" not in docker_kit.render(info)


def test_import_type_of_missing_name_gets_placeholder_and_type_reexports_count(tmp_path):
    from build_readiness import analyze, apply_all_fixes
    root = tmp_path
    (root / "src/types").mkdir(parents=True)
    (root / "src/routes").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"a","type":"module","scripts":{"build":"tsc","start":"node dist/server.js"},"dependencies":{"express":"^4"}}')
    (root / "src/types/index.ts").write_text("export interface User { id: string }\nexport type { Shared } from './shared.js';\n")
    (root / "src/types/shared.ts").write_text("export interface Shared { a: number }\n")
    (root / "src/routes/pay.ts").write_text(
        "import type {\n  User,\n  Shared,\n  PaymentEvent,\n} from '../types/index.js';\n"
        "export function f(e: PaymentEvent, u: User, s: Shared) { return [e, u, s]; }\n")
    result = analyze(root, online=False)
    codes = {i.code: i for i in result.issues}
    assert "NODE_TYPE_NAME_MISSING" in codes and "PaymentEvent" in codes["NODE_TYPE_NAME_MISSING"].message
    assert "Shared" not in codes["NODE_TYPE_NAME_MISSING"].message
    assert "NODE_IMPORT_NAME_MISSING" not in codes
    apply_all_fixes(root)
    text = (root / "src/types/index.ts").read_text()
    assert "export type PaymentEvent = Record<string, any>;" in text and text.count("Shared") == 1


CTX_FILES = {
    "src/context/CartContext.tsx": (
        "import React, { createContext, useState, useMemo, useCallback, ReactNode } from 'react';\n"
        "export interface CartContextType {\n  items: any[];\n  addItem: (i: any) => void;\n}\n"
        "export const CartContext = createContext<CartContextType | undefined>(undefined);\n"
        "export function CartProvider({ children }: { children: ReactNode }) {\n"
        "  const [items, setItems] = useState<any[]>([]);\n"
        "  const addItem = useCallback((i: any) => setItems(p => [...p, i]), []);\n"
        "  const value = useMemo(\n    () => ({\n      items,\n      addItem,\n    }),\n    [items, addItem]\n  );\n"
        "  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;\n}\n"
        "function other() {\n  const ghost = 1;\n  return ghost;\n}\n"),
    "src/hooks/useCart.ts": (
        "import { useContext } from 'react';\nimport { CartContext } from '../context/CartContext';\n"
        "export function useCart() {\n  const context = useContext(CartContext);\n  if (!context) throw new Error('x');\n"
        "  const { items, setItems } = context;\n  return { items, add: (i: any) => setItems((p: any[]) => [...p, i]) };\n}\n"),
}


def test_context_setter_missing_from_provider_value_is_added(tmp_path):
    from build_readiness import ProjectFiles
    import node_fixups as nf
    for rel, text in CTX_FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    files = ProjectFiles(tmp_path)
    notes, writes = nf.context_value_missing(files)
    out = writes["src/context/CartContext.tsx"]
    assert "      addItem,\n      setItems,\n    })" in out
    assert "[items, addItem, setItems]" in out
    assert "  setItems: any;" in out
    assert nf.context_value_gaps(files) == []
    assert _syntax_ok(out, "x.tsx")


def test_context_name_declared_outside_provider_is_reported_not_added(tmp_path):
    from build_readiness import ProjectFiles
    import node_fixups as nf
    files_map = dict(CTX_FILES)
    files_map["src/hooks/useGhost.ts"] = ("import { useContext } from 'react';\nimport { CartContext } from '../context/CartContext';\n"
                                         "export function useGhost() {\n  const { ghost } = useContext(CartContext)!;\n  return ghost;\n}\n")
    files_map["src/hooks/useCart.ts"] = files_map["src/hooks/useGhost.ts"].replace("useGhost", "useCart2")
    for rel, text in files_map.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    files = ProjectFiles(tmp_path)
    notes, writes = nf.context_value_missing(files)
    assert writes == {}
    assert any(g[0].startswith("ghost(") for g in nf.context_value_gaps(files))


def test_hook_return_aliases_fill_renamed_values(tmp_path):
    from build_readiness import ProjectFiles
    import node_fixups as nf
    files_map = {
        "src/hooks/useCart.ts": (
            "import { useState } from 'react';\nexport interface UseCartReturn {\n  items: any[];\n  totalPrice: number;\n"
            "  getTotalItems: () => number;\n  addItem: (i: any) => void;\n}\n"
            "export function useCart(): UseCartReturn {\n  const [items, setItems] = useState<any[]>([]);\n"
            "  const totalPrice = items.reduce((s, i) => s + i.price, 0);\n  const getTotalItems = () => items.length;\n"
            "  const addItem = (i: any) => setItems(p => [...p, i]);\n"
            "  if (items.length > 99) {\n    return { items: [], totalPrice: 0, getTotalItems, addItem };\n  }\n"
            "  return {\n    items,\n    totalPrice,\n    getTotalItems,\n    addItem,\n  };\n}\n"),
        "src/pages/Cart.tsx": ("import { useCart } from '../hooks/useCart';\nexport default function Cart() {\n"
                               "  const { items, total, totalItems, addToCart } = useCart();\n"
                               "  return <div onClick={() => addToCart(items[0])}>{total}{totalItems}</div>;\n}\n"),
        "src/pages/Bad.tsx": ("import { useCart } from '../hooks/useCart';\nexport default function Bad() {\n"
                              "  const { wishlist } = useCart();\n  return <div>{wishlist}</div>;\n}\n"),
    }
    for rel, text in files_map.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    files = ProjectFiles(tmp_path)
    notes, writes = nf.context_value_missing(files)
    out = writes["src/hooks/useCart.ts"]
    assert "    addItem,\n    addToCart: addItem,\n    total: totalPrice,\n    totalItems: getTotalItems(),\n  };" in out
    assert "  total: any;" in out and "  addToCart: any;" in out
    assert "return { items: [], totalPrice: 0, getTotalItems, addItem };" in out  # 안쪽 return 은 그대로
    assert _syntax_ok(out, "x.ts")
    assert [g[0] for g in nf.context_value_gaps(files)] == ["wishlist(src/pages/Bad.tsx ← src/hooks/useCart.ts)"]


def test_repair_round_takes_runtime_breaking_type_errors_first():
    import gen_engine
    ops = [{"file": f"src/p{i}.tsx", "content": "x"} for i in range(3)]
    grouped = {0: ["src/p0.tsx: 3행 TS2322: Type '{ size: string; }' is not assignable"],
               1: ["src/p1.tsx: 9행 TS7006: Parameter 'x' implicitly has an 'any' type"],
               2: ["src/p2.tsx: 78행 TS2554: Expected 2 arguments, but got 1."]}
    assert list(gen_engine._definition_first(ops, grouped))[0] == 2


def test_repair_round_keeps_syntax_and_static_errors_in_front():
    import gen_engine
    ops = [{"file": f"src/p{i}.tsx", "content": "x"} for i in range(4)]
    grouped = {0: ["src/p0.tsx: 3행 TS2322: Type '{ size: string; }' is not assignable"],
               1: ["src/p1.tsx: 9행 TS1005: ',' expected."],
               2: ["vite: Could not resolve './x' from src/p2.tsx"],
               3: ["src/p3.tsx: 9행 TS7006: Parameter 'x' implicitly has an 'any' type"]}
    order = list(gen_engine._definition_first(ops, grouped))
    assert order[:2] == [1, 2] and set(order[2:]) == {0, 3}


def test_review_round3_regressions(tmp_path):
    """검토 3차: 객체 키(변수 아님)로 별칭 금지 · 안쪽 스코프 선언 무시 · 끝 주석 뒤 쉼표 · 같은 파일 두 Provider."""
    from build_readiness import ProjectFiles
    import node_fixups as nf
    files_map = {
        "src/context/Ctx.tsx": (
            "import React, { createContext, useState } from 'react';\n"
            "export const AuthContext = createContext<any>(null);\nexport const CartContext = createContext<any>(null);\n"
            "export function AuthProvider({ children }: any) {\n  const [user, setUser] = useState(null);\n"
            "  const value = { user };\n  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;\n}\n"
            "export function CartProvider({ children }: any) {\n  const [items, setItems] = useState<any[]>([]);\n"
            "  const value = {\n    items, // list\n    totalPrice: items.length,\n  };\n"
            "  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;\n}\n"),
        "src/hooks/useCart.ts": (
            "import { useContext } from 'react';\nimport { CartContext } from '../context/Ctx';\n"
            "export function useCartX() {\n  const ctx = useContext(CartContext);\n  const { items, setItems, total } = ctx;\n  return { items, setItems, total };\n}\n"),
        "src/hooks/useTotal.ts": (
            "import { useState } from 'react';\nexport function useTotal() {\n  const [items] = useState<any[]>([]);\n"
            "  const getTotalPrice = () => {\n    let total = 0;\n    for (const i of items) total += i;\n    return total;\n  };\n"
            "  return {\n    items,\n    getTotalPrice // sum\n  };\n}\n"),
        "src/pages/T.tsx": ("import { useTotal } from '../hooks/useTotal';\nexport default function T() {\n"
                            "  const { total } = useTotal();\n  return <b>{total}</b>;\n}\n"),
    }
    for rel, text in files_map.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    files = ProjectFiles(tmp_path)
    notes, writes = nf.context_value_missing(files)
    # useCartX 는 total 을 채울 수 없어(totalPrice 는 변수가 아니다) 손대지 않고 경고로 남긴다
    assert "src/context/Ctx.tsx" not in writes
    assert any(g[0].startswith("setItems, total(") or g[0].startswith("total(") for g in nf.context_value_gaps(files))
    hook = writes["src/hooks/useTotal.ts"]
    assert "getTotalPrice, // sum\n    total: getTotalPrice()," in hook
    assert _syntax_ok(hook, "x.ts")


def test_core_io_never_depends_on_the_windows_locale():
    """한국어 윈도우(cp949)에서 SBOM(JSON 에 한글·특수 문자)을 읽다 ECS 배포가 멈췄다(실기기 2.0.16 SBOM 단계).
    파일·하위 프로세스 출력은 늘 UTF-8 로 명시한다."""
    import ast
    core = Path(__file__).resolve().parents[1]
    bad = []
    for p in core.rglob("*.py"):
        rel = p.relative_to(core).as_posix()
        if rel.startswith(("tests/", "eval/")) or rel == "singleton.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            kws = {k.arg: k for k in n.keywords}
            if name in ("read_text", "write_text") and isinstance(f, ast.Attribute) and "encoding" not in kws:
                bad.append(f"{rel}:{n.lineno} {name}")
            text = kws.get("text") or kws.get("universal_newlines")
            if name in ("run", "Popen", "check_output") and text is not None and "encoding" not in kws \
                    and isinstance(text.value, ast.Constant) and text.value.value is True:
                bad.append(f"{rel}:{n.lineno} {name}(text=True)")
    assert not bad, bad


def test_sbom_with_non_ascii_is_read_under_a_non_utf8_locale(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    syft = fake / "syft"
    syft.write_text("#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in cyclonedx-json=*) out=\"${a#cyclonedx-json=}\";; esac; done\n"
                    "printf '{\"components\":[{\"name\":\"cafe\",\"description\":\"카페 ☕\"}]}' > \"$out\"\n", encoding="utf-8")
    syft.chmod(0o755)
    code = ("import asyncio,sys;sys.path.insert(0,%r);sys.path.insert(0,%r);from core.sbom import SBOMGenerator;"
            "r=asyncio.run(SBOMGenerator().generate('app:1'));print(r.package_count, r.error)") % (
        str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1]))
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
           "PYTHONCOERCECLOCALE": "0", "RECODER_SBOM_DIR": str(tmp_path / "sbom")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.stdout.strip().startswith("1 None"), out.stdout + out.stderr


def test_generated_tsconfig_module_matches_module_resolution():
    import node_fixups as nf
    out = nf.relax_generated_tsconfig('{"compilerOptions": {"module": "ES2020", "moduleResolution": "nodeNext", "strict": true}}')
    assert json.loads(out)["compilerOptions"]["module"] == "NodeNext"
    out = nf.relax_generated_tsconfig('{"compilerOptions": {"module": "commonjs", "moduleResolution": "bundler"}}')
    assert json.loads(out)["compilerOptions"]["module"] == "ESNext"
    assert nf.relax_generated_tsconfig('{"compilerOptions": {"module": "NodeNext", "moduleResolution": "NodeNext"}}') is None


def test_seed_command_skips_scripts_whose_file_does_not_exist(tmp_path):
    import local_services
    _write(tmp_path, {
        "Dockerfile": docker_kit.render({"server": "backend", "entry": "dist/index.js", "ui": None, "server_build": True,
                                         "port": 3000, "health": "/health"}),
        "backend/package.json": json.dumps({"name": "b", "type": "module", "scripts": {
            "build": "tsc", "start": "node dist/index.js", "db:init": "node dist/db/init.js", "db:setup": "tsx src/db/seed.ts"},
            "dependencies": {"express": "^4", "pg": "^8"}}),
        "backend/tsconfig.json": json.dumps({"compilerOptions": {"rootDir": "./src", "outDir": "./dist"}}),
        "backend/src/index.ts": "import express from 'express';\nexpress().listen(3000);\n",
        "backend/src/db/seed.ts": "console.log('seed');\n",
    })
    cmd = local_services.seed_command(str(tmp_path))
    assert cmd and cmd["script"] == "dist/db/seed.js" and cmd["name"] == "db:setup"


def test_optional_marker_on_object_literal_methods_is_removed_only_there(tmp_path):
    import node_fixups as nf
    src = ("interface H {\n  onFail?(e: any): Promise<void>;\n}\nclass C {\n  m?(): void {}\n}\n"
           "export const h = {\n  async onFail?(e: any) {\n    return 1;\n  },\n  run?(): void {\n  },\n};\n"
           "const t = cond\n  ? (a)\n  : b;\ntype T = { f?(x: number): void };\n")
    (tmp_path / "a.ts").write_text(src, encoding="utf-8")
    notes, writes = nf.optional_object_methods(ProjectFiles(tmp_path))
    out = writes["a.ts"]
    assert "async onFail(e: any) {" in out and "  run(): void {" in out
    assert "onFail?(e: any): Promise<void>;" in out and "m?(): void {}" in out and "f?(x: number)" in out and "? (a)" in out
    assert _syntax_ok(out, "a.ts")
