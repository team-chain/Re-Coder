"""
ReCoder 디스코드 봇 — 배포 클라이언트.

생성된 정적 파일을 운영자 게이트웨이(/deploy/s3)로 업로드하고 공개 URL을 받는다.
학생/봇은 AWS 자격증명이 필요 없다 — 게이트웨이가 운영자 계정으로 대행(Bedrock 게이트웨이와 동일).

환경변수:
  RECODER_GATEWAY_URL   게이트웨이 베이스 URL (예: https://xxxx.execute-api.ap-northeast-2.amazonaws.com)
  RECODER_DEPLOY_TOKEN  배포용 학생 토큰 (운영자가 issue_tokens.py 로 발급한 토큰 1개)
"""
from __future__ import annotations

import os

import aiohttp

GATEWAY_URL = os.getenv("RECODER_GATEWAY_URL", "").rstrip("/")
DEPLOY_TOKEN = os.getenv("RECODER_DEPLOY_TOKEN", "")


def is_configured() -> bool:
    return bool(GATEWAY_URL and DEPLOY_TOKEN)


async def deploy_static(project: str, files: list[dict], *, timeout: float = 30.0) -> dict:
    """
    files: [{"path": "index.html", "content": "..."} , ...]
    반환: {"url": "...", "files": [...], "count": N}
    실패 시 RuntimeError.
    """
    if not is_configured():
        raise RuntimeError(
            "배포 게이트웨이가 설정되지 않았습니다 (RECODER_GATEWAY_URL / RECODER_DEPLOY_TOKEN)."
        )
    url = f"{GATEWAY_URL}/deploy/s3"
    payload = {"project": project, "files": files}
    headers = {"Authorization": f"Bearer {DEPLOY_TOKEN}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    msg = (data or {}).get("message") or (data or {}).get("error") or f"HTTP {r.status}"
                    raise RuntimeError(f"배포 실패: {msg}")
                return data
    except aiohttp.ClientError as e:
        raise RuntimeError(f"게이트웨이 연결 실패: {e}") from e
