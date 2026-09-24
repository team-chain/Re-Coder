import boto3
c = boto3.client("bedrock-runtime", region_name="us-east-1")
r = c.converse(modelId="global.anthropic.claude-haiku-4-5-20251001-v1:0", messages=[{"role":"user","content":[{"text":"hi"}]}])
print(r["output"]["message"]["content"][0]["text"])
