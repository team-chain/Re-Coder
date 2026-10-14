"""게이트웨이는 Bedrock 의 stopReason 을 그대로 넘겨야 코어가 잘린 응답을 나눠서 다시 만든다."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import common  # noqa: E402


class _Client:
    def __init__(self, reason):
        self.reason = reason

    def converse(self, **kwargs):
        return {"output": {"message": {"content": [{"text": '{"ops": ['}]}},
                "usage": {"inputTokens": 10, "outputTokens": kwargs["inferenceConfig"]["maxTokens"]},
                "stopReason": self.reason}


def test_stop_reason_is_returned(monkeypatch):
    monkeypatch.setattr(common, "_bedrock_client", lambda: _Client("max_tokens"))
    out = common.invoke_bedrock([{"role": "user", "content": [{"text": "x"}]}], max_tokens=4096)
    assert out["stop_reason"] == "max_tokens"
    assert out["output_tokens"] == 4096


def test_missing_stop_reason_is_empty_string(monkeypatch):
    monkeypatch.setattr(common, "_bedrock_client", lambda: _Client(None))
    out = common.invoke_bedrock([{"role": "user", "content": [{"text": "x"}]}], max_tokens=10)
    assert out["stop_reason"] == ""
