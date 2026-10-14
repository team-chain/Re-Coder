"""글자 그대로 받기(raw_text) — Bedrock·Gemini 도 JSON 을 뽑지 않고 끊겨도 받은 만큼 돌려준다."""
import asyncio

from llm.bedrock_provider import BedrockProvider


def test_bedrock_글자_그대로_받기():
    bp = BedrockProvider.__new__(BedrockProvider)
    bp._model_id = "m"
    seen = {}

    async def invoke(kwargs):
        seen.update(kwargs)
        return {"output": {"message": {"content": [{"text": '{"name": "shop",'}]}}, "stopReason": "max_tokens"}
    bp._invoke_sync = invoke
    out = asyncio.run(bp.converse([{"role": "user", "content": [{"text": "x"}]}], output_schema={"type": "object"},
                                  max_tokens=8192, raw=True))
    assert out == {"text": '{"name": "shop",', "truncated": True}
    assert "toolConfig" not in seen and seen["inferenceConfig"]["maxTokens"] == 8192
