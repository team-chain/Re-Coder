"""2.0.24 — 마을 공용 게시판(서버 → 화면 API 모양 공유) · 의존 관문(파일 완성 직후 검사·자동 수정·1회 되돌림)."""
from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

import board
import gen_engine as ge


# ── 게시판 추출 ───────────────────────────────────────────────────────────

ORDERS_ROUTE = """import { Router } from 'express';
import { requireAuth } from '../middleware/auth';
const router = Router();
router.get('/', async (req, res) => {
  const orders = await db.order.findMany();
  res.json({ orders, total: orders.length });
});
router.post('/', requireAuth, async (req, res) => {
  if (!req.body.items) return res.status(400).json({ error: 'items 필요' });
  res.status(201).json({ order: { id: 1, items: [], total: 0 } });
});
router.get('/:id', requireAuth, async (req, res) => { const o = await find(req.params.id); res.json(o); });
export default router;
"""


def test_local_routes_reads_paths_success_shape_and_auth():
    rows = board.local_routes(ORDERS_ROUTE)
    assert rows == [
        {"method": "GET", "sub": "/", "shape": "{ orders, total }", "auth": False},
        {"method": "POST", "sub": "/", "shape": "{ order }", "auth": True},   # 400 응답은 성공 모양이 아니다
        {"method": "GET", "sub": "/:id", "shape": None, "auth": True},        # 변수 응답은 지어내지 않는다
    ]


def test_router_level_auth_marks_every_route():
    src = "const r = Router();\nr.use(authenticate);\nr.get('/me', (req, res) => res.json({ user: req.user }));\n"
    assert board.local_routes(src)[0]["auth"] is True


def test_mount_prefix_from_completed_server_entry_or_contract_guess():
    server = "import orders from './routes/orders';\napp.use('/api/orders', orders);\n"
    got = board.board_entries("backend/src/routes/orders.ts", ORDERS_ROUTE, {"backend/src/server.ts": server}, "backend")
    assert [e["path"] for e in got] == ["/api/orders", "/api/orders", "/api/orders/:id"] and not got[0]["guessed"]
    guessed = board.board_entries("backend/src/routes/orders.ts", ORDERS_ROUTE, {}, "backend", "GET /api/v2/orders 목록")
    assert guessed[0]["path"] == "/api/v2/orders" and guessed[0]["guessed"]


def test_board_text_puts_related_resource_first_and_marks_unknown_shape():
    b = {"o": board.board_entries("backend/src/routes/orders.ts", ORDERS_ROUTE, {}, "backend"),
         "p": [{"method": "GET", "path": "/api/products", "shape": "{ products }", "auth": False,
                "file": "backend/src/routes/products.ts", "guessed": False}]}
    text = board.format_board(b, ["frontend/src/api/products.ts"])
    lines = [l for l in text.splitlines() if l.startswith("- ")]
    assert lines[0] == "- GET /api/products → { products }"
    assert "모양 모름" in text and "로그인 필요" in text


def test_api_layer_and_wait_targets():
    sides = frozenset({"frontend", "backend"})
    assert board.is_api_layer("frontend/src/api/orders.ts", {"backend"}, sides)
    assert board.is_api_layer("frontend/src/services/http.js", {"backend"}, sides)
    assert not board.is_api_layer("frontend/src/pages/Orders.tsx", {"backend"}, sides)
    assert not board.is_api_layer("backend/src/api/orders.ts", {"backend"}, sides)
    routes = ["backend/src/routes/orders.ts", "backend/src/routes/products.ts"]
    assert board.api_waits_for("frontend/src/api/orders.ts", routes) == ["backend/src/routes/orders.ts"]
    assert board.api_waits_for("frontend/src/api/client.ts", routes) == routes


# ── 엔진 연결 ─────────────────────────────────────────────────────────────

def _default(path: str) -> str:
    if path.endswith(".json"):
        return '{"compilerOptions": {"strict": true, "jsx": "react-jsx"}}' if "tsconfig" in path else "{}"
    if path.endswith(".html"):
        return '<!doctype html><div id="root"></div><script type="module" src="/src/main.tsx"></script>'
    if path.endswith((".css",)):
        return "body{margin:0}"
    return "export {};\n"


def _engine(plan, contents, monkeypatch, *, fixes=None, emit=None):
    monkeypatch.setattr(ge, "_check_file", lambda p, c: [])
    seen: dict[str, str] = {}
    order: list[str] = []
    fix_prompts: list[str] = []
    lock = threading.Lock()

    def call(self, prompt, schema, operation, max_tokens, agent="", raw=False):
        if operation == "generate_code_manifest":
            return SimpleNamespace(text=json.dumps({"summary": "주문", "contracts": "GET /api/orders", "files": plan, "more": False}))
        if operation == "review_code_manifest":
            return SimpleNamespace(text=json.dumps({"files": [], "more": False}))
        if operation == "generate_code_fix":
            with lock:
                fix_prompts.append(prompt)
            return SimpleNamespace(text=json.dumps({"edits": fixes or []}))
        if operation == "generate_code_file_part":  # 설계가 더한 설정 파일(tsconfig 등)
            path = re.search(r"지금은 (\S+) 파일 하나만", prompt).group(1)
            return SimpleNamespace(text=_default(path) + "\n<<<RECODER_END_OF_FILE>>>\n")
        assert operation == "generate_code_part", operation
        wanted = prompt.rsplit("**아래 파일만**", 1)[1]
        fs = re.findall(r"^- (\S+)$", wanted, re.M)
        with lock:
            for f in fs:
                seen[f] = prompt
                order.append(f)
        return SimpleNamespace(text=json.dumps({"summary": "", "ops": [{"action": "create", "file": f,
                                                                         "content": contents.get(f) or _default(f)} for f in fs]}))

    eng = ge.LargeGeneration("요청", concurrency=3, security_review=False, new_project=True, emit=emit or (lambda e: None))
    eng.call = call.__get__(eng)
    return eng, seen, order, fix_prompts


FULLSTACK = [
    {"file": "backend/package.json", "purpose": "", "layer": 0},
    {"file": "backend/src/routes/orders.ts", "purpose": "주문 API", "layer": 1},
    {"file": "backend/src/server.ts", "purpose": "진입점", "layer": 1},
    {"file": "frontend/package.json", "purpose": "", "layer": 0},
    {"file": "frontend/src/api/orders.ts", "purpose": "주문 API 호출", "layer": 1},
    {"file": "frontend/src/pages/Orders.tsx", "purpose": "주문 화면", "layer": 2},
]
FULLSTACK_CONTENT = {
    "backend/package.json": '{"name":"b","dependencies":{"express":"^4.21.2"}}',
    "backend/src/routes/orders.ts": ORDERS_ROUTE,
    "backend/src/server.ts": "import express from 'express';\nimport orders from './routes/orders';\nconst app = express();\n"
                             "app.use('/api/orders', orders);\napp.listen(3000);\n",
    "frontend/package.json": '{"name":"f","dependencies":{"react":"^18.3.1"}}',
    "frontend/src/api/orders.ts": "export async function listOrders() { const r = await fetch('/api/orders'); return (await r.json()).orders; }\n",
    "frontend/src/pages/Orders.tsx": "import { listOrders } from '../api/orders';\nexport default function Orders() { return null; }\n",
}


def test_frontend_api_file_waits_for_route_and_sees_the_board(monkeypatch):
    events: list[dict] = []
    eng, seen, order, _ = _engine(FULLSTACK, FULLSTACK_CONTENT, monkeypatch, emit=events.append)
    _data, ops, _ = eng.run()
    assert set(FULLSTACK_CONTENT) <= {o["file"] for o in ops}
    assert order.index("backend/src/routes/orders.ts") < order.index("frontend/src/api/orders.ts")
    prompt = seen["frontend/src/api/orders.ts"]
    assert "[게시판 — 서버가 실제로 돌려주는 모양" in prompt and "GET /api/orders → { orders, total }" in prompt
    assert "[게시판 —" not in seen["backend/src/server.ts"]  # 서버 파일끼리는 게시판을 넣지 않는다
    post = [e for e in events if e["step"] == "board_post"]
    assert post and post[0]["file"] == "backend/src/routes/orders.ts" and post[0]["items"][0]["shape"] == "{ orders, total }"
    assert any(e["step"] == "board_read" and e["file"] == "frontend/src/api/orders.ts" for e in events)
    assert eng.state["board"]  # 이어 만들기에서도 남도록 작업 상태에 저장


def test_board_survives_resume():
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False)
    saved = {"manifest": {"summary": "", "contracts": ""}, "files": FULLSTACK, "ops": {},
             "board": {"backend/src/routes/orders.ts": [{"method": "GET", "path": "/api/orders", "shape": "{ orders }",
                                                          "auth": False, "file": "backend/src/routes/orders.ts"}]}}
    assert eng.resume_from(saved)
    assert "GET /api/orders → { orders }" in eng._board_block(["frontend/src/api/orders.ts"])


def test_components_do_not_wait_for_server_routes():
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False)
    eng.state["files"] = FULLSTACK
    deps = eng._deps(FULLSTACK)
    api = {d for d, _ in deps[ge._norm("frontend/src/api/orders.ts")]}
    page = {d for d, _ in deps[ge._norm("frontend/src/pages/Orders.tsx")]}
    assert ge._norm("backend/src/routes/orders.ts") in api
    assert ge._norm("backend/src/routes/orders.ts") not in page
    assert not any(d.startswith("frontend/") for d, _ in deps[ge._norm("backend/src/routes/orders.ts")])


# ── 의존 관문 ─────────────────────────────────────────────────────────────

GATE_PLAN = [
    {"file": "package.json", "purpose": "", "layer": 0},
    {"file": "src/context/CartContext.tsx", "purpose": "장바구니 Context", "layer": 1},
    {"file": "src/pages/Cart.tsx", "purpose": "장바구니 화면", "layer": 2, "uses": ["src/context/CartContext.tsx"]},
]


def test_gate_adds_missing_export_mechanically_before_dependents_continue(monkeypatch):
    contents = {
        "package.json": '{"name":"shop","dependencies":{"react":"^18.3.1"}}',
        "src/context/CartContext.tsx": "import { createContext } from 'react';\nconst CartContext = createContext(null);\n"
                                       "export function CartProvider({ children }: any) { return children; }\n",
        "src/pages/Cart.tsx": "import { useContext } from 'react';\nimport { CartContext } from '../context/CartContext';\n"
                              "export default function Cart() { const c = useContext(CartContext); return c; }\n",
    }
    events: list[dict] = []
    eng, _seen, _order, fixes = _engine(GATE_PLAN, contents, monkeypatch, emit=events.append)
    _data, ops, _ = eng.run()
    ctx = next(o for o in ops if o["file"] == "src/context/CartContext.tsx")["content"]
    assert "export" in ctx.split("CartContext = createContext")[0].splitlines()[-1]
    gate = [e for e in events if e["step"] == "gate_check" and e["file"] == "src/pages/Cart.tsx"]
    assert gate and gate[0]["ok"] is True and gate[0]["fixed"] and not gate[0]["returned"]
    assert fixes == []  # AI 호출 없이 고쳤다


def test_gate_returns_card_to_author_once_and_never_blocks(monkeypatch):
    contents = {
        "package.json": '{"name":"shop","dependencies":{"react":"^18.3.1"}}',
        "src/context/CartContext.tsx": "export function useCart() { return { items: [] }; }\n",
        "src/pages/Cart.tsx": "import { useCartItems } from '../context/CartContext';\n"
                              "export default function Cart() { return useCartItems(); }\n",
    }
    events: list[dict] = []
    edits = [{"find": "import { useCartItems } from '../context/CartContext';", "replace": "import { useCart } from '../context/CartContext';"},
             {"find": "return useCartItems();", "replace": "return useCart();"}]
    eng, _seen, _order, fixes = _engine(GATE_PLAN, contents, monkeypatch, fixes=edits, emit=events.append)
    _data, ops, _ = eng.run()
    assert len(fixes) == 1 and "useCartItems" in fixes[0]
    page = next(o for o in ops if o["file"] == "src/pages/Cart.tsx")["content"]
    assert "useCart()" in page and "useCartItems" not in page
    gate = [e for e in events if e["step"] == "gate_check" and e["file"] == "src/pages/Cart.tsx"]
    assert gate[0]["returned"] is True and gate[0]["ok"] is False and gate[0]["by"].startswith("agent-")


def test_gate_failure_to_fix_still_finishes(monkeypatch):
    contents = {
        "package.json": '{"name":"shop","dependencies":{"react":"^18.3.1"}}',
        "src/context/CartContext.tsx": "export function useCart() { return {}; }\n",
        "src/pages/Cart.tsx": "import { nope } from '../context/CartContext';\nexport default function Cart() { return nope(); }\n",
    }
    eng, _s, _o, fixes = _engine(GATE_PLAN, contents, monkeypatch, fixes=[{"find": "zzz", "replace": "y"}])
    _data, ops, _ = eng.run()
    assert len(fixes) == 1 and set(contents) <= {o["file"] for o in ops}


def test_gate_can_be_disabled_and_turns_itself_off_when_slow(monkeypatch):
    monkeypatch.setenv("RECODER_GATE", "0")
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False)
    op = {"file": "src/a.ts", "content": "export const a = 1;\n"}
    assert eng._gate(op, [], "agent-1") is op
    monkeypatch.delenv("RECODER_GATE")
    eng = ge.LargeGeneration("요청", concurrency=1, security_review=False)
    monkeypatch.setattr(ge, "GATE_BUDGET_S", -1.0)
    eng._gate(dict(op), [], "agent-1")
    eng._gate(dict(op), [], "agent-1")
    assert eng._gate_slow >= 2
    import build_readiness
    monkeypatch.setattr(build_readiness, "analyze", lambda *a, **k: pytest.fail("gate should be off"))
    eng._gate(dict(op), [], "agent-1")
