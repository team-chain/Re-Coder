"""
core/replay/timeline_builder.py — Deploy Replay 타임라인 빌더 (설계서 §38)

§38 Deploy Replay — "인시던트 영상 재생"
  § 38.2  타임라인 데이터 수집·재구성 / 재생 UI (속도 0.5x/1x/2x, 시점 점프)
  § 38.4  학습/포트폴리오/Postmortem 자동 생성

배포 이벤트 소스:
  1. SessionLogger SQLite (배포 기록, LLM 호출 내역)
  2. AuditLog (승인/거부, 롤백 PR)
  3. IncidentTimeline (incident.jsonl, Watchdog 이벤트)
  4. OTel Metric Spike (선택적 — 미연결 시 생략)
  5. Git 커밋 로그 (git log --oneline)

출력: ReplayTimeline — Replay.tsx WebView가 소비하는 JSON 직렬화 가능 객체.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

RECODER_HOME = Path.home() / ".recoder"
SESSIONS_DIR = RECODER_HOME / "sessions"
DB_PATH = RECODER_HOME / "sessions.db"


# ── 데이터 모델 ────────────────────────────────────────────────────────────

@dataclass
class ReplayEvent:
    """단일 재생 이벤트 — 타임라인의 한 프레임."""

    ts: str                        # ISO-8601 타임스탬프
    ts_unix: float                 # Unix epoch (재생 위치 계산용)
    kind: str                      # DEPLOY_START / APPROVAL / ROLLBACK / INCIDENT / LLM_CALL / GIT_COMMIT / METRIC_SPIKE
    title: str                     # 짧은 제목 (UI 표시)
    detail: str                    # 상세 설명 (hover/click 시 표시)
    actor: str = ""                # 트리거한 사용자 또는 시스템
    severity: str = "INFO"         # INFO / WARN / ERROR / CRITICAL
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayTimeline:
    """전체 Deploy Replay 타임라인."""

    deploy_id: str
    service: str
    cluster: str
    region: str
    start_ts: str
    end_ts: Optional[str]
    duration_seconds: float
    events: List[ReplayEvent] = field(default_factory=list)
    otel_available: bool = False
    root_cause: str = ""
    prevention: str = ""
    postmortem_md: str = ""        # §38.4 자동 생성 Postmortem

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ── 빌더 ──────────────────────────────────────────────────────────────────

class ReplayTimelineBuilder:
    """
    여러 소스에서 이벤트를 수집하여 ReplayTimeline을 재구성한다 (§38.2).

    사용법:
        builder = ReplayTimelineBuilder()
        timeline = await builder.build(deploy_id="abc123")
    """

    def __init__(self) -> None:
        self._db_path = DB_PATH
        self._sessions_dir = SESSIONS_DIR

        # OTelQueryService 선택적 로드
        self._otel: Optional[Any] = None
        try:
            from observability.otel_query_service import OTelQueryService
            self._otel = OTelQueryService()
        except Exception:
            try:
                from core.observability.otel_query_service import OTelQueryService  # type: ignore
                self._otel = OTelQueryService()
            except Exception:
                log.debug("OTelQueryService 미연결 — OTel 이벤트 생략")

    async def build(
        self,
        deploy_id: str,
        service: str = "",
        cluster: str = "",
        region: str = "ap-northeast-2",
        window_hours: int = 24,
    ) -> ReplayTimeline:
        """
        deploy_id에 해당하는 전체 타임라인을 수집·정렬·반환한다.

        실제 배포 기록이 없으면 incident.jsonl 기반 skeleton을 생성한다.
        """
        events: List[ReplayEvent] = []

        # 1. SQLite 배포 기록
        deploy_meta = self._load_deploy_meta(deploy_id)
        if not service and deploy_meta:
            service = deploy_meta.get("service", "unknown")
        if not cluster and deploy_meta:
            cluster = deploy_meta.get("cluster", "unknown")

        events.extend(self._collect_session_events(deploy_id))

        # 2. Audit log
        events.extend(self._collect_audit_events(deploy_id))

        # 3. Incident.jsonl (Watchdog)
        events.extend(self._collect_incident_events(deploy_id, window_hours))

        # 4. Git 커밋
        events.extend(self._collect_git_events())

        # 5. OTel metric spikes (선택적)
        otel_available = False
        if self._otel:
            try:
                otel_events = await self._collect_otel_events(service, window_hours)
                events.extend(otel_events)
                otel_available = True
            except Exception as exc:
                log.warning("OTel 이벤트 수집 실패: %s", exc)

        # 시간순 정렬
        events.sort(key=lambda e: e.ts_unix)

        # 타임라인 메타
        start_ts = events[0].ts if events else datetime.now(tz=timezone.utc).isoformat()
        end_ts = events[-1].ts if events else None
        duration = (events[-1].ts_unix - events[0].ts_unix) if len(events) > 1 else 0.0

        timeline = ReplayTimeline(
            deploy_id=deploy_id,
            service=service or "unknown",
            cluster=cluster or "unknown",
            region=region,
            start_ts=start_ts,
            end_ts=end_ts,
            duration_seconds=duration,
            events=events,
            otel_available=otel_available,
        )

        # §38.4 Postmortem 자동 생성
        timeline.postmortem_md = self._generate_postmortem(timeline)

        return timeline

    # ── 소스별 수집 메서드 ─────────────────────────────────────────────────

    def _load_deploy_meta(self, deploy_id: str) -> Dict[str, Any]:
        """SQLite에서 deploy_id 메타데이터를 조회한다."""
        if not self._db_path.exists():
            return {}
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (deploy_id,)
                )
                # cursor.description은 conn.close() 전에 읽어야 한다 — 죽은
                # `if False else []` 분기를 제거하고 진짜로 컬럼명을 수집한다.
                cols = [d[0] for d in cursor.description] if cursor.description else []
                row = cursor.fetchone()
            finally:
                conn.close()
            if row and cols:
                return dict(zip(cols, row))
        except Exception as exc:
            log.debug("deploy meta 조회 실패: %s", exc)
        return {}

    def _collect_session_events(self, deploy_id: str) -> List[ReplayEvent]:
        """SQLite llm_calls 테이블에서 LLM 호출 이벤트를 수집한다."""
        events: List[ReplayEvent] = []
        if not self._db_path.exists():
            return events
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                """
                SELECT called_at, agent, operation, provider, model_identifier,
                       input_tokens, output_tokens, cost_usd
                FROM llm_calls
                WHERE session_id = ?
                ORDER BY called_at
                """,
                (deploy_id,),
            ).fetchall()
            conn.close()

            for row in rows:
                called_at, agent, op, provider, model, inp, out, cost = row
                try:
                    ts_unix = datetime.fromisoformat(called_at).timestamp()
                except Exception:
                    ts_unix = 0.0
                events.append(
                    ReplayEvent(
                        ts=called_at,
                        ts_unix=ts_unix,
                        kind="LLM_CALL",
                        title=f"[{agent}] {op}",
                        detail=(
                            f"모델: {provider}/{model}\n"
                            f"토큰: {inp}in / {out}out\n"
                            f"비용: ${cost:.4f}"
                        ),
                        actor=agent,
                        severity="INFO",
                        metadata={
                            "agent": agent,
                            "op": op,
                            "model": model,
                            "cost_usd": cost,
                        },
                    )
                )
        except Exception as exc:
            log.debug("session events 수집 실패: %s", exc)
        return events

    def _collect_audit_events(self, deploy_id: str) -> List[ReplayEvent]:
        """AuditLog에서 승인/거부 이벤트를 수집한다."""
        events: List[ReplayEvent] = []
        # JSONL 기반 audit log 탐색
        audit_files = list(RECODER_HOME.glob("audit*.jsonl")) + \
                      list(RECODER_HOME.glob("**/audit*.jsonl"))
        for audit_file in audit_files:
            try:
                with open(audit_file, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if deploy_id not in str(entry):
                            continue
                        ts_str = entry.get("ts") or entry.get("timestamp", "")
                        try:
                            ts_unix = datetime.fromisoformat(ts_str).timestamp()
                        except Exception:
                            continue

                        action = entry.get("action", "UNKNOWN")
                        severity = "WARN" if "REJECT" in action or "ROLLBACK" in action else "INFO"
                        events.append(
                            ReplayEvent(
                                ts=ts_str,
                                ts_unix=ts_unix,
                                kind="APPROVAL",
                                title=f"[Audit] {action}",
                                detail=json.dumps(entry, ensure_ascii=False)[:300],
                                actor=entry.get("actor", "system"),
                                severity=severity,
                                metadata=entry,
                            )
                        )
            except Exception as exc:
                log.debug("audit file 파싱 실패 %s: %s", audit_file, exc)
        return events

    def _collect_incident_events(
        self, deploy_id: str, window_hours: int
    ) -> List[ReplayEvent]:
        """Watchdog incident.jsonl에서 인시던트 이벤트를 수집한다."""
        events: List[ReplayEvent] = []
        incident_files = list(RECODER_HOME.glob("**/incident*.jsonl")) + \
                         list(RECODER_HOME.glob("incident*.jsonl"))
        for inc_file in incident_files:
            try:
                with open(inc_file, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        ts_str = (
                            entry.get("detected_at")
                            or entry.get("ts")
                            or entry.get("timestamp", "")
                        )
                        try:
                            ts_unix = datetime.fromisoformat(ts_str).timestamp()
                        except Exception:
                            continue

                        severity_raw = entry.get("severity", "INFO")
                        events.append(
                            ReplayEvent(
                                ts=ts_str,
                                ts_unix=ts_unix,
                                kind="INCIDENT",
                                title=entry.get("title") or entry.get("message", "인시던트"),
                                detail=json.dumps(entry, ensure_ascii=False)[:500],
                                actor="watchdog",
                                severity=severity_raw,
                                metadata=entry,
                            )
                        )
            except Exception as exc:
                log.debug("incident file 파싱 실패 %s: %s", inc_file, exc)
        return events

    def _collect_git_events(self) -> List[ReplayEvent]:
        """Git 커밋 로그에서 최근 20개 커밋 이벤트를 수집한다."""
        events: List[ReplayEvent] = []
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--format=%H %ai %s", "-20"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return events
            for line in result.stdout.strip().splitlines():
                parts = line.split(" ", 3)
                if len(parts) < 3:
                    continue
                commit_hash, ts_str, *rest = parts
                message = " ".join(rest) if rest else ""
                try:
                    ts_unix = datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    continue
                events.append(
                    ReplayEvent(
                        ts=ts_str,
                        ts_unix=ts_unix,
                        kind="GIT_COMMIT",
                        title=f"commit {commit_hash[:7]}: {message[:60]}",
                        detail=f"SHA: {commit_hash}\n{message}",
                        actor="git",
                        severity="INFO",
                        metadata={"hash": commit_hash, "message": message},
                    )
                )
        except Exception as exc:
            log.debug("git log 수집 실패: %s", exc)
        return events

    async def _collect_otel_events(
        self, service: str, window_hours: int
    ) -> List[ReplayEvent]:
        """OTel에서 메트릭 스파이크를 수집한다 (선택적)."""
        events: List[ReplayEvent] = []
        if not self._otel:
            return events
        try:
            spikes = await self._otel.query_metric_spikes(
                service=service, window_hours=window_hours
            )
            for spike in spikes:
                ts_str = spike.get("ts", "")
                try:
                    ts_unix = datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    continue
                events.append(
                    ReplayEvent(
                        ts=ts_str,
                        ts_unix=ts_unix,
                        kind="METRIC_SPIKE",
                        title=f"[OTel] {spike.get('metric', 'unknown')} 스파이크",
                        detail=f"값: {spike.get('value')}\n임계값: {spike.get('threshold')}",
                        actor="otel",
                        severity="WARN",
                        metadata=spike,
                    )
                )
        except Exception as exc:
            log.debug("OTel metric spikes 수집 실패: %s", exc)
        return events

    # ── §38.4 Postmortem 자동 생성 ────────────────────────────────────────

    def _generate_postmortem(self, timeline: ReplayTimeline) -> str:
        """
        타임라인에서 Postmortem Markdown 문서를 자동 생성한다 (§38.4).

        학습/포트폴리오/팀 공유 목적으로 사용.
        """
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        incidents = [e for e in timeline.events if e.kind == "INCIDENT"]
        deployments = [e for e in timeline.events if e.kind in ("DEPLOY_START", "APPROVAL")]
        rollbacks = [e for e in timeline.events if "ROLLBACK" in e.kind.upper()]

        severity = "CRITICAL" if any(e.severity == "CRITICAL" for e in incidents) else \
                   "HIGH" if incidents else "LOW"

        timeline_md = "\n".join(
            f"| `{e.ts[:19]}` | `{e.kind}` | {e.title} |"
            for e in timeline.events[:20]
        )

        postmortem = f"""# Postmortem — {timeline.service} ({now})

> **자동 생성**: ReCoder Deploy Replay §38.4
> **Deploy ID**: `{timeline.deploy_id}`
> **심각도**: `{severity}`

## 개요

| 항목 | 내용 |
|------|------|
| 서비스 | `{timeline.service}` |
| 클러스터 | `{timeline.cluster}` |
| 리전 | `{timeline.region}` |
| 시작 | `{timeline.start_ts[:19]}` |
| 종료 | `{timeline.end_ts[:19] if timeline.end_ts else 'N/A'}` |
| 총 소요 | `{timeline.duration_seconds:.0f}초` |

## 타임라인

| 시각 | 이벤트 | 내용 |
|------|--------|------|
{timeline_md}

## 인시던트 목록

{"인시던트 없음" if not incidents else chr(10).join(f"- [{e.severity}] {e.title}" for e in incidents)}

## 배포 이벤트

{chr(10).join(f"- {e.ts[:19]}: {e.title}" for e in deployments) or "배포 기록 없음"}

## 롤백

{chr(10).join(f"- {e.ts[:19]}: {e.title}" for e in rollbacks) or "롤백 없음"}

## 근본 원인

{timeline.root_cause or "분석 중 — `/recoder rca`로 자동 RCA를 실행하세요."}

## 재발 방지

{timeline.prevention or "조치 검토 중"}

## 학습 포인트

- OTel 연결 상태: `{"연결됨" if timeline.otel_available else "미연결 — skeleton 모드"}`
- 이 Postmortem은 Deploy Replay에서 재생 가능합니다.
- `/recoder replay deploy-id:{timeline.deploy_id}` 로 상황을 다시 볼 수 있습니다.

---
*ReCoder Deploy Replay §38.4 — 자동 생성 문서*
"""
        return postmortem
