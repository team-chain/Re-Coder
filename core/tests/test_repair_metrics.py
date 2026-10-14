import asyncio
import json

import pytest

from grounded_repair.benchmark import report
from llm import breaker, cost_ledger
from llm.base import LLMError, LLMRequest, LLMResponse
from llm.provider_router import LLMProviderRouter


class Provider:
    model_id = "test-small"
    provider_name = "anthropic"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def call(self, request):
        self.calls += 1
        assert request.json_schema is None
        assert "Return JSON matching" in request.prompt
        if self.fail:
            raise LLMError("Provider failed")
        return LLMResponse(text='{"explanation":"ok"}', model_used=self.model_id,
                           provider=self.provider_name, input_tokens=1000, output_tokens=200,
                           token_source="api")


@pytest.fixture(autouse=True)
def reset():
    breaker.reset_all()
    cost_ledger.reset()
    yield
    breaker.reset_all()
    cost_ledger.reset()


def router(provider):
    r = LLMProviderRouter.__new__(LLMProviderRouter)
    r._bedrock_haiku = provider
    r._bedrock_sonnet = Provider()
    r._gemini = Provider()
    return r


def test_actual_usage_and_configured_price_reach_shared_ledger(monkeypatch):
    monkeypatch.setenv("RECODER_REPAIR_PRICES", json.dumps({"test-small": {"input": 1, "output": 5}}))
    r = router(Provider())
    response = asyncio.run(r.call_repair(LLMRequest("data", json_schema={}), tier="fast", run_id="run"))
    record = response.metadata["llm_call_record"]
    assert record["input_tokens"] == 1000 and record["token_source"] == "api"
    assert record["estimated_cost_usd"] == pytest.approx(.002)
    assert len(cost_ledger.all_records()) == 1
    assert cost_ledger.all_records()[0]["call_id"] == record["call_id"]
    assert r._bedrock_sonnet.calls == 0 and r._gemini.calls == 0


def test_missing_price_is_unknown_not_free(monkeypatch):
    monkeypatch.delenv("RECODER_REPAIR_PRICES", raising=False)
    response = asyncio.run(router(Provider()).call_repair(LLMRequest("data"), tier="fast", run_id="run"))
    assert response.metadata["llm_call_record"]["estimated_cost_usd"] is None


def test_provider_error_is_recorded_without_hidden_fallback():
    r = router(Provider(fail=True))
    with pytest.raises(LLMError) as caught:
        asyncio.run(r.call_repair(LLMRequest("data"), tier="fast", run_id="run"))
    assert caught.value.llm_call_record["status"] == "failed"
    assert cost_ledger.all_records()[0]["estimated_cost_usd"] is None
    assert r._gemini.calls == 0 and r._bedrock_sonnet.calls == 0


def test_report_includes_failed_task_cost_and_excludes_rule_successes():
    base = {"strategy": "D", "route": "documents", "llm_calls": 2, "elapsed_ms": 100,
            "cost_complete": True, "estimated_cost_usd": .2, "retrieval_mode": "hybrid"}
    rows = [{**base, "status": "ready_for_approval"}, {**base, "status": "unresolved"},
            {**base, "route": "rules", "status": "ready_for_approval"}]
    stats = report(rows)["D"]
    assert stats["solve_rate"] == .5
    assert stats["estimated_cost_per_solved_usd"] == .4
    assert stats["rules_tasks"] == 1
    assert stats["citation_accuracy"] is None
    rows[1]["cost_complete"] = False
    assert report(rows)["D"]["estimated_cost_per_solved_usd"] is None
