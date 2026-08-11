"""
ReCoder 배포 검증용 샘플 앱 (FR-05-04)

카드 DoD 1번 "샘플 FastAPI 앱이 실제 사용자 AWS ECS에서 기동되고 URL로
접속됨"을 확인하기 위한 최소 앱이다. 일부러 의존성을 fastapi/uvicorn 만
두었다 — 배포 경로를 검증하는 게 목적인데 앱이 무거우면 실패했을 때
원인이 배포인지 앱인지 가려지지 않는다.

`/version` 은 DoD 2번("재배포 시 새 버전으로 갱신됨") 확인용이다.
환경변수 APP_VERSION 을 바꿔 다시 배포하면 이 값이 바뀌어야 한다.
"""

import os
import socket

from fastapi import FastAPI

app = FastAPI(title="ReCoder Sample App")

APP_VERSION = os.environ.get("APP_VERSION", "dev")


@app.get("/health")
def health() -> dict:
    """ECS·로드밸런서가 찌르는 곳. 항상 가볍게 응답한다."""
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    return {
        "message": "ReCoder 가 이 컨테이너를 AWS ECS Fargate 에 띄웠습니다.",
        "version": APP_VERSION,
        "hostname": socket.gethostname(),
    }


@app.get("/version")
def version() -> dict:
    """재배포로 버전이 실제로 바뀌었는지 확인하는 곳 (DoD 2번)."""
    return {"version": APP_VERSION}
