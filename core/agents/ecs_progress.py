"""Recorded ECS execution stages; no inferred progress for historical records."""
from datetime import datetime, timezone

from core.schemas import ECSDeployRecord, ECSDeployRequest, ECSDeployStatus, ECSDeployStep

STEPS = (
    ("preflight", "사전 점검"), ("provisioning", "인프라 확인·준비"),
    ("source_scan", "소스·Dockerfile 검사"), ("building", "이미지 빌드"),
    ("ecr_push", "ECR 업로드"), ("image_scan", "이미지 취약점 검사"),
    ("sbom", "SBOM 생성"), ("task_def", "태스크 정의 등록"),
    ("svc_update", "서비스 갱신"), ("stabilizing", "서비스 안정화·헬스체크"),
    ("url_check", "공개 주소 확인"),
)


def initialize_progress(record: ECSDeployRecord, request: ECSDeployRequest) -> None:
    skipped = set()
    if not request.workspace_path:
        skipped.update(("building", "ecr_push"))
    if not request.run_security_scan:
        skipped.update(("source_scan", "image_scan"))
    if not request.generate_sbom:
        skipped.add("sbom")
    if request.desired_count == 0:
        skipped.update(("stabilizing", "url_check"))
    if request.url_wait_timeout <= 0:
        skipped.add("url_check")
    record.progress_steps = [ECSDeployStep(key=key, label=label,
        status="skipped" if key in skipped else "pending") for key, label in STEPS]


def advance_progress(record: ECSDeployRecord, key: str) -> None:
    step = next((s for s in record.progress_steps if s.key == key), None)
    if step is None or step.status != "pending":
        return
    now = datetime.now(timezone.utc)
    for previous in record.progress_steps:
        if previous.status == "running":
            previous.status = "done"
            previous.finished_at = now
    step.status = "running"
    step.started_at = now


def finish_progress(record: ECSDeployRecord) -> None:
    """Leave never-reached steps pending; cancellation/failure isn't success."""
    now = record.completed_at or datetime.now(timezone.utc)
    for step in record.progress_steps:
        if step.status != "running":
            continue
        if record.status == ECSDeployStatus.SUCCEEDED:
            step.status = "warning" if step.key == "url_check" and record.provisioned.get("url_warning") else "done"
        elif record.status == ECSDeployStatus.CANCELLED:
            step.status = "cancelled"
        else:
            step.status = "failed"
        step.finished_at = now
