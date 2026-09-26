"""boto3/botocore 클라이언트 기본 타임아웃·재시도.

botocore 기본 연결 제한은 60초이고 재시도(최대 5회)마다 다시 기다린다.
네트워크가 막히거나 프록시가 연결을 붙잡으면 AWS 호출 하나가 수 분씩 걸리고,
그동안 연결 상태·배포 화면이 "무한 로딩"으로 보인다. 호출하는 쪽이 config 를
따로 주지 않은 값만 아래 기본값으로 채운다 — 명시한 값은 그대로 이긴다.
"""
from __future__ import annotations

import os

_INSTALLED = False


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def default_config():
    from botocore.config import Config

    #: 재시도 횟수·방식은 넣지 않는다 — ~/.aws/config 의 max_attempts·retry_mode 와
    #: AWS_MAX_ATTEMPTS 같은 사용자 설정을 명시 Config 가 덮어 버리기 때문이다.
    return Config(
        connect_timeout=_float_env("RECODER_AWS_CONNECT_TIMEOUT", 5),
        read_timeout=_float_env("RECODER_AWS_READ_TIMEOUT", 60),
    )


def install() -> bool:
    """botocore Session.create_client 에 기본 Config 를 끼운다. 여러 번 불러도 한 번만."""
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import botocore.session
    except Exception:  # boto3 가 없는 빌드 — 할 일이 없다
        return False

    original = botocore.session.Session.create_client
    if getattr(original, "_recoder_defaults", False):
        _INSTALLED = True
        return True

    def create_client(self, *args, **kwargs):
        config = kwargs.get("config")
        base = default_config()
        kwargs["config"] = base.merge(config) if config is not None else base
        return original(self, *args, **kwargs)

    create_client._recoder_defaults = True  # type: ignore[attr-defined]
    create_client.__wrapped__ = original  # type: ignore[attr-defined]
    botocore.session.Session.create_client = create_client
    _INSTALLED = True
    return True
