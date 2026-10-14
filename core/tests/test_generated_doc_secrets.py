"""생성 직후 검사와 배포 보안 검사가 같은 기준 — README 의 예시 키는 자리표시로, 코드의 키는 남은 문제로."""
import json
from types import SimpleNamespace

import code_agent as ca
from security_scan import scan_text_for_secrets

KEY = "sk_test_51HxYzAbCdEfGhIjKlMnOpQrSt"


def _gen(tmp_path, monkeypatch, ops):
    class R:
        def call(self, req, *a, **k):
            return SimpleNamespace(text=json.dumps({"summary": "s", "ops": ops}), model_used="fake", provider="fake")
    monkeypatch.setattr(ca, "get_router", lambda: R())
    monkeypatch.setattr(ca, "_verify_generated_build", lambda *a, **k: {"kind": "docker-build", "status": "skipped", "passed": True, "output": ""})
    confirm = {"id": "__confirm__", "question": "q", "chosen_key": "proceed", "options": [{"key": "proceed", "label": "진행"}, {"key": "cancel", "label": "취소"}], "impact": ""}
    return ca.generate_code("간단한 서버 만들어줘", decisions=[confirm], project_root=str(tmp_path))


def test_배포_보안_검사와_같은_모양의_결제_키를_잡는다():
    assert [w["rule"] for w in scan_text_for_secrets(f"STRIPE_SECRET_KEY={KEY}\n")] == ["stripe_secret_key"]
    assert scan_text_for_secrets("STRIPE_SECRET_KEY=sk_test_your_key\n") == []


def test_README_와_env_example_의_예시_키는_자리표시로_바뀐다(tmp_path, monkeypatch):
    ops = [
        {"action": "create", "file": "server.js", "language": "javascript", "content": "const k = process.env.STRIPE_SECRET_KEY;\n", "rationale": "r"},
        {"action": "create", "file": "README.md", "language": "markdown", "content": f"# App\n\nSTRIPE_SECRET_KEY={KEY}\n", "rationale": "r"},
        {"action": "create", "file": ".env.example", "language": "text", "content": f"STRIPE_SECRET_KEY={KEY}\nJWT_SECRET=Zx9vQ2mP7rT4wY8kL3nB\n", "rationale": "r"},
    ]
    result = _gen(tmp_path, monkeypatch, ops)
    files = {o["file"]: o for o in result["ops"]}
    assert KEY not in files["README.md"]["content"] and "<STRIPE_SECRET_KEY>" in files["README.md"]["content"]
    assert files[".env.example"]["content"] == "STRIPE_SECRET_KEY=<STRIPE_SECRET_KEY>\nJWT_SECRET=<JWT_SECRET>\n"
    assert not files["README.md"]["secret_warnings"]
    assert not [i for i in result["consistency_issues"] if i["code"] == "GENERATED_SECRET_IN_FILE"]


def test_코드에_남은_키는_적용_전에_남은_문제로_보인다(tmp_path, monkeypatch):
    ops = [{"action": "create", "file": "server.js", "language": "javascript", "content": f"const stripe = require('stripe')('{KEY}');\n", "rationale": "r"}]
    result = _gen(tmp_path, monkeypatch, ops)
    issue = next(i for i in result["consistency_issues"] if i["code"] == "GENERATED_SECRET_IN_FILE")
    assert issue["severity"] == "error" and issue["file"] == "server.js" and KEY not in issue["message"]
