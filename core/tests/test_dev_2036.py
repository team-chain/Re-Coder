"""2.0.36 작업별 AI 공급자 — 등급 라우팅·OpenAI 호환 공급자·비밀값 가리기·비용 원장·공급자 비교.

외부 API 는 부르지 않는다. OpenAI 호환 서버는 127.0.0.1 의 가짜 HTTP 서버(내 PC 라 http 허용),
품질 공급자는 converse 만 있는 가짜다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import model_compare  # noqa: E402
from llm import api_key_provider as akp  # noqa: E402
from llm import breaker as breaker_mod  # noqa: E402
from llm import cost_ledger, pricing, task_tier  # noqa: E402
from llm import provider_router as pr_mod  # noqa: E402
from llm import router as router_mod  # noqa: E402
from llm.base import LLMError, LLMRequest  # noqa: E402
from llm.scrub import scrub_for_external  # noqa: E402

KEY = "sk-compat-fixture-key-000000"
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


class _Server:
    def __init__(self, responder):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": body})
                status, payload = responder(body, len(outer.requests))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def text(self, i: int = -1) -> str:
        return "\n".join(str(m.get("content")) for m in self.requests[i]["body"]["messages"])

    def close(self):
        self.httpd.shutdown()


def _ok(content="{\"answer\": \"ok\"}", tokens=(120, 30)):
    return 200, {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
                 "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


class _Fake:
    def __init__(self, name="bedrock", model="sonnet-x", fail=False):
        self._name, self.model_id, self.fail = name, model, fail
        self.prompts: list[str] = []

    @property
    def provider_name(self):
        return self._name

    async def converse(self, messages, system=None, output_schema=None, *, max_tokens=4096, temperature=0.0, raw=False):
        self.prompts.append(messages[0]["content"][0]["text"])
        if self.fail:
            raise RuntimeError("quality down")
        return {"answer": "quality"}


class _Gemini:
    available = True

    def __init__(self):
        self.calls = 0

    async def generate(self, prompt, schema=None, **_):
        self.calls += 1
        return {"answer": "gemini"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("RECODER_AI_PROVIDER", "RECODER_ECONOMY_PROVIDER", "RECODER_ECONOMY_MODEL", "RECODER_MODEL_PRICES",
                 "RECODER_COMPATIBLE_API_KEY", "RECODER_COMPATIBLE_BASE_URL", "RECODER_COMPATIBLE_MODEL",
                 "RECODER_ANTHROPIC_API_KEY", "RECODER_OPENAI_API_KEY", "RECODER_LLM_GATEWAY_URL", "RECODER_STUDENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECODER_COST_LEDGER_FILE", "")
    breaker_mod.reset_all()
    cost_ledger.reset()
    model_compare.reset()
    router_mod._router_instance = None
    pr_mod._singleton = None
    yield
    breaker_mod.reset_all()
    cost_ledger.reset()
    model_compare.reset()
    router_mod._router_instance = None
    pr_mod._singleton = None


def _compatible(monkeypatch, server, economy=True, model=""):
    monkeypatch.setenv("RECODER_COMPATIBLE_API_KEY", KEY)
    monkeypatch.setenv("RECODER_COMPATIBLE_BASE_URL", server.url)
    if model:
        monkeypatch.setenv("RECODER_COMPATIBLE_MODEL", model)
    if economy:
        monkeypatch.setenv("RECODER_ECONOMY_PROVIDER", "compatible")


def _router(quality=None, fast=None, gemini=None):
    r = pr_mod.get_provider_router(force_rebuild=True)
    r._bedrock_sonnet = quality or _Fake()
    r._bedrock_haiku = fast or _Fake(model="haiku-x")
    r._gemini = gemini or _Gemini()
    router_mod._router_instance = None
    return r


# ── 등급 규칙 ────────────────────────────────────────────────────────


def test_unknown_operations_stay_on_quality():
    assert task_tier.tier_for("generate_plan_topics") == "economy"
    assert task_tier.tier_for("generate_test_draft") == "economy"
    for op in ("generate_code", "generate_code_fix", "generate_patch", "repair_primary", "", "brand_new_operation"):
        assert task_tier.tier_for(op) == "quality", op


@pytest.mark.parametrize("path,expected", [
    ("tests/test_api.py", True), ("src/__tests__/App.jsx", True), ("src/app.test.ts", True),
    ("web/Button.spec.tsx", True), ("pkg/handler_test.go", True), ("test_models.py", True),
    ("src/testing.ts", False), ("src/latest.js", False), ("spec/openapi.yaml", False), ("src/app.ts", False), ("", False),
])
def test_test_draft_paths(path, expected):
    assert task_tier.is_test_file(path) is expected


# ── OpenAI 호환 공급자 ───────────────────────────────────────────────


def test_compatible_endpoint_is_https_or_local_only():
    assert akp.compatible_endpoint("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert akp.compatible_endpoint("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/chat/completions"
    assert akp.compatible_endpoint("http://localhost:11434/v1") == "http://localhost:11434/v1/chat/completions"
    for bad in ("", "http://api.deepseek.com", "ftp://x.y", "https://user:pw@api.x.com", "https://api.x.com/v1?key=1",
                "api.deepseek.com"):
        with pytest.raises(ValueError):
            akp.compatible_endpoint(bad)


def test_compatible_needs_key_and_valid_address(monkeypatch):
    monkeypatch.setenv("RECODER_AI_PROVIDER", "compatible")
    monkeypatch.setenv("RECODER_COMPATIBLE_API_KEY", KEY)
    assert akp.selected_provider() == "", "주소가 없으면 쓰지 않는다"
    monkeypatch.setenv("RECODER_COMPATIBLE_BASE_URL", "http://example.com/v1")
    assert akp.selected_provider() == "", "원격 http 는 거절"
    monkeypatch.setenv("RECODER_COMPATIBLE_BASE_URL", "https://api.deepseek.com")
    assert akp.selected_provider() == "compatible"
    assert akp.model_for("compatible") == "deepseek-chat"


def test_compatible_retries_in_json_mode_when_tools_are_refused(monkeypatch):
    def respond(body, n):
        if "tools" in body:
            return 400, {"error": {"message": "tool_choice is not supported", "type": "invalid_request_error"}}
        return _ok("```json\n{\"answer\": \"42\"}\n```", (11, 4))
    server = _Server(respond)
    try:
        _compatible(monkeypatch, server, economy=False)
        provider = akp.ApiKeyProvider("compatible")
        usage: dict = {}
        out = asyncio.run(provider.converse([{"role": "user", "content": [{"text": "q"}]}], output_schema=SCHEMA,
                                            max_tokens=300, usage=usage))
        assert out == {"answer": "42"} and usage == {"input_tokens": 11, "output_tokens": 4}
        first, second = server.requests[0], server.requests[1]
        assert first["path"] == "/v1/chat/completions"
        assert first["headers"]["authorization"] == f"Bearer {KEY}"
        assert first["body"]["max_tokens"] == 300 and "max_completion_tokens" not in first["body"]
        assert "reasoning_effort" not in first["body"]
        assert "tools" not in second["body"] and second["body"]["response_format"] == {"type": "json_object"}
        assert "\"answer\"" in second["body"]["messages"][-1]["content"], "스키마를 메시지에 붙인다"
    finally:
        server.close()


def test_compatible_key_never_in_error_message(monkeypatch):
    server = _Server(lambda body, n: (401, {"error": {"message": f"bad key {KEY}"}}))
    try:
        _compatible(monkeypatch, server, economy=False)
        with pytest.raises(LLMError) as err:
            akp.ApiKeyProvider("compatible").call(LLMRequest(prompt="q"))
        assert KEY not in str(err.value) and "OpenAI 호환 API" in str(err.value)
    finally:
        server.close()


# ── 비밀값 가리기 ────────────────────────────────────────────────────


def test_scrub_masks_values_but_keeps_code():
    text = (f"DB_PASSWORD=hunter2-very-secret\nexport API_TOKEN='abc def'\nkey={AWS_KEY}\n"
            "API_KEY=${API_KEY}\nconst t = process.env.TOKEN\npassword = req.body.password\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
            "SECRET_KEY = \"[MASKED_AI_KEY_1]\"\n")
    out, n = scrub_for_external(text)
    for secret in ("hunter2-very-secret", "abc def", AWS_KEY, "abcdefghijklmnopqrstuvwxyz012345"):
        assert secret not in out
    for kept in ("API_KEY=${API_KEY}", "process.env.TOKEN", "password = req.body.password", "[MASKED_AI_KEY_1]"):
        assert kept in out
    assert n >= 4
    code, m = scrub_for_external("SECRET_KEY = \"dev-only\"\nkey = '" + AWS_KEY + "'", code=True)
    assert "dev-only" in code and AWS_KEY not in code and m == 1, "코드 결과 작업은 확실한 키만 가린다"


def test_scrub_is_linear_on_hostile_input():
    for text in ("TOKEN" * 20000, " " * 60000, "A" * 60000 + "=", "export " * 10000, "Bearer " * 10000):
        started = time.monotonic()
        scrub_for_external(text)
        assert time.monotonic() - started < 1.0


# ── 등급 라우팅 ─────────────────────────────────────────────────────


def test_without_economy_setting_nothing_changes():
    quality = _Fake()
    _router(quality=quality)
    resp = router_mod.get_router().call(LLMRequest(prompt=f"x {AWS_KEY}", json_schema=SCHEMA),
                                        agent="code_agent", operation="generate_plan_topics")
    assert resp.provider == "bedrock" and AWS_KEY in quality.prompts[0], "품질 공급자 그대로 — 가리지 않는다(기존 동작)"
    rec = cost_ledger.all_records()[-1]
    assert rec["tier"] == "economy" and rec["provider"] == "bedrock" and rec["masked"] == 0


def test_economy_work_goes_to_economy_provider_masked(monkeypatch):
    server = _Server(lambda body, n: _ok(tokens=(1000, 200)))
    try:
        _compatible(monkeypatch, server)
        quality = _Fake()
        _router(quality=quality)
        resp = router_mod.get_router().call(
            LLMRequest(prompt=f"주제를 뽑아 주세요\nDB_PASSWORD=hunter2-very-secret\n{AWS_KEY}", json_schema=SCHEMA),
            agent="code_agent", operation="generate_plan_topics")
        assert resp.provider == "compatible" and resp.model_used == "deepseek-chat" and not quality.prompts
        sent = server.text()
        assert "hunter2-very-secret" not in sent and AWS_KEY not in sent and "주제를 뽑아" in sent
        rec = cost_ledger.all_records()[-1]
        assert (rec["tier"], rec["provider"], rec["token_source"]) == ("economy", "compatible", "api")
        assert (rec["input_tokens"], rec["output_tokens"]) == (1000, 200) and rec["masked"] >= 2
        assert rec["estimated_cost_usd"] is None and rec["pricing_source"] == "unconfigured", "모르는 단가는 0 이 아니다"

        # 코드 생성(품질)은 원래 공급자 — 원래 글 그대로
        resp = router_mod.get_router().call(LLMRequest(prompt=f"코드 {AWS_KEY}", json_schema=SCHEMA),
                                            agent="code_agent", operation="generate_code")
        assert resp.provider == "bedrock" and AWS_KEY in quality.prompts[-1] and len(server.requests) == 1
        assert cost_ledger.all_records()[-1]["tier"] == "quality"
    finally:
        server.close()


def test_configured_prices_give_cost_and_savings(monkeypatch):
    server = _Server(lambda body, n: _ok(tokens=(1_000_000, 100_000)))
    try:
        _compatible(monkeypatch, server)
        monkeypatch.setenv("RECODER_MODEL_PRICES", json.dumps({"deepseek-chat": {"input": 0.5, "output": 1.0},
                                                                 "bad": {"input": -1, "output": 1}, "x": "nope"}))
        assert set(pricing.configured_prices()) == {"deepseek-chat"}
        _router()
        router_mod.get_router().call(LLMRequest(prompt="요약", json_schema=SCHEMA), operation="summarize_scan")
        rec = cost_ledger.all_records()[-1]
        assert rec["estimated_cost_usd"] == pytest.approx(0.6) and rec["pricing_source"] == "configured"
        assert rec["quality_equivalent_usd"] == pytest.approx(3.0 + 1.5), "품질 모델(Sonnet 어림)로 했으면 들었을 비용"
        s = cost_ledger.summarize(cost_ledger.all_records())
        assert s["economy_saved_usd"] == pytest.approx(3.9) and s["unknown_cost_calls"] == 0
        assert s["groups"][0]["tier"] == "economy" and s["groups"][0]["provider"] == "compatible"
    finally:
        server.close()


def test_economy_failure_falls_back_to_quality(monkeypatch):
    server = _Server(lambda body, n: (500, {"error": {"message": "overloaded"}}))
    try:
        _compatible(monkeypatch, server)
        quality = _Fake()
        _router(quality=quality)
        resp = router_mod.get_router().call(LLMRequest(prompt="분류", json_schema=SCHEMA), operation="classify_error")
        assert resp.provider == "bedrock" and resp.fallback_used and len(quality.prompts) == 1
        assert cost_ledger.all_records()[-1]["fallback_used"] is True
    finally:
        server.close()


def test_missing_economy_key_keeps_quality_and_says_why(monkeypatch):
    monkeypatch.setenv("RECODER_ECONOMY_PROVIDER", "openai")
    r = _router()
    status = r.routing_status()
    assert status["economy"]["separate"] is False and "키" in status["economy"]["note"]
    monkeypatch.setenv("RECODER_ECONOMY_PROVIDER", "nonsense")
    assert "알 수 없는" in _router().routing_status()["economy"]["note"]


def test_fast_economy_uses_quality_vendor_without_masking(monkeypatch):
    monkeypatch.setenv("RECODER_ECONOMY_PROVIDER", "fast")
    fast = _Fake(model="haiku-x")
    r = pr_mod.get_provider_router(force_rebuild=True)
    r._bedrock_sonnet, r._bedrock_haiku, r._gemini = _Fake(), fast, _Gemini()
    r._economy = r._build_economy()
    router_mod._router_instance = None
    router_mod.get_router().call(LLMRequest(prompt=f"x {AWS_KEY}"), operation="summarize_request")
    assert fast.prompts and AWS_KEY in fast.prompts[0], "같은 공급자의 빠른 모델 — 나가는 곳이 같아 가리지 않는다"


def test_complete_routes_economy_operations(monkeypatch):
    server = _Server(lambda body, n: _ok("{\"answer\": \"cls\"}"))
    try:
        _compatible(monkeypatch, server)
        quality = _Fake()
        r = _router(quality=quality)
        out = asyncio.run(r.complete("분류해 주세요", agent="code_agent", operation="classify_error", schema=SCHEMA))
        assert json.loads(out) == {"answer": "cls"} and not quality.prompts
        asyncio.run(r.complete("Dockerfile", agent="infra", operation="dockerfile_customise"))
        assert len(quality.prompts) == 1
    finally:
        server.close()


# ── 공급자 고정(비교 도구) ──────────────────────────────────────────


def test_pinned_provider_never_switches(monkeypatch):
    server = _Server(lambda body, n: (500, {"error": {"message": "down"}}))
    try:
        _compatible(monkeypatch, server, economy=False)
        quality, gemini = _Fake(), _Gemini()
        _router(quality=quality, gemini=gemini)
        with task_tier.pinned("compatible", "run-1"):
            with pytest.raises(LLMError):
                router_mod.get_router().call(LLMRequest(prompt="x", json_schema=SCHEMA), operation="generate_code")
        assert not quality.prompts and gemini.calls == 0
        with task_tier.pinned("openai", "run-2"):
            with pytest.raises(LLMError) as err:
                router_mod.get_router().call(LLMRequest(prompt="x"), operation="generate_code")
        assert "키" in str(err.value)
    finally:
        server.close()


def test_pin_reaches_worker_threads_and_tags_records():
    fast = _Fake(model="haiku-x")
    _router(fast=fast)

    def one(i):
        return router_mod.get_router().call(LLMRequest(prompt=f"p{i}"), operation="generate_code").model_used

    with task_tier.pinned("fast", "run-x"):
        with ThreadPoolExecutor(max_workers=3) as pool:
            used = list(pool.map(task_tier.bind(one), range(6)))
    assert used == ["haiku-x"] * 6
    assert [r["run"] for r in cost_ledger.all_records()] == ["run-x"] * 6
    # 고정 밖의 호출은 표시가 없다
    router_mod.get_router().call(LLMRequest(prompt="after"), operation="generate_code")
    assert "run" not in cost_ledger.all_records()[-1] and task_tier.forced_provider() is None


# ── 비용 원장 ───────────────────────────────────────────────────────


def test_ledger_persists_only_known_fields_and_prunes(tmp_path, monkeypatch):
    path = tmp_path / "costs.jsonl"
    monkeypatch.setenv("RECODER_COST_LEDGER_FILE", str(path))
    now = time.time()
    cost_ledger.record({"ts": now - 40 * 86400, "tier": "quality", "provider": "bedrock", "model": "m",
                        "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 9.0})
    cost_ledger.record({"tier": "economy", "provider": "compatible", "model": "deepseek-chat", "input_tokens": 10,
                        "output_tokens": 5, "estimated_cost_usd": None, "prompt": "SECRET PROMPT BODY",
                        "token_source": "api"})
    cost_ledger.record({"tier": "quality", "provider": "bedrock", "model": "m", "input_tokens": 100,
                        "output_tokens": 50, "estimated_cost_usd": 0.25})
    text = path.read_text(encoding="utf-8")
    assert "SECRET PROMPT BODY" not in text and "prompt" not in text
    s = cost_ledger.summary(30)
    assert s["calls"] == 2 and s["total_cost_usd"] == pytest.approx(0.25) and s["unknown_cost_calls"] == 1
    assert s["persisted"] is True
    assert cost_ledger.prune(now - 30 * 86400) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert cost_ledger.summary(30)["calls"] == 2, "깨진 줄은 건너뛴다"


def test_legacy_cost_endpoint_counts_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_COST_LEDGER_FILE", str(tmp_path / "c.jsonl"))
    cost_ledger.record({"tier": "quality", "provider": "bedrock", "model": "m", "estimated_cost_usd": 0.5})
    cost_ledger.record({"tier": "economy", "provider": "compatible", "model": "d", "estimated_cost_usd": None})
    from api.routes import session
    monkeypatch.setattr(session, "_LLM_CALLS_FILE", tmp_path / "none.jsonl")
    out = asyncio.run(session.get_cost_summary())
    assert out.daily_usd == pytest.approx(0.5) and out.monthly_usd == pytest.approx(0.5) and out.call_count == 2


# ── API ─────────────────────────────────────────────────────────────


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import models
    app = FastAPI()
    app.include_router(models.router)
    return TestClient(app)


def test_routing_endpoint_never_returns_keys(monkeypatch):
    server = _Server(lambda body, n: _ok())
    try:
        _compatible(monkeypatch, server)
        _router()
        client = _client()
        resp = client.get("/api/models/routing")
        assert resp.status_code == 200 and KEY not in resp.text
        body = resp.json()
        assert body["economy"]["provider"] == "compatible" and body["economy"]["scrubbed"] is True
        assert "generate_plan_topics" in body["economy_operations"]
        assert {c["spec"]: c["ready"] for c in body["choices"]}["compatible"] is True
        assert client.get("/api/models/costs?days=7").json()["days"] == 7
        assert client.get("/api/models/costs?days=0").status_code == 422
    finally:
        server.close()


# ── 공급자 비교 ─────────────────────────────────────────────────────


def _fake_generate(**kwargs):
    """실제 생성 경로처럼 라우터를 부른다 — 고정된 공급자에 따라 교정 횟수·결과가 다르다."""
    call = router_mod.get_router().call
    pinned = task_tier.forced_provider()
    call(LLMRequest(prompt="topics"), agent="code_agent", operation="generate_plan_topics")
    call(LLMRequest(prompt="code", json_schema=SCHEMA), agent="code_agent", operation="generate_code")
    if pinned == "compatible":
        call(LLMRequest(prompt="code again", json_schema=SCHEMA), agent="code_agent", operation="generate_code")
        call(LLMRequest(prompt="fix", json_schema=SCHEMA), agent="code_agent", operation="generate_code_consistency")
        content = "const q = \"SELECT * FROM users WHERE id = \" + req.query.id;\ndb.query(q);\n"
        issues = [{"severity": "error", "message": "x"}]
    else:
        content = "db.query(\"SELECT * FROM users WHERE id = $1\", [req.query.id]);\n"
        issues = []
    return {"ops": [{"action": "create", "file": "server.js", "content": content}], "consistency_issues": issues}


def test_compare_runs_the_same_request_on_both(monkeypatch, tmp_path):
    server = _Server(lambda body, n: _ok(tokens=(500, 100)))
    try:
        _compatible(monkeypatch, server, economy=False)
        quality = _Fake()
        _router(quality=quality)
        monkeypatch.setattr(model_compare, "_generate", _fake_generate)
        job = model_compare.start("할 일 앱", str(tmp_path), ["quality", "compatible"], runner=lambda fn: fn())
        assert job["status"] == "finished"
        a, b = job["sides"]
        assert (a["provider"], b["provider"]) == ("bedrock", "compatible")
        assert a["metrics"]["calls"] == 2 and b["metrics"]["calls"] == 4, "생성은 비교 모델, 교정은 항상 품질 모델"
        assert len(server.requests) == 3 and len(quality.prompts) == 3
        assert (a["metrics"]["repair_calls"], b["metrics"]["repair_calls"]) == (0, 2)
        assert (a["metrics"]["remaining_errors"], b["metrics"]["remaining_errors"]) == (0, 1)
        assert b["metrics"]["unknown_cost_calls"] == 3 and b["metrics"]["cost_usd"] is not None
        assert b["metrics"]["input_tokens"] >= 1500 and b["metrics"]["estimated_token_calls"] == 1
        assert len(b["metrics"]["used"]) == 2, "교정에 쓴 품질 모델도 사용량에 기록한다"
        assert a["metrics"]["security_score"] is not None
        v = job["verdict"]
        assert v["fewer_repairs"] == "quality" and v["fewer_errors"] == "quality"
        assert v["cheaper"] is None, "한쪽 단가를 모르면 싸다고 말하지 않는다"
        assert model_compare.latest()["id"] == job["id"]
        # 일반 호출에는 고정이 남지 않는다
        router_mod.get_router().call(LLMRequest(prompt="later"), operation="generate_code")
        assert len(server.requests) == 3
    finally:
        server.close()


def test_compare_reports_failures_without_switching(monkeypatch, tmp_path):
    server = _Server(lambda body, n: (500, {"error": {"message": "down"}}))
    try:
        _compatible(monkeypatch, server, economy=False)
        quality = _Fake()
        _router(quality=quality)
        monkeypatch.setattr(model_compare, "_generate", _fake_generate)
        job = model_compare.start("앱", "", ["compatible", "quality"], runner=lambda fn: fn())
        bad, good = job["sides"]
        assert bad["status"] == "failed" and bad["error"] and good["status"] == "succeeded"
        assert len(quality.prompts) == 2, "실패한 쪽이 품질 공급자로 넘어가지 않았다"
        assert job["verdict"]["cheaper"] is None
    finally:
        server.close()


def test_compare_validates_input(monkeypatch, tmp_path):
    _router()
    with pytest.raises(model_compare.CompareError):
        model_compare.start("", "", ["quality", "fast"])
    with pytest.raises(model_compare.CompareError):
        model_compare.start("x" * (model_compare.MAX_INSTRUCTION + 1), "", ["quality", "fast"])
    with pytest.raises(model_compare.CompareError):
        model_compare.start("앱", "", ["quality"])
    with pytest.raises(model_compare.CompareError):
        model_compare.start("앱", "", ["quality", "rm -rf"])
    with pytest.raises(model_compare.CompareError, match="같은"):
        model_compare.start("앱", "", ["quality", "economy"])
    with pytest.raises(model_compare.CompareError, match="키"):
        model_compare.start("앱", "", ["quality", "openai"])
    with pytest.raises(model_compare.CompareError, match="폴더"):
        model_compare.start("앱", str(tmp_path / "missing"), ["quality", "fast"])
    with pytest.raises(model_compare.CompareError, match="폴더"):
        model_compare.start("앱", "relative/path", ["quality", "fast"])


def test_compare_runs_one_at_a_time(monkeypatch):
    _router()
    gate = threading.Event()

    def slow(**kwargs):
        gate.wait(5)
        return {"ops": [], "consistency_issues": []}
    monkeypatch.setattr(model_compare, "_generate", slow)
    client = _client()
    first = client.post("/api/models/compare", json={"instruction": "앱", "providers": ["quality", "fast"]})
    assert first.status_code == 200, first.text
    busy = client.post("/api/models/compare", json={"instruction": "앱", "providers": ["quality", "fast"]})
    assert busy.status_code == 409
    assert client.post("/api/models/compare", json={"instruction": "", "providers": ["quality", "fast"]}).status_code == 400
    gate.set()
    job_id = first.json()["id"]
    for _ in range(100):
        state = client.get(f"/api/models/compare/{job_id}").json()
        if state["status"] == "finished":
            break
        time.sleep(0.05)
    assert state["status"] == "finished" and [s["status"] for s in state["sides"]] == ["succeeded", "succeeded"]
    assert client.get("/api/models/compare/nope").status_code == 404
    assert client.get("/api/models/compare/latest").json()["job"]["id"] == job_id
