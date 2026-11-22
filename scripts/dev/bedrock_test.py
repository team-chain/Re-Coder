"""Bedrock 연결 확인용 한 줄 호출(개발용). AWS 자격 증명이 필요하고 요금이 소액 발생한다."""
import boto3
c = boto3.client("bedrock-runtime", region_name="us-east-1")
r = c.converse(modelId="global.anthropic.claude-haiku-4-5-20251001-v1:0", messages=[{"role":"user","content":[{"text":"hi"}]}])
print(r["output"]["message"]["content"][0]["text"])
