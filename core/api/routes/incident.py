"""
Local Core — Q4: Incident Timeline + RCA API Routes

엔드포인트:
- POST /incident/open            : 장애 등록
- GET  /incident/{id}            : 장애 상태 조회
- POST /incident/{id}/event      : 타임라인 이벤트 추가
- POST /incident/{id}/rca        : RCA 실행
- POST /incident/{id}/postmortem : Postmortem 생성
- POST /incident/{id}/resolve    : 장애 해소
- GET  /incident/list            : 장애 목록
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.agents.incident_agent import incident_agent
from core.schemas import (
    IncidentRecord,
    IncidentSeverity,
    IncidentStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/incident", tags=["incident"])

# 인메모리 레코드 저장소
_incidents: Dict[str, IncidentRecord] = {}


# ---------------------------------------------------------------------------
# Request 모델
# ---------------------------------------------------------------------------

class OpenIncidentRequest(BaseModel):
    project_id: str
    title: str
    severity: IncidentSeverity
    created_by: str = "user"
    initial_events: Optional[List[dict]] = None


class AddTimelineEventRequest(BaseModel):
    source: str                       # "alert" | "user" | "deployment" | "audit_log"
    title: str
    description: str
    occurred_at: Optional[datetime] = None
    related_deployment_id: Optional[str] = None
    related_commit_sha: Optional[str] = None
    metadata: Optional[dict] = None


class RunRCARequest(BaseModel):
    use_llm: bool = False             # LLM RCA 사용 여부 (기본: 휴리스틱)


# ---------------------------------------------------------------------------
# 장애 등록
# ---------------------------------------------------------------------------

@router.post("/open", response_model=IncidentRecord, status_code=201)
async def open_incident(request: OpenIncidentRequest) -> IncidentRecord:
    """
    장애를 등록하고 인시던트 레코드를 생성합니다.

    쐐기 시나리오 Step 1: 장애 감지 → 인시던트 등록
    """
    record = await incident_agent.open_incident(
        project_id=request.project_id,
        title=request.title,
        severity=request.severity,
        initial_events=request.initial_events,
        created_by=request.created_by,
    )
    _incidents[record.incident_id] = record

    # OTel 메트릭 기록
    try:
        from core.observability import observability
        observability.record_incident(request.project_id, request.severity.value)
    except Exception:
        pass

    logger.info(
        "Incident opened: id=%s severity=%s title=%s",
        record.incident_id, request.severity, request.title,
    )
    return record


# ---------------------------------------------------------------------------
# 장애 조회
# ---------------------------------------------------------------------------

@router.get("/{incident_id}", response_model=IncidentRecord)
async def get_incident(incident_id: str) -> IncidentRecord:
    """장애 상태와 타임라인을 조회합니다."""
    record = _incidents.get(incident_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "incident_id": incident_id},
        )
    return record


@router.get("/list/all", response_model=List[IncidentRecord])
async def list_incidents(
    project_id: Optional[str] = Query(None),
    status: Optional[IncidentStatus] = Query(None),
    severity: Optional[IncidentSeverity] = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> List[IncidentRecord]:
    """장애 목록을 반환합니다."""
    records = list(_incidents.values())
    if project_id:
        records = [r for r in records if r.project_id == project_id]
    if status:
        records = [r for r in records if r.status == status]
    if severity:
        records = [r for r in records if r.severity == severity]
    records.sort(key=lambda r: r.detected_at, reverse=True)
    return records[:limit]


# ---------------------------------------------------------------------------
# 타임라인 이벤트 추가
# ---------------------------------------------------------------------------

@router.post("/{incident_id}/event", response_model=IncidentRecord)
async def add_timeline_event(
    incident_id: str,
    request: AddTimelineEventRequest,
) -> IncidentRecord:
    """
    인시던트 타임라인에 이벤트를 추가합니다.

    쐐기 시나리오 Step 2: 타임라인 구성
    """
    record = _incidents.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    if record.status == IncidentStatus.RESOLVED:
        raise HTTPException(
            status_code=409,
            detail={"error": "incident_resolved", "message": "해소된 장애에는 이벤트를 추가할 수 없습니다"},
        )

    record = await incident_agent.add_timeline_event(
        record=record,
        source=request.source,
        title=request.title,
        description=request.description,
        occurred_at=request.occurred_at,
        related_deployment_id=request.related_deployment_id,
        related_commit_sha=request.related_commit_sha,
        metadata=request.metadata,
    )
    _incidents[incident_id] = record
    return record


# ---------------------------------------------------------------------------
# RCA 실행
# ---------------------------------------------------------------------------

@router.post("/{incident_id}/rca", response_model=IncidentRecord)
async def run_rca(
    incident_id: str,
    request: RunRCARequest,
) -> IncidentRecord:
    """
    RCA(근본 원인 분석)를 실행합니다.

    쐐기 시나리오 Step 3: RCA 실행
    use_llm=true 시 LLM 기반 분석, false 시 휴리스틱 분석.
    confidence score 기반 후보 목록 반환.
    """
    record = _incidents.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    if not record.timeline:
        raise HTTPException(
            status_code=422,
            detail={"error": "empty_timeline", "message": "타임라인 이벤트가 없으면 RCA를 실행할 수 없습니다"},
        )

    # LLM 클라이언트 (선택)
    llm_client = None
    if request.use_llm:
        try:
            from core.llm_router import LLMRouter
            llm_client = LLMRouter()
        except Exception as exc:
            logger.warning("LLM 클라이언트 초기화 실패, 휴리스틱으로 대체: %s", exc)

    record = await incident_agent.run_rca(record, llm_client=llm_client)
    _incidents[incident_id] = record

    return record


# ---------------------------------------------------------------------------
# Postmortem 생성
# ---------------------------------------------------------------------------

@router.post("/{incident_id}/postmortem")
async def generate_postmortem(incident_id: str) -> dict:
    """
    Postmortem 마크다운 skeleton을 생성하고 파일 경로를 반환합니다.

    쐐기 시나리오 Step 7: Postmortem 생성
    """
    record = _incidents.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    filepath = await incident_agent.generate_postmortem(record)
    _incidents[incident_id] = record

    return {
        "incident_id": incident_id,
        "postmortem_path": filepath,
        "message": "Postmortem skeleton이 생성됐습니다. 내용을 검토하고 보완하세요.",
    }


# ---------------------------------------------------------------------------
# 장애 해소
# ---------------------------------------------------------------------------

@router.post("/{incident_id}/resolve", response_model=IncidentRecord)
async def resolve_incident(incident_id: str) -> IncidentRecord:
    """장애를 RESOLVED 상태로 전환합니다."""
    record = _incidents.get(incident_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    if record.status == IncidentStatus.RESOLVED:
        raise HTTPException(
            status_code=409,
            detail={"error": "already_resolved"},
        )

    record = await incident_agent.resolve_incident(record)
    _incidents[incident_id] = record

    logger.info("Incident resolved: id=%s", incident_id)
    return record


# ---------------------------------------------------------------------------
# 관측성 메트릭
# ---------------------------------------------------------------------------

@router.get("/metrics/summary")
async def get_observability_metrics() -> dict:
    """OTel 메트릭 스냅샷을 반환합니다."""
    try:
        from core.observability import observability
        metrics = observability.get_metrics_snapshot()
        return {
            "metrics": [m.model_dump() for m in metrics],
            "otel_available": True,
        }
    except Exception as exc:
        return {"error": str(exc), "otel_available": False}
