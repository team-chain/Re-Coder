"""
ReCoder Core — 확장(Extension)용 ECS 배포 엔드포인트

FR-05-04. **이 파일은 404 하나를 고치려고 존재한다.**

확장은 `POST /api/deploy/ecs` 를 부른다(`extension/src/core/ApiClient.ts:828`,
`coreClient.ts:370`). 그런데 그 주소는 실행되지 않는 `core/server.py:1461`
에만 있었다. 실제로 도는 앱(`core/main.py`)에는 없어서 배포 버튼이 그대로
404 였다. 디스코드 봇의 `/api/ecs/deploy` 도 마찬가지였다.

왜 별도 파일인가 — 확장이 보내는 모양과 `ECSDeployRequest` 의 모양이 다르다.
확장은 `ecs_cluster`/`aws_region`/`repo_name` 같은 납작한 이름을 쓰고,
코어는 `cluster`/`region`/`ecr_repo` 를 쓴다. 그 번역을 라우트 본문에
섞으면 어느 쪽이 진짜 계약인지 알 수 없게 된다. 여기 몰아넣고 이 파일만
"확장 호환 계층"으로 읽히게 한다.

**확장 코드는 고치지 않는다.** 이미 배포된 확장이 있을 수 있고, 서버가
맞춰주는 편이 안전하다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

# `core.schemas` 로 맞춘다. 같은 파일을 `schemas` 로도 import 할 수 있는데
# 그러면 **서로 다른 모듈 객체**가 되어 enum 클래스도 갈라진다
# (isinstance 가 거짓이 된다). 협력 상대인 api/routes/ecs.py 가
# `core.schemas` 를 쓰므로 여기서도 같은 쪽을 본다.
from core.schemas import ECSDeployRecord, ECSDeployRequest, ECSDeployStatus
from core import aws_infra

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deploy-ecs"])


class _StaleRollbackProposalError(RuntimeError):
    """승인 대기 중 서비스가 다른 Task Definition으로 진행된 경우."""


# ---------------------------------------------------------------------------
# 확장이 보내는 모양
# ---------------------------------------------------------------------------


class ExtensionEcsDeployRequest(BaseModel):
    """`ApiClient.deployEcs()` 가 보내는 본문. 모든 필드가 선택이다.

    확장이 이미 이 모양으로 보내고 있으므로 이름을 바꾸지 않는다.
    """

    workspace_path: Optional[str] = None
    image_name: Optional[str] = None
    repo_name: Optional[str] = None
    tag: Optional[str] = None
    ecr_registry: Optional[str] = None
    ecs_cluster: Optional[str] = None
    ecs_service: Optional[str] = None
    aws_region: Optional[str] = None
    container_name: Optional[str] = None
    container_port: Optional[int] = None
    cpu: Optional[str] = None
    memory: Optional[str] = None
    task_family: Optional[str] = None
    environment: Optional[str] = None
    branch: Optional[str] = None
    skip_sbom: bool = False
    skip_opa: bool = False
    #: 0 으로 주면 서비스만 만들고 태스크는 안 띄운다(과금 0원).
    desired_count: Optional[int] = None


class EcsDeployStatusResponse(BaseModel):
    """`ApiClient.getEcsDeployStatus()` 가 기대하는 모양.

    필드 이름은 확장 쪽 타입 선언과 **글자 그대로** 맞춰야 한다.
    """

    running: bool = False
    #: 기계 토큰. 확장이 문자열 비교로 분기한다 — 번역 금지.
    stage: str = "idle"
    log_tail: list[str] = Field(default_factory=list)
    image_uri: str = ""
    task_def_arn: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    # 확장 타입에는 없지만 추가로 보낸다 — TS 는 여분 필드를 무시한다.
    # 카드 DoD 1번의 "URL 로 접속됨"을 사이드바가 바로 쓸 수 있게 한다.
    service_url: str = ""
    remedy: str = ""
    deployment_id: str = ""
    #: 사람이 읽을 단계 문구. `stage` 는 기계 토큰이므로 번역하지 않는다.
    stage_text: str = ""
    #: Health Check 실패 뒤 사용자 승인을 기다리는 ECS 롤백 제안.
    #: `None` 이면 승인할 롤백이 없다.
    rollback_proposal: Optional["EcsRollbackProposal"] = None


class EcsRollbackProposal(BaseModel):
    """ECS 이상 감지 후 사람이 검토하는 롤백 제안."""

    proposal_id: str
    deployment_id: str
    cluster: str
    service: str
    region: str
    reason: str
    previous_task_definition: str
    current_task_definition: str = ""
    approval_level: int = 3
    status: str = "pending"


class EcsRollbackRequest(BaseModel):
    """`approved=True`일 때만 실제 ECS 서비스 갱신을 허용한다."""

    proposal_id: str = Field(min_length=1)
    approved: bool


class EcsRollbackResponse(BaseModel):
    status: str
    message: str
    deployment_id: str
    proposal_id: str
    adr: dict[str, str]


# `EcsDeployStatusResponse` 가 아래 클래스의 forward reference 를 가진다.
EcsDeployStatusResponse.model_rebuild()


class EcsReadyResponse(BaseModel):
    ready: bool = False
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 번역
# ---------------------------------------------------------------------------

#: 확장이 이름을 안 줬을 때 쓸 기본값. 권한표의 리소스 범위(recoder-*)와
#: 맞춰야 한다 — 다르면 정책이 인가한 리소스와 실제 리소스가 어긋난다.
DEFAULT_CLUSTER = "recoder-cluster"
DEFAULT_SERVICE = "recoder-app"
#: 코어 ECS 요청의 기본 Task Definition family와 맞춘다. 권한 점검도 이
#: 값을 가져와 실제 CloudWatch 로그 그룹(`/ecs/<family>`)을 검사한다.
DEFAULT_TASK_FAMILY = "recoder-task"


def to_core_request(
    body: ExtensionEcsDeployRequest, *, project_id: str = "extension"
) -> ECSDeployRequest:
    """확장 요청 → 코어 요청.

    `region` 은 값이 있을 때만 넘긴다. 빈 문자열을 넘기면
    `ECSDeployRequest` 의 기본값 계산(AWS_REGION → us-east-1)이 무력화된다.
    """
    service = (body.ecs_service or "").strip() or DEFAULT_SERVICE
    fields: dict[str, Any] = {
        "project_id": project_id,
        "cluster": (body.ecs_cluster or "").strip() or DEFAULT_CLUSTER,
        "service": service,
        "image": (body.image_name or "").strip(),
        "workspace_path": (body.workspace_path or "").strip() or None,
        "ecr_repo": (body.repo_name or "").strip() or None,
        "image_tag": (body.tag or "").strip() or None,
        "generate_sbom": not body.skip_sbom,
    }
    if (body.aws_region or "").strip():
        fields["region"] = body.aws_region.strip()  # type: ignore[union-attr]
    if (body.container_name or "").strip():
        fields["container_name"] = body.container_name.strip()  # type: ignore[union-attr]
    if body.container_port:
        fields["container_port"] = body.container_port
    if (body.cpu or "").strip():
        fields["cpu"] = body.cpu.strip()  # type: ignore[union-attr]
    if (body.memory or "").strip():
        fields["memory"] = body.memory.strip()  # type: ignore[union-attr]
    fields["task_definition_family"] = (
        (body.task_family or "").strip() or DEFAULT_TASK_FAMILY
    )
    if body.desired_count is not None:
        fields["desired_count"] = body.desired_count
    # **정책 평가가 보는 두 값은 반드시 넘긴다.**
    # 예전에는 environment 를 컨테이너 환경변수로만 옮기고 branch 는 통째로
    # 버렸다. 그러면 "프로덕션은 main 에서만" 규칙이 브랜치를 모른 채
    # 평가돼 어떤 브랜치에서든 프로덕션 배포가 통과한다.
    if body.environment:
        # 대소문자 정규화는 **여기서 하지 않는다.** 두 라우트의 합류점인
        # `ecs.start_deployment` 이 정책 평가 직전에 한 번만 접는다 — 정규화가
        # 두 군데 있으면 한쪽만 바뀔 때 조용히 갈라진다. 컨테이너에 넣는
        # 환경변수는 사용자가 쓴 그대로 둔다(그건 앱이 읽는 값이다).
        fields["environment"] = body.environment.strip()
        fields["env_vars"] = {"ENVIRONMENT": body.environment}
    # **브랜치는 여기서 정하지 않는다.**
    #
    # 두 라우트의 합류점인 `ecs.start_deployment` 이 정책 평가 직전에
    # `branch_source.resolve_branch` 로 한 번만 확정한다. 여기서도 부르면
    # 같은 배포에 git 이 네 번 뜨고(느리다), 그 사이에 사용자가 브랜치를
    # 바꾸면 **판단한 브랜치와 요청에 담긴 브랜치가 달라진다.**
    # 여기서는 호출자가 보낸 값을 그대로 실어 보내기만 한다.
    if (body.branch or "").strip():
        fields["branch"] = body.branch.strip()  # type: ignore[union-attr]
    if body.skip_opa:
        # **일부러 반영하지 않는다.** 클라이언트가 보낸 플래그 하나로 정책
        # 게이트를 끌 수 있으면 게이트가 아니다. 다만 조용히 무시하면
        # 사용자는 껐다고 믿게 되므로, 로그로 분명히 남긴다.
        logger.warning(
            "skip_opa 요청을 받았지만 정책 평가는 건너뛰지 않습니다 — "
            "승인 레벨은 서버가 정합니다."
        )
    return ECSDeployRequest(**fields)


#: 코어 상태 → 확장이 읽는 **기계용 단계 토큰.**
#:
#: 이 값은 UI 문구가 아니라 **계약**이다. 확장 네 곳이 문자열을 직접
#: 비교한다 — `sidebar/WorkbenchPanel.ts`, `ui/sidebarProvider.ts`,
#: `sidebar/workbenchHtml.ts`, `test/coreClient.smoke.ts` 가 모두
#: `stage === 'done'` / `stage === 'failed'` 로 분기한다.
#:
#: 예전에 여기 한국어("완료"/"실패")를 넣었다가 그 분기가 전부 빗나갔다.
#: `running` 이 false 로 바뀌어 폴링은 멈추는데 완료 표시도 실패 알림도
#: 뜨지 않는, **끝났는데 아무 말도 없는** 상태가 됐다.
#: 어휘는 기존 계약(`core/server.py:247`)을 그대로 따른다:
#: `idle | building | ecr_push | task_def | svc_update | deploying | done | failed`
#:
#: 사람이 읽을 문구가 필요하면 아래 `stage_text` 를 쓴다. 번역은 UI 몫이다.
_STAGE_TOKEN = {
    ECSDeployStatus.PENDING: "idle",
    ECSDeployStatus.IN_PROGRESS: "deploying",
    ECSDeployStatus.SUCCEEDED: "done",
    # 성공이 아닌 종료는 **전부 failed 로 보낸다.** 확장은 종료 시
    # done/failed 두 갈래만 처리하므로, 여기서 새 토큰을 만들면
    # 취소·롤백·서킷브레이커가 아무 표시 없이 사라진다.
    # 구체적인 사유는 `error` 와 `stage_text` 로 전달한다.
    ECSDeployStatus.FAILED: "failed",
    ECSDeployStatus.CANCELLED: "failed",
    ECSDeployStatus.ROLLED_BACK: "failed",
    ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED: "failed",
}

#: 사람이 읽을 문구. 기계 토큰과 **분리**한다.
_STAGE_TEXT = {
    ECSDeployStatus.PENDING: "대기 중",
    ECSDeployStatus.IN_PROGRESS: "배포 중",
    ECSDeployStatus.SUCCEEDED: "완료",
    ECSDeployStatus.FAILED: "실패",
    ECSDeployStatus.CANCELLED: "취소됨",
    ECSDeployStatus.ROLLED_BACK: "롤백됨",
    ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED: "자동 중단(서킷 브레이커)",
}

_RUNNING_STATES = {ECSDeployStatus.PENDING, ECSDeployStatus.IN_PROGRESS}

#: `provisioned` 중 **사용자가 반드시 봐야 하는** 항목. 나머지는 "무엇을
#: 만들었는지"의 기록일 뿐이라 잘려도 되지만, 이것들은 잘리면 안 된다.
_WARNING_KEYS = (
    "scan_warning",       # 보안 검사를 못 돌렸다
    "url_warning",        # 배포는 끝났는데 URL 접속이 확인 안 됐다
    "network_warning",    # 인터넷 경로를 확인 못 했다
    "health_check",       # 헬스체크가 없어 ECS 가 앱 상태를 못 본다
    "cost_warning",       # 태스크가 아직 떠 있어 요금이 붙는다
    "halt",               # 태스크 수를 0 으로 내렸다
    "ecs_rollback",       # ECS 가 이전 버전으로 되돌렸다
    "rollback",           # 취소로 이전 태스크 정의로 되돌렸다
    "policy_warning",     # 정책 엔진 없이 로컬 규칙으로 판단했다
    "service_warning",    # 앱이 내려가 있다 (요금 문제가 아니라 가용성 문제)
    "rollback_approval",  # 사용자가 승인/무시한 ECS 롤백 결과
)


def _rollback_proposal_from_record(
    record: ECSDeployRecord,
) -> Optional[EcsRollbackProposal]:
    """에이전트가 남긴 최소 제안 정보를 UI 계약으로 완성한다.

    에이전트는 AWS 파이프라인만 책임지고, 이 호환 라우트가 사람이 읽는
    카드 데이터와 승인 API를 책임진다. 그래서 health 실패 경로가 UI를 직접
    호출하지 않아도 상태 폴링만으로 카드가 복구된다.
    """
    if not record.rollback_proposal_id or not record.previous_task_definition_arn:
        return None
    return EcsRollbackProposal(
        proposal_id=record.rollback_proposal_id,
        deployment_id=record.deployment_id,
        cluster=record.cluster or "",
        service=record.service or "",
        region=record.region or "",
        reason=(
            f"배포 후 헬스 체크가 {record.health_check_failures}회 실패했습니다."
            if record.health_check_failures
            else (record.error_message or "배포 상태 이상이 감지되었습니다.")
        ),
        previous_task_definition=record.previous_task_definition_arn,
        current_task_definition=record.task_definition_arn or "",
        approval_level=record.rollback_approval_level or 3,
        status=record.rollback_proposal_status or "pending",
    )


def to_status_response(record: Optional[ECSDeployRecord]) -> EcsDeployStatusResponse:
    """코어 배포 기록 → 확장 폴링 응답."""
    if record is None:
        return EcsDeployStatusResponse()

    # 확장은 log_tail 의 **끝 몇 줄만** 보여준다
    # (workbenchHtml.ts 는 `.slice(-3)`, WorkbenchPanel.ts 는 마지막 한 줄).
    # 그래서 순서가 곧 가시성이다. 예전에는 provisioned 를 먼저 다 쏟고
    # image·url 을 뒤에 붙였는데, 그러면 정작 사용자가 봐야 할 경고
    # ("보안 검사를 못 돌렸다", "태스크가 계속 떠 있다")가 항상 잘려 나갔다.
    # 경고를 **맨 뒤**로 보낸다.
    provisioned = dict(record.provisioned or {})
    warnings = {k: v for k, v in provisioned.items() if k in _WARNING_KEYS}
    facts = {k: v for k, v in provisioned.items() if k not in _WARNING_KEYS}

    log_tail: list[str] = [f"{k}: {v}" for k, v in facts.items()]
    if record.image_uri:
        log_tail.append(f"image: {record.image_uri}")
    if record.service_url:
        log_tail.append(f"url: {record.service_url}")
    if record.error_detail:
        log_tail.append(f"detail: {record.error_detail}")
    log_tail.extend(f"{k}: {v}" for k, v in warnings.items())

    return EcsDeployStatusResponse(
        running=record.status in _RUNNING_STATES,
        stage=_STAGE_TOKEN.get(record.status, "failed"),
        stage_text=_STAGE_TEXT.get(record.status, str(record.status)),
        log_tail=log_tail,
        image_uri=record.image_uri or record.image or "",
        task_def_arn=record.task_definition_arn or "",
        error=record.error_message or "",
        started_at=record.started_at.isoformat() if record.started_at else "",
        finished_at=record.completed_at.isoformat() if record.completed_at else "",
        service_url=record.service_url or "",
        remedy=record.error_remedy or "",
        deployment_id=record.deployment_id,
        rollback_proposal=_rollback_proposal_from_record(record),
    )


def _find_rollback_record(proposal_id: str) -> ECSDeployRecord:
    """제안 ID는 배포 기록에만 귀속된다. 다른 배포에 적용할 수 없다."""
    from api.routes import ecs as ecs_routes

    for record in ecs_routes._deploy_records.values():
        if record.rollback_proposal_id == proposal_id:
            return record
    raise HTTPException(status_code=404, detail="롤백 제안을 찾을 수 없습니다. 다시 상태를 확인하세요.")


def _rollback_adr(record: ECSDeployRecord, *, approved: bool, result: str) -> dict[str, str]:
    """VS Code가 워크스페이스에 쓸 승인 감사 기록을 만든다."""
    action = "승인" if approved else "무시"
    outcome = "이전 태스크 정의로 복귀를 요청했습니다" if result == "completed" else "현재 버전을 유지합니다"
    safe_id = record.deployment_id.replace("/", "-")[:12]
    return {
        "file": f"docs/adr/ADR-ecs-rollback-{safe_id}.md",
        "content": "\n".join([
            "# ECS 롤백 결정 기록",
            "",
            f"- 배포 ID: `{record.deployment_id}`",
            f"- 대상: `{record.cluster}/{record.service}` ({record.region})",
            f"- 이상: {record.error_message or '배포 후 헬스 체크 실패'}",
            f"- 결정: 사용자 {action}",
            f"- 결과: {outcome}",
            f"- 이전 Task Definition: `{record.previous_task_definition_arn}`",
            "",
            "이 기록은 이상 감지 후 자동 롤백하지 않고 사용자 승인을 받은 결과입니다.",
            "",
        ]),
    }


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.post("/api/deploy/ecs")
async def deploy_ecs(
    body: ExtensionEcsDeployRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict:
    """확장의 ECS 배포 버튼. 예전에는 이 주소가 없어서 404 였다."""
    from api.routes import ecs as ecs_routes

    # `to_core_request` 는 이제 **필드 변환만** 한다 — git 도 파일 접근도
    # 없다. 브랜치 판정은 두 라우트의 합류점인 `ecs.start_deployment` 이
    # executor 에서 한 번만 한다. 그래서 여기서 스레드로 넘기지 않는다.
    # (예전에는 여기서 `git rev-parse` 를 동기로 불러 이벤트 루프를 최대
    #  5초 멈췄고, 그동안 사이드바 폴링이 통째로 얼었다.)
    try:
        core_request = to_core_request(body)
    except Exception as exc:  # noqa: BLE001 - 검증 실패를 400 으로 바꾼다
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc

    record = await ecs_routes.start_deployment(
        core_request, background_tasks, request
    )
    # 확장 사이드바는 **`status === 'ok'`** 일 때만 폴링을 시작한다
    # (`extension/media/sidebar.js`, 옛 `core/server.py` 계약).
    # "started" 를 돌려주면 시작 실패로 읽혀서, 배포는 도는데 아무도
    # 지켜보지 않는 상태가 된다.
    return {
        "status": "ok",
        "message": f"{core_request.cluster}/{core_request.service} 배포를 시작했습니다.",
        "deployment_id": record.deployment_id,
    }


@router.get("/api/deploy/ecs/status", response_model=EcsDeployStatusResponse)
async def deploy_ecs_status() -> EcsDeployStatusResponse:
    """가장 최근 배포의 진행 상황.

    확장은 배포 id 없이 폴링한다. 그래서 "가장 최근에 시작된 것"을 본다.
    시작 시각으로 정렬한다 — dict 삽입 순서에 기대면 기록 저장 방식이
    바뀌는 순간 조용히 엉뚱한 배포를 보게 된다.
    """
    from api.routes import ecs as ecs_routes

    records = list(ecs_routes._deploy_records.values())
    if not records:
        return EcsDeployStatusResponse()
    latest = max(records, key=lambda r: r.started_at)
    return to_status_response(latest)


@router.post("/api/deploy/ecs/rollback", response_model=EcsRollbackResponse)
async def resolve_ecs_rollback(body: EcsRollbackRequest) -> EcsRollbackResponse:
    """ECS 롤백 제안을 사용자가 승인하거나 무시한다.

    이 엔드포인트는 제안 생성과 실제 AWS 변경을 의도적으로 분리한다.
    `approved=False` 는 감사 기록만 남기며 AWS API를 전혀 호출하지 않는다.
    `approved=True` 한 경우에만 이전 Task Definition으로 UpdateService를
    요청한다. 따라서 감지·표시만으로 자동 롤백되는 경로는 존재하지 않는다.
    """
    from api.routes import ecs as ecs_routes

    record = _find_rollback_record(body.proposal_id)
    proposal_status = record.rollback_proposal_status or "pending"
    # 일시적인 AWS 오류로 실패한 제안은 같은 사용자가 다시 승인해 재시도할
    # 수 있다. 완료·무시·새 배포로 대체된 제안은 절대 되살리지 않는다.
    if proposal_status not in {"pending", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "이미 처리 중이거나 처리한 롤백 제안입니다. "
                f"현재 상태: {proposal_status}"
            ),
        )
    if not record.previous_task_definition_arn:
        raise HTTPException(status_code=409, detail="되돌릴 이전 Task Definition 정보가 없습니다.")
    if not record.cluster or not record.service or not record.region:
        raise HTTPException(status_code=409, detail="롤백 대상 ECS 리소스 정보가 불완전합니다.")

    # 무시는 ECS 호출 없이 기록만 남긴다. 이 분기가 승인 전 자동 실행을
    # 구조적으로 막는 안전장치다.
    if not body.approved:
        record.rollback_proposal_status = "ignored"
        record.rollback_completed_at = datetime.now(timezone.utc)
        record.provisioned["rollback_approval"] = (
            "사용자가 롤백 제안을 무시했습니다. ECS 서비스는 변경하지 않았습니다."
        )
        ecs_routes._save_records()
        return EcsRollbackResponse(
            status="ignored",
            message="롤백 제안을 무시했습니다. 현재 ECS 서비스는 변경하지 않았습니다.",
            deployment_id=record.deployment_id,
            proposal_id=body.proposal_id,
            adr=_rollback_adr(record, approved=False, result="ignored"),
        )

    # await 전에는 이벤트 루프가 양보되지 않으므로, 상태를 먼저 바꿔 두면
    # 두 웹뷰에서 동시에 승인해도 UpdateService는 한 번만 호출된다.
    record.rollback_proposal_status = "approving"
    ecs_routes._save_records()

    def _apply() -> str:
        import boto3

        ecs = boto3.session.Session(region_name=record.region).client("ecs")
        current = ecs.describe_services(
            cluster=record.cluster or "", services=[record.service or ""],
        ).get("services", [])
        current_task_definition = (
            str(current[0].get("taskDefinition") or "") if current else ""
        )
        # 제안을 낸 실패 배포의 Task Definition이 아직 PRIMARY일 때만
        # 되돌린다. 새 배포나 콘솔 수동 변경이 있었다면 오래된 제안은
        # 더 위험하므로 사용자 승인이라도 실행하지 않는다.
        if not record.task_definition_arn or current_task_definition != record.task_definition_arn:
            raise _StaleRollbackProposalError(
                "서비스가 롤백 제안 이후 다른 Task Definition으로 변경되었습니다."
            )
        return aws_infra.revert_service_task_definition(
            ecs,
            cluster=record.cluster or "",
            service=record.service or "",
            task_definition=record.previous_task_definition_arn or "",
        )

    try:
        result = await asyncio.to_thread(_apply)
    except _StaleRollbackProposalError as exc:
        record.rollback_proposal_status = "superseded"
        record.rollback_completed_at = datetime.now(timezone.utc)
        record.provisioned["rollback_approval"] = f"오래된 제안을 차단했습니다: {exc}"
        ecs_routes._save_records()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - AWS 오류를 재시도 가능한 제안으로 보존
        record.rollback_proposal_status = "failed"
        record.provisioned["rollback_approval"] = f"사용자 승인 후 롤백 호출 실패: {exc}"
        ecs_routes._save_records()
        raise HTTPException(
            status_code=502,
            detail="이전 버전으로 되돌리지 못했습니다. AWS 권한과 ECS 상태를 확인하세요.",
        ) from exc

    # aws_infra 는 취소 경로와 호환되도록 실패를 문장으로 돌려준다. 그 문장을
    # 성공으로 기록하지 않아야 UI가 거짓 완료를 표시하지 않는다.
    if result.startswith("취소했지만"):
        record.rollback_proposal_status = "failed"
        record.provisioned["rollback_approval"] = f"사용자 승인 후 롤백 실패: {result}"
        ecs_routes._save_records()
        raise HTTPException(status_code=502, detail=result)

    record.rollback_proposal_status = "completed"
    record.rollback_completed_at = datetime.now(timezone.utc)
    record.status = ECSDeployStatus.ROLLED_BACK
    record.error_message = "사용자 승인으로 이전 버전 롤백을 요청했습니다."
    record.error_remedy = "ECS 서비스 안정화 상태와 CloudWatch 로그를 확인하세요."
    record.provisioned["rollback_approval"] = result
    ecs_routes._save_records()
    logger.info(
        "ECS rollback approved: deployment=%s proposal=%s target=%s/%s",
        record.deployment_id, body.proposal_id, record.cluster, record.service,
    )
    return EcsRollbackResponse(
        status="completed",
        message="승인한 롤백을 요청했습니다. ECS 서비스가 이전 Task Definition으로 전환 중입니다.",
        deployment_id=record.deployment_id,
        proposal_id=body.proposal_id,
        adr=_rollback_adr(record, approved=True, result="completed"),
    )


@router.get("/api/deploy/ecs/ready", response_model=EcsReadyResponse)
async def deploy_ecs_ready() -> EcsReadyResponse:
    """배포 버튼을 누를 수 있는 상태인지 사전 점검.

    확장은 이 응답으로 버튼을 켜고 끈다. 그래서 **못 미더우면 켜지 않는다** —
    확인 못 한 것을 준비된 것으로 보고하면 사용자는 실패할 배포를 시작한다.
    """
    issues: list[str] = []
    warnings: list[str] = []

    def _probe() -> tuple[list[str], list[str]]:
        found: list[str] = []
        warn: list[str] = []
        try:
            from core.agents import ecs_build

            ecs_build.ensure_docker_available()
        except Exception as exc:  # noqa: BLE001
            found.append(getattr(exc, "message", None) or str(exc))

        try:
            import boto3

            identity = boto3.client("sts").get_caller_identity()
            logger.debug("배포 대상 계정: %s", identity.get("Account"))
        except Exception as exc:  # noqa: BLE001
            found.append(
                "AWS 자격증명을 확인하지 못했습니다. 사이드바에서 AWS 연결을 "
                f"먼저 완료하세요. ({exc})"
            )
        return found, warn

    # `ensure_docker_available` 은 최대 60초짜리 blocking subprocess 이고
    # `get_caller_identity` 도 네트워크를 탄다. async 함수 안에서 그대로
    # 부르면 **Core 의 HTTP 서버 전체가 그동안 멈춘다** — 확장이 같은 시각에
    # 돌리는 배포 상태 폴링까지 같이 멈춘다. 도커 데몬이 응답을 안 하면
    # 1분간 서버가 죽은 것처럼 보인다.
    loop = asyncio.get_running_loop()
    issues, warnings = await loop.run_in_executor(None, _probe)

    return EcsReadyResponse(ready=not issues, issues=issues, warnings=warnings)


class EcsStopRequest(BaseModel):
    """실습을 마칠 때 과금을 멈추는 요청."""

    ecs_cluster: Optional[str] = None
    ecs_service: Optional[str] = None
    aws_region: Optional[str] = None


class EcsStopResponse(BaseModel):
    stopped: bool
    desired_count: int = 0
    message: str = ""


@router.post("/api/deploy/ecs/stop", response_model=EcsStopResponse)
async def stop_ecs_service(body: EcsStopRequest) -> EcsStopResponse:
    """서비스의 태스크 수를 0 으로 내려 과금을 멈춘다.

    카드 DoD 에는 없는 기능이다. 추가한 이유가 있다 — AWS Academy 랩은
    **EC2 인스턴스만** 세션 종료 시 자동으로 멈춘다. Fargate 태스크는
    랩을 꺼도 계속 돌면서 과금된다. 방치하면 월 $12.7(Fargate $9 +
    공인 IPv4 $3.7)이고, 랩 예산 $50 이 넉 달이면 사라진다. 예산을 넘기면
    계정이 비활성화되고 **만들어둔 리소스가 전부 삭제된다.**

    서비스 자체는 남겨둔다. 다음 실습 때 태스크 수만 올리면 바로 뜬다.
    """
    import asyncio as _asyncio

    from core import aws_infra
    from core.schemas import ECSDeployRequest as _Req

    cluster = (body.ecs_cluster or "").strip() or DEFAULT_CLUSTER
    service = (body.ecs_service or "").strip() or DEFAULT_SERVICE
    region = (body.aws_region or "").strip() or _Req(
        project_id="_", cluster=cluster, service=service
    ).region

    def _work() -> int:
        import boto3

        ecs = boto3.session.Session(region_name=region).client("ecs")
        return aws_infra.scale_service(
            ecs, cluster=cluster, service=service, desired_count=0
        )

    loop = _asyncio.get_running_loop()
    try:
        desired = await loop.run_in_executor(None, _work)
    except aws_infra.InfraError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "stop_failed",
                "message": exc.message,
                "remedy": exc.remedy,
            },
        ) from exc

    return EcsStopResponse(
        stopped=desired == 0,
        desired_count=desired,
        message=f"{cluster}/{service} 의 태스크 수를 {desired} 로 내렸습니다. "
                "과금이 멈춥니다. 서비스는 남아 있어 다음에 바로 다시 띄울 수 있습니다.",
    )
