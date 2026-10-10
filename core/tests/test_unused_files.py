"""만들어 놓고 아무도 쓰지 않는 파일 — 같은 API 를 직접 부르는 화면이 있으면 연결하고, 남으면 결과에 알린다."""
import json
from types import SimpleNamespace

import code_agent as ca
import unused_files as uf

ORDER_API = ("import api from './client';\n"
             "export const createOrder = (data) => api.post('/api/orders', data).then(r => r.data);\n")
CLIENT = "import axios from 'axios';\nexport default axios.create({ baseURL: '' });\n"
CHECKOUT = ("import React from 'react';\n"
            "export default function Checkout() {\n  const pay = () => fetch('/api/orders', { method: 'POST' });\n  return <button onClick={pay}>주문</button>;\n}\n")
APP = "import Checkout from './pages/Checkout';\nimport api from './api/client';\nexport default function App() { return <Checkout />; }\n"
MAIN = "import App from './App';\n"
SERVER = "import express from 'express';\nconst app = express();\napp.use('/api/orders', orders);\napp.listen(3001);\n"


def _ops(**extra):
    files = {"client/src/api/orderApi.js": ORDER_API, "client/src/api/client.js": CLIENT, "client/src/pages/Checkout.jsx": CHECKOUT,
             "client/src/App.jsx": APP, "client/src/main.jsx": MAIN, "server/src/index.js": SERVER,
             "client/package.json": '{"name":"c","type":"module","scripts":{"build":"vite build"},"dependencies":{"react":"18","axios":"1"},"devDependencies":{"vite":"5"}}',
             "server/package.json": '{"name":"s","type":"module","dependencies":{"express":"4"}}',
             "client/index.html": '<div id="root"></div><script type="module" src="/src/main.jsx"></script>', **extra}
    return [{"action": "create", "file": f, "content": c} for f, c in files.items()]


def test_아무도_안_쓰는_파일과_그_API_를_직접_부르는_화면을_찾는다():
    found = uf.find(_ops())
    assert [u["file"] for u in found] == ["client/src/api/orderApi.js"]
    assert found[0]["consumers"] == ["client/src/pages/Checkout.jsx"], "서버의 라우트 등록은 부르는 쪽이 아니다"
    issue = uf.wiring_issues(found)[0]
    assert issue["file"] == "client/src/pages/Checkout.jsx" and "'../api/orderApi' 의 createOrder" in issue["fix"]


def test_시작_파일_설정_테스트_폴더_통째로_불러오기는_고립으로_보지_않는다():
    ops = _ops(**{"client/vite.config.js": "export default {}", "client/src/setupTests.js": "x", "server/scripts/seed.js": "x",
                  "client/src/a.test.js": "x", "client/src/types.d.ts": "x"})
    assert [u["file"] for u in uf.find(ops)] == ["client/src/api/orderApi.js"]
    dyn = _ops(**{"client/src/load.js": "fs.readdirSync(dir).forEach(f => require(f))"})
    assert not [u for u in uf.find(dyn) if u["file"].startswith("client/")], "폴더를 통째로 불러오는 쪽은 판단하지 않는다"


def test_이름이_어디서든_쓰이면_고립이_아니다():
    used = _ops(**{"client/src/pages/Checkout.jsx": "import { createOrder } from '../api/orderApi';\n" + CHECKOUT})
    assert uf.find(used) == []


def test_생성_결과에_남은_미사용_파일을_알린다(tmp_path, monkeypatch):
    ops = [dict(o, language="javascript", rationale="r") for o in _ops()]

    class R:  # noqa: D401
        def call(self, req, *a, **k):
            return SimpleNamespace(text=json.dumps({"summary": "s", "ops": ops}), model_used="fake", provider="fake")
    monkeypatch.setattr(ca, "get_router", lambda: R())
    monkeypatch.setattr(ca, "_verify_generated_build", lambda *a, **k: {"kind": "docker-build", "status": "skipped", "passed": True, "output": ""})
    confirm = {"id": "__confirm__", "question": "q", "chosen_key": "proceed", "options": [{"key": "proceed", "label": "진행"}, {"key": "cancel", "label": "취소"}], "impact": ""}
    result = ca.generate_code("주문 화면 만들어줘", decisions=[confirm], project_root=str(tmp_path))
    assert result["unused_files"] == [{"file": "client/src/api/orderApi.js", "consumers": ["client/src/pages/Checkout.jsx"]}]
    assert not [i for i in result["consistency_issues"] if i["code"] == "SERVER_ROUTE_NOT_MOUNTED"]


def _team(tmp_path, monkeypatch, fixed_checkout):
    import gen_engine
    ops = [dict(o, language="javascript", rationale="r") for o in _ops()]
    monkeypatch.setattr(ca, "_generate_split", lambda *a, **k: ({"summary": "s"}, [dict(o) for o in ops], SimpleNamespace(model_used="fake", provider="fake")))
    monkeypatch.setattr(ca, "_verify_generated_build", lambda *a, **k: {"kind": "docker-build", "status": "skipped", "passed": True, "output": ""})
    seen = []

    def fake_round(prompt, ops_in, issues, **k):
        seen.append(issues)
        return [dict(o, content=fixed_checkout) if o["file"].endswith("Checkout.jsx") else o for o in ops_in]
    monkeypatch.setattr(gen_engine, "edit_fix_round", fake_round)
    confirm = {"id": "__confirm__", "question": "q", "chosen_key": "proceed", "options": [{"key": "proceed", "label": "진행"}, {"key": "cancel", "label": "취소"}], "impact": ""}
    return ca.generate_code("주문 화면 만들어줘", decisions=[confirm], project_root=str(tmp_path), mode="team"), seen


def test_팀_모드는_같은_API_를_부르는_화면에_연결한다(tmp_path, monkeypatch):
    wired = "import React from 'react';\nimport { createOrder } from '../api/orderApi';\n" + CHECKOUT.split("\n", 1)[1].replace("fetch('/api/orders', { method: 'POST' })", "createOrder({})")
    result, seen = _team(tmp_path, monkeypatch, wired)
    assert seen and seen[0][0]["code"] == "UNUSED_GENERATED_FILE"
    assert result["unused_files"] == []
    assert "createOrder" in next(o for o in result["ops"] if o["file"].endswith("Checkout.jsx"))["content"]


def test_연결하다_새_오류가_생기면_되돌린다(tmp_path, monkeypatch):
    broken = "import React from 'react';\nimport { makeOrder } from '../api/orderApi';\n" + CHECKOUT.split("\n", 1)[1]
    result, seen = _team(tmp_path, monkeypatch, broken)
    assert seen, "연결을 시도했다"
    assert next(o for o in result["ops"] if o["file"].endswith("Checkout.jsx"))["content"] == CHECKOUT
    assert [u["file"] for u in result["unused_files"]] == ["client/src/api/orderApi.js"]


ROUTE = "import { Router } from 'express';\nconst router = Router();\nrouter.get('/', (req, res) => res.json([]));\nexport default router;\n"


def test_서버가_등록하지_않은_API_파일은_서버_진입_파일을_고칠_오류가_된다():
    found = uf.find(_ops(**{"server/src/routes/reviews.js": ROUTE}))
    route = next(u for u in found if u["file"] == "server/src/routes/reviews.js")
    assert route["kind"] == "route" and route["entry"] == "server/src/index.js"
    issue = uf.route_issues(found)[0]
    assert issue["code"] == "SERVER_ROUTE_NOT_MOUNTED" and issue["file"] == "server/src/index.js"
    assert "'./routes/reviews'" in issue["fix"] and "/api/reviews" in issue["fix"]
    assert not uf.wiring_issues([route]), "API 파일은 화면 연결 대상이 아니다"
    mounted = _ops(**{"server/src/routes/reviews.js": ROUTE,
                      "server/src/index.js": "import reviews from './routes/reviews.js';\n" + SERVER})
    assert not [u for u in uf.find(mounted) if u["file"].endswith("reviews.js")]


def test_파일_위치가_주소인_프레임워크는_판단하지_않는다():
    ops = [{"action": "create", "file": "web/package.json", "content": '{"dependencies":{"next":"14","react":"18"}}'},
           {"action": "create", "file": "web/pages/cart.tsx", "content": "export default function Cart(){return null}"},
           {"action": "create", "file": "web/app/orders/page.tsx", "content": "export default function P(){return null}"},
           {"action": "create", "file": "web/pages/api/orders.ts", "content": "export default function h(req,res){res.json([])}"},
           {"action": "create", "file": "web/lib/unusedHelper.ts", "content": "export const x = 1;"}]
    assert [u["file"] for u in uf.find(ops)] == ["web/lib/unusedHelper.ts"]


def test_생성_중_미등록_API_는_일관성_점검_오류로_남는다(tmp_path, monkeypatch):
    ops = [dict(o, language="javascript", rationale="r") for o in _ops(**{"server/src/routes/reviews.js": ROUTE})]

    class R:
        def call(self, req, *a, **k):
            return SimpleNamespace(text=json.dumps({"summary": "s", "ops": ops}), model_used="fake", provider="fake")
    monkeypatch.setattr(ca, "get_router", lambda: R())
    monkeypatch.setattr(ca, "_verify_generated_build", lambda *a, **k: {"kind": "docker-build", "status": "skipped", "passed": True, "output": ""})
    confirm = {"id": "__confirm__", "question": "q", "chosen_key": "proceed", "options": [{"key": "proceed", "label": "진행"}, {"key": "cancel", "label": "취소"}], "impact": ""}
    result = ca.generate_code("리뷰 API 만들어줘", decisions=[confirm], project_root=str(tmp_path))
    issue = next(i for i in result["consistency_issues"] if i["code"] == "SERVER_ROUTE_NOT_MOUNTED")
    assert issue["severity"] == "error" and issue["file"] == "server/src/index.js"
    assert all(u["file"] != "server/src/routes/reviews.js" for u in result["unused_files"]), "API 파일은 적용에서 빼지 않는다"
