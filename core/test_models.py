import boto3
c = boto3.client("bedrock-runtime", region_name="ap-northeast-2")
models = [
    "apac.anthropic.claude-3-haiku-20240307-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "apac.anthropic.claude-sonnet-4-20250514-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
]
for m in models:
    try:
        r = c.converse(modelId=m, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        print("OK  ", m, "->", r["output"]["message"]["content"][0]["text"][:30])
    except Exception as e:
        msg = str(e)
        tag = "PAYMENT" if "PAYMENT" in msg else ("ACCESS" if "AccessDenied" in msg else ("NOTFOUND" if "NotFound" in msg or "not found" in msg else "ERR"))
        print("FAIL", m, "->", tag)
