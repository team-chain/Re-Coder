"""
watchdog/config.py — Watchdog 데몬 설정 로더.

환경변수를 우선 사용하고, /etc/recoder/watchdog.env 같은 dotenv 파일이 존재하면
선택적으로 로드한다 (python-dotenv 미설치 시 자체 파서 사용).

설계 §4.1.3 — 모든 설정은 환경변수로 관리. systemd EnvironmentFile= 와 호환.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 환경변수 키
# ---------------------------------------------------------------------------

ENV_PREFIX = "RECODER_WATCHDOG_"

ENV_PROJECT_ID = ENV_PREFIX + "PROJECT_ID"
ENV_HOST = ENV_PREFIX + "HOST"
ENV_ENVIRONMENT = ENV_PREFIX + "ENVIRONMENT"
ENV_DISCORD_WEBHOOK_URL = ENV_PREFIX + "DISCORD_WEBHOOK_URL"
ENV_INCIDENT_PATH = ENV_PREFIX + "INCIDENT_PATH"
ENV_HEALTH_CHECK_URLS = ENV_PREFIX + "HEALTH_CHECK_URLS"
ENV_POLL_INTERVAL = ENV_PREFIX + "POLL_INTERVAL"
ENV_HEALTH_INTERVAL = ENV_PREFIX + "HEALTH_INTERVAL"
ENV_LOG_LEVEL = ENV_PREFIX + "LOG_LEVEL"
ENV_DOTENV_PATH = ENV_PREFIX + "DOTENV_PATH"

# 추가 옵션 — 운영상 필요
ENV_MEMORY_THRESHOLD = ENV_PREFIX + "MEMORY_THRESHOLD"
ENV_HEALTH_FAIL_THRESHOLD = ENV_PREFIX + "HEALTH_FAIL_THRESHOLD"
ENV_SPAM_WINDOW = ENV_PREFIX + "SPAM_WINDOW_SECONDS"
ENV_HEALTH_TIMEOUT = ENV_PREFIX + "HEALTH_TIMEOUT"
ENV_DEPLOYMENT_ID = ENV_PREFIX + "DEPLOYMENT_ID"

# ── ECS/CloudWatch 감시 (FR-06-01/02) ────────────────────────────────
# 비워 두면 ECS 감시를 하지 않는다. 로컬 도커만 쓰는 설치에서도 그대로 돈다.
ENV_ECS_CLUSTER = ENV_PREFIX + "ECS_CLUSTER"
ENV_ECS_SERVICE = ENV_PREFIX + "ECS_SERVICE"
ENV_AWS_REGION = ENV_PREFIX + "AWS_REGION"
ENV_ALB_NAME = ENV_PREFIX + "ALB_NAME"
ENV_TARGET_GROUP = ENV_PREFIX + "TARGET_GROUP"
ENV_ECS_INTERVAL = ENV_PREFIX + "ECS_INTERVAL"
ENV_ECS_WINDOW = ENV_PREFIX + "ECS_WINDOW_SECONDS"
ENV_ERROR_RATE = ENV_PREFIX + "ERROR_RATE_THRESHOLD"
ENV_MIN_REQUESTS = ENV_PREFIX + "MIN_REQUESTS"
ENV_P95_SECONDS = ENV_PREFIX + "P95_THRESHOLD_SECONDS"
ENV_UNHEALTHY_POLLS = ENV_PREFIX + "UNHEALTHY_POLLS"


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class WatchdogConfig:
    project_id: str
    host: str
    environment: str
    discord_webhook_url: Optional[str]
    incident_path: Path
    health_check_urls: Dict[str, str]
    poll_interval_seconds: float
    health_interval_seconds: float
    log_level: str
    memory_threshold_percent: float = 90.0
    health_fail_threshold: int = 3
    spam_window_seconds: float = 60.0
    health_timeout_seconds: float = 5.0
    deployment_id: Optional[str] = None
    # ── ECS/CloudWatch 감시 (FR-06-01/02) ────────────────────────────
    #: 클러스터·서비스가 둘 다 있어야 ECS 감시를 켠다. 로컬 도커만 쓰는
    #: 설치에서는 비어 있고, 그때는 이 경로가 통째로 꺼진다.
    ecs_cluster: str = ""
    ecs_service: str = ""
    aws_region: str = ""
    alb_name: str = ""
    target_group: str = ""
    ecs_interval_seconds: float = 60.0
    #: 지표 관측 창. 배포 직후 집중 감시도 같은 길이를 쓴다(기본 5분).
    ecs_window_seconds: int = 300
    error_rate_threshold: float = 0.05
    min_requests: int = 20
    p95_threshold_seconds: float = 3.0
    unhealthy_polls: int = 3
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def ecs_enabled(self) -> bool:
        return bool(self.ecs_cluster and self.ecs_service)

    def ecs_config_problem(self) -> Optional[str]:
        """ECS 감시를 켰는데 못 도는 설정이면 그 이유. 문제 없으면 None.

        **설정 실수는 조용히 실패한다.** 리전을 빼먹으면 botocore 가
        `Invalid endpoint: https://ecs..amazonaws.com` 을 낼 뿐이라 로그를
        봐도 무엇을 고쳐야 하는지 알 수 없고, 그 사이 사람들은 감시가 도는
        줄 안다.
        """
        if not self.ecs_enabled:
            return None
        if not self.aws_region.strip():
            return (
                "ECS 감시를 켰지만 리전이 없습니다 — RECODER_WATCHDOG_AWS_REGION "
                "(또는 AWS_REGION) 을 설정하세요."
            )
        return None

    def summary(self) -> str:
        #: ECS 감시 여부를 요약에 넣는다. 안 넣으면 `--check` 로도 감시가
        #: 켜졌는지 알 수 없어, 꺼진 채로 돌아도 아무도 모른다.
        if self.ecs_enabled:
            ecs = (f"{self.ecs_cluster}/{self.ecs_service}@"
                   f"{self.aws_region or '리전없음'} alb={'yes' if self.alb_name else 'no'}")
        else:
            ecs = "off"
        return (
            f"project_id={self.project_id} host={self.host} env={self.environment} "
            f"poll={self.poll_interval_seconds}s health={self.health_interval_seconds}s "
            f"incident_path={self.incident_path} "
            f"discord_configured={'yes' if self.discord_webhook_url else 'no'} "
            f"health_urls={list(self.health_check_urls.keys())} "
            f"ecs={ecs}"
        )


# ---------------------------------------------------------------------------
# .env 로더 (의존성 없는 단순 구현)
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        log.warning("dotenv load failed: %s", exc)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _parse_health_urls(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not raw:
        return result
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            result[f"service_{len(result) + 1}"] = chunk
            continue
        name, _, url = chunk.partition("=")
        name = name.strip()
        url = url.strip()
        if name and url:
            result[name] = url
    return result


def _to_float(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("invalid float env value %r, using default %s", value, default)
        return default


def _to_int(value: Optional[str], default: int) -> int:
    if not value:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        log.warning("invalid int env value %r, using default %s", value, default)
        return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(dotenv_path: Optional[Path] = None) -> WatchdogConfig:
    raw_dotenv = str(dotenv_path) if dotenv_path else os.environ.get(ENV_DOTENV_PATH, "").strip()
    if raw_dotenv:
        try:
            candidate_path = Path(raw_dotenv)
            if candidate_path.is_file():
                _load_dotenv(candidate_path)
        except Exception as exc:
            log.warning("dotenv load skipped: %s", exc)

    project_id = os.environ.get(ENV_PROJECT_ID, "unknown-project").strip() or "unknown-project"
    host = os.environ.get(ENV_HOST, "").strip() or socket.gethostname()
    environment = os.environ.get(ENV_ENVIRONMENT, "production").strip() or "production"
    discord_url = os.environ.get(ENV_DISCORD_WEBHOOK_URL, "").strip() or None
    incident_path_str = os.environ.get(ENV_INCIDENT_PATH, "").strip() or "/var/log/recoder/incidents.jsonl"
    incident_path = Path(incident_path_str)
    health_urls = _parse_health_urls(os.environ.get(ENV_HEALTH_CHECK_URLS, ""))
    poll_interval = _to_float(os.environ.get(ENV_POLL_INTERVAL), 5.0)
    health_interval = _to_float(os.environ.get(ENV_HEALTH_INTERVAL), 30.0)
    log_level_raw = (os.environ.get(ENV_LOG_LEVEL, "INFO") or "INFO").upper().strip()
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    log_level = log_level_raw if log_level_raw in valid_levels else "INFO"

    cfg = WatchdogConfig(
        project_id=project_id,
        host=host,
        environment=environment,
        discord_webhook_url=discord_url,
        incident_path=incident_path,
        health_check_urls=health_urls,
        poll_interval_seconds=max(1.0, poll_interval),
        health_interval_seconds=max(5.0, health_interval),
        log_level=log_level,
        memory_threshold_percent=_to_float(os.environ.get(ENV_MEMORY_THRESHOLD), 90.0),
        health_fail_threshold=_to_int(os.environ.get(ENV_HEALTH_FAIL_THRESHOLD), 3),
        spam_window_seconds=_to_float(os.environ.get(ENV_SPAM_WINDOW), 60.0),
        health_timeout_seconds=_to_float(os.environ.get(ENV_HEALTH_TIMEOUT), 5.0),
        deployment_id=(os.environ.get(ENV_DEPLOYMENT_ID, "").strip() or None),
        ecs_cluster=os.environ.get(ENV_ECS_CLUSTER, "").strip(),
        ecs_service=os.environ.get(ENV_ECS_SERVICE, "").strip(),
        aws_region=(
            os.environ.get(ENV_AWS_REGION, "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        ),
        alb_name=os.environ.get(ENV_ALB_NAME, "").strip(),
        target_group=os.environ.get(ENV_TARGET_GROUP, "").strip(),
        ecs_interval_seconds=max(15.0, _to_float(os.environ.get(ENV_ECS_INTERVAL), 60.0)),
        ecs_window_seconds=max(60, _to_int(os.environ.get(ENV_ECS_WINDOW), 300)),
        error_rate_threshold=_to_float(os.environ.get(ENV_ERROR_RATE), 0.05),
        min_requests=_to_int(os.environ.get(ENV_MIN_REQUESTS), 20),
        p95_threshold_seconds=_to_float(os.environ.get(ENV_P95_SECONDS), 3.0),
        unhealthy_polls=_to_int(os.environ.get(ENV_UNHEALTHY_POLLS), 3),
    )
    return cfg


# module exports
__all__ = ["WatchdogConfig", "load_config"]
