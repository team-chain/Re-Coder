"""
ReCoder Gateway — /admin 핸들러 (운영자 전용).

ADMIN_KEY 헤더로 보호. 학생 토큰 발급/폐기/쿼터조정/현황 조회.
프로덕션에선 별도 인증(예: IAM, Cognito)으로 강화 권장.

라우트(POST body.action):
  issue   {student_id, name?, max_total?, max_daily?, rpm?}     → 토큰 원문 1회 반환
  revoke  {student_id}
  quota   {student_id, max_total?, max_daily?, rpm?}
  status  {student_id?}   → 학생 또는 풀 현황
"""
from __future__ import annotations

import json
import os

try:
    import common
except ImportError:
    from . import common  # type: ignore

ADMIN_KEY = os.environ.get("GW_ADMIN_KEY", "")


def _check_admin(event) -> bool:
    h = event.get("headers") or {}
    return bool(ADMIN_KEY) and (h.get("x-admin-key") or h.get("X-Admin-Key")) == ADMIN_KEY


def handler(event, context):
    if not _check_admin(event):
        return common.resp(403, {"error": "forbidden", "message": "admin key 필요"})
    try:
        body = json.loads(event.get("body") or "{}")
        action = body.get("action")

        if action == "issue":
            sid = str(body["student_id"])
            token = common.generate_token(sid)
            common.put_student(
                sid, common.hash_token(token), name=body.get("name", ""),
                discord_user_id=str(body.get("discord_user_id", "")),
                max_total=int(body.get("max_total", common.DEF_MAX_TOTAL)),
                max_daily=int(body.get("max_daily", common.DEF_MAX_DAILY)),
                rpm=int(body.get("rpm", common.DEF_RPM)))
            return common.resp(200, {"student_id": sid, "token": token,
                                     "discord_user_id": str(body.get("discord_user_id", "")),
                                     "note": "토큰 원문은 지금만 표시됩니다. 안전하게 전달하세요."})

        if action == "revoke":
            ok = common.set_student_active(str(body["student_id"]), False)
            return common.resp(200 if ok else 404, {"revoked": ok})

        if action == "quota":
            ok = common.set_student_quota(
                str(body["student_id"]),
                max_total=body.get("max_total"), max_daily=body.get("max_daily"),
                rpm=body.get("rpm"))
            return common.resp(200 if ok else 404, {"updated": ok})

        if action == "status":
            if body.get("student_id"):
                item = common.get_student(str(body["student_id"]))
                return common.resp(200 if item else 404, item or {"error": "not_found"})
            if body.get("discord_user_id"):
                item = common.get_student_by_discord(str(body["discord_user_id"]))
                return common.resp(200 if item else 404, item or {"error": "not_found"})
            return common.resp(200, common.get_pool())

        return common.resp(400, {"error": "bad_action"})
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
