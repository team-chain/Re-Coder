"""AI 자유 생성 앱이 실제 Docker 빌드·배포에서 깨진 원인들(2026-10-10 TEMP 쇼핑몰)을 찾고 고친다."""
import json
from pathlib import Path

import build_readiness as br
import code_agent as ca
import node_fixups as nf
import node_manifests as nm


def _write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


ROOT_PKG = json.dumps({
    "name": "shopping-mall", "private": True, "type": "module",
    "scripts": {"build": "npm run build:backend && npm run build:frontend", "build:backend": "cd backend && tsc",
                "build:frontend": "cd frontend && vite build", "start": "node backend/dist/server.js"},
    "workspaces": ["backend", "frontend"], "devDependencies": {"typescript": "^5.3.0"}})
BACKEND_TSCONFIG = json.dumps({"compilerOptions": {"outDir": "./dist", "rootDir": "./src", "strict": True,
                                                   "noUnusedLocals": True, "noUncheckedIndexedAccess": True}})
APP_TS = """import express from 'express';
import { Pool } from 'pg';
import cors from 'cors';
export function createApp() {
  const app = express();
  app.use(cors());
  app.get('/health', (_q, s) => { s.json({ ok: true }); });
  const frontendDistPath = new URL('../../../frontend/dist', import.meta.url).pathname;
  app.use(express.static(frontendDistPath));
  return app;
}
export const pool = new Pool({ connectionString: process.env.DATABASE_URL });
"""
SERVER_TS = """import { createApp } from './app.js';
const PORT = process.env.PORT || 3000;
createApp().listen(PORT);
"""
FRONT = {
    "frontend/index.html": "<div id=root></div><script type=module src=/src/main.tsx></script>",
    "frontend/vite.config.ts": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\n"
                               "export default defineConfig({ plugins: [react()], build: { outDir: 'dist', minify: 'terser' } });\n",
    "frontend/tsconfig.json": json.dumps({"compilerOptions": {"jsx": "react-jsx", "strict": True, "noEmit": True},
                                          "include": ["src"], "references": [{"path": "./tsconfig.node.json"}]}),
    "frontend/src/main.tsx": "import React from 'react';\nimport { createRoot } from 'react-dom/client';\n"
                             "import { BrowserRouter } from 'react-router-dom';\nimport App from './App';\n"
                             "createRoot(document.getElementById('root')!).render(<BrowserRouter><App /></BrowserRouter>);\n",
    "frontend/src/App.tsx": "import ProductCard from './ProductCard';\nexport default function App() { return <ProductCard />; }\n",
    "frontend/src/ProductCard.tsx": "export interface Props { id?: number }\nexport function ProductCard(_p: Props) { return <div />; }\n",
}
DOCKERFILE = """FROM node:20-alpine AS b
WORKDIR /app/backend
COPY backend/package*.json ./
RUN npm ci
COPY backend/ ./
RUN npm run build
FROM node:20-alpine
WORKDIR /app/backend
COPY backend/package*.json ./
RUN npm ci --only=production
COPY --from=b /app/backend/dist ./dist
USER 1001
EXPOSE 3000
CMD ["node", "dist/server.js"]
"""


def _temp_shop(root: Path) -> Path:
    return _write(root, {"package.json": ROOT_PKG, "backend/tsconfig.json": BACKEND_TSCONFIG,
                         "backend/src/app.ts": APP_TS, "backend/src/server.ts": SERVER_TS, "Dockerfile": DOCKERFILE,
                         ".dockerignore": "node_modules\n", **FRONT})


def test_실기기_TEMP_쇼핑몰의_원인을_모두_찾는다(tmp_path):
    root = _temp_shop(tmp_path)
    r = br.analyze(root)
    codes = {i.code for i in r.issues}
    assert {"NODE_WORKSPACE_MANIFEST_MISSING", "NODE_TSCONFIG_REFERENCE_MISSING", "NODE_STATIC_PATH_OUTSIDE_PROJECT",
            "DOCKERFILE_NPM_CI_WITHOUT_LOCK", "NODE_IMPORT_NAME_MISSING"} <= codes
    # 예전 오탐: 루트에서 tsc/vite 를 찾던 점검, 하위 폴더 빌드 결과(backend/dist/server.js)를 없다고 막던 점검
    assert "NODE_BUILD_ENTRY_MISSING" not in codes and "NODE_START_ENTRY_MISSING" not in codes
    # pg 를 쓰므로 PostgreSQL 을 함께 띄운다(예전: DATABASE_URL 을 직접 넣으라고 함), 포트는 TS 원본에서
    assert r.services == ["postgres"] and r.app_port == 3000
    assert all(i.auto_fix for i in r.issues if i.severity == "error")


def test_자동_수정을_차례로_누르면_정적_문제가_모두_사라진다(tmp_path):
    root = _temp_shop(tmp_path)
    for _ in range(10):
        todo = [i for i in br.analyze(root).issues if i.auto_fix and i.code in br.AUTO_FIXABLE]
        if not todo:
            break
        assert br.apply_fix(root, todo[0].code)["applied"]
    assert [i.code for i in br.analyze(root).issues if i.severity == "error"] == []
    backend = json.loads((root / "backend/package.json").read_text())
    assert backend["dependencies"]["pg"] == nm.KNOWN_VERSIONS["pg"] and backend["scripts"]["start"] == "node dist/server.js"
    assert {"@types/express", "@types/pg", "typescript"} <= set(backend["devDependencies"])
    frontend = json.loads((root / "frontend/package.json").read_text())
    assert frontend["scripts"]["build"] == "vite build" and "terser" in frontend["devDependencies"]
    assert "references" not in json.loads((root / "frontend/tsconfig.json").read_text())
    assert "new URL('../../frontend/dist', import.meta.url)" in (root / "backend/src/app.ts").read_text()
    docker = (root / "Dockerfile").read_text()
    assert "npm ci" not in docker and "npm install --no-audit --no-fund --omit=dev" in docker
    assert "export default ProductCard;" in (root / "frontend/src/ProductCard.tsx").read_text()


def test_생성_직후_자동_교정도_같은_것을_고치고_tsconfig_린트만_끈다(tmp_path):
    src = _temp_shop(tmp_path / "src")
    ops = [{"action": "create", "file": p.relative_to(src).as_posix(), "content": p.read_text()} for p in src.rglob("*") if p.is_file()]
    empty = tmp_path / "empty"; empty.mkdir()
    out, notes = ca._autofix_ops(empty, "", ops)
    files = {o["file"]: o["content"] for o in out}
    assert "backend/package.json" in files and "frontend/package.json" in files
    cfg = json.loads(files["backend/tsconfig.json"])["compilerOptions"]
    assert cfg["strict"] is True and cfg["noUnusedLocals"] is False and cfg["noUncheckedIndexedAccess"] is False
    assert not [i for i in ca._consistency_issues(empty, "", out) if i["severity"] == "error"]


def test_하위_폴더_단독_점검은_폴더_밖_경로를_문제로_보지_않는다(tmp_path):
    root = _temp_shop(tmp_path)
    for _ in range(10):
        todo = [i for i in br.analyze(root).issues if i.auto_fix and i.code in br.AUTO_FIXABLE]
        if not todo:
            break
        br.apply_fix(root, todo[0].code)
    (root / "backend/src/app.ts").write_text(APP_TS.replace("../../../frontend", "../../frontend"))
    ops = [{"action": "create", "file": "backend/src/app.ts", "content": (root / "backend/src/app.ts").read_text()},
           {"action": "create", "file": "backend/package.json", "content": (root / "backend/package.json").read_text()}]
    found = ca._consistency_issues(root, "", ops)
    assert not [i for i in found if i["code"] == "NODE_STATIC_PATH_OUTSIDE_PROJECT"]


def test_lock_파일이_있으면_npm_ci_를_그대로_둔다(tmp_path):
    root = _write(tmp_path, {"package.json": "{}", "package-lock.json": "{}", "Dockerfile": "FROM node:20\nCOPY package*.json ./\nRUN npm ci\n"})
    assert nf.npm_ci_without_lock((root / "Dockerfile").read_text(), br.ProjectFiles(root)) is None
    (root / "package-lock.json").unlink()
    fixed = nf.npm_ci_without_lock((root / "Dockerfile").read_text(), br.ProjectFiles(root))
    assert "RUN npm install --no-audit --no-fund" in fixed


def test_path_join_형식도_고친다(tmp_path):
    root = _write(tmp_path, {"server/index.js": "const path = require('path');\napp.use(express.static(path.join(__dirname, '..', '..', 'client', 'dist')));\n",
                             "client/vite.config.js": "export default {}\n", "client/src/a.js": "x"})
    problems, writes = nf.static_paths_outside(br.ProjectFiles(root), {"client/dist"})
    assert problems and "'../client/dist'" in writes["server/index.js"]
    # 프로젝트 안을 가리키면 문제 아님
    root2 = _write(tmp_path / "ok", {"server/index.js": "path.join(__dirname, '../client/dist')"})
    assert nf.static_paths_outside(br.ProjectFiles(root2), set()) == ([], {})


def test_빌드_로그를_파일별_문제로_나눈다():
    log = """#20 [backend-builder 7/7] RUN npm run build
#20 3.6 src/utils/auth.ts(40,28): error TS2769: No overload matches this call.
#20 3.6 src/app.ts(12,10): error TS2724: '"./types.js"' has no exported member named 'HealthResponse'.
#20 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 2
"""
    docker = "FROM node:20 AS backend-builder\nWORKDIR /app/backend\nRUN npm run build\nFROM node:20 AS frontend-builder\nWORKDIR /app/frontend\n"
    ops = ["backend/src/utils/auth.ts", "backend/src/app.ts", "frontend/src/app.ts", "Dockerfile"]
    issues = nf.build_log_issues(log, ops, docker)
    assert {i["file"] for i in issues} == {"backend/src/utils/auth.ts", "backend/src/app.ts"}
    assert "TS2724" in next(i for i in issues if i["file"] == "backend/src/app.ts")["message"]
    vite = """#26 1.350 error during build:
#26 1.350 src/pages/List.tsx (5:7): "default" is not exported by "src/components/Card.tsx", imported by "src/pages/List.tsx".
#26 1.350 file: /app/frontend/src/pages/List.tsx:5:7
"""
    got = nf.build_log_issues(vite, ["frontend/src/pages/List.tsx", "frontend/src/components/Card.tsx"], "")
    assert got and got[0]["file"] == "frontend/src/pages/List.tsx" and "not exported" in got[0]["message"]


HOOK = """import { useAuth } from './useAuth';
export function useApi() {
  const { token } = useAuth();
  async function request(path: string) {
    return fetch('/api' + path, { headers: { Authorization: `Bearer ${token}` } }).then(r => r.json());
  }
  async function get(path: string) { return request(path); }
  return {
    request,
    get,
  };
}
"""
PAGE = """import { useEffect, useState } from 'react';
import { useApi } from '../hooks/useApi';
export function Home() {
  const { get } = useApi();
  const [items, setItems] = useState([]);
  useEffect(() => {
    get('/products').then(setItems);
  }, [get]);
  return null;
}
"""


def test_훅_때문에_요청이_끝없이_반복되는_화면을_찾아_useMemo_로_고친다(tmp_path):
    root = _write(tmp_path, {"src/hooks/useApi.ts": HOOK, "src/pages/Home.tsx": PAGE})
    problems, writes = nf.react_effect_loops(br.ProjectFiles(root))
    assert problems and "src/pages/Home.tsx" in problems[0]
    fixed = writes["src/hooks/useApi.ts"]
    assert "return useMemo(() => ({\n    request,\n    get,\n  }), [token]);" in fixed
    assert fixed.startswith("import { useMemo } from 'react';")
    # 고친 뒤에는 문제가 없다
    (root / "src/hooks/useApi.ts").write_text(fixed)
    assert nf.react_effect_loops(br.ProjectFiles(root)) == ([], {})


def test_의존성에_넣지_않거나_useCallback_이면_건드리지_않는다(tmp_path):
    root = _write(tmp_path, {"src/hooks/useApi.ts": HOOK, "src/pages/Home.tsx": PAGE.replace("}, [get]);", "}, []);")})
    assert nf.react_effect_loops(br.ProjectFiles(root)) == ([], {})
    stable = """import { useCallback } from 'react';
export function useApi() {
  const get = useCallback(async (p: string) => fetch(p), []);
  return { get };
}
"""
    root2 = _write(tmp_path / "b", {"src/hooks/useApi.ts": stable, "src/pages/Home.tsx": PAGE})
    assert nf.react_effect_loops(br.ProjectFiles(root2)) == ([], {})


def test_react_import_에_useMemo_를_더한다(tmp_path):
    hook = "import React, { useState } from 'react';\n" + HOOK
    root = _write(tmp_path, {"src/hooks/useApi.ts": hook, "src/pages/Home.tsx": PAGE})
    _p, writes = nf.react_effect_loops(br.ProjectFiles(root))
    assert "import React, { useState, useMemo } from 'react';" in writes["src/hooks/useApi.ts"]


def test_tsc_빌드인데_tsconfig_가_없으면_만든다(tmp_path):
    root = _write(tmp_path, {"package.json": json.dumps({"name": "m", "type": "module", "scripts": {"build": "tsc", "start": "node dist/server.js"},
                                                         "dependencies": {"express": "^4.21.2"}, "devDependencies": {"typescript": "^5.6.3"}}),
                             "server.ts": "import express from 'express';\nexpress().listen(3000);\n"})
    r = br.analyze(root, dockerfile=None)
    issue = next(i for i in r.issues if i.code == "NODE_TSCONFIG_MISSING")
    assert issue.auto_fix
    br.apply_fix(root, "NODE_TSCONFIG_MISSING")
    cfg = json.loads((root / "tsconfig.json").read_text())["compilerOptions"]
    assert cfg["module"] == "NodeNext" and cfg["outDir"] == "dist" and cfg["rootDir"] == "."


def test_검증된_버전을_아는_패키지는_선언을_자동으로_더한다(tmp_path):
    root = _write(tmp_path, {"package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.21.2"}}),
                             "server.js": "const express = require('express');\nconst cors = require('cors');\nexpress().use(cors()).listen(3000);\n"})
    issue = next(i for i in br.analyze(root, dockerfile=None).issues if i.code == "NODE_UNDECLARED_DEPENDENCY")
    assert issue.auto_fix
    br.apply_fix(root, "NODE_UNDECLARED_DEPENDENCY")
    assert json.loads((root / "package.json").read_text())["dependencies"]["cors"] == nm.KNOWN_VERSIONS["cors"]
    # 모르는 패키지는 지어내지 않는다
    root2 = _write(tmp_path / "b", {"package.json": json.dumps({"name": "a", "scripts": {"start": "node server.js"}}),
                                    "server.js": "require('some-unknown-pkg');\nrequire('http').createServer().listen(3000);\n"})
    assert not next(i for i in br.analyze(root2, dockerfile=None).issues if i.code == "NODE_UNDECLARED_DEPENDENCY").auto_fix


def test_TS_타입_내보내기가_있어도_기본_내보내기_누락을_찾는다():
    assert br._esm_exports("export interface A {}\nexport type B = 1;\nexport function C() {}\n") == {"A", "B", "C"}
    assert br._esm_exports("export * from './x';") is None


def test_설계가_빠뜨린_폴더_package_json_과_tsconfig_를_목록에_넣는다():
    files = [{"file": p, "purpose": ""} for p in ("backend/src/server.ts", "frontend/src/App.tsx", "frontend/index.html")]
    done = {f["file"] for f in ca._complete_fullstack_manifest(files)}
    assert {"backend/package.json", "backend/tsconfig.json", "frontend/package.json", "frontend/tsconfig.json",
            "package.json", "Dockerfile", ".dockerignore", "README.md"} <= done


def test_tsc_빌드_실패는_린트성_오류_수를_따로_알려준다():
    import build_failure as bf
    log = "\n".join([
        "#20 3.6 src/a.ts(1,10): error TS6133: 'x' is declared but its value is never read.",
        "#20 3.6 src/a.ts(2,10): error TS6133: 'y' is declared but its value is never read.",
        "#20 3.6 src/b.ts(5,3): error TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.",
        "#20 ERROR: process \"/bin/sh -c npm run build\" did not complete successfully: exit code: 2"])
    d = bf.diagnose(log)
    assert d.code == "TYPESCRIPT_ERROR" and "3건(2개 파일)" in d.cause and "2건은 '선언만 하고 쓰지 않음'" in d.cause
    assert "noUnusedLocals" in d.fix
