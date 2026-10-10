"""2.0.7 — 실기기 TEMP(영상 18-09-09)에서 개발·배포·보안이 막힌 원인들을 확실히 고친다."""
import json
from pathlib import Path

import build_readiness as br
import code_agent as ca
import docker_kit as dk
import local_services as ls
import node_fixups as nf
import security_fix as sf


def _w(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


BACKEND_PKG = json.dumps({"name": "b", "type": "module", "scripts": {"build": "tsc", "start": "node dist/server.js"},
                          "dependencies": {"express": "^4.21.2", "pg": "^8.13.0", "uuid": "^9.0.1"},
                          "devDependencies": {"typescript": "^5.6.3", "@types/express": "^4.17.21"}})
FRONT_PKG = json.dumps({"name": "f", "type": "module", "scripts": {"build": "tsc && vite build"},
                        "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1", "react-router-dom": "^6.27.0", "axios": "^1.7.7"},
                        "devDependencies": {"typescript": "^5.6.3", "vite": "^5.4.9"}})
AI_DOCKERFILE = """FROM node:18-alpine AS backend-builder
WORKDIR /app
COPY package.json package-lock.json* ./
COPY backend ./backend
RUN npm install --omit=dev
RUN npm run build --workspace=backend

FROM node:18-alpine AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json* ./
COPY frontend ./frontend
RUN npm install --omit=dev
RUN npm run build --workspace=frontend

FROM node:18-alpine
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY --from=backend-builder /app/backend/dist ./backend/dist
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist
USER 1001
CMD ["npm", "start", "--workspace=backend"]
"""


def _shop(root: Path) -> Path:
    return _w(root, {
        "package.json": json.dumps({"name": "shop", "private": True, "workspaces": ["backend", "frontend"],
                                    "scripts": {"start": "npm start --workspace=backend"}}),
        "Dockerfile": AI_DOCKERFILE, ".dockerignore": "node_modules\n",
        "backend/package.json": BACKEND_PKG,
        "backend/tsconfig.json": json.dumps({"compilerOptions": {"outDir": "./dist", "rootDir": "./src", "strict": True}}),
        "backend/src/server.ts": "import express from 'express';\nimport paymentRoutes from './routes/payment.js';\nimport { v4 } from 'uuid';\n"
                                 "const app = express();\napp.get('/health', (_q, s) => { s.json({ ok: v4() }); });\napp.use('/api/payment', paymentRoutes);\n"
                                 "app.use(express.static('frontend/dist'));\napp.listen(process.env.PORT || 3000);\n",
        "backend/src/routes/payment.ts": "import { Router } from 'express';\nconst router = Router();\nrouter.get('/', (_q, s) => { s.json([]); });\n"
                                         "export { router as paymentRouter };\n",
        "backend/scripts/init-db.ts": "await client.query(`\n  CREATE TABLE products (\n    id SERIAL PRIMARY KEY,\n    name TEXT\n  );\n`);\n"
                                      "await client.query('CREATE INDEX idx_products_name ON products(name)');\n",
        "frontend/package.json": FRONT_PKG,
        "frontend/index.html": "<div id=root></div><script type=module src=/src/main.tsx></script>",
        "frontend/vite.config.ts": "import { defineConfig } from 'vite';\nexport default defineConfig({ build: { outDir: 'dist' } });\n",
        "frontend/src/main.tsx": "import React from 'react';\nimport ReactDOM from 'react-dom/client';\nimport { BrowserRouter } from 'react-router-dom';\n"
                                 "import { AuthProvider } from './AuthContext';\nimport App from './App';\n\n"
                                 "ReactDOM.createRoot(document.getElementById('root')!).render(\n  <React.StrictMode>\n    <BrowserRouter>\n"
                                 "      <AuthProvider>\n        <App />\n      </AuthProvider>\n    </BrowserRouter>\n  </React.StrictMode>,\n);\n",
        "frontend/src/AuthContext.tsx": "export function AuthProvider({ children }: { children: any }) { return children; }\n",
        "frontend/src/App.tsx": "import { BrowserRouter as Router } from 'react-router-dom';\nimport { AuthProvider } from './AuthContext';\n"
                                "import Orders from './Orders';\nexport default function App() {\n  return (<Router><AuthProvider><Orders /></AuthProvider></Router>);\n}\n",
        "frontend/src/api/client.ts": "import axios, { AxiosInstance } from 'axios';\nconst API_BASE_URL = '/api';\n"
                                      "const client: AxiosInstance = axios.create({ baseURL: API_BASE_URL });\nexport async function ping() { return client.get('/x'); }\n",
        "frontend/src/types.ts": "export interface Order { id: number }\n",
        "frontend/src/Orders.tsx": "import { useEffect, useState } from 'react';\n\nexport default function Orders() {\n"
                                   "  const [rows, setRows] = useState<Order[]>([]);\n  useEffect(() => { apiClient.get<Order[]>(\n    '/api/orders').then(r => setRows(r.data)); }, []);\n"
                                   "  return <div>{rows.length}</div>;\n}\n",
        "frontend/src/Cart.tsx": "import { apiClient } from './api/client';\nexport const load = () => apiClient.get('/api/cart');\n",
    })


def test_배포가_막히던_원인을_모두_찾고_자동_수정으로_고친다(tmp_path):
    root = _shop(tmp_path)
    codes = {i.code for i in br.analyze(root).issues}
    assert {"DOCKERFILE_RUNTIME_BROKEN", "DOCKERFILE_BUILD_STAGE_OMITS_DEV", "NODE_IMPORT_NAME_MISSING", "NODE_NAME_NOT_IMPORTED",
            "NODE_TYPES_MISSING", "NODE_NESTED_ROUTER"} <= codes
    for _ in range(14):
        todo = [i for i in br.analyze(root).issues if i.auto_fix and i.code in br.AUTO_FIXABLE]
        if not todo:
            break
        assert br.apply_fix(root, todo[0].code)["applied"], todo[0].code
    left = [i.code for i in br.analyze(root).issues if i.severity == "error"]
    assert left == []
    docker = (root / "Dockerfile").read_text()
    assert dk.is_recoder(docker) and 'CMD ["node", "backend/dist/server.js"]' in docker and "WORKDIR /app\n" in docker
    assert "export default router;" in (root / "backend/src/routes/payment.ts").read_text()
    assert "export { client as apiClient };" in (root / "frontend/src/api/client.ts").read_text()
    orders = (root / "frontend/src/Orders.tsx").read_text()
    assert "import { apiClient } from './api/client';" in orders and "import type { Order } from './types';" in orders
    assert "'/orders'" in orders and "'/cart'" in (root / "frontend/src/Cart.tsx").read_text()
    main = (root / "frontend/src/main.tsx").read_text()
    assert "BrowserRouter" not in main and "AuthProvider" not in main and "<App />" in main
    assert "@types/uuid" in json.loads((root / "backend/package.json").read_text())["devDependencies"]


def test_검증_Dockerfile_은_실행_단계가_깨지지_않고_보안_권고가_없다(tmp_path):
    root = _shop(tmp_path)
    info = dk.layout(br.ProjectFiles(root))
    text = dk.render(info)
    assert dk.runtime_breaks(text, info) == []
    assert dk.runtime_breaks(AI_DOCKERFILE, info)
    assert sf.lint_clean(text) == (text, [])
    assert "--workspaces=false" in text and "npm prune --omit=dev" in text and "USER 1000" in text
    assert 'HEALTHCHECK' in text and text.count("RUN ") == 3


def test_빌드_단계만_개발_의존성을_넣는다():
    fixed = nf.build_stage_omits_dev(AI_DOCKERFILE)
    stages = fixed.split("FROM ")
    assert "--omit=dev" not in stages[1] and "--omit=dev" not in stages[2] and "--omit=dev" in stages[3]
    env = "FROM node:20\nENV NODE_ENV=production\nRUN npm ci\nRUN npm run build\n"
    assert "npm ci --include=dev" in nf.build_stage_omits_dev(env)
    assert nf.build_stage_omits_dev("FROM node:20\nRUN npm ci --omit=dev\nCMD [\"node\",\"a.js\"]\n") is None


def test_초기화_스크립트의_테이블을_배포가_빈_DB_에_만든다(tmp_path):
    root = _shop(tmp_path)
    sql = ls.ddl_from_code(root)
    assert "CREATE TABLE IF NOT EXISTS products" in sql and "CREATE INDEX IF NOT EXISTS idx_products_name" in sql
    assert sql.index("CREATE TABLE") < sql.index("CREATE INDEX")
    # 앱이 시작할 때 IF NOT EXISTS 없이 만들면 미리 만들지 않는다(그 앱이 already exists 로 죽는다)
    _w(root, {"backend/src/db.ts": "await q(`CREATE TABLE users (id INT)`);\n"})
    assert ls.ddl_from_code(root) is None


def test_이름이_어긋나면_고칠_파일마다_실제_export_목록과_함께_알린다(tmp_path):
    files = br.ProjectFiles(_w(tmp_path, {
        "src/services/order.ts": "export async function getAllOrders(page: number) {}\nexport function getOrder() {}\n",
        "src/routes/orders.ts": "import { getOrdersByAdmin } from '../services/order.js';\ngetOrdersByAdmin(1);\n",
        "src/types.ts": "export interface Order {}\n",
        "src/pages/Detail.tsx": "import { Receipt } from '../types';\nconst r: Receipt = load();\n"}))
    out = ca._name_mismatch_issues(files, [("src/routes/orders.ts", "getOrdersByAdmin", "src/services/order.ts"),
                                           ("src/pages/Detail.tsx", "Receipt", "src/types.ts")])
    by = {i["file"]: i["message"] for i in out}
    assert "getAllOrders" in by["src/routes/orders.ts"] and "실제로 내보내는 이름" in by["src/routes/orders.ts"]
    assert "src/types.ts" in by and "const r: Receipt = load();" in by["src/types.ts"]


def test_보안_권고를_같은_동작으로_정리한다():
    text = "FROM node:20-alpine\nRUN apk add curl\nRUN npm ci\n# note\nRUN npm run build\nUSER node\nCMD node server.js\n"
    out, done = sf.lint_clean(text)
    assert "# hadolint ignore=DL3018" in out and "apk add --no-cache curl" in out
    assert out.count("RUN ") == 1 and "&& npm ci" in out and "&& npm run build" in out
    assert 'CMD ["node", "server.js"]' in out
    assert sf.lint_clean(out) == (out, [])
    # 다른 위치에 남은 예외 표시는 지운다(같은 표시가 두 번 보였다)
    stale = "FROM node:20-alpine\n# hadolint ignore=DL3018\n# Install dumb-init\nRUN apk add --no-cache dumb-init\n"
    fixed, _ = sf.lint_clean(stale)
    assert fixed.count("hadolint ignore=DL3018") == 1 and fixed.split("\n")[-3].startswith("# hadolint ignore=DL3018")


def test_생성_직후_검증_Dockerfile_을_고정_파일로_넣는다(tmp_path):
    src = _shop(tmp_path / "src")
    ops = [{"action": "create", "file": p.relative_to(src).as_posix(), "content": p.read_text()} for p in src.rglob("*") if p.is_file()]
    empty = tmp_path / "empty"; empty.mkdir()
    out, _notes = ca._autofix_ops(empty, "", ops)
    out, _ = ca._apply_docker_kit(empty, "", out)
    docker = next(o for o in out if o["file"] == "Dockerfile")
    assert docker.get("fixed") and dk.is_recoder(docker["content"])
    merged = ca._merge_ops(out, [{"file": "Dockerfile", "content": "FROM x\n"}])
    assert dk.is_recoder(next(o for o in merged if o["file"] == "Dockerfile")["content"])
