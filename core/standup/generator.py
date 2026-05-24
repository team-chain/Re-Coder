"""
core/standup/generator.py — Daily Standup 매일 아침 운영 브리핑 생성기 (설계서 §39)

§39 Daily Standup:
  - 매일 아침(기본 09:00) 자동으로 전날 배포·인시던트·LLM 비용을 요약
  - claude-haiku-4-5 (Haiku)를 사용하여 한국어 간결 요약 생성
  - Discord Bot / VSCode WebView 양쪽에서 소비
  - OTel 미연결 시에도 SQLite 기반 skeleton 생성

출력: StandupReport — Discord embed 및 WebView 카드 형식.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

RECODER_HOME = Path.home() / ".recoder"
DB_PATH = RECODER_HOME / "sessions.db"


# ── 데이터 모델 ────────────────────────────────────────────────────────────

@dataclass
class DeployBrief:
    """단일 배포 요약."""
    service: str
    cluster: str
    deployed_at: str
    success: bool
    image_tag: str = ""
    duration_seconds: float = 0.0
    llm_cost_usd: float = 0.0


@dataclass
class IncidentBrief:
    """단일 인시던트 요약."""
    id: str
    title: str
    severity: str
    detected_at: str
    resolved: bool = False
    resolution_minutes: float = 0.0


@dataclass
class StandupReport:
    """Daily Standup 전체 리포트."""
    date: str                              # YYYY-MM-DD
    generated_at: str                      # ISO-8601
    summary: str                           # Haiku 자동 요약 (한국어 2~3문장)
    yesterday: List[str] = field(default_factory=list)   # 어제 완료 항목
    today: List[str] = field(default_factory=list)       # 오늘 예정 항목
    blockers: List[str] = field(default_factory=list)    # 블로커
    deploys: List[DeployBrief] = field(default_factory=list)
    incidents: List[IncidentBrief] = field(default_factory=list)
    total_llm_cost_usd: float = 0.0
    total_deploys: int = 0
    total_incidents: int = 0
    otel_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ── 생성기 ────────────────────────────────────────────────────────────────

class StandupGenerator:
    """
    전날 활동을 분석하여 Standup 리포트를 생성한다 (§39).

    Haiku 요약 실패 시 rule-based fallback으로 동작.
    """

    def __init__(self) -> None:
        self._db_path = DB_PATH
        self._haiku: Optional[Any] = None
        self._try_init_haiku()

    def _try_init_haiku(self) -> None:
        """Anthropic claude-haiku-4-5 클라이언트 초기화 (선택적)."""
        try:
            import anthropic
            import os
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                self._haiku = anthropic.AsyncAnthropic(api_key=api_key)
                log.info("Haiku 클라이언트 초기화 완료")
            else:
                log.debug("ANTHROPIC_API_KEY 미설정 — rule-based fallback 사용")
        except ImportError:
            log.debug("anthropic 패키지 미설치 — rule-based fallback 사용")

    async def generate(
        self,
        external_data: Optional[Dict[str, Any]] = None,
        hours: int = 24,
    ) -> StandupReport:
        """
        지난 `hours` 시간의 활동을 분석하여 StandupReport를 반환한다.

        external_data: Discord Bot이 Local Core /api/session/history 에서 가져온 데이터.
                       None이면 SQLite를 직접 조회한다.
        """
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(hours=hours)
        date_str = now.strftime("%Y-%m-%d")

        # 데이터 수집
        if external_data:
            deploys, incidents, cost = self._parse_external_data(external_data)
        else:
            deploys, incidents, cost = self._collect_from_sqlite(since)

        # 요약 항목 구성
        yesterday_items = self._build_yesterday_items(deploys, incidents)
        today_items = self._build_today_items(deploys, incidents)
        blockers = self._build_blockers(incidents)

        # Haiku 자동 요약
        raw_data_str = self._build_raw_summary(deploys, incidents, cost)
        summary = await self._haiku_summarize(raw_data_str)

        return StandupReport(
            date=date_str,
            generated_at=now.isoformat(),
            summary=summary,
            yesterday=yesterday_items,
            today=today_items,
            blockers=blockers,
            deploys=deploys,
            incidents=incidents,
            total_llm_cost_usd=cost,
            total_deploys=len(deploys),
            total_incidents=len(incidents),
            otel_available=False,
        )

    # ── 데이터 수집 ───────────────────────────────────────────────────────

    def _collect_from_sqlite(
        self, since: datetime
    ) -> tuple[List[DeployBrief], List[IncidentBrief], float]:
        """SQLite sessions.db에서 직접 데이터를 수집한다."""
        deploys: List[DeployBrief] = []
        incidents: List[IncidentBrief] = []
        total_cost = 0.0

        if not self._db_path.exists():
            log.warning("sessions.db 없음 — 빈 리포트 생성")
            return deploys, incidents, total_cost

        try:
            conn = sqlite3.connect(self._db_path)
            since_str = since.isoformat()

            # LLM 비용 합산
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE called_at >= ?",
                (since_str,),
            ).fetchone()
            if cost_row:
                total_cost = float(cost_row[0])

            # 세션(배포) 기록
            rows = conn.execute(
                "SELECT session_id, project_id, start_time, end_time FROM sessions "
                "WHERE start_time >= ? ORDER BY start_time DESC LIMIT 20",
                (since_str,),
            ).fetchall()
            for row in rows:
                session_id, project_id, start_time, end_time = row
                deploys.append(
                    DeployBrief(
                        service=project_id or "unknown",
                        cluster="default",
                        deployed_at=start_time,
                        success=end_time is not None,
                    )
                )

            conn.close()

        except Exception as exc:
            log.error("SQLite 조회 실패: %s", exc)

        # Incident JSONL 스캔
        incidents = self._scan_incident_jsonl(since)

        return deploys, incidents, total_cost

    def _scan_incident_jsonl(self, since: datetime) -> List[IncidentBrief]:
        """~/.recoder/**/*.jsonl에서 인시던트를 스캔한다."""
        incidents: List[IncidentBrief] = []
        for inc_file in RECODER_HOME.glob("**/incident*.jsonl"):
            try:
                with open(inc_file, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        ts_str = entry.get("detected_at") or entry.get("ts", "")
                        try:
                            ts = datetime.fromisoformat(ts_str)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts < since:
                                continue
                        except Exception:
                            continue

                        incidents.append(
                            IncidentBrief(
                                id=entry.get("id", "unknown")[:12],
                                title=entry.get("title") or entry.get("message", "인시던트"),
                                severity=entry.get("severity", "INFO"),
                                detected_at=ts_str,
                                resolved=bool(entry.get("resolved_at")),
                            )
                        )
            except Exception as exc:
                log.debug("incident file 파싱 실패 %s: %s", inc_file, exc)
        return incidents

    def _parse_external_data(
        self, data: Dict[str, Any]
    ) -> tuple[List[DeployBrief], List[IncidentBrief], float]:
        """Local Core API 응답에서 배포/인시던트/비용을 파싱한다."""
        deploys: List[DeployBrief] = []
        incidents: List[IncidentBrief] = []
        cost = float(data.get("total_cost_usd", 0))

        for d in data.get("deploys", []):
            deploys.append(
                DeployBrief(
                    service=d.get("service", "unknown"),
                    cluster=d.get("cluster", "unknown"),
                    deployed_at=d.get("deployed_at", ""),
                    success=d.get("success", True),
                    image_tag=d.get("image_tag", ""),
                    duration_seconds=float(d.get("duration_seconds", 0)),
                    llm_cost_usd=float(d.get("llm_cost_usd", 0)),
                )
            )

        for i in data.get("incidents", []):
            incidents.append(
                IncidentBrief(
                    id=i.get("id", "unknown"),
                    title=i.get("title", "인시던트"),
                    severity=i.get("severity", "INFO"),
                    detected_at=i.get("detected_at", ""),
                    resolved=i.get("resolved", False),
                    resolution_minutes=float(i.get("resolution_minutes", 0)),
                )
            )

        return deploys, incidents, cost

    # ── 요약 항목 구성 ─────────────────────────────────────────────────────

    def _build_yesterday_items(
        self, deploys: List[DeployBrief], incidents: List[IncidentBrief]
    ) -> List[str]:
        items: List[str] = []
        for d in deploys[:5]:
            status = "✅ 성공" if d.success else "❌ 실패"
            items.append(f"{status} 배포: {d.service} ({d.deployed_at[:10]})")
        resolved = [i for i in incidents if i.resolved]
        for inc in resolved[:3]:
            items.append(f"✅ 인시던트 해결: [{inc.severity}] {inc.title}")
        return items or ["어제 배포/인시던트 기록 없음"]

    def _build_today_items(
        self, deploys: List[DeployBrief], incidents: List[IncidentBrief]
    ) -> List[str]:
        items: List[str] = []
        failed = [d for d in deploys if not d.success]
        if failed:
            items.append(f"🔁 실패 배포 재시도: {', '.join(d.service for d in failed[:3])}")
        unresolved = [i for i in incidents if not i.resolved]
        if unresolved:
            items.append(
                f"🚨 미해결 인시던트 처리: {', '.join(i.title[:20] for i in unresolved[:3])}"
            )
        if not items:
            items.append("📊 운영 모니터링 — 특이사항 없음")
        return items

    def _build_blockers(self, incidents: List[IncidentBrief]) -> List[str]:
        critical = [i for i in incidents if i.severity in ("CRITICAL", "HIGH") and not i.resolved]
        if critical:
            return [f"🚨 미해결 {i.severity} 인시던트: {i.title}" for i in critical[:3]]
        return []

    def _build_raw_summary(
        self,
        deploys: List[DeployBrief],
        incidents: List[IncidentBrief],
        cost: float,
    ) -> str:
        """Haiku에 전달할 원시 데이터 요약 텍스트."""
        lines = [
            f"배포 {len(deploys)}건 (성공 {sum(1 for d in deploys if d.success)}건, 실패 {sum(1 for d in deploys if not d.success)}건)",
            f"인시던트 {len(incidents)}건 (미해결 {sum(1 for i in incidents if not i.resolved)}건)",
            f"LLM 총 비용: ${cost:.4f}",
        ]
        if deploys:
            lines.append(f"최근 배포 서비스: {', '.join(d.service for d in deploys[:3])}")
        if incidents:
            sev_list = [f"{i.severity} — {i.title[:30]}" for i in incidents[:3]]
            lines.append(f"주요 인시던트: {'; '.join(sev_list)}")
        return "\n".join(lines)

    # ── Haiku 자동 요약 ────────────────────────────────────────────────────

    async def _haiku_summarize(self, raw: str) -> str:
        """
        claude-haiku-4-5로 운영 브리핑 2~3문장 한국어 요약을 생성한다 (§39).

        API 실패 시 rule-based fallback을 반환한다.
        """
        if not self._haiku:
            return self._fallback_summary(raw)

        prompt = (
            "당신은 DevOps 팀의 Daily Standup 브리핑 도우미입니다.\n"
            "다음 운영 데이터를 바탕으로 팀이 오전 standup에서 공유할 내용을\n"
            "2~3문장의 간결한 한국어로 요약해주세요.\n\n"
            f"[운영 데이터]\n{raw}\n\n"
            "요약 (2~3문장, 이모지 1개 포함):"
        )

        try:
            response = await self._haiku.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            log.warning("Haiku 요약 실패: %s — fallback 사용", exc)
            return self._fallback_summary(raw)

    def _fallback_summary(self, raw: str) -> str:
        """Haiku 없이 rule-based로 생성하는 요약."""
        lines = raw.splitlines()
        return f"📊 " + " | ".join(lines[:3]) if lines else "오늘의 운영 브리핑입니다."
