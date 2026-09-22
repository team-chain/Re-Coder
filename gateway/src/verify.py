"""
ReCoder Gateway — /verify 핸들러.

디스코드 봇의 /recoder link 가 **바인딩 전에** 토큰이 진짜 발급된 것인지
발급처(DynamoDB)에 확인하는 용도. 토큰 형식만으로는 소유를 증명할 수 없다 —
`rcdr_<피해자>_<아무거나>` 는 형식이 유효하지만 발급된 적이 없다.

요청 (POST /verify):  { "token": "rcdr_<sid>_<secret>" }
응답 200: { "valid": true,  "student_id": "<sid>" }
응답 200: { "valid": false, "reason": "<code>" }   # 형식/미등록/불일치/만료
"""
from __future__ import annotations

import json

try:
    import common
except ImportError:
    from . import common  # type: ignore


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        token = str(body.get("token", ""))
        try:
            item = common.authenticate(token)   # 발급처 권위 검증(해시 대조)
        except common.QuotaError as qe:
            # 유효하지 않은 토큰 — valid:false 로만 알린다(정보 최소 노출).
            return common.resp(200, {"valid": False, "reason": qe.code})
        return common.resp(200, {
            "valid": True,
            "student_id": item["pk"].split("#", 1)[1],
        })
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
