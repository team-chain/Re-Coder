"""AI 결제 앱에 ReCoder 결제 모듈(고정 파일)을 넣고, AI 는 불러 쓰기만 한다(실기기 TEMP: AI 가 자기 방식 결제를 만들었다)."""
import json

import code_agent as ca
import deploy_settings as ds
import gen_engine as ge
import payment_contract as pc
import payment_kit as pk


def test_서버_폴더와_언어에_맞는_자리에_넣고_AI_모의_결제_서버는_뺀다():
    files = [{"file": f} for f in ("package.json", "backend/src/server.ts", "backend/src/routes/orders.ts", "frontend/src/App.tsx",
                                   "mock-payment-server/server.ts", "mock-payment-server/package.json", "backend/package.json")]
    new, ops, extra = pk.plan_with_kit(files)
    paths = [f["file"] for f in new]
    assert paths[0] == "backend/src/payments/stripe.ts" and new[0]["layer"] == 0 and new[0]["fixed"]
    assert not [p for p in paths if p.startswith("mock-payment-server/")]
    assert ops[0]["content"] == pk.TS_KIT and ops[0]["fixed"]
    assert "backend/src/payments/stripe.ts" in extra and "express.json() 보다 먼저" in extra
    assert pk.plan_with_kit([{"file": "server.js"}, {"file": "routes/orders.js"}])[1][0]["file"] == "payments/stripe.cjs"
    assert pk.plan_with_kit([{"file": "server/index.js"}, {"file": "client/src/App.jsx"}])[1][0]["file"] == "server/payments/stripe.cjs"
    # 서버가 없는 앱(정적 화면)은 그대로
    assert pk.plan_with_kit([{"file": "index.html"}]) == ([{"file": "index.html"}], [], "")


def test_결제_모듈은_데모_조건과_필수_설정을_만족한다(tmp_path):
    for name, body in (("server/payments/stripe.cjs", pk.CJS_KIT), ("api/src/payments/stripe.ts", pk.TS_KIT)):
        ws = tmp_path / name.split("/")[0]
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(body)
        (ws / name.split("/")[0] / "package.json").write_text("{}")
        assert ds.demo_supported(str(ws))
        assert "STRIPE_SECRET_KEY" in ds.required_env(str(ws))  # 실제 키로 띄울 때는 입력받는다(데모가 대신 채움)
    for body in (pk.TS_KIT, pk.CJS_KIT):
        assert "express.raw({ type: 'application/json' })" in body and "constructEvent(" in body
        assert "process.env.STRIPE_MOCK_PORT" in body and "/api/webhooks/stripe" in body


def test_서버가_모듈을_쓰지_않으면_고칠_파일을_지목한다():
    kit = {"file": "backend/src/payments/stripe.ts", "content": pk.TS_KIT, "fixed": True}
    own = {"file": "backend/src/utils/payment.ts", "content": "import crypto from 'crypto';\nexport const sign = () => crypto.createHmac('sha256', 'x').update('webhook');\n"}
    stripe_own = {"file": "backend/src/routes/payment.ts", "content": "import Stripe from 'stripe';\n"}
    app = {"file": "backend/src/app.ts", "content": "import express from 'express';\nconst app = express();\n"}
    found = {i["code"]: i for i in pc.issues([kit, own, stripe_own, app])}
    assert found["PAYMENT_WEBHOOK_MISSING"]["file"] == "backend/src/app.ts"
    assert found["PAYMENT_INTENT_MISSING"]["severity"] == "error"
    assert found["PAYMENT_DUPLICATE_STRIPE"]["file"] == "backend/src/routes/payment.ts"
    wired = dict(app, content="import express from 'express';\nimport { createStripeWebhookRouter, createPaymentIntent, STRIPE_WEBHOOK_PATH } "
                              "from './payments/stripe.js';\nconst app = express();\napp.use(STRIPE_WEBHOOK_PATH, createStripeWebhookRouter({ onPaymentSucceeded }));\n"
                              "app.post('/api/orders', async () => createPaymentIntent({ amount: 1, currency: 'krw', orderId: 1 }));\n")
    assert pc.issues([kit, wired]) == []


def test_고정_파일은_AI_교정이_덮어쓰지_못한다():
    base = [{"file": "server/payments/stripe.cjs", "content": pk.CJS_KIT, "fixed": True}, {"file": "server/index.js", "content": "a"}]
    merged = ca._merge_ops(base, [{"file": "server/payments/stripe.cjs", "content": "x"}, {"file": "server/index.js", "content": "b"}])
    assert merged[0]["content"] == pk.CJS_KIT and merged[1]["content"] == "b"
    assert ge.edit_fix_round("p", base, [{"severity": "error", "file": "server/payments/stripe.cjs", "message": "m", "fix": "f"}]) is None


def test_설계_직후_결제_모듈이_완성_파일로_들어가고_약속에_붙는다(monkeypatch):
    plan = {"summary": "쇼핑몰", "contracts": "약속", "more": False,
            "files": [{"file": "server/index.js", "purpose": "", "layer": 1}, {"file": "mock-payment-server/index.js", "purpose": "", "layer": 1}]}

    class Router:
        def call(self, request, agent=None, operation=None):
            assert operation == "generate_code_manifest"
            return type("R", (), {"text": json.dumps(plan), "model_used": "m", "provider": "p", "metadata": {}})()

    monkeypatch.setattr(ca, "get_router", lambda: Router())
    e = ge.LargeGeneration("결제 쇼핑몰", job_id="3f89c434c07345af", limiter=ge.RateLimiter(0), plan_hook=pk.plan_with_kit)
    e.plan()
    paths = [f["file"] for f in e.state["files"]]
    assert "server/payments/stripe.cjs" in paths and "mock-payment-server/index.js" not in paths
    assert e.state["ops"]["server/payments/stripe.cjs"]["content"] == pk.CJS_KIT
    assert "[결제 모듈" in e.state["manifest"]["contracts"]
