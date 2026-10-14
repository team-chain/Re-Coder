"""생성 직후 실행 점검(runtime_check) — 경로 표·사용자 흐름·로그 → 원인 파일."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import runtime_check as rc

FILES = {
    "backend/src/app.ts": ("import express from 'express';\nimport productRoutes from './routes/products.js';\n"
                           "import authRoutes from './routes/auth.js';\nimport cartRoutes from './routes/cart.js';\n"
                           "import orderRoutes from './routes/orders.js';\nconst app = express();\n"
                           "app.use('/api/products', productRoutes);\napp.use('/api/auth', authRoutes);\n"
                           "app.use('/api/cart', authenticate, cartRoutes);\napp.use('/api/orders', orderRoutes);\n"
                           "app.get('/health', (_q, r) => r.json({ ok: true }));\nexport default app;\n"),
    "backend/src/routes/products.ts": ("import { Router } from 'express';\nimport * as svc from '../services/productService.js';\n"
                                       "const router = Router();\nrouter.get('/', list);\nrouter.get('/:id', one);\nexport default router;\n"),
    "backend/src/routes/auth.ts": "const router = Router();\nrouter.post('/register', r);\nrouter.post('/login', l);\nexport default router;\n",
    "backend/src/routes/cart.ts": "const router = Router();\nrouter.get('/', g);\nrouter.post('/items', a);\nexport default router;\n",
    "backend/src/routes/orders.ts": "const router = Router();\nrouter.get('/', g);\nrouter.post('/', c);\nexport default router;\n",
    "backend/src/services/productService.ts": "export async function list() {}\n",
    "backend/src/config/database.ts": "export async function query() {}\n",
}


def test_route_table_joins_mount_prefixes():
    table = rc.route_table(FILES, "backend")
    assert ("GET", "/api/products", "backend/src/routes/products.ts") in table
    assert ("GET", "/api/products/:id", "backend/src/routes/products.ts") in table
    assert ("POST", "/api/auth/register", "backend/src/routes/auth.ts") in table
    assert ("POST", "/api/cart/items", "backend/src/routes/cart.ts") in table
    assert ("POST", "/api/orders", "backend/src/routes/orders.ts") in table


class _App(BaseHTTPRequestHandler):
    calls: list = []

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        _App.calls.append(("GET", self.path, bool(auth)))
        if self.path == "/api/products":
            return self._send(200, {"data": {"products": [{"id": 7, "name": "a"}]}})
        if self.path == "/api/products/7":
            return self._send(200, {"id": 7})
        if self.path in ("/api/cart", "/api/orders"):
            return self._send(200 if auth else 401, {})
        return self._send(404, {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        _App.calls.append(("POST", self.path, body))
        if self.path == "/api/auth/register":
            return self._send(201, {"status": "success", "data": {"token": "t0k", "user": {"id": 1}}})
        if self.path == "/api/cart/items":
            return self._send(201, {})
        if self.path == "/api/orders":
            return self._send(500, {"error": "boom"})
        return self._send(404, {})

    def log_message(self, *a):
        pass


def test_scenario_walks_signup_cart_order_and_flags_only_5xx():
    server = HTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _App.calls = []
        logs = iter([[], [], [], [], [], [], [], [], [], [], [], ["Error: boom at createOrder", "    at async x (file:///app/backend/dist/services/orderService.js:3:1)"]] + [[]] * 20)
        results = rc.scenario(f"http://127.0.0.1:{server.server_port}", rc.route_table(FILES, "backend"), lambda: next(logs))
    finally:
        server.shutdown()
    seen = [(r["method"], r["path"], r["status"]) for r in results]
    assert ("GET", "/api/products/7", 200) in seen
    assert ("POST", "/api/auth/register", 201) in seen
    assert ("GET", "/api/cart", 200) in seen  # 로그인 토큰으로 다시
    assert ("POST", "/api/cart/items", 201) in seen
    assert ("POST", "/api/orders", 500) in seen
    order_body = next(c[2] for c in _App.calls if c[:2] == ("POST", "/api/orders"))
    assert order_body["items"][0]["productId"] == 7
    assert [r for r in results if r["status"] and r["status"] >= 500 and r.get("logs") is not None]


def test_stack_maps_to_sources_and_noise_is_dropped():
    lines = ["Database query error: error: could not determine data type of parameter $1",
             "    at async query (file:///app/backend/dist/config/database.js:15:24)",
             "    at async getProducts (file:///app/backend/dist/services/productService.js:35:28)",
             "  detail: undefined,", "  severity: 'ERROR',", "} Query: SELECT * FROM products OFFSET $2 LIMIT $3"]
    assert rc.source_files(lines, FILES, "backend") == ["backend/src/config/database.ts", "backend/src/services/productService.ts"]
    kept = rc.useful_lines(lines)
    assert "  detail: undefined," not in kept and any("OFFSET $2" in l for l in kept)
    issues = rc.issues({"failures": [{"request": "GET /api/products", "status": 500, "lines": kept,
                                      "files": ["backend/src/services/productService.ts"]}]})
    assert issues[0]["code"] == "GENERATED_RUNTIME_ERROR" and "GET /api/products" in issues[0]["message"]


def test_disabled_in_unit_tests(monkeypatch):
    monkeypatch.setenv("RECODER_TEST_MODE", "1")
    assert not rc.enabled()
