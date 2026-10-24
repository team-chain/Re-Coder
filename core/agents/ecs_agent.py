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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.agents.preflight_agent import PreflightAgent
from core.agents import ecs_build
from core.agents.ecs_health import configure_health_check, python_http_health_check
from core.agents import ecs_runtime
from core.agents.ecs_progress import initialize_progress, advance_progress, finish_progress
from core.sbom import sbom_generator
from core import aws_infra, aws_policy
from core import shield as shield_mod
from core import aws_edge
from core import aws_cdn, aws_rds
from core import ecs_stable
from core.aws_infra import InfraError
from core.schemas import (
    ECSDeployRecord,
    ECSDeployRequest,
    ECSDeployStatus,
)
from core.security_scan import security_scanner

logger = logging.getLogger(__name__)


def boto3_client_like(clients: dict, name: str):
    """clients 묶음에 없으면(옛 호출자·시험) 같은 기본 세션으로 만든다."""
    if name not in clients:
        import boto3
        clients[name] = boto3.client(name)
    return clients[name]

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
try:
    from resources import resource as _resource
except ImportError:  # pragma: no cover
    from core.resources import resource as _resource
#: 실행 파일에서 core.agents.ecs_agent 로 불려도 번들의 템플릿을 찾는다(resources 참고)
_TEMPLATE_PATH = _resource("registry", "file_templates", "ecs-task-definition.json.template")


def _probe_http(
    url: str,
    *,
    attempts: int = 6,
    interval: float = 5.0,
    timeout: float = 5.0,
    require_success: bool = False,
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
            if require_success or 500 <= exc.code < 600:
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

    @staticmethod
    def _plan_runtime(request: ECSDeployRequest) -> "ecs_runtime.RuntimePlan":
        """ECS 실행 설정 계획(값은 기록하지 않는다). 사용자가 준 일반 환경변수·비밀값 참조 이름은 그대로 둔다."""
        return ecs_runtime.plan(
            request.workspace_path,
            key=ecs_runtime.store_key(request.region, request.cluster, request.service),
            environment=request.environment or "",
            provided=set(request.env_vars or {}) | set(request.secret_refs or {}),
            container_port=request.container_port,
            managed_db=request.database == "rds",
        )

    def __init__(self, on_progress: Optional[Callable[[], None]] = None) -> None:
        self._preflight = PreflightAgent()
        self._on_progress = on_progress
        #: 배포 요청별 ECS 실행 설정(값 포함) — 태스크 정의를 등록할 때 꺼내 쓰고 버린다. 기록·디스크에는 남기지 않는다.
        self._runtime: dict[int, ecs_runtime.RuntimePlan] = {}
        #: 2.0.25 배포 요청별 방패 결정(켤 수 있나·앱 포트) — 태스크 정의·서비스(로드 밸런서 대상 컨테이너)가 쓴다.
        self._shield: dict[int, "shield_mod.ShieldDecision"] = {}
        #: 2.0.26 배포 요청별 대규모 방패(ALB·WAF) 결과 — 주소 확인·실패 안내가 쓴다.
        self._edge: dict[int, "aws_edge.EdgeResult"] = {}
        #: 2.0.27 HTTPS 주소(CloudFront)·관리형 DB(RDS) — 비밀번호는 메모리에만, 배포가 끝나면 버린다.
        self._cdn: dict[int, "aws_cdn.CdnResult"] = {}
        self._db: dict[int, "aws_rds.DbHandle"] = {}

    @staticmethod
    def _plan_shield(request: ECSDeployRequest) -> "shield_mod.ShieldDecision":
        """방패를 켤 수 있는지 — 앱이 PORT 환경변수를 읽어야 앱을 방패 뒤 포트로 옮길 수 있다."""
        if not request.shield:
            return shield_mod.decide(enabled=False, level=request.shield_level, public_port=request.container_port,
                                     port_from_env=False)
        if not request.workspace_path:
            return shield_mod.ShieldDecision(False, request.shield_level,
                                             "작업 폴더 없이 이미지만 배포해 앱이 PORT 를 읽는지 확인하지 못했습니다 — 방패 없이 배포합니다.")
        from pathlib import Path
        from static_frontend import generated_static_runtime_port
        try:
            from build_readiness import analyze
        except ImportError:  # pragma: no cover
            from core.build_readiness import analyze  # type: ignore
        static = generated_static_runtime_port(Path(request.workspace_path) / request.dockerfile) is not None
        try:
            readiness = analyze(request.workspace_path, online=False)
            port_from_env = bool(readiness.port_from_env)
            if readiness.runtime == "python":
                # 2.0.32 — 파이썬은 시작 명령·코드가 PORT 를 정말 쓰는지 본다(포트를 고정한 앱에 켜면 태스크가 계속 교체된다).
                port_from_env = shield_mod.python_reads_port(request.workspace_path, request.dockerfile)
        except Exception:  # noqa: BLE001 - 판단하지 못하면 켜지 않는다(앱이 안 뜨는 것보다 방패 없는 편이 낫다)
            port_from_env = False
        return shield_mod.decide(enabled=True, level=request.shield_level, public_port=request.container_port,
                                 port_from_env=port_from_env, static_site=static)

    def _save_progress(self) -> None:
        if self._on_progress:
            try:
                self._on_progress()
            except Exception:
                logger.warning("ECS 진행 기록 저장 실패", exc_info=True)

    def _advance_progress(self, record: ECSDeployRecord, key: str) -> None:
        advance_progress(record, key)
        self._save_progress()

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
            "elbv2": session.client("elbv2"),
            #: 2.0.25 공격·장애 감지 알림(지표 경보·메일)
            "cloudwatch": session.client("cloudwatch"),
            "sns": session.client("sns"),
            #: 2.0.26 대규모 방패(ALB 앞 WAF)
            "wafv2": session.client("wafv2"),
            #: 2.0.31 HTTPS 주소 + WAF — CloudFront 에 붙이는 웹 ACL 은 전역 범위라 us-east-1 에서만 다룬다.
            "wafv2_global": session.client("wafv2", region_name="us-east-1"),
            #: 2.0.27 HTTPS 주소 — CloudFront 는 전역, 인증서는 us-east-1 에서만 붙는다(CloudFront 규칙).
            "cloudfront": session.client("cloudfront"),
            "acm_global": session.client("acm", region_name="us-east-1"),
            #: 2.0.35 원본 구간 HTTPS — ALB 에 붙이는 인증서는 배포 리전에 있어야 한다.
            "acm": session.client("acm"),
            "route53": session.client("route53"),
            #: 2.0.27 관리형 DB
            "rds": session.client("rds"),
            "secretsmanager": session.client("secretsmanager"),
            #: 2.0.32 production 관리형 DB — 실행 역할이 DB 비밀을 읽을 수 있는지 미리 시뮬레이션
            "iam": session.client("iam"),
            #: 2.0.48 24시간 감시자 — 배포가 끝나면 롤백 기준 버전을 감시자 기록 표에 알려 준다
            "dynamodb": session.client("dynamodb"),
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
                finish_progress(result)
                self._save_progress()

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
            initialize_progress(record, request)
            self._advance_progress(record, "preflight")
            aws_policy.validate_region(request.region)
            # Generated static clients have a fixed listen port. Reject a stale
            # UI/default before any AWS resource is created or changed.
            if request.workspace_path:
                from pathlib import Path
                from static_frontend import generated_static_runtime_port
                static_port = generated_static_runtime_port(Path(request.workspace_path) / request.dockerfile)
                if static_port is not None and static_port != request.container_port:
                    raise InfraError(
                        f"컨테이너 포트 {request.container_port}와 앱의 포트 {static_port}가 다릅니다.",
                        remedy=f"배포 설정의 컨테이너 포트를 {static_port}로 수정하세요.",
                    )
            # 0-1. 앱이 ECS 에서 실제로 시작할 수 있는가 — DB·서명 키·결제 키. 비어 있는 값을 아무도 채울 수 없으면
            #      AWS 자원을 하나도 만들기 전에 멈춘다(실기기 2.0.11: DATABASE_URL·JWT_SECRET 없이 배포 → 태스크가 계속 죽음).
            if request.workspace_path:
                runtime = await asyncio.get_running_loop().run_in_executor(None, self._plan_runtime, request)
                if runtime.blocked:
                    message, remedy = runtime.block_message()
                    raise InfraError(message, remedy=remedy)
                self._runtime[id(request)] = runtime
                for i, note in enumerate(runtime.notes):
                    record.provisioned[f"app_runtime_{i + 1}"] = note
                if runtime.services and not request.assign_public_ip:
                    record.provisioned["app_runtime_network"] = (
                        "공인 IP 를 끈 배포입니다 — DB 이미지(ECR Public)를 받으려면 서브넷에 NAT 게이트웨이가 있어야 합니다.")
            decision = await asyncio.get_running_loop().run_in_executor(None, self._plan_shield, request)
            self._shield[id(request)] = decision
            info = shield_mod.summary(decision)
            record.provisioned["shield"] = (
                f"켜짐 — 강도 {info['label']}(IP당 초당 {info['rate']}회, 순간 {info['burst']}회), 공개 포트 {decision.public_port} "
                f"→ 앱 {decision.app_port}" if decision.on else f"꺼짐 — {decision.reason}")
            self._check_edge_request(request)
            if request.database == "rds":
                record.provisioned["database"] = "관리형 DB(RDS) — 비용 확인함"
            if request.edge != "none":
                record.provisioned["edge"] = (
                    "대규모 방패 — 로드밸런서" + (" + AWS WAF(IP당 "
                    f"{request.waf_window // 60}분 {request.waf_rate_limit:,}회 + AWS 관리 규칙 2개)"
                    if request.edge == "alb_waf" else "만") + " (비용 확인함)")
            clients = self._clients(request.region)

            # 1. Preflight
            if request.run_preflight:
                record = await self._step_preflight(request, record)
                if not record.preflight_passed:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message = "Preflight 점검 실패 — 배포를 중단합니다"
                    # "사이드바의 점검 결과" 라는 화면은 없다(실기기 C4). 항목별
                    # 조치는 _step_preflight 가 error_detail/error_remedy 에 담았다.
                    record.error_remedy = record.error_remedy or (
                        "실패한 항목을 만들거나 고친 뒤 다시 배포하세요."
                    )
                    return record

            # 2. 인프라 확보 (없으면 생성, 있으면 재사용)
            record.status = ECSDeployStatus.IN_PROGRESS
            self._abort_if_cancelled(record, "preflight")
            self._advance_progress(record, "provisioning")
            network = await self._step_provision(request, record, clients)

            self._abort_if_cancelled(record, "인프라 확보")

            # 3-0. 소스·Dockerfile 보안 검사 — **빌드 전에** 한다.
            #      시크릿이나 Dockerfile 오류는 이미지를 만들어 보지 않아도
            #      알 수 있다. 예전에는 스캔이 통째로 푸시 **뒤**에 있어서,
            #      워크스페이스에 테스트용 AWS 키 하나만 있어도 몇 분에 걸쳐
            #      빌드하고 ECR 에 올린 다음에야 막혔다.
            if request.run_security_scan:
                self._advance_progress(record, "source_scan")
                record = await self._step_scan_sources(request, record)
                blocked = self._scan_gate_message(record.scan_result)
                if blocked:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message, record.error_remedy = blocked
                    return record

            # 3. 이미지 빌드 + ECR 업로드
            self._advance_progress(record, "building")
            image_uri = await self._step_build_and_push(request, record, clients)
            self._advance_progress(record, "ecr_push")

            # 여기서 멈추면 ECR 에 이미지만 남는다 — 태스크는 안 떴으므로
            # 과금 0원이고, 리포지토리 수명 정책이 알아서 정리한다.
            self._abort_if_cancelled(record, "빌드·업로드")

            # 4. 이미지 취약점 검사 — 이건 이미지가 있어야만 할 수 있다.
            if request.run_security_scan:
                self._advance_progress(record, "image_scan")
                record = await self._step_security_scan(request, record, image_uri)
                blocked = self._scan_gate_message(record.scan_result)
                if blocked:
                    record.status = ECSDeployStatus.FAILED
                    record.error_message, record.error_remedy = blocked
                    self._warn_if_resources_may_be_running(request, record)
                    return record

            # 5. SBOM 생성
            if request.generate_sbom:
                self._advance_progress(record, "sbom")
                record = await self._step_sbom(request, record, image_uri)

            # Both extension and direct/Discord requests meet here. Resolve
            # before registration so a UI that omits the command still gets it.
            self._advance_progress(record, "task_def")
            loop = asyncio.get_running_loop()
            health_gap = await loop.run_in_executor(None, configure_health_check, request)
            if health_gap:
                record.provisioned["health_check"] = (
                    "없음 — ECS 컨테이너 헬스체크를 설정하지 않았습니다. " + health_gap
                )
                logger.warning("ECS 컨테이너 헬스체크 미설정: %s", health_gap)
            else:
                record.provisioned["container_health_check"] = "태스크 정의에 컨테이너 헬스체크를 설정합니다."

            self._abort_if_cancelled(record, "보안 스캔")

            # 5-1. 2.0.27 관리형 DB — 인프라 단계에서 만들기 시작한 RDS 가 준비되기를 기다린다(그 사이 빌드는 끝남).
            await self._step_database_ready(request, record, clients)

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
            self._advance_progress(record, "svc_update")
            await self._step_ensure_service(request, record, clients,
                                            task_def_arn, network)
            # 7-1. 공격·장애 감지 알림 준비(실패해도 배포는 계속)
            await self._step_alerts(request, record, clients)

            # 8. desired_count 가 0 이면 띄울 태스크가 없다 — 폴링도 URL 도
            #    의미가 없다. 여기서 성공으로 끝낸다(과금 0원 상태).
            if request.desired_count == 0:
                # **끝내기 전에 취소를 한 번 더 본다.**
                #
                # 취소는 "지금 도는 단계가 끝난 뒤" 반영된다고 문서에 적어
                # 놨는데, 이 갈래만 그 약속을 안 지켰다. 바로 위
                # `_step_ensure_service` 가 AWS 를 기다리는 동안 사용자가
                # 취소를 눌러도, 여기서 곧장 SUCCEEDED 로 덮어써서 **취소가
                # 없었던 일이 된다.** 다른 종료 지점(URL 확인 뒤)은 이미
                # 같은 이유로 다시 확인하고 있다.
                self._abort_if_cancelled(record, "서비스 확보")
                record.status = ECSDeployStatus.SUCCEEDED
                record.completed_at = datetime.now(timezone.utc)
                logger.info(
                    "서비스는 준비됐고 태스크 수는 0 입니다: %s/%s",
                    request.cluster, request.service,
                )
                return record

            # 9. 배포 상태 폴링 + Circuit Breaker
            self._advance_progress(record, "stabilizing")
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
                            "Deploy 캔버스 › 배포 상태의 '서비스 중지'로 태스크 수를 0 으로 내리세요."
                        )
                    record.error_remedy = (
                        f"{state} CloudWatch 로그 그룹 "
                        f"{self.log_group_name(request)} 에서 새 이미지가 왜 "
                        "기동하지 못했는지 확인한 뒤 다시 배포하세요."
                    )
                    diagnosis = await self._diagnose_stopped(request, record, clients)
                    if diagnosis:
                        record.provisioned["diagnosis"] = "\n".join(diagnosis)
                        record.error_detail = "\n".join(diagnosis)
                    await self._tell_watcher(request, record, clients)
                    return record

                # Rollback proposal 생성 (Approval Level 3)
                record.rollback_proposal_id = await self._create_rollback_proposal(request, record)
                # 메시지는 **실제로 만들어졌을 때만** 만들어졌다고 말한다.
                # 이전 태스크 정의가 없는 첫 배포에서는 제안이 만들어지지
                # 않는데, 예전에는 그때도 "생성됨"이라고 보고했다.
                if record.rollback_proposal_id:
                    record.rollback_approval_level = 3  # 설계서 §Q3-A
                    record.rollback_proposal_status = "pending"
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
                diagnosis = await self._diagnose_stopped(request, record, clients)
                if diagnosis:
                    record.provisioned["diagnosis"] = "\n".join(diagnosis)
                    record.error_detail = "\n".join(diagnosis)
                await self._halt_failed_deployment(request, record, clients)
                # 감시자의 롤백 기준은 **마지막 성공 버전**으로 둔다 — 실패한 이번 버전이 기준이 되지 않게.
                await self._tell_watcher(request, record, clients)
                return record

            # 10. 공개 주소 확인 — 카드 DoD 1번 "URL 로 접속됨"
            self._advance_progress(record, "url_check")
            await self._step_resolve_url(request, record, clients)

            # URL 확인은 최대 300초를 기다린다. 그 사이 취소를 눌렀다면
            # 여기서 다시 봐야 한다 — 안 보면 취소한 배포가 SUCCEEDED 로
            # 기록되고, 새로 만든 서비스의 뒷정리도 건너뛴다.
            self._abort_if_cancelled(record, "공개 주소 확인")

            record.status = ECSDeployStatus.SUCCEEDED
            record.completed_at = datetime.now(timezone.utc)
            self._record_success(request, record)
            await self._tell_watcher(request, record, clients)
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
            # 취소는 태스크만 멈춘다 — 이미 만든 로드밸런서·WAF·CloudFront·RDS 는 남아 요금이 나온다.
            # 실패 경로와 같은 비용 안내를 남기고, 무엇이 남았는지 기록한다.
            if self._warn_billing_leftovers(request, record):
                record.error_remedy = (
                    "취소 전에 만든 리소스(" + record.provisioned["billing_resources"] + ")는 지우지 않고 남겼습니다 — "
                    "요금이 계속 나옵니다. 다시 배포하면 그대로 쓰고, 그만 쓸 거면 「AWS 리소스 정리」에서 지우세요.")
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
        finally:
            #: 실행 설정(비밀값 포함)은 이 배포에서만 쓴다 — 중간에 멈춘 배포가 남긴 것을 다른 배포가 집어 가지 않게
            #: (검토 2.0.15: id() 는 재사용된다).
            self._runtime.pop(id(request), None)
            self._shield.pop(id(request), None)
            self._edge.pop(id(request), None)
            self._cdn.pop(id(request), None)
            self._db.pop(id(request), None)

    # ------------------------------------------------------------------
    # 단계별 구현
    # ------------------------------------------------------------------

    @staticmethod
    def _check_edge_request(req: ECSDeployRequest) -> None:
        """대규모 방패는 AWS 자원을 하나도 만들기 전에 조건을 본다 — 비용 확인 · 직접 지정한 대상 그룹 · provision."""
        if req.database == "rds":
            if not req.db_cost_approved:
                raise InfraError("관리형 DB(RDS)는 켜 두는 동안 계속 요금이 나와 비용 확인이 필요합니다.",
                                 remedy="배포 설정의 「관리형 DB」에서 비용 확인에 체크한 뒤 다시 배포하세요. " + aws_rds.COST_NOTE)
            if not req.provision:
                raise InfraError("인프라 자동 준비(provision)를 끈 배포에는 관리형 DB 를 만들 수 없습니다.",
                                 remedy="provision 을 켜거나 DATABASE_URL 을 비밀값 참조로 직접 넣으세요.")
        if req.https and req.edge == "none":
            #: HTTPS 주소(CloudFront)는 로드밸런서 앞에 둔다 — 로드밸런서만(WAF 없이) 켠다.
            req.edge = "alb"
        if req.custom_domain and not req.https:
            raise InfraError("내 도메인은 HTTPS 주소(CloudFront)를 켜야 연결할 수 있습니다.",
                             remedy="배포 설정에서 HTTPS 주소를 켜세요.")
        if req.edge == "none":
            return
        if not req.edge_cost_approved:
            raise InfraError("대규모 방패(로드밸런서·WAF)는 켜 두기만 해도 매달 요금이 나와 비용 확인이 필요합니다.",
                             remedy="배포 설정의 「대규모 방패」에서 비용 확인에 체크한 뒤 다시 배포하세요. " + aws_edge.COST_NOTE)
        if req.target_group_arn:
            raise InfraError("대상 그룹을 직접 지정한 배포에는 대규모 방패를 함께 켤 수 없습니다.",
                             remedy="대상 그룹 ARN 을 비우거나(ReCoder 가 로드밸런서를 만듦) 대규모 방패를 끄세요.")
        if not req.provision:
            raise InfraError("인프라 자동 준비(provision)를 끈 배포에는 대규모 방패를 켤 수 없습니다.",
                             remedy="provision 을 켜거나 대규모 방패를 끄세요.")

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
        failed = [c for c in report.checks if not c.passed and c.severity == "error"]
        failures = [c.name for c in failed]
        if failed:
            # 항목 이름만 남기면 사용자는 "무엇이 없는지·어떻게 만들지" 를 알 수
            # 없다(실기기 C4: "IAM Role 'ecsTaskExecutionRole' 존재 확인" 한 줄).
            # 각 점검이 이미 들고 있는 detail·fix_guide 를 그대로 싣는다.
            lines = []
            for c in failed:
                line = c.detail or c.name
                if getattr(c, "fix_guide", None):
                    line += f" → {c.fix_guide}"
                lines.append(line)
            rec.error_detail = "실패 항목: " + " / ".join(lines)
            rec.error_remedy = "위 항목을 만들거나 고친 뒤 다시 배포하세요."
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

    def resource_tags(self, req: ECSDeployRequest) -> dict[str, str]:
        """ReCoder 가 새로 만드는 리소스에 붙이는 태그(정리 기능이 이것으로 찾는다)."""
        return aws_infra.recoder_tags(req.project_id, req.service)

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
        if req.target_group_arn and not req.security_group_ids:
            raise InfraError(
                "ALB 배포에는 앱 보안 그룹을 명시해야 합니다.",
                remedy="ALB 보안 그룹에서 앱 포트로 들어오는 연결만 허용한 "
                       "security_group_ids를 지정하세요. 공개 인바운드 그룹을 자동 생성하지 않습니다.",
            )
        if not req.provision:
            if not req.subnet_ids or not req.security_group_ids:
                raise InfraError(
                    "provision 을 끄면 subnet_ids 와 security_group_ids 를 "
                    "직접 지정해야 합니다.",
                    remedy="배포 요청에 두 값을 넣거나 provision 을 켜세요.",
                )
            # 만들어 주지는 않지만 **있어야 하는 것은 있는지 확인한다.**
            # 로그 그룹이 없으면 태스크가 기동 중에 죽는데, preflight 는
            # 이걸 경고로만 남겨 통과시킨다. 배포를 시작한 뒤 죽는 것보다
            # 여기서 세우는 편이 낫다.
            # 만들어 주지는 않지만 **있어야 하는 것은 전부 확인한다.**
            # 로그 그룹만 보던 때는 서비스 이름을 잘못 적어도 그냥 통과해서,
            # 7단계에서 **새 서비스가 만들어지고 과금이 시작**됐다.
            # "아무것도 만들지 않는다"는 약속이 가장 비싼 자리에서 깨졌다.
            loop = asyncio.get_running_loop()

            def _verify() -> None:
                if req.target_group_arn:
                    aws_infra.validate_target_group_network(
                        clients["ec2"], clients["elbv2"], req.target_group_arn, req.subnet_ids
                    )
                aws_infra.require_cluster(clients["ecs"], req.cluster)
                aws_infra.require_service(
                    clients["ecs"], cluster=req.cluster, service=req.service
                )
                aws_infra.require_log_group(
                    clients["logs"], self.log_group_name(req)
                )

            await loop.run_in_executor(None, _verify)
            rec.provisioned["cluster"] = f"{req.cluster} (기존)"
            rec.provisioned["log_group"] = f"{self.log_group_name(req)} (기존)"
            return aws_infra.NetworkTarget(
                vpc_id="", subnet_ids=tuple(req.subnet_ids), internet_routable=False
            )

        loop = asyncio.get_running_loop()

        def _work() -> aws_infra.NetworkTarget:
            aws_infra.ensure_cluster(clients["ecs"], req.cluster, tags=self.resource_tags(req))
            rec.provisioned["cluster"] = req.cluster

            aws_infra.ensure_log_group(clients["logs"], self.log_group_name(req), tags=self.resource_tags(req))
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
            if req.target_group_arn:
                aws_infra.validate_target_group_network(
                    clients["ec2"], clients["elbv2"], req.target_group_arn, target.subnet_ids
                )
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

            # 2.0.26 대규모 방패 — ALB(+WAF) 를 만들고, 태스크는 ALB 보안 그룹에서 오는 연결만 받는다.
            if req.edge != "none":
                edge = aws_edge.ensure_edge(
                    clients["ec2"], clients["elbv2"], clients["wafv2"], service=req.service,
                    vpc_id=target.vpc_id, subnet_ids=list(target.subnet_ids), app_port=req.container_port,
                    health_path=req.health_check_path, waf=req.edge == "alb_waf",
                    rate_limit=req.waf_rate_limit, window=req.waf_window, tags=self.resource_tags(req),
                    cdn=req.https)
                self._edge[id(req)] = edge
                rec.provisioned["load_balancer"] = (
                    f"{edge.name} — {edge.url} ({'새로 만듦' if 'alb' in edge.created else '기존 것 사용'})")
                rec.provisioned["waf"] = (
                    f"켜짐 — IP당 {req.waf_window // 60}분 {req.waf_rate_limit:,}회 넘으면 차단 + AWS 관리 규칙 2개"
                    if edge.web_acl_arn else "꺼짐 — 로드밸런서만")
                for i, note in enumerate(edge.notes):
                    rec.provisioned[f"edge_note_{i + 1}"] = note
                groups = list(req.security_group_ids) + [edge.task_security_group]
                rec.provisioned["security_groups"] = ",".join(groups)
                req.security_group_ids = groups
                #: 아래 서비스 단계가 이 대상 그룹에 연결하고, 방패(nginx)는 ALB 가 넘긴 실제 IP 를 쓴다.
                req.target_group_arn = edge.target_group_arn
                if req.https:
                    self._provision_cdn(req, rec, clients, edge)
                else:
                    self._note_leftovers(req, rec, clients, cdn_only=True)
                self._start_database(req, rec, clients, target)
                return target
            self._note_leftovers(req, rec, clients)

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
                        tags=self.resource_tags(req),
                        require_managed=True,
                    )
                ]
            rec.provisioned["security_groups"] = ",".join(groups)
            # 뒤 단계가 쓰도록 요청에 되돌려 심는다.
            req.security_group_ids = groups
            self._start_database(req, rec, clients, target)
            return target

        return await loop.run_in_executor(None, _work)

    # ------------------------------------------------------------------
    # 2.0.27 HTTPS 주소(CloudFront) · 관리형 DB(RDS)
    # ------------------------------------------------------------------

    def _note_leftovers(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict, *, cdn_only: bool = False) -> None:
        """대규모 방패·HTTPS 를 끈 배포 — 예전에 만든 로드밸런서·WAF·CloudFront 는 지우지 않고 남는다(되돌리기 쉽게).
        요금이 계속 나가므로 알린다(2.0.32). 확인이 안 되면 조용히 넘어간다."""
        name = aws_edge.edge_name(req.service)
        left: list[str] = []
        try:
            if not cdn_only and aws_edge._find_load_balancer(clients["elbv2"], name) is not None:
                left.append(f"로드밸런서 {name}" + (" + WAF" if aws_edge.find_web_acl(clients["wafv2"], name) else ""))
            if "cloudfront" in clients and aws_cdn._find(clients["cloudfront"], aws_cdn.comment_for(name, req.region),
                                                         legacy=aws_cdn.comment_for(name), region=req.region):
                left.append("HTTPS 주소(CloudFront)")
        except Exception as exc:  # noqa: BLE001
            logger.debug("남은 리소스 확인 생략: %s", aws_infra.error_message(exc))
            return
        if left:
            rec.provisioned["leftover_resources"] = (
                f"이번 배포에서 끈 {', '.join(left)} 은(는) 지우지 않고 남겨 두었습니다 — 계속 요금이 나옵니다. "
                "다시 켤 계획이 없으면 운영 화면의 「AWS 리소스 정리」에서 지우세요.")

    def _prepare_origin_tls(self, req: ECSDeployRequest, clients: dict, edge: "aws_edge.EdgeResult",
                            cert: "Optional[aws_cdn.CertificateState]", tags: dict) -> "aws_cdn.OriginTls":
        """origin.<도메인> → ALB 443(배포 리전 인증서). 모든 준비가 끝난 배포에서만 ready — 그 전에는 지금처럼 HTTP 원본.

        실패해도 배포는 계속한다(원본 잠금 머리글과 CloudFront 전용 보안 그룹이 계속 막는다)."""
        host = aws_cdn.origin_host(req.custom_domain)
        out = aws_cdn.OriginTls(host=host)
        zone_id = cert.zone_id if cert is not None else ""
        if not zone_id:
            out.notes.append(f"{req.custom_domain} 영역이 Route 53 에 없어 CloudFront 와 로드밸런서 사이는 HTTP 로 둡니다"
                             "(원본 잠금 머리글로 막음). Route 53 으로 옮기면 다음 배포에서 HTTPS 로 바뀝니다.")
            return out
        if "acm" not in clients or not edge.zone_id:
            return out
        try:
            origin_cert = aws_cdn.ensure_certificate(
                clients["acm"], clients["route53"], host, then="CloudFront 와 로드밸런서 사이가 HTTPS 로 바뀝니다",
                tags={**tags, "recoder:region": req.region, aws_cdn.ORIGIN_TAG: "true"})
            out.certificate_arn = origin_cert.arn
            conflict = aws_cdn.point_origin(clients["route53"], zone_id, host, edge.dns_name, edge.zone_id)
            if conflict:
                out.notes.append(conflict)
                return out
            if not origin_cert.issued:
                out.status = "pending"
                out.notes += origin_cert.notes
                return out
            aws_edge.allow_https_from_cloudfront(clients["ec2"], edge.alb_security_group,
                                                 aws_cdn.cloudfront_prefix_list(clients["ec2"]))
            out.listener_arn = aws_edge.ensure_https_listener(
                clients["elbv2"], alb_arn=edge.alb_arn, tg_arn=edge.target_group_arn, certificate_arn=origin_cert.arn,
                http_listener_arn=edge.listener_arn, tags=tags)
            out.status = "ready"
        except Exception as exc:  # noqa: BLE001 - 원본 구간은 HTTP 로 계속(머리글 잠금 유지)
            out.status = "skipped"
            out.listener_arn = ""
            out.notes.append(f"CloudFront 와 로드밸런서 사이 HTTPS 를 준비하지 못했습니다({aws_infra.error_message(exc)}) — "
                             "지금은 HTTP 로 두고 원본 잠금 머리글로 막습니다. 권한 템플릿 스택을 2.0.35 템플릿으로 업데이트한 뒤 다시 배포하세요.")
        return out

    def _provision_cdn(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict,
                       edge: "aws_edge.EdgeResult") -> None:
        tags = self.resource_tags(req)
        notes: list[str] = []
        cert = None
        if req.custom_domain:
            cert = aws_cdn.ensure_certificate(clients["acm_global"], clients["route53"], req.custom_domain,
                                              tags={**tags, "recoder:region": req.region})
            notes += cert.notes
        #: 2.0.31 — CloudFront 뒤에서는 ALB 가 보는 주소가 모두 CloudFront 엣지라, WAF 의 IP 당 제한은 CloudFront 에 붙여야
        #: 실제 사용자 IP 로 센다(전역 범위 웹 ACL, us-east-1).
        waf = req.edge == "alb_waf"
        global_acl: Optional[str] = ""
        legacy_waf = False
        if waf:
            try:
                global_acl, _made = aws_edge.ensure_web_acl(
                    clients["wafv2_global"], aws_edge.global_acl_name(edge.name, req.region), rate_limit=req.waf_rate_limit,
                    window=req.waf_window, tags=tags, scope=aws_edge.CLOUDFRONT)
            except InfraError:
                raise
            except Exception as exc:  # noqa: BLE001
                if aws_infra.error_code(exc) in ("AccessDenied", "AccessDeniedException", "WAFInvalidPermissionPolicyException",
                                                 "UnauthorizedOperation"):
                    #: 옛 권한표 — 2.0.30 처럼 ALB 에 지역 웹 ACL 을 두고 배포는 계속한다(업데이트 안내).
                    legacy_waf = True
                    global_acl = None
                    acl_arn, _made = aws_edge.ensure_web_acl(clients["wafv2"], edge.name, rate_limit=req.waf_rate_limit,
                                                             window=req.waf_window, tags=tags)
                    attached = str(((clients["wafv2"].get_web_acl_for_resource(ResourceArn=edge.alb_arn) or {})
                                    .get("WebACL") or {}).get("ARN") or "")
                    if attached != acl_arn:
                        aws_edge.associate(clients["wafv2"], acl_arn, edge.alb_arn)
                    edge.web_acl_arn = acl_arn
                    notes.append("권한표가 옛 버전이라 WAF 를 로드밸런서에 두었습니다 — CloudFront 뒤에서는 IP 당 제한이 "
                                 "엣지 주소로 묶이니 권한 템플릿 스택을 2.0.31 템플릿으로 업데이트한 뒤 다시 배포하세요.")
                else:
                        raise InfraError("HTTPS 주소(CloudFront)에 붙일 WAF 웹 ACL 을 준비하지 못했습니다.",
                                     detail=aws_infra.error_message(exc),
                                     remedy="권한 템플릿 스택을 2.0.31 템플릿으로 업데이트하세요(전역 범위 WAF 권한). "
                                            "급하면 대규모 방패를 「로드밸런서만」으로 바꿔 배포할 수 있습니다.") from exc
        #: 2.0.35 원본 구간 HTTPS — 내 도메인이 Route 53 에 있으면 CloudFront → ALB 도 HTTPS 로(준비된 배포부터).
        tls = self._prepare_origin_tls(req, clients, edge, cert, tags) if req.custom_domain else aws_cdn.OriginTls()
        notes += tls.notes
        cdn = aws_cdn.ensure_distribution(
            clients["cloudfront"], name=edge.name, origin=tls.host if tls.ready else edge.dns_name, tags=tags,
            alias=req.custom_domain if cert is not None and cert.issued else "",
            certificate_arn=cert.arn if cert is not None and cert.issued else "",
            web_acl_id=global_acl, region=req.region, origin_https=tls.ready)
        self._cdn[id(req)] = cdn
        listeners = [edge.listener_arn] + ([tls.listener_arn] if tls.listener_arn else [])
        if waf and not legacy_waf:
            edge.web_acl_arn = str(global_acl)
            if cdn.web_acl_live:
                # CloudFront 쪽 WAF 가 이미 퍼져 있을 때만 로드밸런서의 옛 웹 ACL 을 뗀다(그 사이 보호 공백 없음).
                try:
                    note = aws_edge.detach_recoder_acl(clients["wafv2"], edge.alb_arn, edge.name,
                                                       reason="WAF 를 HTTPS 주소(CloudFront) 쪽으로 옮겼습니다")
                except Exception as exc:  # noqa: BLE001 - 보호는 이미 CloudFront 쪽에 있다
                    note = f"로드밸런서에 붙어 있던 옛 웹 ACL 을 떼지 못했습니다({aws_infra.error_message(exc)}) — 「AWS 리소스 정리」에서 지우세요."
                if note:
                    notes.append(note)
            elif aws_edge.find_web_acl(clients["wafv2"], edge.name) is not None:
                notes.append("WAF 를 CloudFront 에 붙이는 중입니다 — 퍼지는 동안(15분쯤) 로드밸런서의 WAF 도 그대로 두고, 다음 배포에서 뗍니다.")
        elif not waf:
            try:
                if aws_edge.delete_web_acl(clients["wafv2_global"], aws_edge.global_acl_name(edge.name, req.region),
                                           aws_edge.CLOUDFRONT, attempts=1):
                    notes.append("WAF 를 껐습니다 — CloudFront 의 웹 ACL 을 지웠습니다.")
            except Exception as exc:  # noqa: BLE001 - 떼어 냈으니 동작엔 영향 없음, 요금만
                if aws_infra.error_code(exc) in ("WAFAssociatedItemException",):
                    notes.append("WAF 를 껐습니다 — CloudFront 에서 웹 ACL 을 뗐고, 퍼진 뒤(15분쯤) 다시 배포하거나 「AWS 리소스 정리」로 지우면 요금이 멈춥니다.")
                else:
                    logger.info("CloudFront 웹 ACL 확인 생략: %s", aws_infra.error_message(exc))
        if (waf and not legacy_waf and cdn.web_acl_live) or not waf:
            # 2.0.31 의 리전 표시 없는 CloudFront 웹 ACL — 새 이름으로 옮겨 퍼졌거나 WAF 를 껐으면 지운다(요금만 남지 않게).
            try:
                if aws_edge.delete_web_acl(clients["wafv2_global"], edge.name, aws_edge.CLOUDFRONT, attempts=1):
                    notes.append("예전 이름의 CloudFront 웹 ACL 을 지웠습니다.")
            except Exception as exc:  # noqa: BLE001 - 아직 연결돼 있으면 다음 배포·정리에서
                logger.info("옛 CloudFront 웹 ACL 정리 생략: %s", aws_infra.error_message(exc))
        #: 원본 잠금 — CloudFront 가 붙이는 비밀 머리글이 맞는 요청만 앱으로. 머리글이 이미 퍼진 배포에서만 잠근다
        #: (방금 머리글을 넣은 옛 배포를 바로 잠그면 퍼지는 동안 사용자가 403 을 본다).
        if cdn.origin_secret_live:
            try:
                for listener in listeners:
                    aws_edge.lock_origin(clients["elbv2"], listener, edge.target_group_arn, cdn.origin_secret)
                rec.provisioned["origin_lock"] = "켜짐 — CloudFront 를 거치지 않은 요청은 로드밸런서가 403 으로 막습니다."
            except Exception as exc:  # noqa: BLE001 - 보안 그룹이 이미 CloudFront 주소만 받는다(한 겹 더 하는 보호)
                rec.provisioned["origin_lock"] = (f"켜지 못함({aws_infra.error_message(exc)}) — 권한 템플릿 스택을 최신으로 업데이트한 뒤 "
                                                  "다시 배포하세요. 보안 그룹은 계속 CloudFront 주소만 받습니다.")
        else:
            widened = False
            try:
                # 이미 잠긴 리스너(머리글 값이 바뀐 경우) — 새 값도 받게 넓혀, 퍼지는 동안 403 이 나지 않게 한다.
                widened = False
                for listener in listeners:
                    widened = aws_edge.widen_origin_lock(clients["elbv2"], listener, edge.target_group_arn,
                                                         cdn.origin_secret) or widened
            except Exception as exc:  # noqa: BLE001
                logger.info("원본 잠금 확인 생략: %s", aws_infra.error_message(exc))
            rec.provisioned["origin_lock"] = (
                "켜짐 — 새 잠금 값을 CloudFront 에 퍼뜨리는 중이라 옛 값과 새 값을 함께 받습니다(다음 배포에서 새 값만)."
                if widened else "준비 중 — CloudFront 에 잠금 머리글을 넣었습니다. 퍼진 뒤 다음 배포에서 잠급니다.")
        if cert is not None and cert.issued:
            if cert.zone_id:
                try:
                    dns_notes = aws_cdn.point_domain(clients["route53"], cert.zone_id, req.custom_domain, cdn.domain)
                except Exception as exc:  # noqa: BLE001 - 배포는 CloudFront 주소로 계속
                    dns_notes = [f"Route 53 에 별칭을 넣지 못했습니다({aws_infra.error_message(exc)}) — 도메인 DNS 에 "
                                 f"{req.custom_domain} 를 {cdn.domain} 로 연결하세요."]
                notes += dns_notes
                rec.provisioned["domain"] = (f"{req.custom_domain} — 인증서 발급됨, Route 53 별칭을 CloudFront 로 연결했습니다."
                                             if not dns_notes else f"{req.custom_domain} — 인증서 발급됨, DNS 는 직접 확인이 필요합니다(아래 안내).")
            else:
                rec.provisioned["domain"] = (f"{req.custom_domain} — 인증서 발급됨. 도메인 DNS 에 "
                                             f"{req.custom_domain} CNAME {cdn.domain} 을 넣으세요.")
        elif cert is not None:
            rec.provisioned["domain"] = f"{req.custom_domain} — 인증서 발급 대기 중(지금은 {cdn.domain} 로 서비스)"
        for i, note in enumerate(notes):
            rec.provisioned[f"domain_note_{i + 1}"] = note
        rec.provisioned["https"] = (f"켜짐 — https://{cdn.domain} ({'새로 만듦 · 전 세계에 퍼지는 데 5~15분' if cdn.created else '기존 것 사용'}), "
                                    "로드밸런서는 CloudFront 에서 오는 연결만 받습니다.")
        rec.provisioned["origin_tls"] = (
            f"켜짐 — CloudFront 와 로드밸런서 사이도 HTTPS({tls.host}, TLS 1.2 이상)입니다." if tls.ready else
            "준비 중 — 로드밸런서 인증서 발급을 기다립니다. 발급 뒤 다음 배포에서 CloudFront 와 로드밸런서 사이를 HTTPS 로 바꿉니다." if tls.status == "pending" else
            "꺼짐 — CloudFront 와 로드밸런서 사이는 HTTP 입니다(원본 잠금 머리글로 막음). 내 도메인을 Route 53 에 두면 HTTPS 로 바뀝니다.")
        if waf:
            rec.provisioned["waf"] = (f"켜짐({'로드밸런서 — 권한표 업데이트 필요' if legacy_waf else 'CloudFront'}) — 사용자 IP당 {req.waf_window // 60}분 {req.waf_rate_limit:,}회 넘으면 차단 "
                                      "+ AWS 관리 규칙 2개")
        req.cloudfront_domain = cdn.domain

    def _start_database(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict,
                        target: aws_infra.NetworkTarget) -> None:
        if req.database != "rds":
            return
        runtime = self._runtime.get(id(req))
        if runtime is None or not runtime.managed_db:
            rec.provisioned["database"] = ("만들지 않음 — 앱이 PostgreSQL 을 쓰지 않거나 DB 접속 정보를 이미 넣었습니다."
                                           if runtime is not None else "만들지 않음 — 작업 폴더 없이 이미지로만 배포해 앱의 DB 를 알 수 없습니다.")
            return
        if (req.environment or "").strip().lower() == "production":
            # 2.0.32 — production 은 DB 접속 정보를 비밀 참조로 넣는다. 실행 역할이 그 비밀을 못 읽으면 태스크가
            # 시작하지 못하므로(ResourceInitializationError) RDS 를 만들기 **전에** 확인한다.
            self._check_db_secret_access(req, rec, clients)
        handle = aws_rds.start(clients["rds"], clients["ec2"], clients["secretsmanager"], service=req.service,
                               vpc_id=target.vpc_id, task_security_groups=list(req.security_group_ids),
                               tags=self.resource_tags(req))
        self._db[id(req)] = handle
        rec.provisioned["database"] = (f"관리형 DB {handle.identifier} 만드는 중(5~10분 — 그 사이 이미지를 빌드합니다)"
                                       if handle.created else f"관리형 DB {handle.identifier} 다시 사용")

    def _check_db_secret_access(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict) -> None:
        exec_arn, _task = self._resolve_role_arns(req, clients)
        partition, account = exec_arn.split(":")[1], exec_arn.split(":")[4]
        secret_arn = f"arn:{partition}:secretsmanager:{req.region}:{account}:secret:{aws_rds.secret_name(req.service)}-AbCdEf"
        try:
            iam = boto3_client_like(clients, "iam")
            result = iam.simulate_principal_policy(PolicySourceArn=exec_arn, ActionNames=["secretsmanager:GetSecretValue"],
                                                   ResourceArns=[secret_arn])
            decision = str((result.get("EvaluationResults") or [{}])[0].get("EvalDecision", ""))
        except Exception as exc:  # noqa: BLE001 - 확인 권한이 없으면(학교 계정 등) 막지 않고 알린다
            rec.provisioned["database_secret_check"] = (f"실행 역할이 DB 접속 정보 비밀을 읽을 수 있는지 확인하지 못했습니다"
                                                        f"({aws_infra.error_message(exc)}) — 태스크가 시작하지 못하면 아래 권한을 추가하세요.")
            return
        if decision == "allowed":
            return
        role = exec_arn.rsplit("/", 1)[-1]
        #: 2.0.33 이 서비스·이 리전의 DB 비밀 하나만 읽게 한다(전에는 모든 리전의 recoder/* 전체였다).
        #: 정책 이름도 서비스마다 달라야 한다 — 같은 이름으로 put-role-policy 하면 앞 서비스의 권한을 덮어 쓴다.
        doc = json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
            "Resource": f"arn:{partition}:secretsmanager:{req.region}:{account}:secret:{aws_rds.secret_name(req.service)}-*"}]},
            separators=(",", ":"))
        policy_name = ("recoder-db-secret-" + re.sub(r"[^A-Za-z0-9+=,.@_-]", "-", f"{req.service}-{req.region}"))[:128]
        raise InfraError(
            "production 관리형 DB 는 접속 정보를 Secrets Manager 비밀로 넣는데, ECS 실행 역할이 그 비밀을 읽을 권한이 없습니다.",
            detail=f"실행 역할 {exec_arn} 의 secretsmanager:GetSecretValue 판정: {decision or '알 수 없음'}",
            remedy=("관리자 권한으로 한 번 실행한 뒤 다시 배포하세요(이 서비스의 DB 비밀 하나만 읽게 함): "
                    f"aws iam put-role-policy --role-name {role} --policy-name {policy_name} --policy-document '{doc}'. "
                    "또는 staging 으로 배포하면 접속 정보를 환경변수로 넣어 이 권한 없이 동작합니다. "
                    + self._created_so_far(rec)))

    def _created_so_far(self, rec: ECSDeployRecord) -> str:
        """DB 비밀 권한 오류 안내의 끝 문장 — 이미 만든 요금 리소스가 있으면 사실대로 말한다."""
        left = self._billing_resources(rec)
        if not left:
            return "요금이 나오는 리소스는 아직 아무것도 만들지 않았습니다."
        rec.provisioned["billing_resources"] = ", ".join(left)
        return (f"다만 {', '.join(left)} 은(는) 이미 만들어져 요금이 나옵니다 — 다시 배포하면 그대로 쓰고, "
                "그만 쓸 거면 「AWS 리소스 정리」에서 지우세요.")

    async def _step_database_ready(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict) -> None:
        """RDS 가 준비되면 접속 정보를 비밀에 쓰고, 태스크에 넣을 값(staging)·참조(production)와 초기화 컨테이너를 정한다."""
        handle = self._db.get(id(req))
        runtime = self._runtime.get(id(req))
        if handle is None or runtime is None:
            return
        loop = asyncio.get_running_loop()

        def on_wait(status: str, waited: float) -> None:
            rec.provisioned["database"] = f"관리형 DB {handle.identifier} 준비 기다리는 중({status}, {int(waited // 60)}분 지남)"
            self._save_progress()

        def _work() -> dict:
            db = aws_rds.wait(clients["rds"], handle.identifier, on_wait=on_wait)
            values = aws_rds.connection(db["Endpoint"], handle.password, runtime.managed_db.get("env_names", []))
            aws_rds.store_connection(clients["secretsmanager"], handle.secret_arn, values)
            return values

        values = await loop.run_in_executor(None, _work)
        production = (req.environment or "").strip().lower() == "production"
        if production:
            refs = aws_rds.secret_refs(handle.secret_arn, sorted(values))
            req.secret_refs = {**refs, **dict(req.secret_refs)}
            rec.provisioned["database_secret"] = (
                f"접속 정보는 Secrets Manager({aws_rds.secret_name(req.service)}) 참조로 넣었습니다 — 태스크 실행 역할에 "
                "이 비밀의 secretsmanager:GetSecretValue 권한이 있어야 태스크가 시작합니다.")
        else:
            for name, value in values.items():
                runtime.env.setdefault(name, value)
        init_sql = runtime.managed_db.get("init_sql_b64", "")
        if init_sql:
            init = aws_rds.init_container(init_sql, plain_url="" if production else values["DATABASE_URL"],
                                          secret_url_ref=f"{handle.secret_arn}:DATABASE_URL::" if production else "")
            runtime.sidecars.append(init)
            runtime.depends_on.append({"containerName": init["name"], "condition": "COMPLETE"})
        rec.provisioned["database"] = (f"관리형 DB {handle.identifier} 준비됨 — 데이터가 태스크와 상관없이 남습니다"
                                       + (f", 비어 있으면 처음 한 번 {runtime.managed_db.get('init_name')} 로 테이블을 만듭니다" if init_sql else "")
                                       + ".")

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
            # provision 을 끄면 **리포지토리도 만들지 않는다.** 예전에는
            # 여기만 예외적으로 만들고 있어서 "아무것도 만들지 않는다"는
            # 약속이 반만 지켜졌다.
            if req.provision:
                repository_uri = aws_infra.ensure_ecr_repository(
                    clients["ecr"], repo_name, tags=self.resource_tags(req)
                )
                rec.provisioned["ecr_repo"] = repo_name
            else:
                repository_uri = aws_infra.require_ecr_repository(
                    clients["ecr"], repo_name
                )
                rec.provisioned["ecr_repo"] = f"{repo_name} (기존)"
            return ecs_build.build_and_push(
                clients["ecr"],
                workspace_path=req.workspace_path or "",
                repository_uri=repository_uri,
                tag=tag,
                dockerfile=req.dockerfile,
                on_progress=lambda key: loop.call_soon_threadsafe(self._advance_progress, rec, key),
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

        # **검사가 못 돈 것도 차단 사유다.** 취약점 0 건이 아니라 "검사 안 됨"
        # 이므로, 위 세 건과 별개로 반드시 표면화한다.
        if getattr(result, "tool_errors", None):
            names = ", ".join(result.tool_errors)
            reasons.append(f"실행되지 못한 검사({names})")
            fixes.append(
                "해당 스캐너를 설치하거나(trivy·gitleaks·hadolint), ECR "
                "이미지를 못 받아온 경우라면 권한표의 ecr:BatchGetImage · "
                "ecr:GetDownloadUrlForLayer 를 확인한 뒤 다시 배포하세요. "
                "검사하지 않은 이미지는 배포하지 않습니다."
            )

        if not reasons:   # 방어: blocked 인데 이유를 못 찾은 경우
            reasons.append("보안 검사 위반")
            fixes.append("사이드바의 검사 결과를 확인하세요.")

        return "보안 검사 차단: " + ", ".join(reasons), " ".join(fixes)

    async def _step_scan_sources(
        self, req: ECSDeployRequest, rec: ECSDeployRecord
    ) -> ECSDeployRecord:
        """이미지 없이 할 수 있는 검사 — Dockerfile(hadolint) 과 시크릿.

        **못 돌렸으면 못 돌렸다고 남긴다.** 이 단계가 조용히 넘어가면
        `scan_result` 가 None 인 채로 게이트를 지나가고, 사용자는 "보안 검사
        켜고 배포했다"고 믿는데 시크릿 검사도 Dockerfile 검사도 한 적이 없는
        상태가 된다. 로그만 남기는 것으로는 부족하다 — 사이드바는 로그가
        아니라 `provisioned` 의 경고를 읽는다.
        """
        if not req.workspace_path:
            self._add_scan_gaps(
                rec, ["작업 폴더 없음 — 시크릿·Dockerfile 검사 불가"]
            )
            logger.warning(
                "작업 폴더가 없어 Dockerfile·시크릿 검사를 건너뜁니다."
            )
            return rec

        dockerfile_path: str | None = None
        candidate = Path(req.workspace_path) / req.dockerfile
        if candidate.is_file():
            dockerfile_path = str(candidate)
        else:
            self._add_scan_gaps(
                rec, [f"Dockerfile 없음({candidate}) — Dockerfile 검사 불가"]
            )
            logger.warning("Dockerfile 을 찾지 못했습니다: %s", candidate)

        rec.scan_result = await security_scanner.scan_all(
            image=None,
            dockerfile_path=dockerfile_path,
            repo_path=req.workspace_path,
        )
        self._record_scan_gaps(rec)
        return rec

    @staticmethod
    def _add_scan_gaps(rec: ECSDeployRecord, reasons: list[str]) -> None:
        """못 돌린 검사를 **누적하되 중복 없이** 모아 한 줄로 보여준다.

        ## 왜 문자열을 이어 붙이면 안 되나

        이 함수는 배포 한 번에 여러 번 불린다 — 빌드 전 소스 검사, 빌드 후
        이미지 검사, 그리고 작업 폴더·Dockerfile 이 없을 때. 스캐너가 하나도
        안 깔린 **가장 흔한 상황**에서는 두 번의 `tool_errors` 가 겹치므로,
        앞 문구에 뒷 문구를 그냥 이어 붙이면 같은 말이 두 번 나온다.

        사유를 **모아 두고 문구는 매번 다시 렌더**한다. 그러면 몇 번을
        불러도 결과가 같다.

        ## 왜 `provisioned` 에 안 담나

        `provisioned` 는 `dict[str, str]` 이다. 거기에 list 를 in-place 로
        넣으면 파이단틱이 검사하지 않아 **쓸 때는 조용히 통과**하고,
        `_load_records()` 가 다시 읽을 때 ValidationError 로 기록 한 건이
        통째로 버려진다. 진행 중이던 배포가 그렇게 사라지면 비용 경고도
        클러스터 이름도 없어져 떠 있는 태스크를 멈출 방법이 사라진다 —
        `_save_records` 가 막으려던 바로 그 상황이다.
        그래서 전용 필드 `ECSDeployRecord.scan_gaps` 를 쓴다.
        """
        gaps = list(rec.scan_gaps)
        for reason in reasons:
            reason = (reason or "").strip()
            if reason and reason not in gaps:
                gaps.append(reason)
        if not gaps:
            return
        rec.scan_gaps = gaps
        rec.provisioned["scan_warning"] = (
            "실행되지 않은 보안 검사가 있습니다: "
            + ", ".join(gaps)
            + " — 이 배포의 '취약점 0건'은 검사 결과가 아닙니다."
        )
        logger.warning("보안 검사 일부 미실행: %s", ", ".join(gaps))

    @classmethod
    def _record_scan_gaps(cls, rec: ECSDeployRecord) -> None:
        """스캐너가 보고한 미실행 목록을 경고에 합친다."""
        if rec.scan_result is None or not rec.scan_result.tool_errors:
            return
        cls._add_scan_gaps(rec, list(rec.scan_result.tool_errors))

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
        """SBOM 을 만든다. **못 만들면 배포를 세운다.**

        예전에는 syft 가 없거나 죽어도 빈 기록이 돌아왔고, 그런데도
        `sbom_version` 을 찍고 배포를 계속했다. 그러면 두 가지가 동시에
        거짓이 된다 — 기록에는 **존재하지 않는 SBOM 의 버전**이 남고,
        정책 게이트는 `generate_sbom=True` 라는 **요청**만 보고 통과시킨
        뒤 SBOM 없는 이미지가 배포된다. 프리셋 정책 1번이 "SBOM 없는
        배포 차단"인데 그 규칙을 우리 손으로 우회한 셈이다.

        사용자가 SBOM 을 원하지 않으면 `generate_sbom=False` 로 끄면 된다.
        켜 놓고 조용히 실패하는 경우를 없앤다.
        """
        sbom = await sbom_generator.generate(image)
        rec.sbom = sbom
        rec.sbom_path = sbom.sbom_path

        if not sbom.sbom_path:
            raise InfraError(
                "SBOM 을 만들지 못해 배포를 중단했습니다.",
                detail=sbom.error or "syft 가 결과 파일을 남기지 않았습니다.",
                remedy="syft 를 설치하거나(https://github.com/anchore/syft), "
                       "SBOM 이 필요 없다면 배포 요청에서 generate_sbom 을 "
                       "꺼 주세요. 정책상 SBOM 없는 배포는 차단됩니다.",
            )

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
        # 기본 이름 `ecsTaskRole` 은 AWS 가 만들어 주는 역할이 **아니다**
        # (실행 역할과 달리 콘솔이 자동 생성해 주지 않고, 우리 문서도 만들라고
        # 안내하지 않는다). 그런데 태스크 정의에는 항상 들어갔다. 그래서
        # 평범한 계정에서 기본 설정으로 배포하면 없는 역할을 가리켜 PassRole
        # 거부나 "unable to assume the role" 로 죽었다 — 정작 태스크 역할은
        # 앱이 AWS API 를 부를 때만 필요하고, 샘플 앱을 포함한 대부분의
        # 컨테이너는 필요 없는데도.
        #
        # 판단 근거는 `resolve_roles()` 가 돌려준 **빈 문자열 하나**다. 여기서
        # 환경변수를 다시 읽어 따로 판단하면 권한표(`build_policy`)와 배포가
        # 서로 다른 근거로 갈라진다 — 이 파일이 이미 한 번 겪은 실패다.
        task_arn = ""
        if task_name:
            task_arn = aws_policy._arn(
                "iam", f"role/{task_name}", ctx, global_service=True
            )
        return exec_arn, task_arn

    async def _step_register_task_definition(
        self, req: ECSDeployRequest, clients: dict, image: str
    ) -> tuple[str, Optional[str]]:
        """Task Definition JSON 생성 → AWS 등록. 이전 revision ARN 반환."""
        ecs = clients["ecs"]

        # 롤백 대상 = **마지막으로 정상 배포가 끝난** 태스크 정의(이 PC 의 기록).
        #
        # 예전에는 서비스가 지금 가리키는 태스크 정의를 그대로 썼다. 직전 배포가 실패했으면
        # 그건 실패한 버전이다(v2 정상 → v3 실패 → v4 의 롤백 대상이 v3). 기록이 없을 때만
        # (이 PC 에서 처음 배포하는 기존 서비스) 서비스가 **안정 상태**일 때의 태스크 정의를 쓴다.
        # 서비스가 아직 없을 수도 있다 — 그건 정상이므로 조용히 넘어간다.
        steady_arn = ""
        try:
            services = ecs.describe_services(
                cluster=req.cluster, services=[req.service]
            ).get("services", [])
            if services:
                steady_arn = ecs_stable.steady_task_definition(services[0])
        except Exception as exc:  # noqa: BLE001
            logger.debug("이전 태스크 정의 조회 생략: %s", aws_infra.error_message(exc))

        exec_arn, task_arn = self._resolve_role_arns(req, clients)
        task_def = self._render_task_definition(
            req, image=image, execution_role_arn=exec_arn, task_role_arn=task_arn,
            runtime=self._runtime.pop(id(req), None),
            shield_decision=self._shield.get(id(req)),
        )

        try:
            resp = ecs.register_task_definition(**task_def)
        except Exception as exc:  # noqa: BLE001
            raise InfraError(
                "태스크 정의를 등록하지 못했습니다.",
                detail=aws_infra.error_message(exc),
                # 태스크 역할은 안 붙는 것이 기본이다. 빈 값을 그대로 문장에
                # 끼워 넣으면 "... 과  에 대해" 처럼 말이 안 되는 안내가 나가고,
                # 사용자는 있지도 않은 역할을 찾게 된다.
                remedy=(
                    f"권한표의 iam:PassRole 이 {exec_arn} 과 {task_arn} 에 대해 "
                    "허용돼 있는지 확인하세요. 학교 계정은 두 역할 모두 "
                    "LabRole 이어야 합니다."
                    if task_arn else
                    f"권한표의 iam:PassRole 이 {exec_arn} 에 대해 허용돼 있는지 "
                    "확인하세요. 학교 계정은 실행 역할이 LabRole 이어야 합니다."
                ),
            ) from exc

        arn = resp["taskDefinition"]["taskDefinitionArn"]
        logger.info("태스크 정의 등록: %s", arn)
        aws_infra.tag_created(ecs, "ecs", arn, self.resource_tags(req))
        prev_arn = self._rollback_target(req, arn, steady_arn)
        return arn, prev_arn

    @staticmethod
    def _rollback_target(req: ECSDeployRequest, new_arn: str, steady_arn: str = "") -> Optional[str]:
        """이번 배포가 실패하면 되돌아갈 버전 — 마지막 성공 배포(새 리비전 자신은 제외). 없으면 None."""
        try:
            known = ecs_stable.last_success(req.region, req.cluster, req.service, exclude=new_arn)
        except Exception as exc:  # noqa: BLE001 - 기록을 못 읽으면 안정 상태 판정으로만
            logger.warning("성공 배포 기록을 읽지 못했습니다: %s", exc)
            known = ""
        if known:
            return known
        if steady_arn and ecs_stable._rev(steady_arn) != ecs_stable._rev(new_arn):
            return steady_arn
        return None

    @staticmethod
    def _record_success(req: ECSDeployRequest, rec: ECSDeployRecord) -> None:
        """정상 배포(헬스·주소 확인까지 끝남)만 롤백 기준으로 적는다."""
        if not rec.task_definition_arn:
            return
        try:
            ecs_stable.record_success(req.region, req.cluster, req.service, rec.task_definition_arn)
        except Exception as exc:  # noqa: BLE001 - 기록 실패가 배포 성공을 바꾸지 않는다
            logger.warning("성공 배포 기록을 남기지 못했습니다: %s", exc)

    # ------------------------------------------------------------------
    # 7단계: 서비스 확보
    # ------------------------------------------------------------------

    async def _tell_watcher(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict) -> None:
        """2.0.48 — 감시자가 설치돼 있으면 '이전 버전'(밤 롤백 링크의 대상)을 바꾼다. best-effort.

        3.0.2 — 대상은 언제나 **마지막으로 정상 배포된 버전**이다(previous_task_definition_arn 은 이제
        ecs_stable 기록에서 온다). 이번 배포가 성공했으면 그 직전 성공 버전, 실패했으면 마지막 성공 버전 —
        실패한 버전은 절대 기준이 되지 않는다."""
        if not rec.task_definition_arn or not rec.previous_task_definition_arn:
            return
        try:
            try:
                import watcher  # type: ignore
            except ImportError:  # pragma: no cover
                from core import watcher  # type: ignore
            loop = asyncio.get_running_loop()
            changed = await loop.run_in_executor(None, lambda: watcher.set_versions(
                watcher.from_clients(clients), region=req.region, cluster=req.cluster, service=req.service,
                current_td=rec.task_definition_arn or "", stable_td=rec.previous_task_definition_arn or ""))
            if changed:
                rec.provisioned["watcher"] = (
                    "24시간 감시자의 롤백 기준을 마지막으로 정상 배포된 버전"
                    f"({ecs_stable._rev(rec.previous_task_definition_arn or '')})으로 맞췄습니다.")
        except Exception as exc:  # noqa: BLE001 - 감시자 갱신 실패가 배포 성공을 바꾸지 않는다
            logger.info("감시자 기준 버전 갱신 생략: %s", exc)

    async def _step_alerts(self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict) -> None:
        """방패 접속 기록으로 요청·차단·오류 지표와 경보를 만들고, Core 감시 목록에 올린다(2.0.25, best-effort)."""
        if not req.alerts:
            rec.provisioned["alerts"] = "꺼짐 — 배포 설정에서 감지 알림을 껐습니다."
            return
        shield_on = bool(self._shield.get(id(req)) and self._shield[id(req)].on)
        try:
            import ops_alerts  # type: ignore
        except ImportError:  # pragma: no cover
            from core import ops_alerts  # type: ignore
        loop = asyncio.get_running_loop()

        def _work() -> dict:
            result = (ops_alerts.setup_aws(clients, service=req.service, log_group=self.log_group_name(req),
                                           email=req.alert_email, tags=self.resource_tags(req))
                      if shield_on else {"filters": [], "alarms": [], "topic": "", "email": "", "notes": []})
            ops_alerts.register(region=req.region, cluster=req.cluster, service=req.service,
                                log_group=self.log_group_name(req), health_path=req.health_check_path,
                                metrics=bool(result["filters"]))
            return result

        try:
            result = await loop.run_in_executor(None, _work)
        except Exception as exc:  # noqa: BLE001
            rec.provisioned["alerts"] = f"준비하지 못했습니다 — {aws_infra.error_message(exc)}"
            return
        if shield_on and result["filters"]:
            rec.provisioned["alerts"] = (
                "켜짐 — 요청 급증·차단 급증·오류 증가·태스크 부족을 1분마다 봅니다(Core 가 켜져 있을 때 VS Code·디스코드 알림)"
                + (", PC 가 꺼져 있어도 CloudWatch 경보가 메일로 보냅니다" if result["email"] else "")
                + ".")
        else:
            rec.provisioned["alerts"] = ("부분 — 방패가 꺼져 요청 수를 셀 수 없어 태스크 부족(장애)만 봅니다."
                                         if not shield_on else "부분 — 요청 지표를 만들지 못해 태스크 부족(장애)만 봅니다.")
        for i, note in enumerate(result["notes"]):
            rec.provisioned[f"alerts_note_{i + 1}"] = note

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
                assign_public_ip=req.assign_public_ip,
                target_group_arn=req.target_group_arn,
                #: 방패가 켜졌으면 로드 밸런서는 방패 컨테이너(공개 포트)로 보낸다.
                container_name=(shield_mod.CONTAINER if (self._shield.get(id(req)) and self._shield[id(req)].on)
                                else req.container_name),
                container_port=req.container_port,
                tags=self.resource_tags(req),
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

        edge = self._edge.get(id(req))
        cdn = self._cdn.get(id(req))

        def _work() -> str:
            if cdn is not None:
                return cdn.url
            if req.cloudfront_domain:
                return "https://" + req.cloudfront_domain
            if edge is not None:
                return edge.url
            return aws_infra.wait_for_public_url(
                clients["ecs"],
                clients["ec2"],
                cluster=req.cluster,
                service=req.service,
                port=req.container_port,
                timeout=req.url_wait_timeout,
                task_definition_arn=rec.task_definition_arn or "",
            )

        try:
            rec.service_url = await loop.run_in_executor(None, _work)
            # 주소를 만들었다고 접속이 되는 건 아니다. 카드 DoD 1번은
            # "URL 로 **접속됨**"이므로 실제로 한 번 받아본다. 여기서
            # 확인하지 않으면 "성공"이라고 보고한 주소가 열리지 않는
            # 상황이 그대로 사용자에게 간다.
            # ALB 주소는 만든 뒤 DNS 가 퍼지고 대상이 healthy 가 되기까지 1~3분 걸린다 — 더 오래 기다린다.
            attempts = max(6, int(min(req.url_wait_timeout, 300) // 10)) if (edge is not None or cdn is not None) else 6
            reachable, detail = await loop.run_in_executor(
                None,
                lambda: _probe_http(
                    (rec.service_url or "") + req.health_check_path, require_success=True,
                    attempts=attempts, interval=10.0 if (edge is not None or cdn is not None) else 5.0,
                ),
            )
            if not reachable and not req.cloudfront_domain and edge is None:
                # 그 사이 태스크가 교체됐을 수 있다(앱이 기동 중 죽으면 ECS 가 새 태스크를 띄우고 IP 가 바뀐다) — 다시 찾아 한 번 더.
                fresh = await loop.run_in_executor(None, lambda: aws_infra._probe_public_ip(
                    clients["ecs"], clients["ec2"], cluster=req.cluster, service=req.service,
                    task_definition_arn=rec.task_definition_arn or "").ip)
                if fresh:
                    url = f"http://{fresh}" if req.container_port in (80, 443) else f"http://{fresh}:{req.container_port}"
                    if url != rec.service_url:
                        rec.service_url = url
                        reachable, detail = await loop.run_in_executor(
                            None, lambda: _probe_http(url + req.health_check_path, require_success=True))
            if not reachable and cdn is not None and cdn.created:
                #: 새 CloudFront 배포는 전 세계에 퍼지는 데 5~15분 걸린다 — 실패가 아니다(태스크는 healthy).
                rec.provisioned["url_warning"] = (
                    f"HTTPS 주소({rec.service_url})가 아직 퍼지는 중입니다({detail}) — 새 CloudFront 는 5~15분 뒤 열립니다. "
                    f"그 전에는 로드밸런서도 CloudFront 에서 온 연결만 받아 바로 열 수 없습니다.")
                logger.info("CloudFront 전파 대기: %s (%s)", rec.service_url, detail)
            elif not reachable:
                diagnosis = await self._diagnose_stopped(req, rec, clients)
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
                if diagnosis:
                    #: 앱이 시작하다 죽고 있다 — "잠시 뒤 다시" 가 아니라 원인을 먼저 보여 준다.
                    rec.provisioned["diagnosis"] = "\n".join(diagnosis)
                    rec.error_message = (
                        f"주소({rec.service_url})가 응답하지 않습니다 — 앱 컨테이너가 시작하다 멈추고 있습니다."
                    )
                    rec.error_detail = "\n".join(diagnosis)
                    rec.error_remedy = (
                        "아래 앱 로그의 오류를 고친 뒤 다시 배포하세요. 같은 오류가 Local Docker 에서도 나는지 먼저 확인하면 빠릅니다."
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

    async def _diagnose_stopped(
        self, req: ECSDeployRequest, rec: ECSDeployRecord, clients: dict
    ) -> list[str]:
        """멈춘 태스크의 이유·종료 코드·앱 로그 끝부분. 어떤 실패도 배포 결과를 바꾸지 않는다."""
        if "logs" not in clients or "ecs" not in clients:
            return []
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: aws_infra.diagnose_stopped_tasks(
                clients["ecs"], clients["logs"], cluster=req.cluster, service=req.service,
                log_group=self.log_group_name(req), app_container=req.container_name,
                task_definition_arn=rec.task_definition_arn or ""))
        except Exception as exc:  # noqa: BLE001
            logger.info("멈춘 태스크 진단 생략: %s", exc)
            return []

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
            # **서비스 전체**의 실행 태스크 수를 본다. PRIMARY 배포의
            # runningCount 는 **새 리비전만** 센다. 멀쩡히 돌던 서비스를
            # 새 리비전으로 갱신하다 실패하면 PRIMARY 는 0 인데 이전
            # ACTIVE 배포의 태스크는 전부 살아 있다. 그 상태에서 "떠 있는
            # 게 없다"고 판단하면 아래 중지 로직이 **멀쩡한 이전 버전을
            # 통째로 내린다** — 사용자가 요청하지도 않은 장애가 된다.
            rec.running_task_count = int(svc.get("runningCount") or 0)

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

        1) ECS 자체 서킷 브레이커 — 실패 태스크의 무한 재시작을 멈춘다.
           일반 ReCoder 배포에서는 자동 롤백을 끈다. 이전 버전 복귀는
           사람이 승인한 `/api/deploy/ecs/rollback`만 할 수 있다.
        2) 과거에 자동 롤백이 켜진 서비스가 되돌아간 경우에는 그것을
           성공으로 착각하지 않는다 (`ecs_rolled_back`).
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

        if rec.running_task_count > 0 or not rec.service_created_by_this_run:
            # 두 가지 중 하나라도 참이면 손대지 않는다.
            #
            #  · 떠 있는 태스크가 있다 — 느리게 기동하는 정상 앱이거나,
            #    갱신 실패 후에도 살아 있는 **이전 버전**이다.
            #  · 이 서비스는 우리가 만든 게 아니다 — 원래 돌던 사용자의
            #    서비스를 우리 판단으로 내리면 요청하지 않은 장애다.
            #
            # 자동 중지는 "이번 실행이 새로 만들었고, 아무것도 못 뜬"
            # 경우에만 한다. 그때는 내려도 잃을 게 없다.
            rec.provisioned["cost_warning"] = (
                f"태스크 {rec.running_task_count}개가 아직 실행 중입니다 — "
                "요금이 계속 발생합니다. Deploy 캔버스 › 배포 상태의 "
                "'서비스 중지'(배포 중지)로 태스크 수를 0 으로 내리세요."
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

        # **태스크를 0 개로 요청한 배포는 이야기가 다르다.**
        #
        # 이 함수는 `desired_count >= 1` 만 오던 시절에 쓰였다. 태스크 0 개
        # 갈래에도 취소 확인을 넣으면서 여기로 오게 됐는데, 아래 두 문장이
        # 그 경우에는 **둘 다 거짓말**이 된다.
        #
        #   - "태스크는 그대로 두었습니다" — `ensure_service` 가 이미
        #     `desiredCount=0` 으로 갱신했다. 사용자 앱은 내려가 있는데
        #     화면에는 그대로 두었다고 뜬다. 게다가 이 문구는
        #     `cost_warning` 키로 나가서 사이드바가 "요금이 나가고 있다"로
        #     보여준다 — 사실은 정반대다.
        #   - `halt_service` 로 0 으로 내리기 — 이미 0 이다. 실패 경로에서
        #     굳이 AWS 를 한 번 더 부르는 셈이고, 여기 온 원인이 자격증명
        #     문제라면 그 호출이 또 터진다.
        if req.desired_count == 0:
            if not rec.service_created_by_this_run:
                rec.provisioned["service_warning"] = (
                    "취소했지만 이 배포는 이미 기존 서비스의 태스크 수를 "
                    "0 으로 바꾼 뒤였습니다 — 원래 앱이 내려가 있습니다. "
                    "되돌리려면 태스크 수를 원래대로 올려 다시 배포하세요."
                )
            return

        loop = asyncio.get_running_loop()

        if not rec.service_created_by_this_run:
            # **취소만으로는 롤아웃이 안 멈춘다.**
            #
            # 기존 서비스를 갱신하던 중이었다면 `ensure_service` 가 이미
            # `UpdateService` 로 **새(=취소된) 태스크 정의로 롤아웃을
            # 시작**했다. 여기서 그냥 돌아가면 ECS 는 태스크를 0 으로 내리지
            # 않았다는 이유로 안심하는 게 아니라, 계속해서 옛 태스크를 방금
            # 취소한 버전으로 갈아치운다. 기록은 CANCELLED 인데 실제 배포는
            # 끝까지 나간다.
            #
            # 그래서 **이전 리비전으로 되돌린다.** 이전 리비전을 모르면
            # (첫 배포라 이전이 없었거나 등록 전에 취소된 경우) 되돌릴 대상이
            # 없으므로, 그때만 사용자가 직접 멈추도록 안내한다.
            prev = rec.previous_task_definition_arn
            if prev:
                rec.provisioned["rollback"] = await loop.run_in_executor(
                    None,
                    lambda: aws_infra.revert_service_task_definition(
                        clients["ecs"], cluster=req.cluster,
                        service=req.service, task_definition=prev,
                    ),
                )
            else:
                rec.provisioned["cost_warning"] = (
                    "취소했지만 이 배포는 이미 기존 서비스의 롤아웃을 "
                    "시작한 뒤였고, 되돌릴 이전 태스크 정의를 찾지 못했습니다. "
                    "Deploy 캔버스 › 배포 상태의 '서비스 중지'(배포 중지)로 태스크 수를 0 으로 "
                    "내리거나 AWS 콘솔에서 서비스를 직접 되돌리세요."
                )
            return

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
        ECSAgent._warn_billing_leftovers(req, rec)
        if not rec.provisioned.get("service"):
            return
        if "halt" in rec.provisioned or "cost_warning" in rec.provisioned:
            return
        rec.provisioned["cost_warning"] = (
            f"서비스 '{req.service}' 는 이미 만들어졌고 태스크가 떠 있을 수 "
            "있습니다 — 배포는 실패했지만 요금은 계속 발생합니다. "
            "Deploy 캔버스 › 배포 상태의 '서비스 중지'(배포 중지)로 태스크 수를 0 으로 "
            "내리거나, 원인을 고친 뒤 다시 배포하세요."
        )

    @staticmethod
    def _billing_resources(rec: ECSDeployRecord) -> list[str]:
        """이번 배포가 이미 만들었거나 다시 쓰기 시작한, 태스크와 상관없이 요금이 나오는 리소스."""
        out: list[str] = []
        if rec.provisioned.get("load_balancer"):
            out.append("로드밸런서 " + str(rec.provisioned["load_balancer"]).split(" — ", 1)[0])
        if str(rec.provisioned.get("waf", "")).startswith("켜짐"):
            out.append("WAF")
        if str(rec.provisioned.get("https", "")).startswith("켜짐"):
            out.append("HTTPS 주소(CloudFront)")
        #: "관리형 DB(RDS) — 비용 확인함" 은 승인 표시일 뿐이다. 실제로 만든(다시 쓴) DB 는 "관리형 DB <식별자> …".
        database = str(rec.provisioned.get("database", ""))
        parts = database.split(" ", 3)
        if database.startswith("관리형 DB ") and len(parts) >= 3:
            out.append(f"관리형 DB {parts[2]}")
        return out

    @staticmethod
    def _warn_billing_leftovers(req: ECSDeployRequest, rec: ECSDeployRecord) -> bool:
        """실패·취소로 끝났는데 남아 요금이 나오는 리소스를 기록하고 안내한다. 남은 것이 있으면 True."""
        left = ECSAgent._billing_resources(rec)
        if not left:
            return False
        rec.provisioned["billing_resources"] = ", ".join(left)
        if any(item.startswith("관리형 DB") for item in left) and "db_cost_warning" not in rec.provisioned:
            rec.provisioned["db_cost_warning"] = (
                "관리형 DB(RDS)는 배포가 실패하거나 취소돼도 남아 계속 요금이 나옵니다 — 다시 배포하면 그대로 쓰고(데이터 유지), "
                "그만 쓸 거면 「AWS 리소스 정리」에서 지우세요.")
        if any(item.startswith(("로드밸런서", "WAF", "HTTPS")) for item in left) and "edge_cost_warning" not in rec.provisioned:
            rec.provisioned["edge_cost_warning"] = (
                "로드밸런서(·WAF·HTTPS 주소)는 배포가 실패하거나 취소돼도 남아 있어 시간당 요금이 붙습니다 — "
                "다시 배포하면 그대로 다시 쓰고, 그만 쓸 거면 「AWS 리소스 정리」에서 지우세요.")
        return True

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
        runtime: Optional["ecs_runtime.RuntimePlan"] = None,
        shield_decision: Optional["shield_mod.ShieldDecision"] = None,
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

        #: 앱이 process.env.PORT / os.environ["PORT"] 로 포트를 정하면 ECS 가 여는 포트와 맞춰야 한다.
        #: 로컬 Docker 배포는 이미 PORT 를 넘긴다 — ECS 만 빠져 있었다. 사용자가 정한 값이 우선.
        env_vars = {"PORT": str(req.container_port), **dict(req.env_vars or {})}
        env_vars_list = [{"name": k, "value": v} for k, v in env_vars.items()]
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
            if container.get("name") == req.container_name and req.secret_refs:
                container["secrets"] = [
                    {"name": name, "valueFrom": arn} for name, arn in req.secret_refs.items()
                ]
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
        # 앱이 시작할 때 요구하는 DB·서명 키·모의 결제(staging) — `ecs_runtime.plan` 이 배포 시작 때 정했다.
        rendered = ecs_runtime.apply(rendered, req.container_name, runtime, image)
        if shield_decision is not None and shield_decision.on:
            #: 2.0.25 기본 방패 — 공개 포트는 nginx 가, 앱은 다음 포트에서. ALB 뒤면 X-Forwarded-For 로 실제 IP 를 본다.
            rendered = shield_mod.apply(rendered, req.container_name, shield_decision, health_path=req.health_check_path,
                                        trust_vpc_proxy=bool(req.target_group_arn), trust_cdn=bool(req.https))
        return rendered
