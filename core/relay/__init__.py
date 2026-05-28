"""
core.relay — Hybrid Cloud Relay (설계서 §6.4.2 흐름 1)

PC가 꺼진 상태에서 Discord로 들어온 명령을 클라우드 큐(DynamoDB)에 저장하고,
PC가 켜지면 Local Core 의 RelayPoller 가 큐를 polling 하여 비운다.

본 패키지는 다음을 제공한다:
- DynamoCommandQueue : boto3 기반 DynamoDB 큐 클라이언트 (lazy import)
- RelayPoller       : asyncio 백그라운드 polling 루프
"""

from __future__ import annotations

__all__ = ["DynamoCommandQueue", "RelayPoller"]


def __getattr__(name):  # noqa: D401 — lazy import 가드
    """boto3 import 비용을 피하기 위해 lazy 로드."""
    if name == "DynamoCommandQueue":
        from .dynamo_queue import DynamoCommandQueue
        return DynamoCommandQueue
    if name == "RelayPoller":
        from .poller import RelayPoller
        return RelayPoller
    raise AttributeError(f"module 'core.relay' has no attribute {name!r}")
