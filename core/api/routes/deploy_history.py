"""Saved deployment history and explicit approval of existing rollback targets."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.routes import deploy, deploy_ecs, ecs
import local_deploy_store
from schemas import DeployMethod, DeployStatus

router = APIRouter(prefix="/api/deploy/history", tags=["deployment-history"])


class HistoryEvent(BaseModel):
    at: str = ""
    title: str
    detail: str = ""


class HistoryRollback(BaseModel):
    available: bool = False
    reason: str = ""
    target: str = ""
    approval_level: int = 2


class HistoryEntry(BaseModel):
    key: str
    source: Literal["local", "ecs"]
    deployment_id: str
    project_id: str = ""
    target: str
    region: str = ""
    status: str
    status_text: str
    started_at: str
    image: str = ""
    service_url: str = ""
    error: str = ""
    remedy: str = ""
    warnings: list[str] = Field(default_factory=list)
    events: list[HistoryEvent] = Field(default_factory=list)
    rollback: HistoryRollback = Field(default_factory=HistoryRollback)


class HistoryResponse(BaseModel):
    entries: list[HistoryEntry]
    total: int
    warnings: list[str] = Field(default_factory=list)


class HistoryApproval(BaseModel):
    approved: bool


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _local_entry(record, latest_ids: set[str]) -> HistoryEntry:
    reason = ""
    if record.method != DeployMethod.LOCAL_DOCKER:
        reason = "이 화면에서는 로컬 Docker 롤백만 지원합니다."
    elif record.deployment_id not in latest_ids:
        reason = "이후 배포가 있습니다. 같은 컨테이너의 최신 기록을 선택하세요."
    elif record.status == DeployStatus.ROLLED_BACK or record.rollback_status == "succeeded":
        reason = "이미 롤백한 배포입니다."
    elif record.rollback_status in {"running", "unknown"}:
        reason = "롤백이 진행 중이거나 결과가 미확인입니다. 컨테이너 상태를 먼저 확인하세요."
    elif not record.rollback_target:
        reason = "저장된 이전 이미지가 없습니다."
    elif not record.image_id:
        reason = "현재 이미지를 대조할 정보가 없는 기록입니다."
    events = [HistoryEvent(at=_iso(record.deployed_at), title="배포 기록", detail=record.image)]
    if record.rollback_status:
        labels = {"running": "롤백 진행 중", "succeeded": "롤백 실행 완료", "failed": "롤백 실패", "unknown": "롤백 결과 미확인"}
        events.append(HistoryEvent(at=_iso(record.rollback_completed_at), title=labels[record.rollback_status], detail=record.rollback_error or record.rollback_target or ""))
    elif record.status == DeployStatus.ROLLED_BACK:
        events.append(HistoryEvent(title="롤백 기록", detail="이전 기록에는 롤백 시각이 저장되지 않았습니다."))
    port = next(iter(record.ports), "")
    labels = {"success": "실행 완료", "failed": "실패", "rolled_back": "롤백됨", "pending": "대기", "in_progress": "배포 중", "cancelled": "취소됨"}
    return HistoryEntry(
        key=f"local:{record.deployment_id}", source="local", deployment_id=record.deployment_id,
        project_id=record.project_id, target=record.container_name, status=record.status.value,
        status_text=labels.get(record.status.value, record.status.value), started_at=_iso(record.deployed_at),
        image=record.image, service_url=f"http://localhost:{port}" if port.isdigit() and 1 <= int(port) <= 65535 else "",
        error=record.rollback_error or "",
        warnings=["배포 기록 당시 헬스 확인을 통과했습니다." if record.rollback_eligible else "이 기록만으로 현재 앱의 정상 동작을 확인할 수 없습니다."],
        events=events, rollback=HistoryRollback(available=not reason, reason=reason, target=record.rollback_target or ""),
    )


def _ecs_entry(record) -> HistoryEntry:
    status = deploy_ecs.to_status_response(record)
    proposal = status.rollback_proposal
    available = bool(proposal and proposal.status in {"pending", "failed"})
    reason = ""
    if not available:
        reason = "승인 대기 중인 롤백 제안이 없습니다."
    elif any(r.deployment_id != record.deployment_id and r.cluster == record.cluster and r.service == record.service and r.region == record.region and local_deploy_store.timestamp(r.started_at) >= local_deploy_store.timestamp(record.started_at) for r in ecs._deploy_records.values()):
        available = False
        reason = "이후 배포가 있습니다. 최신 배포의 롤백 제안을 확인하세요."
    events = [HistoryEvent(at=_iso(record.started_at), title="배포 시작", detail=record.image_uri or record.image or "")]
    for step in record.progress_steps:
        if step.started_at:
            events.append(HistoryEvent(at=_iso(step.started_at), title=step.label, detail=step.status))
    if record.completed_at:
        events.append(HistoryEvent(at=_iso(record.completed_at), title="배포 처리 종료", detail=record.error_detail or ""))
    if record.rollback_proposal_id:
        roll_status = record.rollback_proposal_status or "pending"
        labels = {"pending": "롤백 승인 대기", "approving": "롤백 승인 처리 중", "completed": "롤백 요청 완료", "ignored": "롤백 제안 무시", "failed": "롤백 요청 실패", "superseded": "이후 배포로 롤백 제안 만료"}
        events.append(HistoryEvent(at=_iso(record.rollback_completed_at), title=labels.get(roll_status, roll_status), detail=record.previous_task_definition_arn or ""))
    return HistoryEntry(
        key=f"ecs:{record.deployment_id}", source="ecs", deployment_id=record.deployment_id,
        project_id=record.project_id or "", target=f"{record.cluster or ''} / {record.service or ''}", region=record.region or "",
        status=record.status.value, status_text=status.stage_text, started_at=_iso(record.started_at),
        image=record.image_uri or record.image or "", service_url=record.service_url or "",
        error=record.error_message or "", remedy=record.error_remedy or "", warnings=status.warnings,
        events=events, rollback=HistoryRollback(available=available, reason=reason, target=record.previous_task_definition_arn or "", approval_level=3),
    )


@router.get("", response_model=HistoryResponse)
async def list_history(source: Literal["all", "local", "ecs"] = "all", limit: int = Query(200, ge=1, le=500)) -> HistoryResponse:
    entries = []
    if source in {"all", "local"}:
        records = [r for r in deploy._records_newest_first() if r.method == DeployMethod.LOCAL_DOCKER]
        seen = set()
        latest_ids = set()
        for record in records:
            if record.container_name not in seen:
                seen.add(record.container_name)
                latest_ids.add(record.deployment_id)
        entries.extend(_local_entry(r, latest_ids) for r in records)
    if source in {"all", "ecs"}:
        entries.extend(_ecs_entry(r) for r in ecs._deploy_records.values())
    entries.sort(key=lambda row: datetime.fromisoformat(row.started_at).timestamp(), reverse=True)
    warnings = []
    if local_deploy_store.load_incomplete:
        warnings.append("일부 로컬 배포 기록을 읽지 못했습니다. 표시된 목록이 전체 이력이 아닐 수 있습니다.")
    if local_deploy_store.save_failed:
        warnings.append("최근 로컬 배포 기록 저장에 실패했습니다. Core를 종료하기 전에 로그를 확인하세요.")
    return HistoryResponse(entries=entries[:limit], total=len(entries), warnings=warnings)


@router.post("/{source}/{deployment_id}/rollback")
async def approve_history_rollback(source: Literal["local", "ecs"], deployment_id: str, body: HistoryApproval) -> dict:
    if not body.approved:
        raise HTTPException(status_code=400, detail="실행 대상 확인과 사용자 승인이 필요합니다.")
    if source == "local":
        record = deploy._deployment_records.get(deployment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="배포 기록을 찾을 수 없습니다.")
        latest = next((r for r in deploy._records_newest_first() if r.container_name == record.container_name), None)
        entry = _local_entry(record, {latest.deployment_id} if latest else set())
        if not entry.rollback.available:
            raise HTTPException(status_code=409, detail=entry.rollback.reason)
        result = await deploy.rollback(deploy.RollbackRequest(deployment_id=deployment_id, require_current=True))
        return {"status": result["status"], "message": "이전 이미지로 롤백했습니다." if result["status"] == "ok" else result.get("error") or result.get("warning") or "롤백에 실패했습니다. 컨테이너 로그를 확인하세요.", "warning": result.get("warning")}
    record = ecs._deploy_records.get(deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="배포 기록을 찾을 수 없습니다.")
    entry = _ecs_entry(record)
    if not entry.rollback.available:
        raise HTTPException(status_code=409, detail=entry.rollback.reason)
    result = await deploy_ecs.resolve_ecs_rollback(deploy_ecs.EcsRollbackRequest(proposal_id=record.rollback_proposal_id, approved=True))
    return result.model_dump()
