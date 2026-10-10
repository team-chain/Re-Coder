"""AI 가 처음부터 만드는 앱도 결제 시작 방식(모의 결제 / 실제 키)을 묻고, 고른 대로 배포까지 이어진다."""
import json
from types import SimpleNamespace

import pytest

import code_agent as ca
import deploy_settings as ds
import payment_contract as pc


def _plan_router(decisions, seen=None):
    class R:
        def call(self, req, *a, **k):
            if seen is not None:
                seen.append(req.prompt)
            return SimpleNamespace(text=json.dumps({"decisions": decisions}), model_used="fake", provider="fake")
    return R()


def _decision(i, question="데이터 저장 방식", labels=("PostgreSQL", "SQLite")):
    return {"id": f"d{i}", "question": question, "impact": "x",
            "options": [{"key": f"k{j}", "label": l, "summary": "s", "pros": [], "cons": [], "recommended": j == 0}
                        for j, l in enumerate(labels)]}


def test_결제가_들어가는_요청만_결제_시작_방식을_묻는다():
    assert pc.applies("결제 되는 강의 사이트 만들어줘")
    assert pc.applies("쇼핑몰 만들어줘")
    assert not pc.applies("할 일 관리 앱 만들어줘")
    #: 다른 결제사를 콕 집으면 Stripe 기준 모의 결제를 강요하지 않는다
    assert not pc.applies("토스페이먼츠로 결제되는 쇼핑몰")


def test_AI_자유_생성_뒤에도_결제_시작_방식을_마지막에_묻는다(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ca, "get_router", lambda: _plan_router([_decision(1), _decision(2, "로그인 방식", ("JWT", "세션"))], seen))
    plan = ca.generate_plan("실제 운영 가능한 쇼핑몰을 만들어줘", project_root=str(tmp_path), after_starter="custom")
    ids = [d["id"] for d in plan["decisions"]]
    assert ids[-1] == pc.PAYMENT_ID and ids[:2] == ["d1", "d2"]
    assert {o["key"] for o in plan["decisions"][-1]["options"]} == {"mock", "keys"}
    assert "결제 시작 방식을 따로 묻습니다" in seen[0]


def test_AI_가_스스로_만든_결제_모드_결정은_겹치지_않게_뺀다(tmp_path, monkeypatch):
    dup = _decision(3, "결제는 어떻게 시작할까요", ("모의 결제", "실제 키"))
    monkeypatch.setattr(ca, "get_router", lambda: _plan_router([_decision(1), dup]))
    plan = ca.generate_plan("결제 되는 예약 사이트 만들어줘", project_root=str(tmp_path))
    assert [d["id"] for d in plan["decisions"]] == ["d1", pc.PAYMENT_ID]


def test_결정이_상한이면_결제_카드_자리를_만든다(tmp_path, monkeypatch):
    from adr import MAX_DECISIONS
    monkeypatch.setattr(ca, "get_router", lambda: _plan_router([_decision(i, f"질문 {i}") for i in range(MAX_DECISIONS)]))
    plan = ca.generate_plan("결제 되는 사이트", project_root=str(tmp_path))
    assert len(plan["decisions"]) == MAX_DECISIONS and plan["decisions"][-1]["id"] == pc.PAYMENT_ID


def test_기존_프로젝트나_결제_없는_요청은_묻지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "get_router", lambda: _plan_router([_decision(1)]))
    assert [d["id"] for d in ca.generate_plan("할 일 앱", project_root=str(tmp_path))["decisions"]] == ["d1"]
    (tmp_path / "index.js").write_text("x")
    assert [d["id"] for d in ca.generate_plan("결제 붙여줘", project_root=str(tmp_path))["decisions"]] == ["d1"]


GOOD_PAYMENT = """import Stripe from 'stripe';
const key = process.env.STRIPE_SECRET_KEY;
const mode = process.env.PAYMENT_MODE;
const host = process.env.STRIPE_MOCK_HOST;
const port = process.env.STRIPE_MOCK_PORT;
export const stripe = new Stripe(key, mode === 'mock' ? { host, port: parseInt(port, 10), protocol: 'http' } : {});
export function verify(raw, sig) { return stripe.webhooks.constructEvent(raw, sig, process.env.STRIPE_WEBHOOK_SECRET); }
"""
GOOD_SERVER = """import express from 'express';
const app = express();
app.post('/api/webhooks/stripe', express.raw({ type: 'application/json' }), handler);
app.listen(process.env.PORT || 3001);
"""


def test_약속을_지킨_결과는_문제가_없다():
    ops = [{"file": "server/src/payment.js", "content": GOOD_PAYMENT}, {"file": "server/src/index.js", "content": GOOD_SERVER},
           {"file": "client/src/pages/Checkout.jsx", "content": "export default () => null"}]
    assert pc.issues(ops) == []


def test_약속이_빠지면_고칠_파일을_지목한_오류가_된다():
    ops = [{"file": "server/src/payment.js", "content": "import Stripe from 'stripe'; export default new Stripe(process.env.STRIPE_SECRET_KEY);"},
           {"file": "server/src/index.js", "content": "import express from 'express'; const app = express();\n"
                                                     "if (process.env.NODE_ENV !== 'test') {\n  app.listen(3001);\n}"}]
    found = {i["code"]: i for i in pc.issues(ops)}
    assert found["PAYMENT_MOCK_CONTRACT_MISSING"]["file"] == "server/src/payment.js"
    assert found["PAYMENT_WEBHOOK_MISSING"]["file"] == "server/src/index.js"
    assert found["PAYMENT_DEMO_NO_LISTEN"]["file"] == "server/src/index.js"
    assert all(i["severity"] == "error" for i in found.values())


def test_코드_생성은_약속을_지시하고_빠지면_일관성_점검으로_고친다(tmp_path, monkeypatch):
    pay = pc.payment_decision(); pay["chosen_key"] = "mock"
    prompts = []
    first = {"summary": "s", "ops": [
        {"action": "create", "file": "server/package.json", "language": "json", "content": '{"name":"s","type":"module","dependencies":{"express":"4","stripe":"14"}}', "rationale": "r"},
        {"action": "create", "file": "server/src/payment.js", "language": "javascript",
         "content": "import Stripe from 'stripe';\nexport default new Stripe(process.env.STRIPE_SECRET_KEY);\n", "rationale": "r"},
        {"action": "create", "file": "server/src/index.js", "language": "javascript", "content": GOOD_SERVER.replace("handler", "(q, s) => s.end()"), "rationale": "r"},
    ]}
    fixed = {"summary": "s", "ops": [{"action": "create", "file": "server/src/payment.js", "language": "javascript", "content": GOOD_PAYMENT, "rationale": "r"}]}

    class R:
        def call(self, req, *a, **k):
            prompts.append(req.prompt)
            body = first if len(prompts) == 1 else fixed
            return SimpleNamespace(text=json.dumps(body), model_used="fake", provider="fake")

    monkeypatch.setattr(ca, "get_router", lambda: R())
    monkeypatch.setattr(ca, "_verify_generated_build", lambda *a, **k: {"kind": "docker-build", "status": "skipped", "passed": True, "output": ""})
    result = ca.generate_code("결제 되는 사이트 만들어줘", decisions=[pay], project_root=str(tmp_path))
    assert "[결제 약속" in prompts[0] and "STRIPE_MOCK_HOST" in prompts[0]
    assert len(prompts) >= 2 and "모의 결제 모드가 빠졌습니다" in prompts[1]
    files = {o["file"]: o["content"] for o in result["ops"]}
    assert "process.env.STRIPE_MOCK_HOST" in files["server/src/payment.js"]
    assert not [i for i in result["consistency_issues"] if i["code"].startswith("PAYMENT_")]
    assert ds.payment_choice_for(str(tmp_path))["payment"] == "mock"


def test_AI_앱도_서버가_하위_폴더면_데모를_지원한다(tmp_path):
    ws = tmp_path / "ws"; (ws / "server" / "src").mkdir(parents=True)
    (ws / "server" / "package.json").write_text("{}")
    (ws / "server" / "src" / "payment.js").write_text(GOOD_PAYMENT)
    assert ds.demo_supported(str(ws))


def test_데모를_못_쓰면_이유와_방법을_알려_준다(tmp_path):
    ws = tmp_path / "ws"; (ws / "server").mkdir(parents=True)
    (ws / "server" / "package.json").write_text("{}")
    (ws / "server" / "pay.js").write_text("if (!process.env.STRIPE_SECRET_KEY) throw new Error('x');\n")
    state = ds.evaluate("ws-nodemo", str(ws), set())
    assert state["demo"]["available"] is False
    assert "모의 결제 모드가 없어" in state["demo"]["unavailable_reason"]
