"""
incident_timeline.py — Incident Timeline MVP 빌더 (설계서 §Q4 Must-Wedge).

설계서:
    OTel backend 연결 불가 시:
      Watchdog incident.jsonl + AuditLog 기반으로 skeleton 생성,
      "observability 데이터 없음" 표시.

본 빌더는 다음 입력을 시간순으로 통합한다.
  1. DeploymentRecord (배포 이벤트)
  2. AuditLog 행 (승인/거부, rollback PR 생성 등)
  3. Watchdog incident.jsonl 항목 (health check / restart / alert)
  4. (가능 시) OTel 로그/메트릭 spike 시점 — OTelQueryService 결과를 IncidentEvent 로 변환

출력: IncidentTimeline (schemas.py 정의).

원칙:
  - OTel 사용 가능 여부를 timeline.otel_available 로 명시 — 외부 UI 가 fallback 라벨링.
  - 시간 정렬 후 detected_at 이전 한정 / 24h 등 옵션으로 잘라낸다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

try:
    from schemas import (
        DeploymentRecord,
        IncidentEvent,
        IncidentEventKind,
        IncidentSeverity,
        IncidentTimeline,
    )
except ImportError:
    from core.schemas import (  # type: ignore
        DeploymentRecord,
        IncidentEvent,
        IncidentEventKind,
        IncidentSeverity,
        IncidentTimeline,
    )

# OTelQueryService 는 선택적 — 미설치/미연결 시에도 동작
try:
    from observability.otel_query_service import OTelQueryService
except ImportError:
    try:
        from core.observability.otel_query_service import OTelQueryService  # type: ignore
    except Exception:
        OTelQueryService = None  # type: ignore


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass
class TimelineBuildInput:
    incident_id: str
    detected_at: datetime
    project_id: Optional[str] = None
    severity: IncidentSeverity = IncidentSeverity.SEV3
    service_name: Optional[str] = None
    container_name: Optional[str] = None

    deployments: list[DeploymentRecord] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    watchdog_jsonl_path: Optional[Path] = None

    window_before: timedelta = timedelta(hours=2)
    window_after: timedelta = timedelta(minutes=30)

    # OTelQueryService 인스턴스 (없으면 None — fallback 모드)
    otel_service: Optional[Any] = None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_timeline(inp: TimelineBuildInput) -> IncidentTimeline:
    start = inp.detected_at - inp.window_before
    end = inp.detected_at + inp.window_after

    events: list[IncidentEvent] = []

    # 1) DeploymentRecord
    for dep in inp.deployments:
        if not _in_window(dep.deployed_at, start, end):
            continue
        events.append(
            IncidentEvent(
                incident_id=inp.incident_id,
                occurred_at=_aware(dep.deployed_at),
                kind=IncidentEventKind.DEPLOYMENT,
                title=f"deploy {dep.image}",
                detail=f"method={dep.method} container={dep.container_name} status={dep.status}",
                source="deployment_record",
                refs={
                    "deployment_id": dep.deployment_id,
                    "image": dep.image,
                    "git_commit": dep.git_commit,
                },
            )
        )

    # 2) AuditLog 행
    for row in inp.audit_rows:
        ts = _parse_dt(row.get("occurred_at") or row.get("timestamp"))
        if ts is None or not _in_window(ts, start, end):
            continue
        action = row.get("action", "audit")
        events.append(
            IncidentEvent(
                incident_id=inp.incident_id,
                occurred_at=ts,
                kind=_kind_for_audit(action),
                title=str(action),
                detail=row.get("resource_id") or row.get("resource_type"),
                source="auditlog",
                refs={k: v for k, v in row.items() if k in (
                    "actor_user_id", "actor_device_id", "policy_bundle_version",
                    "resource_id",
                )},
            )
        )

    # 3) Watchdog incident.jsonl (옵션)
    if inp.watchdog_jsonl_path and inp.watchdog_jsonl_path.exists():
        try:
            for line in inp.watchdog_jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_dt(row.get("occurred_at") or row.get("ts"))
                if ts is None or not _in_window(ts, start, end):
                    continue
                events.append(
                    IncidentEvent(
                        incident_id=inp.incident_id,
                        occurred_at=ts,
                        kind=IncidentEventKind.ALERT,
                        title=row.get("type", "watchdog_alert"),
                        detail=row.get("message"),
                        source="watchdog",
                        refs={k: row[k] for k in ("container", "host", "severity") if k in row},
                    )
                )
        except OSError as exc:  # pragma: no cover - 파일 IO 실패는 fallback
            log.warning("watchdog jsonl read failed: %s", exc)

    # 4) OTel 데이터 (가능 시) — 로그 발췌만 Q4 1차에 포함
    otel_available = False
    fallback_reason: Optional[str] = None
    if inp.otel_service is not None:
        try:
            otel_available = bool(inp.otel_service.available())
        except Exception:  # noqa: BLE001
            otel_available = False
            fallback_reason = "otel service raised"
        if otel_available and inp.container_name:
            try:
                lines = inp.otel_service.container_error_excerpt(
                    container_name=inp.container_name,
                    minutes=int(inp.window_before.total_seconds() // 60) or 15,
                    keyword="error",
                    limit=20,
                )
                if lines:
                    events.append(
                        IncidentEvent(
                            incident_id=inp.incident_id,
                            occurred_at=inp.detected_at,
                            kind=IncidentEventKind.LOG_PATTERN,
                            title=f"loki: {len(lines)} error lines",
                            detail=lines[0][:240],
                            source="loki",
                            refs={"sample_count": len(lines)},
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                log.debug("otel log excerpt failed: %s", exc)
                fallback_reason = fallback_reason or "otel log query failed"
    else:
        fallback_reason = "no otel service provided"

    # 정렬
    events.sort(key=lambda e: e.occurred_at)

    return IncidentTimeline(
        incident_id=inp.incident_id,
        project_id=inp.project_id,
        severity=inp.severity,
        detected_at=_aware(inp.detected_at),
        events=events,
        otel_available=otel_available,
        fallback_reason=None if otel_available else fallback_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return _aware(start) <= _aware(dt) <= _aware(end)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        v = value.replace("Z", "+00:00")
        try:
            return _aware(datetime.fromisoformat(v))
        except ValueError:
            return None
    return None


def _kind_for_audit(action: str) -> IncidentEventKind:
    a = (action or "").lower()
    if "approve" in a or "approval" in a:
        return IncidentEventKind.APPROVAL
    if "rollback" in a:
        return IncidentEventKind.ROLLBACK
    if "gitops" in a or "pr" in a:
        return IncidentEventKind.GITOPS_PR
    return IncidentEventKind.AUDIT


__all__ = ["TimelineBuildInput", "build_timeline"]
