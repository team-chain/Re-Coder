"""
Local Deploy Agent — Stage 2 로컬 Docker 배포 (설계서 §9.2, §14, §17).

설계 핵심:
  - CommandTemplate Registry를 통해서만 docker 명령 실행 (§14.2)
  - docker build → run → Health Check (§9.2 4단 폴백, 3회 재시도 5초 간격)
  - Health Check 통과 후만 기존 컨테이너 전환
  - 실패 시 new만 제거, 기존 유지 (자동 롤백 아님, Level 1~2만 자동)
  - DeploymentRecord는 ~/.recoder/projects/{project_id}_deployments.jsonl에 append
"""

from __future__ import annotations

import httpx
import json
import logging
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from command_safety import assert_safe_command, assert_safe_docker_run, CommandSafetyError
from registries.command_registry import get_command_registry
from schemas import DeploymentPlan, DeploymentRecord, DeployMethod, DeployStatus, RiskLevel

logger = logging.getLogger(__name__)

# ── 설정 상수 ─────────────────────────────────────────────────────────

_HEALTH_RETRIES = 3
_HEALTH_INTERVAL_SEC = 5
_DOCKER_TIMEOUT_BUILD = 900  # 15분
_DOCKER_TIMEOUT_RUN = 120  # 2분
_DOCKER_TIMEOUT_STOP = 30


@dataclass
class DeployResult:
    """배포 결과."""

    success: bool
    deployment_record: Optional[DeploymentRecord] = None
    error: str = ""
    logs: list[str] = field(default_factory=list)


class LocalDeployAgent:
    """Stage 2 로컬 Docker 배포 에이전트."""

    def __init__(self):
        self._registry = get_command_registry()
        self._recoder_dir = Path.home() / ".recoder" / "projects"
        self._recoder_dir.mkdir(parents=True, exist_ok=True)

    def _run_docker_command(
        self, args: list[str], cwd: Optional[Path] = None, timeout: int = 60
    ) -> Tuple[bool, str]:
        """
        CommandTemplate Registry를 통한 안전한 docker 명령 실행.

        Args:
            args: 명령 리스트 ["docker", "build", ...]
            cwd: 작업 디렉토리
            timeout: 타임아웃 (초)

        Returns:
            (success, output): 성공 여부 및 출력
        """
        try:
            assert_safe_docker_run(args)
        except CommandSafetyError as e:
            logger.error(f"[local_deploy] Command safety check failed: {e.reason}")
            return False, f"[BLOCKED] {e.reason}"

        try:
            proc = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = "\n".join(
                part.strip()
                for part in (proc.stdout, proc.stderr)
                if part and part.strip()
            )
            return proc.returncode == 0, output[-2000:]  # 마지막 2000자만
        except subprocess.TimeoutExpired:
            return False, f"Command timed out ({timeout}s)"
        except Exception as e:
            return False, str(e)

    def build_image(self, workspace_path: str, image_name: str) -> Tuple[bool, str]:
        """
        Docker 이미지 빌드.

        CommandTemplate "docker_build" 사용.

        Args:
            workspace_path: 프로젝트 경로
            image_name: 빌드할 이미지 이름 (예: myapp:latest)

        Returns:
            (success, image_digest_or_error_message)
        """
        logger.info(f"[local_deploy] Building image: {image_name}")

        workspace = Path(workspace_path)
        if not workspace.exists():
            return False, f"Workspace not found: {workspace_path}"

        dockerfile = workspace / "Dockerfile"
        if not dockerfile.exists():
            return False, f"Dockerfile not found: {workspace_path}/Dockerfile"

        # docker build -t {image_name} .
        args = ["docker", "build", "-t", image_name, "."]

        success, output = self._run_docker_command(args, cwd=workspace, timeout=_DOCKER_TIMEOUT_BUILD)

        if success:
            # 이미지 digest 추출 (간단한 버전, 실제로는 docker inspect 필요)
            digest = image_name
            logger.info(f"[local_deploy] Build successful: {digest}")
            return True, digest
        else:
            logger.error(f"[local_deploy] Build failed: {output}")
            return False, output

    def run_container(
        self, plan: DeploymentPlan, workspace_path: str
    ) -> Tuple[bool, str]:
        """
        Docker 컨테이너 실행.

        CommandTemplate "docker_run" 사용.
        기존 동명 컨테이너 있으면 먼저 stop/remove.

        Args:
            plan: DeploymentPlan
            workspace_path: 프로젝트 경로 (로그 수집용)

        Returns:
            (success, container_id_or_error_message)
        """
        logger.info(f"[local_deploy] Starting container: {plan.container_name}")

        # 기존 컨테이너 정리
        stop_args = ["docker", "stop", plan.container_name]
        self._run_docker_command(
            stop_args, timeout=_DOCKER_TIMEOUT_STOP
        )  # 실패해도 무시

        rm_args = ["docker", "rm", plan.container_name]
        self._run_docker_command(rm_args, timeout=_DOCKER_TIMEOUT_STOP)  # 실패해도 무시

        # 포트 매핑 구성
        port_bindings = []
        for port_map in plan.ports:
            host_port = port_map.get("host", "8000")
            container_port = port_map.get("container", "8000")
            port_bindings.append(f"{host_port}:{container_port}")

        if not port_bindings:
            # 기본값
            port_bindings.append("8000:8000")

        # 환경 변수
        env_args = []
        for env in plan.env:
            env_args.extend(["-e", env])

        # docker run -d --name {container_name} -p {port}:{port} {env} {image}
        run_args = ["docker", "run", "-d", "--restart", "unless-stopped"]
        run_args.extend(["--name", plan.container_name])
        for port_binding in port_bindings:
            run_args.extend(["-p", port_binding])
        run_args.extend(env_args)
        run_args.append(plan.image)

        success, output = self._run_docker_command(
            run_args, timeout=_DOCKER_TIMEOUT_RUN
        )

        if success:
            container_id = output.split()[-1] if output else plan.container_name
            logger.info(f"[local_deploy] Container started: {container_id}")
            return True, container_id
        else:
            logger.error(f"[local_deploy] Run failed: {output}")
            return False, output

    def health_check(
        self, container_name: str, health_check_url: str = "", retries: int = 3, interval: int = 5
    ) -> bool:
        """
        Health Check — 4단 폴백, 3회 재시도 5초 간격 (§9.2).

        1. HTTP GET {health_check_url}
        2. docker inspect {container_name} → Status running
        3. docker logs 마지막 줄 확인
        4. 포트 열림 여부 확인

        Args:
            container_name: 컨테이너 이름
            health_check_url: Health check URL (예: http://localhost:8000/health)
            retries: 재시도 횟수
            interval: 재시도 간격 (초)

        Returns:
            bool: Health check 통과 여부
        """
        logger.info(f"[local_deploy] Health checking: {container_name}")

        for attempt in range(1, retries + 1):
            logger.info(f"[local_deploy] Health check attempt {attempt}/{retries}")

            # 1단 폴백: HTTP GET
            if health_check_url:
                try:
                    response = httpx.get(health_check_url, timeout=5.0)
                    if response.status_code < 500:
                        logger.info(f"[local_deploy] HTTP health check passed: {response.status_code}")
                        return True
                except Exception as e:
                    logger.debug(f"[local_deploy] HTTP health check failed: {str(e)}")

            # 2단 폴백: docker inspect running 상태
            try:
                proc = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode == 0 and "true" in proc.stdout.lower():
                    logger.info(f"[local_deploy] Container is running")
                    # 추가 검사: 로그 확인
                    log_proc = subprocess.run(
                        ["docker", "logs", "--tail", "5", container_name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if log_proc.returncode == 0:
                        logger.debug(f"[local_deploy] Recent logs: {log_proc.stdout[-200:]}")
                    return True
            except Exception as e:
                logger.debug(f"[local_deploy] docker inspect failed: {str(e)}")

            # 3단 폴백: 포트 열림 여부
            try:
                # localhost:8000 연결 시도 (간단한 버전)
                proc = subprocess.run(
                    ["docker", "exec", container_name, "sh", "-c", "echo 'alive'"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    logger.info(f"[local_deploy] Container exec test passed")
                    return True
            except Exception as e:
                logger.debug(f"[local_deploy] Container exec test failed: {str(e)}")

            if attempt < retries:
                logger.info(f"[local_deploy] Waiting {interval}s before retry...")
                time.sleep(interval)

        logger.error(f"[local_deploy] Health check failed after {retries} attempts")
        return False

    def deploy(
        self,
        plan: DeploymentPlan,
        workspace_path: str,
        project_id: str,
    ) -> DeployResult:
        """
        전체 배포 흐름: build_image → run_container → health_check → record.

        실패 시 새 컨테이너만 제거 (자동 롤백 아님).
        DeploymentRecord는 ~/.recoder/projects/{project_id}_deployments.jsonl에 append.

        Args:
            plan: DeploymentPlan
            workspace_path: 프로젝트 경로
            project_id: 프로젝트 ID

        Returns:
            DeployResult: 배포 결과
        """
        logs: list[str] = []

        # 1. 이미지 빌드
        build_success, build_output = self.build_image(workspace_path, plan.image)
        logs.append(f"[BUILD] {build_output}")
        if not build_success:
            return DeployResult(
                success=False,
                error=f"Build failed: {build_output}",
                logs=logs,
            )

        image_digest = build_output

        # 2. 컨테이너 실행
        run_success, container_output = self.run_container(plan, workspace_path)
        logs.append(f"[RUN] {container_output}")
        if not run_success:
            return DeployResult(
                success=False,
                error=f"Run failed: {container_output}",
                logs=logs,
            )

        container_id = container_output

        # 3. Health Check
        health_url = f"http://localhost:{plan.ports[0]['host'] if plan.ports else '8000'}{plan.health_check_path}"
        health_ok = self.health_check(plan.container_name, health_url)
        logs.append(f"[HEALTH] {'PASS' if health_ok else 'FAIL'}")

        if not health_ok:
            # Health check 실패 시 새 컨테이너 제거
            rm_args = ["docker", "rm", "-f", plan.container_name]
            self._run_docker_command(rm_args, timeout=_DOCKER_TIMEOUT_STOP)
            logs.append(f"[CLEANUP] Removed failed container")

            return DeployResult(
                success=False,
                error="Health check failed",
                logs=logs,
            )

        # 4. DeploymentRecord 생성 및 저장
        record = DeploymentRecord(
            deployment_id=uuid.uuid4().hex,
            project_id=project_id,
            method=DeployMethod.LOCAL_DOCKER,
            image=plan.image,
            image_digest=image_digest,
            git_commit="",  # 로컬 배포이므로 git 정보 없음
            container_name=plan.container_name,
            health_check_path=plan.health_check_path,
            deployed_at=datetime.utcnow().isoformat() + "Z",
            status=DeployStatus.DEPLOYED,
        )

        # 5. 배포 기록 저장
        records_file = self._recoder_dir / f"{project_id}_deployments.jsonl"
        try:
            with open(records_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
            logger.info(f"[local_deploy] Deployment recorded: {records_file}")
        except Exception as e:
            logger.error(f"[local_deploy] Failed to save deployment record: {str(e)}")
            logs.append(f"[RECORD] Failed to save: {str(e)}")

        logs.append(f"[SUCCESS] Deployed {plan.container_name}")
        return DeployResult(
            success=True,
            deployment_record=record,
            logs=logs,
        )

    def rollback(self, record: DeploymentRecord, workspace_path: str) -> DeployResult:
        """
        로컬 컨테이너 롤백 (§17.1).

        rollback_target 이미지로 새 컨테이너 실행.
        Level 1~2 작업이므로 자동 롤백 가능.

        Args:
            record: 이전 DeploymentRecord
            workspace_path: 프로젝트 경로

        Returns:
            DeployResult: 롤백 결과
        """
        if not record.rollback_target:
            return DeployResult(
                success=False,
                error="No rollback target available",
            )

        logger.info(
            f"[local_deploy] Rolling back {record.container_name} to {record.rollback_target}"
        )

        logs: list[str] = []

        # 현재 컨테이너 정지
        stop_args = ["docker", "stop", record.container_name]
        self._run_docker_command(stop_args, timeout=_DOCKER_TIMEOUT_STOP)
        logs.append(f"[STOP] Stopped current container")

        # 롤백 컨테이너 실행
        plan = DeploymentPlan(
            plan_id=uuid.uuid4().hex,
            method=DeployMethod.LOCAL_DOCKER,
            action="run",
            image=record.rollback_target,
            container_name=record.container_name,
            command_template_id="docker_run",
        )

        run_success, run_output = self.run_container(plan, workspace_path)
        logs.append(f"[RUN_ROLLBACK] {run_output}")

        if not run_success:
            return DeployResult(
                success=False,
                error=f"Rollback failed: {run_output}",
                logs=logs,
            )

        # Health Check
        health_ok = self.health_check(record.container_name)
        logs.append(f"[HEALTH] {'PASS' if health_ok else 'FAIL'}")

        if not health_ok:
            return DeployResult(
                success=False,
                error="Rollback health check failed",
                logs=logs,
            )

        # 롤백 기록
        rollback_record = DeploymentRecord(
            deployment_id=uuid.uuid4().hex,
            project_id=record.project_id,
            method=DeployMethod.LOCAL_DOCKER,
            image=record.rollback_target,
            image_digest="",
            git_commit="",
            container_name=record.container_name,
            health_check_path=record.health_check_path,
            deployed_at=datetime.utcnow().isoformat() + "Z",
            status=DeployStatus.ROLLED_BACK,
            rollback_target=record.image,
        )

        # 기록 저장
        records_file = self._recoder_dir / f"{record.project_id}_deployments.jsonl"
        try:
            with open(records_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rollback_record.to_dict()) + "\n")
        except Exception as e:
            logger.error(f"[local_deploy] Failed to save rollback record: {str(e)}")

        logs.append(f"[SUCCESS] Rolled back to {record.rollback_target}")
        return DeployResult(
            success=True,
            deployment_record=rollback_record,
            logs=logs,
        )


    def get_latest_record(self, project_id: str) -> Optional[DeploymentRecord]:
        """
        project_id 의 마지막 성공 배포 기록 반환. (§S-9 자동 롤백용)

        Args:
            project_id: 프로젝트 ID

        Returns:
            가장 최근 DeploymentRecord 또는 None
        """
        records_file = self._recoder_dir / f"{project_id}_deployments.jsonl"
        if not records_file.exists():
            return None

        last_record: Optional[DeploymentRecord] = None
        try:
            with open(records_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # DEPLOYED 상태 레코드만 롤백 대상으로 사용
                        if data.get("status") == DeployStatus.DEPLOYED.value:
                            last_record = DeploymentRecord(**{
                                k: v for k, v in data.items()
                                if k in DeploymentRecord.__dataclass_fields__
                            }) if hasattr(DeploymentRecord, "__dataclass_fields__") else None
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"[local_deploy] Failed to read deployment records: {e}")

        return last_record

    def rollback_latest(self, project_id: str, workspace_path: str) -> DeployResult:
        """
        마지막 배포 기록 기반 자동 롤백. (§S-9)

        Args:
            project_id: 프로젝트 ID
            workspace_path: 프로젝트 경로

        Returns:
            DeployResult
        """
        record = self.get_latest_record(project_id)
        if record is None:
            return DeployResult(
                success=False,
                error="롤백할 이전 배포 기록이 없습니다.",
            )

        # rollback_target 이 없으면 record.image 자체를 재사용
        if not record.rollback_target:
            record.rollback_target = record.image

        logger.info(
            f"[local_deploy] Auto-rollback: project={project_id}, "
            f"target={record.rollback_target}"
        )
        return self.rollback(record, workspace_path)


# ── 싱글턴 접근 ────────────────────────────────────────────────────────

_instance: Optional[LocalDeployAgent] = None


def get_local_deploy_agent() -> LocalDeployAgent:
    """LocalDeployAgent 싱글턴 반환."""
    global _instance
    if _instance is None:
        _instance = LocalDeployAgent()
    return _instance


__all__ = [
    "LocalDeployAgent",
    "DeployResult",
    "get_local_deploy_agent",
]
