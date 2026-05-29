"""
ReCoder Gateway — /llm/invoke 핸들러.

학생 Local Core 가 본인 토큰으로 호출 → 쿼터 확인 → Bedrock 호출 대행.
운영자 AWS 키는 이 Lambda 의 IAM Role(Bedrock 전용)로만 사용되며 학생에게 노출되지 않는다.

요청 (POST):
  헤더: Authorization: Bearer <학생토큰>
  바디: { "messages":[{"role":"user","content":[{"text":"..."}]}],
          "system":"", "output_schema":{...}|null, "max_tokens":2048, "model":null }
응답: { "text", "parsed", "model_used", "input_tokens", "output_tokens", "cost_usd" }
"""
from __future__ import annotations

import json

try:
    import common
except ImportError:
    from . import common  # type: ignore


def _bearer(event) -> str:
    h = event.get("headers") or {}
    auth = h.get("authorization") or h.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def handler(event, context):
    try:
        token = _bearer(event)
        item = common.authenticate(token)               # 인증
        sid = item["pk"].split("#", 1)[1]
        common.check_rate(sid, int(item.get("rpm", common.DEF_RPM)))  # 분당
        common.check_quota_before(item)                  # 총/일/풀

        body = json.loads(event.get("body") or "{}")
        messages = body.get("messages")
        if not messages:
            prompt = body.get("prompt", "")
            messages = [{"role": "user", "content": [{"text": prompt}]}]
        result = common.invoke_bedrock(
            messages,
            model=body.get("model"),
            system=body.get("system", ""),
            max_tokens=int(body.get("max_tokens", 2048)),
            output_schema=body.get("output_schema"),
        )
        cost = common.record_usage(sid, result["input_tokens"], result["output_tokens"])
        result["cost_usd"] = round(cost, 6)
        return common.resp(200, result)

    except common.QuotaError as qe:
        code = 429 if qe.code in ("rate_limited", "daily_exceeded",
                                  "total_exceeded", "pool_exceeded") else 401
        return common.resp(code, {"error": qe.code, "message": qe.message})
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
