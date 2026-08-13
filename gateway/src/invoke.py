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
        common.check_quota_before(item)                  # 빠른 사전 차단(리셋 포함)

        body = json.loads(event.get("body") or "{}")
        messages = body.get("messages")
        if not messages:
            prompt = body.get("prompt", "")
            messages = [{"role": "user", "content": [{"text": prompt}]}]

        # max_tokens 는 호출자 입력이다 — 상한 없이는 예산 자체가 무의미하다.
        max_tokens = min(int(body.get("max_tokens", 2048)), common.MAX_TOKENS_CEILING)
        system = body.get("system", "")

        # **유료 호출 전에 예산을 원자적으로 선점**한다. 사후 기록만으로는
        # 한도 직전의 큰 요청과 동시 요청 무리가 전부 통과한다.
        budget = common.estimate_request_tokens(messages, system, max_tokens)
        common.reserve_quota(item, budget)
        try:
            result = common.invoke_bedrock(
                messages,
                model=body.get("model"),
                system=system,
                max_tokens=max_tokens,
                output_schema=body.get("output_schema"),
            )
        except Exception:
            common.release_reservation(sid, budget)      # 실패 — 선점분 반환
            raise
        cost = common.reconcile_usage(
            sid, budget, result["input_tokens"], result["output_tokens"])
        result["cost_usd"] = round(cost, 6)
        return common.resp(200, result)

    except common.QuotaError as qe:
        code = 429 if qe.code in ("rate_limited", "daily_exceeded",
                                  "total_exceeded", "pool_exceeded") else 401
        return common.resp(code, {"error": qe.code, "message": qe.message})
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
