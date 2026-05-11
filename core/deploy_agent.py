"""
deploy_agent.py — AWS EC2 배포 에이전트 (설계서 §2S-1~§2S-3)

배포 흐름:
  1. Docker 이미지 빌드 (로컬)
  2. ECR 로그인 → 이미지 push
  3. EC2 SSH 접속 → docker pull → docker run

필요 환경 변수 (GitHub Secrets 또는 .env):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
  ECR_REGISTRY  — 예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com
  EC2_HOST      — EC2 퍼블릭 IP 또는 도메인
  EC2_SSH_KEY   — PEM 키 전체 본문 (BEGIN/END 포함)
  EC2_USER      — EC2 접속 사용자 (기본값: ec2-user)
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DOCKER_BUILD_TIMEOUT = 900   # 15분
_ECR_PUSH_TIMEOUT     = 600   # 10분
_SSH_TIMEOUT          = 120   # 2분
_HEALTH_RETRIES       = 5
_HEALTH_INTERVAL      = 10    # 초


@dataclass
class EC2DeployConfig:
    """EC2 배포에 필요한 설정값 묶음."""
    ecr_registry:   str          # 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com
    ec2_host:       str          # EC2 퍼블릭 IP 또는 도메인
    ec2_ssh_key:    str          # PEM 키 전체 본문
    aws_region:     str = "ap-northeast-2"
    ec2_user:       str = "ec2-user"
    container_name: str = "recoder-app"
    host_port:      int = 8000
    container_port: int = 8000
    health_check_path: str = "/health"
    env_vars:       list[str] = field(default_factory=list)   # ["KEY=VALUE", ...]

    @classmethod
    def from_env(cls) -> "EC2DeployConfig":
        """환경변수에서 설정 로드. 필수값 없으면 ValueError."""
        registry = os.getenv("ECR_REGISTRY", "").strip()
        host     = os.getenv("EC2_HOST", "").strip()
        key      = os.getenv("EC2_SSH_KEY", "").strip()

        if not registry:
            raise ValueError("ECR_REGISTRY 환경변수가 설정되지 않았습니다.")
        if not host:
            raise ValueError("EC2_HOST 환경변수가 설정되지 않았습니다.")
        if not key:
            raise ValueError("EC2_SSH_KEY 환경변수가 설정되지 않았습니다.")

        region = (
            os.getenv("AWS_DEFAULT_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("BEDROCK_REGION")
            or "ap-northeast-2"
        )

        return cls(
            ecr_registry=registry,
            ec2_host=host,
            ec2_ssh_key=key,
            aws_region=region,
            ec2_user=os.getenv("EC2_USER", "ec2-user"),
        )


@dataclass
class EC2DeployResult:
    """EC2 배포 결과."""
    success:      bool
    image_uri:    str = ""
    error:        str = ""
    logs:         list[str] = field(default_factory=list)
    deployed_at:  str = ""


class EC2DeployAgent:
    """
    ECR push + EC2 SSH 배포 에이전트.

    모든 public 메서드는 동기. server.py 에서 asyncio.to_thread 로 호출.
    """

    # ── 내부 헬퍼 ────────────────────────────────────────────────────

    def _run(
        self,
        args: list[str],
        cwd: Optional[str] = None,
        timeout: int = 60,
        env: Optional[dict] = None,
    ) -> tuple[int, str, str]:
        """subprocess.run 래퍼. (returncode, stdout, stderr)"""
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **(env or {})},
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"명령 타임아웃 ({timeout}s)"
        except FileNotFoundError as e:
            return -1, "", f"명령을 찾을 수 없음: {e}"
        except Exception as e:
            return -1, "", str(e)

    def _ssh(
        self,
        host: str,
        user: str,
        key_path: str,
        command: str,
        timeout: int = _SSH_TIMEOUT,
    ) -> tuple[int, str, str]:
        """SSH 원격 명령 실행."""
        args = [
            "ssh",
            "-i", key_path,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=yes",
            f"{user}@{host}",
            command,
        ]
        return self._run(args, timeout=timeout)

    # ── Step 1: Docker 이미지 빌드 ───────────────────────────────────

    def build_image(
        self,
        workspace_path: str,
        image_name: str,
    ) -> tuple[bool, str]:
        """
        docker build -t {image_name} .

        Returns:
            (success, image_name_or_error)
        """
        workspace = Path(workspace_path)
        if not (workspace / "Dockerfile").exists():
            return False, f"Dockerfile 없음: {workspace_path}/Dockerfile"

        logger.info(f"[ec2_deploy] docker build: {image_name}")
        rc, out, err = self._run(
            ["docker", "build", "-t", image_name, "."],
            cwd=str(workspace),
            timeout=_DOCKER_BUILD_TIMEOUT,
        )
        if rc != 0:
            return False, f"docker build 실패:\n{err or out}"
        return True, image_name

    # ── Step 2: ECR 로그인 + 이미지 push ────────────────────────────

    def ecr_login(self, registry: str, region: str) -> tuple[bool, str]:
        """
        AWS ECR 로그인.
        aws ecr get-login-password | docker login

        Returns:
            (success, error_message)
        """
        logger.info(f"[ec2_deploy] ECR 로그인: {registry}")

        # ECR 토큰 발급
        rc, token, err = self._run(
            ["aws", "ecr", "get-login-password", "--region", region],
            timeout=30,
        )
        if rc != 0:
            return False, f"ECR 토큰 발급 실패: {err}"

        # docker login
        rc2, _, err2 = self._run(
            ["docker", "login", "--username", "AWS", "--password-stdin", registry],
            timeout=30,
            env={"DOCKER_LOGIN_PASSWORD": token},  # stdin 대신 pipe
        )
        # docker login은 stdin으로 비밀번호를 받기 때문에 직접 subprocess로 처리
        try:
            proc = subprocess.run(
                ["docker", "login", "--username", "AWS", "--password-stdin", registry],
                input=token,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return False, f"docker login 실패: {proc.stderr}"
        except Exception as e:
            return False, f"docker login 예외: {e}"

        return True, ""

    def push_to_ecr(
        self,
        local_image: str,
        registry: str,
        repo_name: str,
        tag: str = "latest",
    ) -> tuple[bool, str]:
        """
        로컬 이미지를 ECR에 push.

        Args:
            local_image: 로컬 이미지명 (예: myapp:latest)
            registry:    ECR 레지스트리 URL
            repo_name:   ECR 레포지토리명
            tag:         이미지 태그

        Returns:
            (success, ecr_image_uri_or_error)
        """
        ecr_uri = f"{registry}/{repo_name}:{tag}"
        logger.info(f"[ec2_deploy] ECR push: {local_image} → {ecr_uri}")

        # ECR 레포지토리 생성 (없으면)
        self._run(
            ["aws", "ecr", "describe-repositories", "--repository-names", repo_name],
            timeout=15,
        )
        rc_create, _, _ = self._run(
            ["aws", "ecr", "create-repository", "--repository-name", repo_name],
            timeout=15,
        )
        # 이미 있으면 create가 실패해도 무시

        # 태그 지정
        rc, _, err = self._run(
            ["docker", "tag", local_image, ecr_uri],
            timeout=30,
        )
        if rc != 0:
            return False, f"docker tag 실패: {err}"

        # push
        rc2, out, err2 = self._run(
            ["docker", "push", ecr_uri],
            timeout=_ECR_PUSH_TIMEOUT,
        )
        if rc2 != 0:
            return False, f"docker push 실패: {err2 or out}"

        return True, ecr_uri

    # ── Step 3: EC2 SSH 배포 ─────────────────────────────────────────

    def deploy_to_ec2(
        self,
        config: EC2DeployConfig,
        ecr_image_uri: str,
    ) -> tuple[bool, str]:
        """
        EC2에 SSH 접속하여 docker pull + run.

        흐름:
          1. ECR 로그인 (EC2에서)
          2. docker pull
          3. 기존 컨테이너 정지/삭제
          4. docker run
          5. Health Check

        Returns:
            (success, error_message)
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False
        ) as f:
            f.write(config.ec2_ssh_key)
            key_path = f.name

        # PEM 파일 권한 설정 (SSH 요구사항)
        os.chmod(key_path, 0o600)

        try:
            # 1) EC2에서 ECR 로그인
            logger.info(f"[ec2_deploy] EC2 ECR 로그인...")
            ecr_login_cmd = (
                f"aws ecr get-login-password --region {config.aws_region} | "
                f"docker login --username AWS --password-stdin {config.ecr_registry}"
            )
            rc, _, err = self._ssh(config.ec2_host, config.ec2_user, key_path, ecr_login_cmd)
            if rc != 0:
                return False, f"EC2 ECR 로그인 실패: {err}"

            # 2) docker pull
            logger.info(f"[ec2_deploy] EC2 docker pull: {ecr_image_uri}")
            rc, _, err = self._ssh(
                config.ec2_host, config.ec2_user, key_path,
                f"docker pull {ecr_image_uri}",
                timeout=300,
            )
            if rc != 0:
                return False, f"docker pull 실패: {err}"

            # 3) 기존 컨테이너 정리
            self._ssh(
                config.ec2_host, config.ec2_user, key_path,
                f"docker stop {config.container_name} 2>/dev/null || true",
            )
            self._ssh(
                config.ec2_host, config.ec2_user, key_path,
                f"docker rm {config.container_name} 2>/dev/null || true",
            )

            # 4) docker run
            env_flags = " ".join(f"-e {e}" for e in config.env_vars)
            run_cmd = (
                f"docker run -d --restart unless-stopped "
                f"--name {config.container_name} "
                f"-p {config.host_port}:{config.container_port} "
                f"{env_flags} "
                f"{ecr_image_uri}"
            )
            logger.info(f"[ec2_deploy] EC2 docker run...")
            rc, out, err = self._ssh(
                config.ec2_host, config.ec2_user, key_path, run_cmd
            )
            if rc != 0:
                return False, f"docker run 실패: {err or out}"

            # 5) Health Check
            health_url = f"http://{config.ec2_host}:{config.host_port}{config.health_check_path}"
            logger.info(f"[ec2_deploy] Health Check: {health_url}")
            for attempt in range(1, _HEALTH_RETRIES + 1):
                time.sleep(_HEALTH_INTERVAL)
                hc_cmd = f"curl -sf --max-time 5 {health_url} > /dev/null && echo OK || echo FAIL"
                rc_hc, hc_out, _ = self._ssh(
                    config.ec2_host, config.ec2_user, key_path, hc_cmd, timeout=30
                )
                if rc_hc == 0 and "OK" in hc_out:
                    logger.info(f"[ec2_deploy] Health Check 통과 (시도 {attempt})")
                    return True, ""
                logger.info(f"[ec2_deploy] Health Check 대기 중... ({attempt}/{_HEALTH_RETRIES})")

            return False, f"Health Check 실패 ({health_url})"

        finally:
            try:
                os.unlink(key_path)
            except Exception:
                pass

    # ── 전체 파이프라인 ───────────────────────────────────────────────

    def deploy(
        self,
        workspace_path: str,
        image_name: str,
        repo_name: str,
        config: EC2DeployConfig,
        tag: str = "latest",
    ) -> EC2DeployResult:
        """
        전체 EC2 배포 파이프라인.

        1. docker build
        2. ECR 로그인
        3. ECR push
        4. EC2 배포 (pull + run + health check)

        Args:
            workspace_path: 프로젝트 경로 (Dockerfile 위치)
            image_name:     로컬 이미지명 (예: myapp)
            repo_name:      ECR 레포지토리명 (예: myapp)
            config:         EC2DeployConfig
            tag:            이미지 태그 (기본값: latest)

        Returns:
            EC2DeployResult
        """
        logs: list[str] = []
        image_tag = f"{image_name}:{tag}"

        # Step 1: 빌드
        logs.append(f"[BUILD] {image_tag} 빌드 시작...")
        ok, out = self.build_image(workspace_path, image_tag)
        logs.append(f"[BUILD] {'완료' if ok else '실패'}: {out[:200]}")
        if not ok:
            return EC2DeployResult(success=False, error=out, logs=logs)

        # Step 2: ECR 로그인
        logs.append(f"[ECR] 로그인 중... ({config.ecr_registry})")
        ok, err = self.ecr_login(config.ecr_registry, config.aws_region)
        logs.append(f"[ECR] 로그인 {'완료' if ok else '실패'}")
        if not ok:
            return EC2DeployResult(success=False, error=err, logs=logs)

        # Step 3: ECR push
        logs.append(f"[ECR] push 중... ({repo_name}:{tag})")
        ok, ecr_uri = self.push_to_ecr(image_tag, config.ecr_registry, repo_name, tag)
        logs.append(f"[ECR] push {'완료' if ok else '실패'}: {ecr_uri[:100]}")
        if not ok:
            return EC2DeployResult(success=False, error=ecr_uri, logs=logs)

        # Step 4: EC2 배포
        logs.append(f"[EC2] {config.ec2_host} 배포 시작...")
        ok, err = self.deploy_to_ec2(config, ecr_uri)
        logs.append(f"[EC2] 배포 {'완료' if ok else '실패'}" + (f": {err}" if err else ""))
        if not ok:
            return EC2DeployResult(success=False, error=err, logs=logs, image_uri=ecr_uri)

        deployed_at = datetime.utcnow().isoformat() + "Z"
        logs.append(f"[SUCCESS] EC2 배포 완료 → http://{config.ec2_host}:{config.host_port}")
        return EC2DeployResult(
            success=True,
            image_uri=ecr_uri,
            logs=logs,
            deployed_at=deployed_at,
        )


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[EC2DeployAgent] = None


def get_ec2_deploy_agent() -> EC2DeployAgent:
    global _instance
    if _instance is None:
        _instance = EC2DeployAgent()
    return _instance


__all__ = ["EC2DeployAgent", "EC2DeployConfig", "EC2DeployResult", "get_ec2_deploy_agent"]
