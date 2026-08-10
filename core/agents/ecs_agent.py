"""
Local Core — Q3: ECS Rolling Update Agent

설계서 §Q3-A (Must):
1. Preflight 점검 (read-only IAM)
2. 보안 스캔 (Trivy / Hadolint / gitleaks)
3. SBOM 생성 (Syft CycloneDX)
4. ECS Task Definition JSON 생성 (FileTemplate Registry)
5. ECR 로그인 + docker build + 이미지 태그 + ECR push
6. boto3 update-service --force-new-deployment
7. CloudWatch 배포 상태 폴링 (Sidebar에 표시)
8. Health Check 실패 시 rollback proposal (Approval Level 3)
9. Circuit Breaker (5분 내 실패율 50% 초과 → 자동 중단)
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.agents.preflight_agent import PreflightAgent
from core.agents import ecs_build
from core.sbom import sbom_generator
from core import aws_infra, aws_policy
from core.aws_infra import InfraError
from core.schemas import (
    ECSDeployRecord,
    ECSDeployRequest,
    ECSDeployStatus,
)
from core.security_scan import security_scanner

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 15           # CloudWatch 폴링 간격 (초)
_MAX_POLL_ATTEMPTS = 40       # 최대 폴링 횟수 (= 10분)
_CIRCUIT_BREAKER_WINDOW = 300 # 5분 (초)
_CIRCUIT_BREAKER_THRESHOLD = 0.5  # 50%
#: AWS 를 연속 몇 번 못 부르면 포기할지. 15초 간격이므로 5회 = 75초.
#: 이건 "앱이 아프다"가 아니라 "우리가 못 본다"이므로 서킷 브레이커와
#: 별도로 센다. 섞으면 네트워크 끊김이 배포 실패로 둔갑한다.
_MAX_CONSECUTIVE_API_ERRORS = 5

#: 더 이상 진행하지 않는 상태. 여기에 들어가면 종료 시각이 찍혀야 한다.
_TERMINAL_STATUSES = frozenset({
    ECSDeployStatus.SUCCEEDED,
    ECSDeployStatus.FAILED,
    ECSDeployStatus.CANCELLED,
    ECSDeployStatus.ROLLED_BACK,
    ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED,
})

#: 자격증명·권한 문제를 나타내는 AWS 오류 코드.
#: 이것들은 "앱이 아프다"가 아니라 "우리가 못 들어간다"이므로,
#: 배포 건강 상태의 실패로 세면 안 된다.
_AUTH_ERROR_CODES = frozenset({
    "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
    "UnrecognizedClientException", "RequestExpired",
    "AccessDenied", "AccessDeniedException", "AuthFailure",
})

# FileTemplate 경로
_TEMPLATE_PATH = Path(__file__).parent.parent / "registry" / "file_templates" / "ecs-task-definition.json.template"


def python_http_health_check(
    port: int, path: str = "/health", *, timeout: int = 5
) -> list[str]:
    """파이썬 이미지에서 쓸 수 있는 ECS 컨테이너 헬스체크 명령.

    curl 을 쓰지 않는다 — `python:slim` 런타임에 curl 은 없다. 예전에
    태스크 정의가 curl 을 호출하는 바람에 컨테이너가 항상 UNHEALTHY 로
    찍혀 ECS 가 무한 재시작했다. python 은 이 이미지에 반드시 있다.
    """
    probe = (
        "import sys,urllib.request; "
        f"sys.exit(0 if urllib.request.urlopen("
        f"'http://127.0.0.1:{port}{path}', timeout={timeout}).status == 200 else 1)"
    )
    return ["CMD-SHELL", f'python -c "{probe}" || exit 1']


def _probe_http(
    url: str,
    *,
    attempts: int = 6,
    interval: float = 5.0,
    timeout: float = 5.0,
    sleep=None,
) -> tuple[bool, str]:
    """URL 에 실제로 접속되는지 확인한다. (성공 여부, 설명).

    표준 라이브러리만 쓴다 — 배포 검증이 추가 의존성 때문에 실패하면 곤란하다.
    HTTP 4xx 는 "서버가 응답했다"로 본다 — 확인하려는 건 앱의 라우팅이
    아니라 네트워크 경로가 열렸는가다. 반면 **5xx 는 실패로 센다.**
    앱이 500 만 뱉고 있는데 "배포 성공"이라고 보고하면 안 된다.
    """
    import time as _time
    import urllib.error
    import urllib.request

    sleeper = sleep or _time.sleep
    last = "시도하지 않음"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            # 4xx 는 "도달했다"로 본다 — 서버가 응답했고 네트워크 경로는
            # 열려 있다. 경로를 잘못 짚었을 뿐이다.
            # 그러나 **5xx 는 앱이 망가진 것**이다. 이걸 성공으로 세면
            # 500 만 뱉는 서비스를 "배포 성공"으로 보고하게 된다.
            if 500 <= exc.code < 600:
                last = f"HTTP {exc.code} (서버 오류)"
                if i < attempts - 1:
                    sleeper(interval)
                continue
            return True, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            if i < attempts - 1:
                sleeper(interval)
    return False, last


class ECSAgent:
    """
    ECS Rolling Update 전체 오케스트레이션.
    LLM을 사용하지 않는 결정론적 배포 에이전트.
    """

    def __init__(self) -> None:
        self._preflight = PreflightAgent()

    # ------------------------------------------------------------------
    # AWS 클라이언트
    # ------------------------------------------------------------------

    @staticmethod
    def _clients(region: str) -> dict:
        """이 배포에서 쓸 boto3 클라이언트 묶음.

        **한 세션에서 모두 만든다.** 단계마다 따로 만들면 중간에 자격증명이
        바뀌었을 때 어떤 단계는 옛 자격증명으로 도는 상태가 생긴다.
        """
        import boto3

        session = boto3.session.Session(region_name=region)
        return {
            "ecs": session.client("ecs"),
            "ecr": session.client("ecr"),
            "ec2": session.client("ec2"),
            "logs": session.client("logs"),
            "sts": session.client("sts"),
        }

    async def deploy(
        self,
        request: ECSDeployRequest,
        record: Optional[ECSDeployRecord] = None,
    ) -> ECSDeployRecord:
        """`_deploy_pipeline` 을 감싸 **종료 시각을 반드시 남긴다.**

        예전에는 성공 경로 두 곳에서만 `completed_at` 을 찍었다. 실패로
        끝나면 값이 비어서, 확장은 그 배포를 영원히 "진행 중"으로 표시했다
        (`finished_at` 이 빈 문자열로 내려갔다). 개별 return 마다 챙기면
        경로가 늘 때마다 또 빠뜨리므로 여기서 한 번에 보장한다.
        """
        result = record
        try:
            result = await self._deploy_pipeline(request, record)
            return result
        finally:
            if result is not None and result.status in _TERMINAL_STATUSES:
                if result.completed_at is None:
                    result.completed_at = datetime.now(timezone.utc)

    class _Cancelled(Exception):
        """사용자가 취소를 요청했다. 파이프라인 내부 전용 신호."""

    @staticmethod
    def _abort_if_cancelled(rec: ECSDeployRecord, step: str) -> None:
        """단계 경계마다 취소 신호를 확인한다.

        취소가 **표시만 되고 실제로는 아무것도 멈추지 않던** 문제를 막는다.
        예전에는 취소 엔드포인트가 기록의 상태만 FAILED 로 바꿨고,
        파이프라인은 그걸 모른 채 계속 돌아 이미지를 올리고 서비스를 만든 뒤
        같은 기록을 SUCCEEDED 로 덮어썼다. 사용자는 "취소됨"을 보고 손을
        뗐는데 Fargate 태스크는 계속 돌면서 과금됐다.

        단계 **중간**에는 끊지 않는다. docker build 를 반쯤 자르면 뭐가
        남았는지 알 수 없게 된다. 대신 단계가 끝나는 즉시 멈춘다.
        """
        if rec.cancel_requested:
            logger.info("배포 취소 요청 확인 — '%s' 단계 뒤에서 중단합니다.", step)
            raise ECSAgent._Cancelled(step)

    async def _deploy_pipeline(
        self,
        request: ECSDeployRequest,
        record: Optional[ECSDeployRecord] = None,
    ) -> ECSDeployRecord:
        """ECS Fargate 배포 전체 파이프라인 실행.

        FR-05-04. 순서가 예전과 달라진 곳이 한 군데 있다: **빌드·업로드가
        보안 스캔보다 먼저** 온다. 예전에는 아직 존재하지 않는 이미지를
        스캔하려 들었다 — 스캔이 조용히 빈 결과를 내고 "취약점 0건 = 통과"로
        보고되던 경로다.

        `record` 를 넘기면 **그 객체를 그대로 채운다.** 라우트가 저장소에
        넣어둔 바로 그 객체를 받아야 사용자가 진행 상황을 볼 수 있다.
        예전에는 여기서 새 기록을 따로 만들어 채우고 끝에 한 번 교체했는데,
        그러는 동안 사이드바는 몇 분 내내 "대기 중"만 보다가 갑자기 결과로
        건너뛰었다. 취소 요청도 곧 덮어써질 객체를 건드려 무의미했고,
        롤백 제안 id 도 사용자가 들고 있는 배포 id 와 달랐다.
        """
        if record is None:
            record = ECSDeployRecord(
                project_id=request.project_id,
                cluster=request.cluster,
                service=request.service,
                region=request.region,
                image=request.image or None,
                status=ECSDeployStatus.PENDING,
            )
        else:
            record.project_id = request.project_id
            record.cluster = request.cluster
            record.service = request.service
            record.region = request.region
            record.image = request.image or None
            record.status = ECSDeployStatus.PENDING

        try:
            # 0. 리전을 먼저 검증한다. 틀린 리전으로 가면 이후 모든 호출이
            #    엉뚱한 곳을 향하고, 오류 메시지도 원인을 가리키지 않는다.
            aws_policy.validate_region(request.region)
            clients = self._clients(request.region)

            # 1. Preflight
            if request.run_preflight:
                record = await self._step_preflight(request, record)
                if not record.preflight_passed:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message = "Preflight 점검 실패 — 배포를 중단합니다"
                    record.error_remedy = (
                        "사이드바의 점검 결과에서 실패한 항목을 확인하세요."
                    )
                    return record

            # 2. 인프라 확보 (없으면 생성, 있으면 재사용)
            record.status = ECSDeployStatus.IN_PROGRESS
            self._abort_if_cancelled(record, "preflight")
            network = await self._step_provision(request, record, clients)

            self._abort_if_cancelled(record, "인프라 확보")

            # 3-0. 소스·Dockerfile 보안 검사 — **빌드 전에** 한다.
            #      시크릿이나 Dockerfile 오류는 이미지를 만들어 보지 않아도
            #      알 수 있다. 예전에는 스캔이 통째로 푸시 **뒤**에 있어서,
            #      워크스페이스에 테스트용 AWS 키 하나만 있어도 몇 분에 걸쳐
            #      빌드하고 ECR 에 올린 다음에야 막혔다.
            if request.run_security_scan:
                record = await self._step_scan_sources(request, record)
                blocked = self._scan_gate_message(record.scan_result)
                if blocked:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message, record.error_remedy = blocked
                    return record

            # 3. 이미지 빌드 + ECR 업로드
            image_uri = await self._step_build_and_push(request, record, clients)

            # 여기서 멈추면 ECR 에 이미지만 남는다 — 태스크는 안 떴으므로
            # 과금 0원이고, 리포지토리 수명 정책이 알아서 정리한다.
            self._abort_if_cancelled(record, "빌드·업로드")

            # 4. 이미지 취약점 검사 — 이건 이미지가 있어야만 할 수 있다.
            if request.run_security_scan:
                record = await self._step_security_scan(request, record, image_uri)
                blocked = self._scan_gate_message(record.scan_result)
                if blocked:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message, record.error_remedy = blocked
                    self._warn_if_resources_may_be_running(request, record)
                    return record

            # 5. SBOM 생성
            if request.generate_sbom:
                record = await self._step_sbom(request, record, image_uri)

            # 헬스체크가 없으면 ECS 가 컨테이너 상태를 감시하지 않는다.
            # 조용히 넘어가면 "배포 성공"이 실제 동작을 보장하지 않는데도
            # 사용자는 그 사실을 알 수 없다. 기록에 남겨 표면화한다.
            if not request.health_check_command:
                record.provisioned["health_check"] = (
                    "없음 — ECS 가 컨테이너 상태를 감시하지 않습니다. "
                    "앱이 응답하지 않아도 롤백·서킷 브레이커가 걸리지 않습니다."
                )
                logger.warning(
                    "health_check_command 가 없습니다 — ECS 컨테이너 헬스체크 없이 "
                    "배포합니다. 파이썬 이미지라면 python_http_health_check() 를 "
                    "쓰세요."
                )

            self._abort_if_cancelled(record, "보안 스캔")

            # 6. Task Definition 생성 + 등록
            task_def_arn, prev_arn = await self._step_register_task_definition(
                request, clients, image_uri
            )
            record.task_definition_arn = task_def_arn
            record.previous_task_definition_arn = prev_arn

            # **마지막 안전한 지점.** 이 뒤로는 서비스가 만들어지고
            # 태스크가 떠서 과금이 시작된다.
            self._abort_if_cancelled(record, "태스크 정의 등록")

            # 7. 서비스 확보 (없으면 생성, 있으면 새 태스크 정의로 갱신)
            await self._step_ensure_service(request, record, clients,
                                            task_def_arn, network)

            # 8. desired_count 가 0 이면 띄울 태스크가 없다 — 폴링도 URL 도
            #    의미가 없다. 여기서 성공으로 끝낸다(과금 0원 상태).
            if request.desired_count == 0:
                record.status = ECSDeployStatus.SUCCEEDED
                record.completed_at = datetime.now(timezone.utc)
                logger.info(
                    "서비스는 준비됐고 태스크 수는 0 입니다: %s/%s",
                    request.cluster, request.service,
                )
                return record

            # 9. 배포 상태 폴링 + Circuit Breaker
            success, failure_count, breaker_triggered = await self._step_poll_deployment(request, record)

            if not success:
                record.health_check_failures = failure_count
                if breaker_triggered:
                    record.circuit_breaker_triggered = True
                    record.status = ECSDeployStatus.CIRCUIT_BREAKER_TRIGGERED
                else:
                    record.status = ECSDeployStatus.FAILED

                # ECS 가 이미 이전 버전을 되살린 경우에는 롤백 제안을 만들지
                # 않는다. 만들면 "승인하면 이전 버전으로 되돌립니다"라고
                # 안내하게 되는데, **이미 되돌아가 있다.** 사용자는 이미 끝난
                # 일을 승인하게 되고, 그 승인은 같은 리비전을 한 번 더 배포한다.
                if record.ecs_rolled_back:
                    record.status = ECSDeployStatus.ROLLED_BACK
                    record.error_message = (
                        "배포 실패 — ECS 가 이전 버전으로 자동 복구했습니다. "
                        "이번에 올린 이미지는 실행되고 있지 않습니다."
                    )
                    # "이전 버전으로 계속 동작합니다"를 사실처럼 말하지
                    # 않는다. ecs_rolled_back 은 ECS 가 되돌리기를 **시작**했다는
                    # 뜻일 뿐, 그 이전 버전이 멀쩡히 떠 있다는 보장이 아니다
                    # (그 리비전도 똑같이 망가졌을 수 있다). 관측된 태스크 수로
                    # 말한다.
                    if record.running_task_count > 0:
                        state = (
                            f"서비스는 이전 버전으로 동작 중입니다 "
                            f"(태스크 {record.running_task_count}개)."
                        )
                    else:
                        state = (
                            "다만 되돌린 이전 버전도 아직 떠 있지 않습니다 — "
                            "그 버전에도 문제가 있을 수 있으니 요금이 걱정되면 "
                            "배포 중지로 태스크 수를 0 으로 내리세요."
                        )
                    record.error_remedy = (
                        f"{state} CloudWatch 로그 그룹 "
                        f"{self.log_group_name(request)} 에서 새 이미지가 왜 "
                        "기동하지 못했는지 확인한 뒤 다시 배포하세요."
                    )
                    return record

                # Rollback proposal 생성 (Approval Level 3)
                record.rollback_proposal_id = await self._create_rollback_proposal(request, record)
                # 메시지는 **실제로 만들어졌을 때만** 만들어졌다고 말한다.
                # 이전 태스크 정의가 없는 첫 배포에서는 제안이 만들어지지
                # 않는데, 예전에는 그때도 "생성됨"이라고 보고했다.
                if record.rollback_proposal_id:
                    record.rollback_approval_level = 3  # 설계서 §Q3-A
                    record.error_message = (
                        "배포 Health Check 실패 — 롤백 제안을 만들었습니다 "
                        "(승인 Level 3 필요)"
                    )
                    record.error_remedy = (
                        "사이드바에서 롤백 제안을 승인하면 이전 버전으로 되돌립니다."
                    )
                else:
                    record.error_message = (
                        "배포 Health Check 실패 — 되돌릴 이전 버전이 없어 "
                        "롤백 제안은 만들지 못했습니다"
                    )
                    record.error_remedy = (
                        "첫 배포입니다. CloudWatch 로그 그룹 "
                        f"{self.log_group_name(request)} 에서 컨테이너가 왜 "
                        "죽는지 확인하세요."
                    )
                await self._halt_failed_deployment(request, record, clients)
                return record

            # 10. 공개 주소 확인 — 카드 DoD 1번 "URL 로 접속됨"
            await self._step_resolve_url(request, record, clients)

            record.status = ECSDeployStatus.SUCCEEDED
            record.completed_at = datetime.now(timezone.utc)
            logger.info(
                "ECS 배포 성공: %s/%s image=%s url=%s",
                request.cluster, request.service, record.image_uri, record.service_url,
            )
            return record

        except ECSAgent._Cancelled as stop:
            # 사용자가 취소했다. **여기서 상태를 정한다** — 취소 엔드포인트가
            # 아니라. 그래야 파이프라인이 실제로 멈춘 뒤에야 배포가 끝난
            # 것으로 취급되고, 그 전까지는 409 가드가 두 번째 파이프라인을
            # 막아준다.
            logger.info("배포 취소 완료: %s 단계에서 중단", stop)
            record.status = ECSDeployStatus.CANCELLED
            record.error_message = f"사용자 요청으로 취소되었습니다 ({stop} 단계 뒤)"
            await self._stop_after_cancel(request, record, clients)
            return record

        except InfraError as exc:
            # 우리가 만든 오류 — 사람이 읽을 수 있는 문장과 대처법이 이미 있다.
            logger.error("ECS 배포 실패: %s | %s", exc.message, exc.detail)
            record.status = ECSDeployStatus.FAILED
            record.error_message = exc.message
            record.error_remedy = exc.remedy or None
            record.error_detail = exc.detail or None
            self._warn_if_resources_may_be_running(request, record)
            return record

        except Exception as exc:
            # 예상 못 한 오류. 원문을 그대로 두되, 사용자가 아무것도 할 수
            # 없는 상태로 두지는 않는다.
            logger.error("ECS 배포 중 예상치 못한 오류: %s", exc, exc_info=True)
            record.status = ECSDeployStatus.FAILED
            record.error_message = f"배포 중 예상치 못한 오류가 발생했습니다: {exc}"
            record.error_detail = aws_infra.error_message(exc)
            record.error_remedy = (
                "AWS 자격증명이 만료되지 않았는지 확인한 뒤 다시 시도하세요. "
                "계속 실패하면 Core 로그를 확인하세요."
            )
            self._warn_if_resources_may_be_running(request, record)
            return record

    # ------------------------------------------------------------------
    # 단계별 구현
    # ------------------------------------------------------------------

    async def _step_preflight(self, req: ECSDeployRequest, rec: ECSDeployRecord) -> ECSDeployRecord:
        # ECR 리포지토리 이름은 파이프라인 전체가 **한 곳**에서 계산한다.
        # 예전에는 여기서 이미지 주소를 잘라 따로 유추했는데, 그러면 빌드가
        # 올릴 리포지토리와 점검하는 리포지토리가 서로 다를 수 있었다.
        # 게다가 이번 배포에서 새로 만들 리포지토리를 "없다"고 실패시켰다.
        ecr_repo = self.ecr_repo_name(req) if not req.provision else None
        report = await self._preflight.run(
            cluster=req.cluster,
            service=req.service,
            region=req.region,
            task_definition_family=req.task_definition_family,
            ecr_repo=ecr_repo,
            log_group=self.log_group_name(req),
            # 우리가 만들어 줄 리소스가 아직 없는 것은 실패가 아니다.
            will_provision=req.provision,
        )
        rec.preflight_passed = report.passed
        failures = [c.name for c in report.checks if not c.passed and c.severity == "error"]
        if failures:
            rec.error_detail = "실패 항목: " + ", ".join(failures)
        logger.info(
            "Preflight: passed=%s checks=%d 실패=%s",
            report.passed, len(report.checks), failures or "없음",
        )
        return rec

    # ------------------------------------------------------------------
    # 2단계: 인프라 확보 (FR-05-04)
    # ------------------------------------------------------------------

    def log_group_name(self, req: ECSDeployRequest) -> str:
        return f"/ecs/{req.task_definition_family}"

    def ecr_repo_name(self, req: ECSDeployRequest) -> str:
        return req.ecr_repo or req.service

    async def _step_provision(
        self,
        req: ECSDeployRequest,
        rec: ECSDeployRecord,
        clients: dict,
    ) -> aws_infra.NetworkTarget | None:
        """클러스터·로그그룹·ECR·네트워크·보안그룹을 확보한다.

        `provision=False` 면 아무것도 만들지 않고, 요청에 담긴 서브넷·보안
        그룹만 쓴다. 이미 인프라를 손으로 관리하는 사용자를 위한 탈출구다.
        """
        if not req.provision:
            if not req.subnet_ids or not req.security_group_ids:
                raise InfraError(
                    "provision 을 끄면 subnet_ids 와 security_group_ids 를 "
                    "직접 지정해야 합니다.",
                    remedy="배포 요청에 두 값을 넣거나 provision 을 켜세요.",
                )
            return aws_infra.NetworkTarget(
                vpc_id="", subnet_ids=tuple(req.subnet_ids), internet_routable=False
            )

        loop = asyncio.get_running_loop()

        def _work() -> aws_infra.NetworkTarget:
            aws_infra.ensure_cluster(clients["ecs"], req.cluster)
            rec.provisioned["cluster"] = req.cluster

            aws_infra.ensure_log_group(clients["logs"], self.log_group_name(req))
            rec.provisioned["log_group"] = self.log_group_name(req)

            # 서브넷 — 요청에 있으면 그걸 쓰되, **VPC 는 조회해서 알아낸다.**
            # 예전에는 vpc_id 를 빈 문자열로 두고 넘어갔다. 그러면 아래
            # 보안 그룹 자동 생성이 무조건 실패해서, 서브넷을 지정하는
            # 순간 보안 그룹까지 반드시 함께 넘겨야 하는 **문서에도 없는
            # 규칙**이 생겼다 — 요청 모델은 보안 그룹을 생략하면 자동으로
            # 만든다고 말하고 있는데도.
            if req.subnet_ids:
                target = aws_infra.resolve_subnet_network(
                    clients["ec2"], req.subnet_ids
                )
            else:
                target = aws_infra.discover_default_network(clients["ec2"])
            rec.provisioned["vpc"] = target.vpc_id
            rec.provisioned["subnets"] = ",".join(target.subnet_ids)
            # 라우팅을 확인하지 못했다는 사실을 **기록에 남긴다.** 값만
            # 계산해두고 아무도 안 읽으면, 애써 구분한 "확인함/확인 못 함"이
            # 사용자에게 닿지 않는다.
            if not target.internet_routable:
                rec.provisioned["network_warning"] = (
                    "서브넷이 인터넷으로 나가는지 확인하지 못했습니다. "
                    "이미지 pull 이 실패하면 이 부분을 먼저 보세요."
                )
                logger.warning(
                    "서브넷 인터넷 경로 미확인: %s", ",".join(target.subnet_ids)
                )

            # 보안 그룹
            if req.security_group_ids:
                groups = list(req.security_group_ids)
            else:
                # 불변식 확인. 위의 두 경로 모두 실제 VPC 를 채워 주므로
                # 여기가 참이 되면 안 된다 — 참이면 코드가 깨진 것이지
                # 사용자가 뭘 잘못한 게 아니다.
                if not target.vpc_id:
                    raise InfraError(
                        "내부 오류: 서브넷의 VPC 를 확인하지 못한 채 보안 그룹을 "
                        "만들려 했습니다.",
                        remedy="security_group_ids 를 직접 지정하면 우회할 수 "
                               "있습니다. 이 오류는 버그이니 함께 알려주세요.",
                    )
                groups = [
                    aws_infra.ensure_security_group(
                        clients["ec2"],
                        vpc_id=target.vpc_id,
                        name=f"{req.service}-sg",
                        port=req.container_port,
                    )
                ]
            rec.provisioned["security_groups"] = ",".join(groups)
            # 뒤 단계가 쓰도록 요청에 되돌려 심는다.
            req.security_group_ids = groups
            return target

        return await loop.run_in_executor(None, _work)

    # ------------------------------------------------------------------
    # 3단계: 빌드 + ECR 업로드 (FR-05-04)
    # ------------------------------------------------------------------

    async def _step_build_and_push(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict
    ) -> str:
        """이미지를 만들어 ECR 에 올리고 최종 이미지 주소를 돌려준다.

        `workspace_path` 가 없으면 빌드를 건너뛰고 요청에 담긴 `image` 를
        그대로 쓴다 — 이미 ECR 에 있는 이미지를 재배포하는 경우다.
        """
        if not req.workspace_path:
            if not req.image:
                raise InfraError(
                    "배포할 이미지가 없습니다.",
                    remedy="빌드할 작업 폴더(workspace_path)를 지정하거나, "
                           "이미 ECR 에 있는 이미지 주소(image)를 지정하세요.",
                )
            rec.image_uri = req.image
            return req.image

        repo_name = self.ecr_repo_name(req)
        tag = req.image_tag or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        loop = asyncio.get_running_loop()

        def _work() -> ecs_build.PushResult:
            repository_uri = aws_infra.ensure_ecr_repository(clients["ecr"], repo_name)
            rec.provisioned["ecr_repo"] = repo_name
            return ecs_build.build_and_push(
                clients["ecr"],
                workspace_path=req.workspace_path or "",
                repository_uri=repository_uri,
                tag=tag,
                dockerfile=req.dockerfile,
            )

        result = await loop.run_in_executor(None, _work)
        rec.image_uri = result.image_uri
        rec.image_digest = result.digest
        rec.image = result.image_uri
        # 뒤 단계(태스크 정의)가 같은 이미지를 보도록 요청에도 반영한다.
        req.image = result.image_uri
        return result.image_uri

    @staticmethod
    def _scan_gate_message(result) -> tuple[str, str] | None:
        """차단 사유와 대처법. 통과면 None.

        예전 메시지는 숫자 세 개만 나열하고 대처법은 "취약한 의존성을
        올리거나 Dockerfile 을 고치세요"였다. **시크릿은 한 마디도 없었다.**
        그런데 실제로 가장 자주 걸리는 게 시크릿이라, 사용자는 엉뚱한 곳을
        고치게 된다. 무엇이 걸렸는지, 어느 파일인지까지 말해준다.
        """
        if result is None or result.scan_passed:
            return None

        reasons: list[str] = []
        fixes: list[str] = []

        if result.secret_count:
            spots = [
                f.location for f in result.findings
                if (f.title or "").startswith("secret_leak:") and f.location
            ]
            shown = ", ".join(spots[:3])
            more = f" 외 {len(spots) - 3}곳" if len(spots) > 3 else ""
            reasons.append(f"시크릿 의심 {result.secret_count}건")
            fixes.append(
                f"소스에서 자격증명을 제거하세요({shown}{more}). "
                "테스트용 더미 값이라면 'EXAMPLE' 같은 표시를 넣거나 "
                ".env / AWS Secrets Manager 로 옮기세요."
            )
        if result.critical_count:
            reasons.append(f"critical 취약점 {result.critical_count}건")
            fixes.append("베이스 이미지를 올리거나 취약한 패키지를 교체하세요.")
        if result.hadolint_error_count:
            reasons.append(f"Dockerfile 오류 {result.hadolint_error_count}건")
            fixes.append("hadolint 오류를 수정하세요.")

        if not reasons:   # 방어: blocked 인데 이유를 못 찾은 경우
            reasons.append("보안 검사 위반")
            fixes.append("사이드바의 검사 결과를 확인하세요.")

        return "보안 검사 차단: " + ", ".join(reasons), " ".join(fixes)

    async def _step_scan_sources(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> ECSDeployRecord:
        """이미지 없이 할 수 있는 검사 — Dockerfile(hadolint) 과 시크릿."""
        if not req.workspace_path:
            logger.warning(
                "작업 폴더가 없어 Dockerfile·시크릿 검사를 건너뜁니다."
            )
            return rec

        dockerfile_path: str | None = None
        candidate = Path(req.workspace_path) / req.dockerfile
        if candidate.is_file():
            dockerfile_path = str(candidate)
        else:
            logger.warning("Dockerfile 을 찾지 못했습니다: %s", candidate)

        rec.scan_result = await security_scanner.scan_all(
            image=None,
            dockerfile_path=dockerfile_path,
            repo_path=req.workspace_path,
        )
        self._record_scan_gaps(rec)
        return rec

    @staticmethod
    def _record_scan_gaps(rec: ECSDeployRecord) -> None:
        """못 돌린 검사를 사용자에게 알린다."""
        if rec.scan_result is None or not rec.scan_result.tool_errors:
            return
        rec.provisioned["scan_warning"] = (
            "실행되지 않은 보안 검사가 있습니다: "
            + ", ".join(rec.scan_result.tool_errors)
            + " — 이 배포의 '취약점 0건'은 검사 결과가 아닙니다."
        )
        logger.warning(
            "보안 검사 일부 미실행: %s", ", ".join(rec.scan_result.tool_errors)
        )

    async def _step_security_scan(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, image: str
    ) -> ECSDeployRecord:
        """세 스캐너를 **전부** 돌린다.

        예전에는 `scan_all(image=...)` 만 불렀다. `scan_all` 은 인자가 있는
        도구만 실행하므로 hadolint 와 gitleaks 는 아예 돌지 않았다. 그런데도
        실패 메시지는 `hadolint_err=` 와 `secrets=` 를 출력했고 라우트 문서에는
        "Hadolint error=block, gitleaks always-block" 이라고 적혀 있었다.
        즉 두 게이트가 **있는 척만** 하고 있었다.
        """
        # 소스·Dockerfile 은 빌드 **전에** 이미 봤다(`_step_scan_sources`).
        # 여기서 또 돌리면 같은 검사를 두 번 하고, 결과 객체를 덮어써서
        # 앞 단계에서 찾은 것이 사라진다.
        previous = rec.scan_result
        rec.scan_result = await security_scanner.scan_all(
            image=image, dockerfile_path=None, repo_path=None
        )
        if previous is not None:
            # 앞 단계 결과를 합친다 — 사용자에게는 한 번의 검사로 보여야 한다.
            rec.scan_result.findings = list(previous.findings) + list(
                rec.scan_result.findings
            )
            rec.scan_result.dockerfile_path = previous.dockerfile_path
            rec.scan_result.repo_path = previous.repo_path
            rec.scan_result.compute_pass()
        # **못 돌린 검사는 반드시 말한다.** 도구가 안 깔렸거나 이미지를 못
        # 받아오면 findings 가 비고, 그대로 두면 "취약점 0건 = 안전"으로
        # 읽힌다. 스캐너는 그 사실을 tool_errors 에 담아 주는데, 예전에는
        # 아무도 읽지 않아서 사용자에게 닿지 않았다.
        self._record_scan_gaps(rec)
        return rec

    async def _step_sbom(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, image: str
    ) -> ECSDeployRecord:
        sbom = await sbom_generator.generate(image)
        rec.sbom_path = sbom.sbom_path
        rec.sbom_version = f"v{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        return rec

    # ------------------------------------------------------------------
    # 6단계: Task Definition
    # ------------------------------------------------------------------

    def _resolve_role_arns(self, req: ECSDeployRequest, clients: dict) -> tuple[str, str]:
        """실행 역할·태스크 역할의 완전한 ARN.

        역할 **이름**은 권한표(aws_policy)와 같은 출처를 본다. 여기서 따로
        정하면 권한표가 인가한 역할과 태스크 정의에 넣는 역할이 어긋나
        RegisterTaskDefinition 이 PassRole 로 거부된다.

        계정 번호는 STS 로 그때그때 확인한다 — 박아두면 다른 계정에서
        조용히 틀린 ARN 이 만들어진다.
        """
        # NOTE: 여기 예전에 "환경변수가 완전한 ARN 이면 그대로 쓴다"는 분기가
        # 있었는데 **죽은 코드**였다. `role_from_env()` 는 ARN 을 받아도
        # `arn:...:role/` 를 떼어낸 **이름**을 돌려주므로 `startswith("arn:")`
        # 이 참이 되는 경우가 없다. 이름으로 통일해서 아래 한 경로만 남긴다.
        execution_name, task_name = aws_policy.resolve_roles()
        try:
            account = clients["sts"].get_caller_identity()["Account"]
        except Exception as exc:  # noqa: BLE001
            raise InfraError(
                "AWS 계정 번호를 확인하지 못했습니다.",
                detail=aws_infra.error_message(exc),
                remedy="AWS 자격증명이 만료되지 않았는지 확인하세요. "
                       "학교 계정은 세션이 4시간마다 끊깁니다.",
            ) from exc

        ctx = aws_policy.ArnContext.of(account_id=account, region=req.region)
        # 경로(path)를 **떼지 않는다.** `role_short_name()` 은 `iam:GetRole` 처럼
        # 경로 없는 이름을 받는 API 전용이다. ARN 에 쓰면 권한표가 인가한
        # `role/team/EcsExec` 대신 존재하지 않는 `role/EcsExec` 을 가리키게 되고,
        # RegisterTaskDefinition 이 PassRole 로 거부된다.
        # (aws_policy.py 의 `_check_role_name` 주석에 적혀 있는 바로 그 함정)
        exec_arn = aws_policy._arn(
            "iam", f"role/{execution_name}", ctx, global_service=True
        )
        # **태스크 역할은 명시적으로 설정했을 때만 쓴다.**
        #
        # `resolve_roles()` 의 기본값은 `ecsTaskRole` 인데, 그건 AWS 가
        # 만들어 주는 역할이 **아니다**(실행 역할과 달리 콘솔이 자동 생성해
        # 주지 않고, 우리 문서도 만들라고 안내하지 않는다). 그런데 태스크
        # 정의에는 항상 들어갔다. 그래서 평범한 계정에서 기본 설정으로
        # 배포하면 없는 역할을 가리켜 PassRole 거부나 "unable to assume the
        # role" 로 죽었다 — 정작 태스크 역할은 앱이 AWS API 를 부를 때만
        # 필요하고, 샘플 앱을 포함한 대부분의 컨테이너는 필요 없는데도.
        task_arn = ""
        if aws_policy.role_from_env(aws_policy.ENV_TASK_ROLE_ARN):
            task_arn = aws_policy._arn(
                "iam", f"role/{task_name}", ctx, global_service=True
            )
        return exec_arn, task_arn

    async def _step_register_task_definition(
        self, req: ECSDeployRequest, clients: dict, image: str
    ) -> tuple[str, Optional[str]]:
        """Task Definition JSON 생성 → AWS 등록. 이전 revision ARN 반환."""
        ecs = clients["ecs"]

        # 이전 Task Definition ARN 조회 (rollback 대상).
        # 서비스가 아직 없을 수도 있다 — 그건 정상이므로 조용히 넘어간다.
        prev_arn: Optional[str] = None
        try:
            services = ecs.describe_services(
                cluster=req.cluster, services=[req.service]
            ).get("services", [])
            if services:
                prev_arn = services[0].get("taskDefinition")
        except Exception as exc:  # noqa: BLE001
            logger.debug("이전 태스크 정의 조회 생략: %s", aws_infra.error_message(exc))

        exec_arn, task_arn = self._resolve_role_arns(req, clients)
        task_def = self._render_task_definition(
            req, image=image, execution_role_arn=exec_arn, task_role_arn=task_arn
        )

        try:
            resp = ecs.register_task_definition(**task_def)
        except Exception as exc:  # noqa: BLE001
            raise InfraError(
                "태스크 정의를 등록하지 못했습니다.",
                detail=aws_infra.error_message(exc),
                remedy=f"권한표의 iam:PassRole 이 {exec_arn} 과 {task_arn} 에 "
                       "대해 허용돼 있는지 확인하세요. 학교 계정은 두 역할 모두 "
                       "LabRole 이어야 합니다.",
            ) from exc

        arn = resp["taskDefinition"]["taskDefinitionArn"]
        logger.info("태스크 정의 등록: %s", arn)
        return arn, prev_arn

    # ------------------------------------------------------------------
    # 7단계: 서비스 확보
    # ------------------------------------------------------------------

    async def _step_ensure_service(
        self,
        req: ECSDeployRequest,
        rec: ECSDeployRecord,
        clients: dict,
        task_def_arn: str,
        network: aws_infra.NetworkTarget | None,
    ) -> None:
        if network is None:
            raise InfraError("배포할 네트워크가 정해지지 않았습니다.")
        loop = asyncio.get_running_loop()

        def _work() -> aws_infra.ServiceResult:
            return aws_infra.ensure_service(
                clients["ecs"],
                cluster=req.cluster,
                service=req.service,
                task_definition=task_def_arn,
                subnet_ids=network.subnet_ids,
                security_group_ids=req.security_group_ids,
                desired_count=req.desired_count,
            )

        result = await loop.run_in_executor(None, _work)
        rec.provisioned["service"] = f"{req.service} ({result.action})"
        # 취소·실패 때 태스크를 0 으로 내려도 되는지 판단하는 근거.
        # 갱신이었다면 원래 돌던 서비스이므로 함부로 내리면 안 된다.
        rec.service_created_by_this_run = result.action == "created"
        logger.info(
            "ECS 서비스 %s: %s/%s",
            "생성" if result.action == "created" else "갱신",
            req.cluster, req.service,
        )

    # ------------------------------------------------------------------
    # 10단계: 공개 주소 — 카드 DoD 1번
    # ------------------------------------------------------------------

    async def _step_resolve_url(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict
    ) -> None:
        """태스크의 공인 IP 를 찾아 접속 URL 을 기록한다.

        URL 확인 실패는 **배포 실패로 올리지 않는다.** 여기까지 왔다는 건
        태스크가 이미 정상 기동했다는 뜻이고, 주소를 못 알아낸 것과
        배포가 실패한 것은 다르다. 대신 이유를 기록에 남긴다.
        """
        if req.url_wait_timeout <= 0:
            return
        loop = asyncio.get_running_loop()

        def _work() -> str:
            return aws_infra.wait_for_public_url(
                clients["ecs"],
                clients["ec2"],
                cluster=req.cluster,
                service=req.service,
                port=req.container_port,
                timeout=req.url_wait_timeout,
            )

        try:
            rec.service_url = await loop.run_in_executor(None, _work)
            # 주소를 만들었다고 접속이 되는 건 아니다. 카드 DoD 1번은
            # "URL 로 **접속됨**"이므로 실제로 한 번 받아본다. 여기서
            # 확인하지 않으면 "성공"이라고 보고한 주소가 열리지 않는
            # 상황이 그대로 사용자에게 간다.
            reachable, detail = await loop.run_in_executor(
                None,
                lambda: _probe_http(
                    (rec.service_url or "") + req.health_check_path
                ),
            )
            if not reachable:
                # 상태는 SUCCEEDED 로 둔다 — ECS 기준으로 배포는 실제로
                # 끝났고 태스크는 healthy 다. FAILED 로 바꾸면 느리게 뜨는
                # 정상 앱을 실패로 몰고 태스크까지 내리게 된다.
                #
                # 다만 **조용히 초록불을 켜지는 않는다.** 확장의 done 분기는
                # `error` 를 읽지 않고 "배포 완료 ✓" 만 그린다. 그래서
                # provisioned 의 경고 키로 보낸다 — 그건 log_tail 맨 뒤에
                # 실려 done 화면에서도 보인다. 카드 DoD 1번이 "URL 로
                # 접속됨"인데, 접속을 확인하지 못한 사실이 화면에서 사라지면
                # DoD 를 스스로 못 지켰는지도 모르게 된다.
                rec.provisioned["url_warning"] = (
                    f"주소({rec.service_url})에 접속을 확인하지 못했습니다 "
                    f"({detail}). 앱이 기동 중일 수 있으니 잠시 뒤 다시 "
                    f"열어보세요. 계속 안 되면 CloudWatch 로그 그룹 "
                    f"{self.log_group_name(req)} 를 확인하세요."
                )
                logger.warning(
                    "배포는 끝났지만 접속 확인 실패: %s (%s)",
                    rec.service_url, detail,
                )
                rec.error_message = (
                    f"태스크는 떴고 주소({rec.service_url})도 받았지만 "
                    "아직 응답하지 않습니다."
                )
                rec.error_detail = detail
                rec.error_remedy = (
                    "앱이 기동 중일 수 있으니 잠시 뒤 다시 열어보세요. "
                    f"계속 안 되면 CloudWatch 로그 그룹 "
                    f"{self.log_group_name(req)} 를 확인하세요."
                )
            else:
                logger.info("접속 확인 완료: %s", rec.service_url)
        except InfraError as exc:
            logger.warning("공개 주소를 확인하지 못했습니다: %s", exc.message)
            rec.provisioned["url_warning"] = (
                f"접속 주소를 확인하지 못했습니다: {exc.message}"
                + (f" ({exc.remedy})" if exc.remedy else "")
            )
            rec.error_remedy = exc.remedy or None
            rec.error_detail = exc.detail or None
            rec.error_message = (
                f"배포는 성공했지만 접속 주소를 확인하지 못했습니다: {exc.message}"
            )

    async def _step_poll_deployment(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> tuple[bool, int, bool]:
        """
        CloudWatch 배포 상태 폴링.

        Circuit Breaker (설계서 §Q3-A): "최근 5분 내 Health Check 실패 비율 50% 초과 시
        배포 자동 중단". sliding window 로 정확하게 구현 — 단순히 처음 5분만 보는 게 아니라
        매 폴링 시점 기준 이전 5분 구간을 평가한다.

        반환: (success, 총 failure_count, circuit_breaker_triggered)
        """
        import boto3
        try:
            from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
        except Exception:  # noqa: BLE001
            BotoCoreError = ClientError = Exception  # type: ignore

        ecs = boto3.client("ecs", region_name=req.region)

        # sliding window: (timestamp, observation) — observation: "pass" | "fail"
        window: collections.deque[tuple[datetime, str]] = collections.deque()
        total_failures = 0
        #: AWS 가 돌려주는 failedTasks 는 누적값이라 직전 값을 들고 있어야
        #: 신규 실패만 셀 수 있다.
        previous_failed_tasks = 0
        #: AWS 를 연속으로 못 부른 횟수. 앱 건강과 **별도로** 센다.
        consecutive_api_errors = 0

        for attempt in range(_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_POLL_INTERVAL)
            # 폴링은 최대 10분이다. 그 사이 취소를 눌렀는데 10분을 다
            # 기다리게 하면, 사용자는 취소가 안 먹은 줄 알고 창을 닫는다.
            self._abort_if_cancelled(rec, f"배포 상태 확인 {attempt + 1}회차")

            try:
                resp = ecs.describe_services(cluster=req.cluster, services=[req.service])
            except (BotoCoreError, ClientError) as exc:  # noqa: BLE001
                # 자격증명이 끊긴 것과 앱이 아픈 것은 완전히 다른 사건이다.
                # 예전에는 둘 다 "fail" 로 세서, 학교 계정 세션이 4시간 만에
                # 만료되면 45초 뒤 "배포 Health Check 실패"라고 보고했다.
                # 사용자는 멀쩡한 앱을 들여다보게 된다.
                if aws_infra.error_code(exc) in _AUTH_ERROR_CODES:
                    raise InfraError(
                        "배포 상태를 확인하는 중 AWS 자격증명이 만료됐습니다.",
                        detail=aws_infra.error_message(exc),
                        remedy="사이드바에서 AWS 를 다시 연결한 뒤 배포 상태를 "
                               "확인하세요. 학교 계정은 세션이 4시간마다 끊깁니다. "
                               "이미 시작된 태스크는 그대로 떠 있습니다.",
                    ) from exc
                # **AWS 에 못 물어본 것을 앱이 아픈 것으로 세지 않는다.**
                # 예전에는 여기서 fail 을 창에 넣었다. 그러면 노트북 wifi 가
                # 45초만 끊겨도 (3표본 × 50%) 서킷 브레이커가 걸리고,
                # 멀쩡히 기동 중이던 새 서비스가 0 으로 내려가면서
                # "배포 Health Check 실패"라고 보고됐다. 사용자는 정상인
                # CloudWatch 로그를 들여다보게 된다.
                #
                # 자격증명 오류만 걸러낸 게 부족했던 이유: 연결 오류는
                # `.response` 자체가 없어서 error_code() 가 "" 를 돌려주므로
                # 그 allow-list 에 **절대** 걸리지 않는다.
                consecutive_api_errors += 1
                logger.warning(
                    "describe_services 실패 (%d회차, 연속 %d회): %s",
                    attempt + 1, consecutive_api_errors, exc,
                )
                if consecutive_api_errors >= _MAX_CONSECUTIVE_API_ERRORS:
                    raise InfraError(
                        "배포 상태를 확인할 수 없습니다 — AWS 에 연결하지 "
                        f"못했습니다({consecutive_api_errors}회 연속 실패).",
                        detail=aws_infra.error_message(exc),
                        remedy="네트워크 연결을 확인하세요. **배포 자체는 "
                               "계속 진행 중일 수 있습니다** — 사이드바에서 "
                               "배포 상태를 다시 조회하거나 AWS 콘솔에서 "
                               "서비스를 확인하세요.",
                    ) from exc
                continue
            # describe_services 는 없는 서비스에 대해 빈 목록과 failures 를
            # 돌려준다 — 키가 있으므로 `.get(..., [{}])` 기본값은 절대 안 뜬다.
            services = resp.get("services") or []
            if not services:
                reasons = ", ".join(
                    str(f.get("reason", "?")) for f in resp.get("failures", [])
                ) or "이유 없음"
                raise InfraError(
                    f"배포 상태를 확인할 서비스가 없습니다: {req.cluster}/{req.service}",
                    detail=reasons,
                    remedy="서비스가 삭제되지 않았는지, 클러스터·서비스 이름과 "
                           "리전이 맞는지 확인하세요.",
                )
            consecutive_api_errors = 0
            svc = services[0]
            deployments = svc.get("deployments", [])

            # 현재 배포 중인 PRIMARY 배포 확인
            primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
            if primary is None:
                continue

            running = primary.get("runningCount", 0)
            desired = primary.get("desiredCount", 0)
            failed_tasks = primary.get("failedTasks", 0)
            rollout_state = primary.get("rolloutState", "")
            rec.running_task_count = int(running or 0)

            # ECS 서킷 브레이커가 우리 리비전을 버리고 이전 리비전을 다시
            # PRIMARY 로 올렸을 수 있다. 그러면 서비스는 **정상 동작하지만
            # 우리가 올린 이미지가 아니다.** 이걸 확인하지 않으면
            # `rolloutState=COMPLETED, running>=desired` 만 보고 "배포 성공"이라
            # 보고하게 된다 — 사용자는 배포되지 않은 코드를 배포됐다고 믿는다.
            primary_td = str(primary.get("taskDefinition") or "")
            if (
                rec.task_definition_arn
                and primary_td
                and primary_td != rec.task_definition_arn
            ):
                logger.warning(
                    "ECS 가 이전 리비전으로 되돌렸습니다: 요청=%s 실행중=%s",
                    rec.task_definition_arn, primary_td,
                )
                rec.ecs_rolled_back = True
                rec.provisioned["ecs_rollback"] = (
                    f"ECS 서킷 브레이커가 이전 버전({primary_td.rsplit('/', 1)[-1]})"
                    "으로 자동 복구했습니다. 서비스는 살아 있지만 **이번에 올린 "
                    "이미지는 실행되고 있지 않습니다.**"
                )
                return False, total_failures, False

            logger.debug(
                "Poll %d/%d: running=%d desired=%d failed=%d state=%s",
                attempt + 1, _MAX_POLL_ATTEMPTS, running, desired, failed_tasks, rollout_state,
            )

            now = datetime.now(timezone.utc)
            # `failedTasks` 는 **누적 카운트**다. 폴링마다 그 값을 통째로
            # 새 실패로 기록하면 태스크 하나가 한 번 실패했을 뿐인데
            # 폴링마다 fail 이 쌓이고 pass 는 한 번도 안 들어가서,
            # 정상 배포가 45초 만에 서킷 브레이커로 중단된다.
            # 직전 값과의 **증가분**만 신규 실패로 센다.
            new_failures = max(0, failed_tasks - previous_failed_tasks)
            previous_failed_tasks = failed_tasks
            if new_failures > 0:
                for _ in range(new_failures):
                    window.append((now, "fail"))
                total_failures += new_failures
            else:
                window.append((now, "pass"))

            self._trim_window(window, _CIRCUIT_BREAKER_WINDOW)
            if self._breaker_trips(window):
                logger.warning(
                    "Circuit breaker triggered: fail_rate=%.1f%% (window=%ds)",
                    self._failure_rate(window) * 100, _CIRCUIT_BREAKER_WINDOW,
                )
                return False, total_failures, True

            # 성공 판단
            if rollout_state == "COMPLETED" and running >= desired and desired > 0:
                return True, total_failures, False

            # 명시적 실패. ECS 자체 브레이커가 걸린 경우도 여기로 온다 —
            # 우리 sliding window 는 폴링 간격(15초) 때문에 그보다 늦게
            # 반응하거나 아예 임계값에 못 미칠 수 있다. AWS 의 판정을
            # 우리 판정보다 우선한다.
            if rollout_state == "FAILED":
                return False, total_failures, True

        # 폴링 시간 초과 → 실패 처리
        logger.warning("Deployment polling timed out after %d attempts", _MAX_POLL_ATTEMPTS)
        return False, total_failures, False

    # ------------------------------------------------------------------
    # Circuit Breaker sliding window helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trim_window(
        window: "collections.deque[tuple[datetime, str]]",
        window_seconds: int,
    ) -> None:
        """현재 시각 기준 window_seconds 보다 오래된 observation 을 제거."""
        cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
        while window and window[0][0].timestamp() < cutoff:
            window.popleft()

    @staticmethod
    def _failure_rate(window: "collections.deque[tuple[datetime, str]]") -> float:
        if not window:
            return 0.0
        fails = sum(1 for _, kind in window if kind == "fail")
        return fails / len(window)

    @classmethod
    def _breaker_trips(cls, window: "collections.deque[tuple[datetime, str]]") -> bool:
        """sliding window 의 실패율이 임계값 초과 + 의미 있는 표본수일 때만 트리거."""
        # 표본 수가 너무 적으면 (1~2개) 단발성 오류로 차단되는 걸 막는다.
        if len(window) < 3:
            return False
        return cls._failure_rate(window) >= _CIRCUIT_BREAKER_THRESHOLD

    async def _halt_failed_deployment(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict
    ) -> None:
        """실패한 배포가 태스크를 계속 새로 띄우지 못하게 막는다.

        예전에는 서킷 브레이커가 "작동"해도 우리 기록의 상태값만 바뀌었다.
        AWS 에는 아무 말도 하지 않았으므로 ECS 는 죽는 태스크를 계속
        새로 띄웠다 — **기록에는 "자동 중단", 청구서에는 계속 과금.**

        이제 세 겹이다.

        1) ECS 자체 서킷 브레이커(`ensure_service`, rollback=True) — 되돌아갈
           버전이 있으면 ECS 가 그 버전을 되살린다.
        2) 되살린 걸 **성공으로 착각하지 않기** (`ecs_rolled_back`).
        3) 그러고도 아무것도 안 떠 있으면 여기서 태스크 수를 0 으로 내린다.

        판단 기준은 `previous_task_definition_arn` 이 아니라 **실제로 떠 있는
        태스크 수**다. 이전 리비전이 있다는 사실은 그 리비전이 지금 멀쩡히
        떠 있다는 뜻이 아니다 — 삭제된 서비스도, 똑같이 망가진 이전
        리비전도 그 값을 채운다. 관측값을 쓰면 그런 착각이 없다.
        """
        if rec.ecs_rolled_back:
            # ECS 가 이미 이전 버전을 되살려 놨다. 여기서 0 으로 내리면
            # 멀쩡히 굴러가는 이전 버전을 우리 손으로 끄는 꼴이 된다.
            return

        if rec.running_task_count > 0:
            # 뭔가는 떠 있다. 느리게 기동하는 정상 앱일 수 있으므로
            # 자동으로 끄지 않고, 끄는 방법을 알려준다.
            rec.provisioned["cost_warning"] = (
                f"태스크 {rec.running_task_count}개가 아직 실행 중입니다 — "
                "요금이 계속 발생합니다. 사이드바의 배포 중지"
                "(POST /api/deploy/ecs/stop)로 태스크 수를 0 으로 내리세요."
                + ("" if req.health_check_command else
                   " 컨테이너 헬스체크가 없어 앱이 죽어도 ECS 가 알아채지 "
                   "못합니다 — python_http_health_check() 를 쓰면 이런 상태를 "
                   "자동으로 잡습니다.")
            )
            return

        # 떠 있는 태스크가 하나도 없다. 배포는 실패했고 ECS 는 계속
        # 재시도할 수 있다. 0 으로 내려도 잃을 게 없고, 안 내리면 돈이 샌다.
        loop = asyncio.get_running_loop()
        rec.provisioned["halt"] = await loop.run_in_executor(
            None,
            lambda: aws_infra.halt_service(
                clients["ecs"], cluster=req.cluster, service=req.service
            ),
        )

    async def _stop_after_cancel(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict
    ) -> None:
        """취소 후 뒷정리. 돈이 새는 것만 막고, 남의 것은 건드리지 않는다.

        취소는 "이번 배포를 그만둔다"이지 "내 앱을 내린다"가 아니다.
        그래서 **이번 실행에서 새로 만든 서비스만** 0 으로 내린다. 원래
        돌던 서비스를 갱신하던 중이었다면 그건 사용자의 운영 중인 앱이므로,
        내리는 순간 요청하지도 않은 장애를 만드는 셈이다.
        """
        if not rec.provisioned.get("service"):
            # 서비스 단계 전에 멈췄다 — 떠 있는 태스크가 없다.
            return

        if not rec.service_created_by_this_run:
            rec.provisioned["cost_warning"] = (
                "이 배포는 기존 서비스를 갱신하던 중이었습니다. 원래 앱을 "
                "내리지 않으려고 태스크는 그대로 두었습니다 — 정말 멈추려면 "
                "배포 중지(POST /api/deploy/ecs/stop)를 쓰세요."
            )
            return

        loop = asyncio.get_running_loop()
        rec.provisioned["halt"] = await loop.run_in_executor(
            None,
            lambda: aws_infra.halt_service(
                clients["ecs"], cluster=req.cluster, service=req.service
            ),
        )

    @staticmethod
    def _warn_if_resources_may_be_running(
        req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> None:
        """실패로 끝났는데 태스크가 떠 있을 수 있는 경우 반드시 알린다.

        폴링 중 자격증명이 만료되는 경우가 이 환경에서 가장 흔한 실패다
        (학교 계정은 4시간마다 끊기고 폴링 창은 10분이다). 그때 서비스는
        이미 만들어져 태스크가 돌고 있는데, 예전에는 그냥 "실패"로만
        보고했다. 사용자는 실패했다고 믿고 손을 떼는데 Fargate 는 계속
        과금한다. 게다가 안내문이 "다시 시도하세요"였으므로, 재시도가
        버려진 배포 위에 또 하나를 얹었다.

        여기서 AWS 를 부르지 않는 이유: 이 경로에 오는 가장 큰 원인이
        **AWS 를 못 부르는 상황**이다. 그래서 끄는 방법을 알려주기만 한다.
        """
        if not rec.provisioned.get("service"):
            return
        if "halt" in rec.provisioned or "cost_warning" in rec.provisioned:
            return
        rec.provisioned["cost_warning"] = (
            f"서비스 '{req.service}' 는 이미 만들어졌고 태스크가 떠 있을 수 "
            "있습니다 — 배포는 실패했지만 요금은 계속 발생합니다. "
            "배포 중지(POST /api/deploy/ecs/stop)로 태스크 수를 0 으로 "
            "내리거나, 원인을 고친 뒤 다시 배포하세요."
        )

    async def _create_rollback_proposal(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> Optional[str]:
        """
        Health Check 실패 시 이전 Task Definition으로 rollback proposal 생성.
        Approval Level 3 (설계서 §Q3-A).
        ADR-005: 프로덕션은 Git revert PR 기본. ECS는 task definition rollback.
        """
        if not rec.previous_task_definition_arn:
            logger.warning("No previous task definition ARN — cannot create rollback proposal")
            return None

        proposal_id = f"rollback-{rec.deployment_id[:8]}"
        logger.warning(
            "Rollback proposal created: %s → revert to %s (Level 3 approval required)",
            proposal_id, rec.previous_task_definition_arn,
        )
        # 실제 승인 요청은 Control Plane API를 통해 생성 (Extension이 표시)
        return proposal_id

    # ------------------------------------------------------------------
    # Task Definition 렌더링
    # ------------------------------------------------------------------

    def _render_task_definition(
        self,
        req: ECSDeployRequest,
        *,
        image: str,
        execution_role_arn: str,
        task_role_arn: str,
    ) -> dict:
        """FileTemplate 에서 Task Definition dict 생성.

        역할 ARN 은 **인자로 받는다.** 예전에는 이 안에서 환경변수를 읽고
        없으면 `arn:aws:iam::000000000000:role/...` 이라는 가짜 계정 번호로
        떨어졌다. 그 ARN 은 형식만 맞고 존재하지 않아서, 등록이 거부될 때
        원인이 "계정 번호가 0으로 채워졌다"라는 걸 알아채기 어려웠다.
        이제 호출자가 STS 로 확인한 실제 ARN 을 넘긴다.
        """
        for label, arn in (("실행 역할", execution_role_arn), ("태스크 역할", task_role_arn)):
            if not arn:
                continue
            if not arn.startswith("arn:") or "::000000000000:" in arn:
                raise InfraError(
                    f"{label} ARN 이 올바르지 않습니다: {arn}",
                    remedy="AWS 자격증명이 연결돼 있는지 확인하세요.",
                )

        env_vars_list = [{"name": k, "value": v} for k, v in req.env_vars.items()]
        template_str = _TEMPLATE_PATH.read_text(encoding="utf-8")
        replacements = {
            "{{task_definition_family}}": req.task_definition_family,
            "{{cpu}}": req.cpu,
            "{{memory}}": req.memory,
            "{{container_name}}": req.container_name,
            "{{image}}": image,
            "{{container_port}}": str(req.container_port),
            "{{health_check_path}}": req.health_check_path,
            "{{region}}": req.region,
            "{{env_vars_json}}": json.dumps(env_vars_list),
            "{{execution_role_arn}}": execution_role_arn,
            "{{task_role_arn}}": task_role_arn,
        }
        for key, value in replacements.items():
            template_str = template_str.replace(key, str(value))

        rendered = json.loads(template_str)

        # **태스크 역할은 없으면 뺀다.**
        #
        # AWS 는 `ecsTaskExecutionRole` 은 콘솔에서 자동으로 만들어 주지만
        # `ecsTaskRole` 은 **아무도 만들어 주지 않는다.** 그런데 템플릿은
        # 이 키를 항상 넣었고, 기본값이 그 없는 역할이었다. 결과적으로
        # 평범한 AWS 계정에서 기본 설정으로 배포하면 RegisterTaskDefinition
        # 이 PassRole 로 거부되거나, 태스크가 "unable to assume the role"
        # 로 기동에 실패했다. preflight 는 실행 역할만 확인하므로 이걸
        # 잡아내지도 못한다.
        #
        # 태스크 역할은 **앱이 AWS API 를 부를 때만** 필요하다. 샘플 앱을
        # 포함해 대부분의 컨테이너는 필요 없다. 그래서 명시적으로 지정된
        # 경우에만 넣고, 아니면 키 자체를 뺀다.
        if not task_role_arn:
            rendered.pop("taskRoleArn", None)
            logger.info(
                "태스크 역할이 지정되지 않아 taskRoleArn 을 넣지 않습니다 "
                "(앱이 AWS API 를 부른다면 ECS_TASK_ROLE_ARN 을 설정하세요)."
            )
        # 템플릿의 로그 그룹 이름과 우리가 실제로 만든 그룹 이름이 어긋나면
        # 컨테이너가 로그를 못 남기고 태스크 시작 자체가 실패한다.
        # 파생값을 원본과 같은 곳에서 계산해 어긋날 여지를 없앤다.
        for container in rendered.get("containerDefinitions", []):
            options = container.get("logConfiguration", {}).get("options")
            if isinstance(options, dict):
                options["awslogs-group"] = self.log_group_name(req)
                options["awslogs-region"] = req.region

            # ECS 는 **태스크 정의에 적힌 헬스체크만** 감시한다. 이미지에
            # 구워둔 Docker HEALTHCHECK 는 보지 않는다. 그래서 여기 없으면
            # "프로세스는 살아 있는데 앱은 죽은" 상태를 ECS 가 못 잡고,
            # 롤백 제안도 서킷 브레이커도 걸리지 않은 채 배포가 성공으로
            # 보고된다.
            if req.health_check_command:
                container["healthCheck"] = {
                    "command": list(req.health_check_command),
                    "interval": 30,
                    "timeout": 5,
                    "retries": 3,
                    "startPeriod": 60,
                }
        return rendered
