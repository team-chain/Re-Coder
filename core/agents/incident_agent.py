"""
Local Core — Q4: Incident Timeline + RCA 에이전트

설계서 §Q4-A (Must):
- 장애 이벤트 수집 → 타임라인 생성
- RCA: 원인 후보 제안 (confidence score 기반)
- Postmortem skeleton 마크다운 자동 생성
- LLM(Bedrock/Gemini) 활용 — confidence score는 LLM이 반환

쐐기 시나리오 7단계:
  1. 장애 감지 (Alert)
  2. 타임라인 구성
  3. RCA 실행
  4. rollback PR 생성
  5. 2인 승인
  6. ArgoCD sync
  7. Postmortem 생성
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.schemas import (
    IncidentRecord,
    IncidentSeverity,
    IncidentStatus,
    RCACandidate,
    TimelineEvent,
)

logger = logging.getLogger(__name__)

# Postmortem 파일 저장 디렉토리
_POSTMORTEM_DIR = Path.home() / ".recoder" / "postmortems"


class IncidentAgent:
    """
    장애 감지 → 타임라인 → RCA → Postmortem 파이프라인.
    """

    async def open_incident(
        self,
        project_id: str,
        title: str,
        severity: IncidentSeverity,
        initial_events: Optional[list[dict]] = None,
        created_by: str = "system",
    ) -> IncidentRecord:
        """장애 레코드를 생성하고 초기 이벤트를 타임라인에 추가한다."""
        record = IncidentRecord(
            project_id=project_id,
            title=title,
            severity=severity,
            created_by=created_by,
        )

        if initial_events:
            for ev in initial_events:
                record.timeline.append(
                    TimelineEvent(
                        occurred_at=ev.get("occurred_at", datetime.now(timezone.utc)),
                        source=ev.get("source", "user"),
                        title=ev.get("title", ""),
                        description=ev.get("description", ""),
                        related_deployment_id=ev.get("related_deployment_id"),
                        related_commit_sha=ev.get("related_commit_sha"),
                        metadata=ev.get("metadata", {}),
                    )
                )

        logger.info(
            "Incident opened: id=%s severity=%s title=%s",
            record.incident_id, severity, title,
        )
        return record

    async def add_timeline_event(
        self,
        record: IncidentRecord,
        source: str,
        title: str,
        description: str,
        occurred_at: Optional[datetime] = None,
        related_deployment_id: Optional[str] = None,
        related_commit_sha: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> IncidentRecord:
        """타임라인에 이벤트를 추가한다."""
        event = TimelineEvent(
            occurred_at=occurred_at or datetime.now(timezone.utc),
            source=source,
            title=title,
            description=description,
            severity=record.severity,
            related_deployment_id=related_deployment_id,
            related_commit_sha=related_commit_sha,
            metadata=metadata or {},
        )
        record.timeline.append(event)
        record.timeline.sort(key=lambda e: e.occurred_at)
        logger.debug("Timeline event added: %s", title)
        return record

    async def run_rca(
        self,
        record: IncidentRecord,
        llm_client=None,  # core.llm_router.LLMRouter 인스턴스 (선택)
    ) -> IncidentRecord:
        """
        RCA 실행.

        LLM이 있으면 LLM 기반 RCA, 없으면 규칙 기반 휴리스틱.
        confidence score 0.0~1.0 반환.
        """
        if llm_client is not None:
            record = await self._rca_with_llm(record, llm_client)
        else:
            record = await self._rca_heuristic(record)

        # confidence 내림차순 정렬
        record.rca_candidates.sort(key=lambda c: c.confidence, reverse=True)
        logger.info(
            "RCA completed: incident=%s candidates=%d top_confidence=%.2f",
            record.incident_id,
            len(record.rca_candidates),
            record.rca_candidates[0].confidence if record.rca_candidates else 0.0,
        )
        return record

    async def generate_postmortem(self, record: IncidentRecord) -> str:
        """
        Postmortem 마크다운 skeleton 생성.

        반환: 저장된 파일 경로
        """
        _POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"postmortem-{record.incident_id[:8]}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
        filepath = _POSTMORTEM_DIR / filename

        content = self._render_postmortem(record)
        filepath.write_text(content, encoding="utf-8")
        record.postmortem_path = str(filepath)

        logger.info("Postmortem generated: %s", filepath)
        return str(filepath)

    async def resolve_incident(self, record: IncidentRecord) -> IncidentRecord:
        """장애를 RESOLVED 상태로 전환한다."""
        record.status = IncidentStatus.RESOLVED
        record.resolved_at = datetime.now(timezone.utc)
        logger.info(
            "Incident resolved: id=%s duration=%s",
            record.incident_id,
            record.resolved_at - record.detected_at,
        )
        return record

    # ------------------------------------------------------------------
    # RCA 내부 구현
    # ------------------------------------------------------------------

    async def _rca_with_llm(self, record: IncidentRecord, llm_client) -> IncidentRecord:
        """LLM 기반 RCA — timeline을 요약해 원인 후보 생성."""
        timeline_text = "\n".join(
            f"[{e.occurred_at.isoformat()}] [{e.source}] {e.title}: {e.description}"
            for e in record.timeline
        )
        prompt = f"""You are an SRE expert performing a Root Cause Analysis.

Incident: {record.title}
Severity: {record.severity}
Timeline:
{timeline_text}

Identify the top 3 root cause candidates. For each, provide:
1. A concise title
2. A detailed description
3. A confidence score (0.0 to 1.0)
4. Key evidence from the timeline
5. A suggested fix

Respond in JSON format:
{{
  "candidates": [
    {{
      "title": "...",
      "description": "...",
      "confidence_score": 0.85,
      "evidence": ["...", "..."],
      "suggested_fix": "..."
    }}
  ]
}}"""

        try:
            response = await llm_client.complete(prompt)
            data = json.loads(response)
            for c in data.get("candidates", []):
                description = c.get("description")
                evidence = ([description] if description else []) + list(c.get("evidence", []))
                record.rca_candidates.append(
                    RCACandidate(
                        hypothesis=c["title"],
                        evidence=evidence,
                        confidence=float(c.get("confidence_score", 0.5)),
                        rollback_hint=c.get("suggested_fix"),
                    )
                )
        except Exception as exc:
            logger.warning("LLM RCA failed, falling back to heuristic: %s", exc)
            record = await self._rca_heuristic(record)

        return record

    async def _rca_heuristic(self, record: IncidentRecord) -> IncidentRecord:
        """규칙 기반 RCA 휴리스틱 (LLM 없을 때)."""
        # 최근 배포 이벤트가 있으면 배포 관련 원인 제안
        deploy_events = [e for e in record.timeline if e.related_deployment_id]
        if deploy_events:
            latest_deploy = deploy_events[-1]
            record.rca_candidates.append(
                RCACandidate(
                    hypothesis="최근 배포와의 상관관계",
                    confidence=0.75,
                    evidence=[
                        (
                            f"장애 직전 배포({latest_deploy.related_deployment_id})가 "
                            f"발생했습니다. 코드 변경 또는 설정 변경이 원인일 가능성이 높습니다."
                        ),
                        f"배포 ID: {latest_deploy.related_deployment_id}",
                        f"배포 시각: {latest_deploy.occurred_at.isoformat()}",
                    ],
                    rollback_hint="배포를 롤백하고 변경 사항을 코드 리뷰합니다",
                )
            )

        # commit SHA가 있으면 코드 변경 원인 제안
        commit_events = [e for e in record.timeline if e.related_commit_sha]
        if commit_events:
            latest_commit = commit_events[-1]
            record.rca_candidates.append(
                RCACandidate(
                    hypothesis="코드 변경 원인 추정",
                    confidence=0.60,
                    evidence=[
                        (
                            f"커밋 {latest_commit.related_commit_sha[:8]}이 "
                            f"장애 전후로 배포됐습니다."
                        ),
                        f"커밋 SHA: {latest_commit.related_commit_sha}",
                    ],
                    rollback_hint="해당 커밋을 git revert하고 PR을 생성합니다 (ADR-005)",
                )
            )

        # 기본 후보 (항상 추가)
        record.rca_candidates.append(
            RCACandidate(
                hypothesis="외부 의존성 장애",
                confidence=0.30,
                evidence=[
                    "데이터베이스, 캐시, 외부 API 등 의존성 서비스 장애 가능성",
                    "타임라인 이벤트 기반 추정",
                ],
                rollback_hint="의존성 서비스 상태를 확인하고 서킷 브레이커 설정을 검토합니다",
            )
        )

        return record

    # ------------------------------------------------------------------
    # Postmortem 렌더링
    # ------------------------------------------------------------------

    def _render_postmortem(self, record: IncidentRecord) -> str:
        duration = ""
        if record.resolved_at:
            delta = record.resolved_at - record.detected_at
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            duration = f"{hours}시간 {minutes}분"

        timeline_md = "\n".join(
            f"- `{e.occurred_at.strftime('%Y-%m-%d %H:%M:%S UTC')}` **[{e.source}]** {e.title}  \n"
            f"  {e.description}"
            for e in record.timeline
        )

        rca_md = "\n\n".join(
            f"### {i+1}. {c.hypothesis} (confidence: {c.confidence:.0%})\n\n"
            f"**근거**:\n" + "\n".join(f"- {ev}" for ev in c.evidence) +
            (f"\n\n**권장 조치**: {c.rollback_hint}" if c.rollback_hint else "")
            for i, c in enumerate(record.rca_candidates)
        ) if record.rca_candidates else "_RCA 결과 없음_"

        return f"""# Postmortem — {record.title}

> **생성일**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
> **인시던트 ID**: `{record.incident_id}`
> **Severity**: {record.severity.upper()}
> **상태**: {record.status}

---

## 요약

| 항목 | 값 |
|------|-----|
| 제목 | {record.title} |
| 심각도 | {record.severity.upper()} |
| 감지 시각 | {record.detected_at.strftime('%Y-%m-%d %H:%M UTC')} |
| 해소 시각 | {record.resolved_at.strftime('%Y-%m-%d %H:%M UTC') if record.resolved_at else '미해결'} |
| 장애 지속 시간 | {duration or '측정 중'} |
| 프로젝트 | {record.project_id} |

---

## 영향 범위

> _[작성 필요] 영향을 받은 사용자 수, 기능, 지역을 기술하세요._

---

## 장애 타임라인

{timeline_md or '_이벤트 없음_'}

---

## 근본 원인 분석 (RCA)

{rca_md}

---

## 대응 과정

> _[작성 필요] 장애 대응 과정을 단계별로 기술하세요._

---

## 재발 방지 대책

| 우선순위 | 조치 항목 | 담당자 | 기한 |
|----------|-----------|--------|------|
| P1 | _[작성 필요]_ | | |
| P2 | _[작성 필요]_ | | |
| P3 | _[작성 필요]_ | | |

---

## 교훈

> _[작성 필요] 이번 장애에서 얻은 교훈을 기술하세요._

---

_이 문서는 ReCoder가 자동으로 생성한 Postmortem skeleton입니다. 내용을 검토하고 보완하세요._
"""


# 모듈 레벨 싱글톤
incident_agent = IncidentAgent()
