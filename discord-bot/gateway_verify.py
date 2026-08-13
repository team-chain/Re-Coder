"""
게이트웨이 토큰 검증 클라이언트 — /recoder link 가 바인딩 전에 호출한다.

토큰이 발급처(게이트웨이 DynamoDB)에서 실제로 발급된 것인지 확인한다.
형식(rcdr_<sid>_<secret>)만으로는 소유를 증명할 수 없다.
"""
from __future__ import annotations

import os

import aiohttp

GATEWAY_URL = os.getenv("RECODER_GATEWAY_URL", "").rstrip("/")
#: 게이트웨이가 설정돼 있으면 토큰 검증을 **강제**한다. 설정이 없으면
#: 권위 저장소가 없는 로컬/단일 PC 배포이므로(사칭할 다른 학생이 없음)
#: 자칭 바인딩을 허용한다. 이 escape 를 명시적으로 끄려면 아래 env=1.
REQUIRE_VERIFY = os.getenv("RECODER_LINK_REQUIRE_VERIFY", "0") == "1"


def gateway_configured() -> bool:
    return bool(GATEWAY_URL)


async def verify_token(raw_token: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """(valid, student_id) 반환. 검증 불가/거부면 (False, "").

    게이트웨이 미설정 + REQUIRE_VERIFY=0 이면 (True, sid) 로 통과시킨다
    (단일 PC 데모 경로 — 사칭 위협이 없음). REQUIRE_VERIFY=1 이면 미설정도 거부.
    """
    sid_from_token = ""
    if raw_token.startswith("rcdr_") and raw_token.count("_") >= 2:
        sid_from_token = raw_token.split("_", 2)[1]

    if not GATEWAY_URL:
        if REQUIRE_VERIFY:
            return False, ""
        return (bool(sid_from_token), sid_from_token)

    url = f"{GATEWAY_URL}/verify"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={"token": raw_token},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return False, ""
                data = await resp.json()
                if not data.get("valid"):
                    return False, ""
                sid = str(data.get("student_id", ""))
                # 게이트웨이가 인정한 sid 와 토큰이 주장하는 sid 가 일치해야 한다.
                if sid_from_token and sid != sid_from_token:
                    return False, ""
                return True, sid
    except Exception:
        # 검증 자체에 실패하면(네트워크 등) 소유를 증명 못 하므로 거부.
        return False, ""
