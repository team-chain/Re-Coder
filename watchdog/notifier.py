"""
watchdog/notifier.py — Discord Webhook 알림 전송.

설계 §3.2.4 / §4.1.3 — 알림 실패가 incident.jsonl 저장을 막아서는 안 된다.
재시도 3회 지수 백오프 후에도 실패하면 로그만 남기고 조용히 포기한다.

전송되는 페이로드는 반드시 마스킹이 끝난 alert dict 만 사용.
호출자(recoder_watchdog.py)가 mask 후 dict 를 전달한다.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover — install.sh 가 설치
    requests = None  # type: ignore

log = logging.getLogger(__name__)


# severity → embed color (Discord 색상은 정수 RGB)
_COLOR_MAP = {
    "critical": 0xE74C3C,  # red
    "error": 0xE74C3C,
    "sev1": 0xE74C3C,
    "warning": 0xF1C40F,   # yellow
    "warn": 0xF1C40F,
    "sev2": 0xF1C40F,
    "info": 0x3498DB,      # blue
    "sev3": 0x3498DB,
    "debug": 0x95A5A6,
}

_DEFAULT_TIMEOUT_SEC = 10
_MAX_RETRIES = 3
_BACKOFF_BASE_SEC = 1.0


# ---------------------------------------------------------------------------
# Payload 빌더
# ---------------------------------------------------------------------------


def _severity_color(severity: Optional[str]) -> int:
    if not severity:
        return _COLOR_MAP["info"]
    return _COLOR_MAP.get(severity.lower(), _COLOR_MAP["info"])


def _truncate(value: Any, max_len: int = 1000) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= max_len else s[:max_len] + "...[TRUNCATED]"


def build_discord_payload(alert: Dict[str, Any]) -> Dict[str, Any]:
    """incident.jsonl 형식의 alert dict 를 Discord webhook 페이로드로 변환."""
    severity = (alert.get("severity") or "info")
    color = _severity_color(severity)

    alert_type = alert.get("alert_type") or "unknown"
    container = alert.get("container_name") or "n/a"
    host = alert.get("host") or "n/a"
    env = alert.get("environment") or "n/a"
    project = alert.get("project_id") or "n/a"
    detected_at = alert.get("detected_at") or ""

    title = f"[{severity.upper()}] {alert_type} — {container}"
    description_parts = []
    if alert.get("message"):
        description_parts.append(_truncate(alert["message"], 800))

    logs_excerpt = alert.get("logs_excerpt") or []
    if isinstance(logs_excerpt, list) and logs_excerpt:
        joined = "\n".join(str(line) for line in logs_excerpt[:10])
        description_parts.append("```\n" + _truncate(joined, 1200) + "\n```")

    description = "\n\n".join(description_parts)[:3500]  # Discord embed limit 4096

    fields = [
        {"name": "Project", "value": _truncate(project, 200), "inline": True},
        {"name": "Environment", "value": _truncate(env, 200), "inline": True},
        {"name": "Host", "value": _truncate(host, 200), "inline": True},
        {"name": "Container", "value": _truncate(container, 200), "inline": True},
        {"name": "Detected", "value": _truncate(detected_at, 200), "inline": True},
        {"name": "Alert ID", "value": _truncate(alert.get("alert_id"), 200), "inline": True},
    ]

    health_result = alert.get("health_check_result")
    if isinstance(health_result, dict) and health_result:
        fields.append({
            "name": "Health",
            "value": _truncate(
                ", ".join(f"{k}={v}" for k, v in list(health_result.items())[:6]),
                500,
            ),
            "inline": False,
        })

    metric = alert.get("metric_snapshot")
    if isinstance(metric, dict) and metric:
        fields.append({
            "name": "Metric",
            "value": _truncate(
                ", ".join(f"{k}={v}" for k, v in list(metric.items())[:6]),
                500,
            ),
            "inline": False,
        })

    if alert.get("recent_deployment_id"):
        fields.append({
            "name": "Recent deployment",
            "value": _truncate(alert["recent_deployment_id"], 200),
            "inline": True,
        })

    embed = {
        "title": _truncate(title, 250),
        "description": description or "(no description)",
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"ReCoder Watchdog • fingerprint={_truncate(alert.get('fingerprint'), 64)}",
        },
        "timestamp": detected_at if detected_at else None,
    }
    # None timestamp 제거 (Discord 가 None 거부)
    if embed["timestamp"] is None:
        embed.pop("timestamp")

    content = f":rotating_light: **{severity.upper()}** {alert_type} on `{container}`"
    return {
        "content": _truncate(content, 1500),
        "embeds": [embed],
        "username": "ReCoder Watchdog",
    }


# ---------------------------------------------------------------------------
# 전송
# ---------------------------------------------------------------------------


def notify_discord(
    webhook_url: Optional[str],
    alert: Dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
    max_retries: int = _MAX_RETRIES,
) -> bool:
    """Discord webhook 전송. 성공 True / 실패 False (예외 raise 안 함).

    재시도: 최대 max_retries 회, 지수 백오프 (1s, 2s, 4s).
    requests 미설치 / webhook_url 미설정 시 False 만 반환.
    """
    if not webhook_url:
        log.debug("notify_discord: no webhook url configured, skipping")
        return False
    if requests is None:
        log.error("notify_discord: 'requests' library not installed — cannot send")
        return False

    payload = build_discord_payload(alert)

    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(  # type: ignore[attr-defined]
                webhook_url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            if 200 <= resp.status_code < 300:
                log.info("discord notify ok (attempt=%d, status=%d)", attempt, resp.status_code)
                return True
            # 429 rate limit — Retry-After 헤더 반영
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1") or "1")
                log.warning("discord 429 rate-limited, sleeping %.1fs", retry_after)
                time.sleep(min(retry_after, 30.0))
                continue
            log.warning(
                "discord notify failed attempt=%d status=%d body=%s",
                attempt,
                resp.status_code,
                resp.text[:200] if resp.text else "",
            )
        except Exception as exc:  # noqa: BLE001 — 네트워크 예외 전체 catch
            last_exc = exc
            log.warning("discord notify exception attempt=%d: %s", attempt, exc)
        # 지수 백오프
        if attempt < max_retries:
            time.sleep(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))

    if last_exc:
        log.error("discord notify giving up after %d attempts: %s", max_retries, last_exc)
    else:
        log.error("discord notify giving up after %d attempts", max_retries)
    return False


__all__ = ["build_discord_payload", "notify_discord"]
