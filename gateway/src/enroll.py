"""
ReCoder Gateway — /enroll 핸들러 (학생 자가발급).

학생이 반(class) 공유 enroll 코드로 본인만의 **난수 고유 토큰**을 1회 발급받는다.
- 토큰은 절대 겹치지 않음(192bit secret + 조건부 삽입으로 원자적 고유 id).
- 오남용 방지: enroll 코드 필수 + 디스코드 1:1 중복 거부 + 정원 제한(ENROLL_MAX).

요청 (POST /enroll):
  { "code": "<반 공유 enroll 코드>", "name": "...", "discord_user_id": "..." }
응답: { "student_id", "token", "note" }
"""
from __future__ import annotations

import json
import os

try:
    import common
except ImportError:
    from . import common  # type: ignore

ENROLL_CODE = os.environ.get("GW_ENROLL_CODE", "")


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        if not ENROLL_CODE or body.get("code") != ENROLL_CODE:
            return common.resp(403, {"error": "bad_code",
                                     "message": "올바른 enroll 코드가 필요합니다."})
        sid, token = common.enroll_student(
            name=str(body.get("name", "")),
            discord_user_id=str(body.get("discord_user_id", "")),
        )
        return common.resp(200, {
            "student_id": sid, "token": token,
            "note": "이 토큰은 본인 전용입니다. VSCode 확장에 1회 입력하세요. 다시 표시되지 않습니다.",
        })
    except common.QuotaError as qe:
        code = 409 if qe.code in ("already_enrolled", "enroll_full") else 400
        return common.resp(code, {"error": qe.code, "message": qe.message})
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
