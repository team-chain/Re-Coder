"""
ReCoder Core — Rollback Policy (설계서 §17)

설계서 매핑:
- §17.1 Rollback 범위:
    * CodeRollback         — patch 적용 실패 / 사용자 요청 시 백업 파일 복원
    * LocalContainerRollback — docker run / health check 실패 시 이전 컨테이너 재실행
    * RemoteDeployRollback — EC2 배포 실패 시 rollback_image로 복원
    * EnvRollback          — env 변경 실패 시 env snapshot 적용
- §17.2 자동 트리거 범위:
    * Level 1~2 작업은 자동 rollback 허용
    * Level 3~4 작업의 rollback 은 항상 사용자 승인 필요
- §17.3 가능 / 불완전 조건:
    * RollbackFeasibility 로 평가 후 risk_level / risk_reasons 채움

이 모듈은 **정책 결정** 과 **각 종류별 실행 함수** 를 함께 제공한다. Code Agent /
Deploy Agent / Ops Agent 가 import 해서 사용한다.

핵심 원칙:
- LLM 은 RollbackPlan 을 직접 생성하지 않는다. 이 모듈의 결정 로직만 신뢰한다.
- 자동 rollback 은 Level 1~2 에서만 허용 (원격 인프라 변경은 위험).
- snapshot / backup 이 부재하면 ``can_rollback=False`` 로 결정되며 manual 가이드만 제공.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


# ===========================================================================
# Enums & dataclasses
# ===========================================================================


class RollbackKind(str, Enum):
    """설계서 §17.1 — 4 종 rollback."""

    CODE             = "code"
    LOCAL_CONTAINER  = "local_container"
    REMOTE_DEPLOY    = "remote_deploy"
    ENV              = "env"


class RollbackOutcome(str, Enum):
    SUCCEEDED          = "succeeded"
    FAILED             = "failed"
    SKIPPED            = "skipped"          # 자동 트리거 조건 미충족
    AWAITING_APPROVAL  = "awaiting_approval"  # Level 3~4 — 사용자 승인 대기
    INFEASIBLE         = "infeasible"       # snapshot/백업 부재


@dataclass
class RollbackFeasibility:
    """설계서 §17.3 — rollback 가능성 평가 결과."""

    can_rollback:   bool
    kind:           RollbackKind
    reasons:        list[str] = field(default_factory=list)
    warnings:       list[str] = field(default_factory=list)
    requires_user_approval: bool = False  # Level 3~4 일 때 True
    rollback_target: Optional[str] = None  # 이전 image / commit / env snapshot

    def to_dict(self) -> dict:
        return {
            "can_rollback":           self.can_rollback,
            "kind":                   self.kind.value,
            "reasons":                self.reasons,
            "warnings":               self.warnings,
            "requires_user_approval": self.requires_user_approval,
            "rollback_target":        self.rollback_target,
        }


@dataclass
class RollbackResult:
    """rollback 실행 결과."""

    kind:        RollbackKind
    outcome:     RollbackOutcome
    started_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    target:      Optional[str] = None        # 복원된 image / commit / snapshot id
    message:     str = ""
    details:     dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind":        self.kind.value,
            "outcome":     self.outcome.value,
            "started_at":  self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "target":      self.target,
            "message":     self.message,
            "details":     self.details,
        }


# ===========================================================================
# RollbackPolicy — 결정 로직 + 실행 함수
# ===========================================================================


class RollbackPolicy:
    """
    설계서 §17 의 모든 정책 결정을 한곳에 모은 클래스.

    사용 패턴::

        policy = RollbackPolicy()
        feas = policy.assess_code_rollback(session_id, file_paths)
        if feas.can_rollback and not feas.requires_user_approval:
            result = policy.execute_code_rollback(session_id, file_paths)

    Code Agent / Deploy Agent 는 이 패턴을 따라 자동/수동 rollback 을 결정한다.
    """

    # 설계서 §16 Approval Level 1~2 만 자동 rollback 허용
    AUTO_TRIGGER_MAX_LEVEL: int = 2

    # ~/.recoder/backups/{session_id}/{relative_file_path}
    _BACKUP_BASE: Path = Path.home() / ".recoder" / "backups"

    # ~/.recoder/env_snapshots/{snapshot_id}.json
    _ENV_SNAPSHOT_BASE: Path = Path.home() / ".recoder" / "env_snapshots"

    # ~/.recoder/deployments/{deployment_id}.json
    _DEPLOYMENT_BASE: Path = Path.home() / ".recoder" / "deployments"

    def __init__(self) -> None:
        for base in (self._BACKUP_BASE, self._ENV_SNAPSHOT_BASE, self._DEPLOYMENT_BASE):
            base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Generic helper — 자동 트리거 여부 결정 (설계서 §17.2)
    # ------------------------------------------------------------------

    def should_auto_trigger(self, approval_level: int) -> bool:
        """approval_level 이 1~2 이하일 때만 자동 rollback 가능."""
        try:
            return int(approval_level) <= self.AUTO_TRIGGER_MAX_LEVEL
        except (TypeError, ValueError):
            return False

    # ==================================================================
    # 1) Code Rollback — patch 적용 실패 시 백업 파일 복원
    # ==================================================================

    def assess_code_rollback(
        self,
        session_id: str,
        file_paths: list[str],
    ) -> RollbackFeasibility:
        """백업 파일이 모두 존재하는지 확인."""
        backup_dir = self._BACKUP_BASE / session_id
        reasons: list[str] = []
        warnings: list[str] = []

        missing: list[str] = []
        for rel_path in file_paths:
            backup_path = backup_dir / rel_path
            if not backup_path.exists():
                missing.append(rel_path)

        if missing:
            reasons.append(
                f"Backup files missing for {len(missing)} file(s): {missing[:3]}"
                + ("..." if len(missing) > 3 else "")
            )
            return RollbackFeasibility(
                can_rollback=False,
                kind=RollbackKind.CODE,
                reasons=reasons,
            )

        return RollbackFeasibility(
            can_rollback=True,
            kind=RollbackKind.CODE,
            reasons=["All backup files present"],
            warnings=warnings,
            requires_user_approval=False,  # Code rollback은 Level 1, 항상 자동 가능
            rollback_target=str(backup_dir),
        )

    def execute_code_rollback(
        self,
        session_id: str,
        file_paths: list[str],
        workspace_path: str,
    ) -> RollbackResult:
        """백업 파일들을 워크스페이스 상대 경로로 복원."""
        backup_dir = self._BACKUP_BASE / session_id
        ws = Path(workspace_path)

        restored: list[str] = []
        errors: list[str] = []

        for rel_path in file_paths:
            src = backup_dir / rel_path
            dst = ws / rel_path
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored.append(rel_path)
            except FileNotFoundError:
                errors.append(f"backup missing: {rel_path}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel_path}: {exc}")

        outcome = RollbackOutcome.SUCCEEDED if not errors else RollbackOutcome.FAILED
        return RollbackResult(
            kind=RollbackKind.CODE,
            outcome=outcome,
            finished_at=datetime.now(timezone.utc),
            target=str(backup_dir),
            message=(
                f"Restored {len(restored)} file(s)"
                + (f"; {len(errors)} error(s)" if errors else "")
            ),
            details={"restored": restored, "errors": errors},
        )

    # ==================================================================
    # 2) Local Container Rollback — 이전 이미지로 컨테이너 재실행
    # ==================================================================

    def assess_local_container_rollback(
        self,
        container_name: str,
        previous_image: Optional[str],
        approval_level: int = 2,
    ) -> RollbackFeasibility:
        """이전 image tag 가 DeploymentRecord 에 존재해야 함 (§17.3)."""
        reasons: list[str] = []
        warnings: list[str] = []

        if not previous_image:
            return RollbackFeasibility(
                can_rollback=False,
                kind=RollbackKind.LOCAL_CONTAINER,
                reasons=["No previous_image recorded in DeploymentRecord — cannot rollback"],
            )

        # Verify the image is still present locally (best-effort)
        exists = self._docker_image_exists(previous_image)
        if not exists:
            warnings.append(
                f"Image '{previous_image}' not found locally — rollback will attempt to pull"
            )

        return RollbackFeasibility(
            can_rollback=True,
            kind=RollbackKind.LOCAL_CONTAINER,
            reasons=[f"Previous image available: {previous_image}"],
            warnings=warnings,
            requires_user_approval=not self.should_auto_trigger(approval_level),
            rollback_target=previous_image,
        )

    def execute_local_container_rollback(
        self,
        container_name: str,
        previous_image: str,
        host_port: int,
        container_port: int,
        env: Optional[dict[str, str]] = None,
    ) -> RollbackResult:
        """기존 컨테이너 제거 후 이전 이미지로 재실행. command-template 검증 필수."""
        try:
            from registries.command_registry import get_command_registry, ValidationError
        except ImportError:
            from core.registries.command_registry import get_command_registry, ValidationError  # type: ignore

        registry = get_command_registry()

        # CommandRegistry 로 파라미터 검증
        try:
            registry.build_command("docker_remove", {"container_name": container_name})
            registry.build_command("docker_run", {
                "container_name":  container_name,
                "host_port":       host_port,
                "container_port":  container_port,
                "image":           previous_image,
            })
        except ValidationError as exc:
            return RollbackResult(
                kind=RollbackKind.LOCAL_CONTAINER,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message=f"CommandRegistry validation failed: {exc}",
            )

        # Step 1: stop + remove existing container (best-effort)
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, timeout=15
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("docker rm -f failed (will continue): %s", exc)

        # Step 2: run previous image
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{host_port}:{container_port}",
            "--restart", "unless-stopped",
        ]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(previous_image)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0
            return RollbackResult(
                kind=RollbackKind.LOCAL_CONTAINER,
                outcome=RollbackOutcome.SUCCEEDED if ok else RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message=proc.stderr.strip() if not ok else f"Restored {container_name} from {previous_image}",
                details={"stdout": proc.stdout, "stderr": proc.stderr},
            )
        except subprocess.TimeoutExpired:
            return RollbackResult(
                kind=RollbackKind.LOCAL_CONTAINER,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message="docker run timed out after 60s",
            )
        except FileNotFoundError:
            return RollbackResult(
                kind=RollbackKind.LOCAL_CONTAINER,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message="Docker is not installed or not in PATH",
            )

    @staticmethod
    def _docker_image_exists(image: str) -> bool:
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, timeout=10
            )
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # ==================================================================
    # 3) Remote Deploy Rollback — EC2 SSH 기반 이전 컨테이너 복원
    # ==================================================================

    def assess_remote_deploy_rollback(
        self,
        deployment_record: dict[str, Any],
        approval_level: int = 3,  # 원격 인프라 변경은 Level 3
    ) -> RollbackFeasibility:
        """
        Remote rollback 은 항상 사용자 승인 필요 (Level 3~4).

        설계서 §17.3 의 불완전 조건 평가:
          - DB migration 이 이미 실행된 경우 → high risk warning
          - 외부 리소스 (S3/RDS) 삭제 → infeasible
          - IAM / Secret snapshot 부재 → high risk warning
          - ECR lifecycle 삭제 → infeasible
        """
        reasons: list[str] = []
        warnings: list[str] = []
        infeasible: list[str] = []

        rollback_target = deployment_record.get("rollback_target") or \
                          deployment_record.get("image_digest")
        if not rollback_target:
            return RollbackFeasibility(
                can_rollback=False,
                kind=RollbackKind.REMOTE_DEPLOY,
                reasons=["No rollback_target image_digest in DeploymentRecord"],
            )

        # 불완전 조건 평가
        if deployment_record.get("db_migration_applied"):
            warnings.append(
                "DB migration was applied during this deployment — "
                "rollback may leave schema in inconsistent state"
            )
        if deployment_record.get("external_resources_deleted"):
            infeasible.append(
                "External resources (S3 objects / RDS data) were deleted — "
                "rollback cannot restore them"
            )
        if deployment_record.get("iam_or_secret_changed") and \
           not deployment_record.get("iam_or_secret_snapshot"):
            warnings.append(
                "IAM or Secret was changed without snapshot — manual cleanup required"
            )
        if deployment_record.get("ecr_image_deleted"):
            infeasible.append(
                "Previous ECR image was deleted by lifecycle policy — "
                "cannot pull rollback image"
            )

        if infeasible:
            return RollbackFeasibility(
                can_rollback=False,
                kind=RollbackKind.REMOTE_DEPLOY,
                reasons=infeasible,
                warnings=warnings,
            )

        reasons.append(f"Rollback image available: {rollback_target}")
        return RollbackFeasibility(
            can_rollback=True,
            kind=RollbackKind.REMOTE_DEPLOY,
            reasons=reasons,
            warnings=warnings,
            requires_user_approval=True,  # 항상 사용자 승인
            rollback_target=str(rollback_target),
        )

    def execute_remote_deploy_rollback(
        self,
        deployment_record: dict[str, Any],
        ssh_host: str,
        ssh_user: str,
        ssh_port: int = 22,
        ssh_key_path: Optional[str] = None,
    ) -> RollbackResult:
        """
        SSH 로 원격 호스트에 접속해서 ssh_docker_rollback 템플릿 실행.

        주의: 호출 측이 반드시 사용자 승인 (Level 3) 을 받은 후에 호출해야 한다.
        """
        try:
            from registries.command_registry import get_command_registry, ValidationError
        except ImportError:
            from core.registries.command_registry import get_command_registry, ValidationError  # type: ignore

        registry = get_command_registry()

        previous_image = deployment_record.get("rollback_target") or \
                         deployment_record.get("image_digest")
        container_name = deployment_record.get("container_name", "app")

        if not previous_image:
            return RollbackResult(
                kind=RollbackKind.REMOTE_DEPLOY,
                outcome=RollbackOutcome.INFEASIBLE,
                finished_at=datetime.now(timezone.utc),
                message="No rollback_target in DeploymentRecord",
            )

        try:
            command = registry.build_command("ssh_docker_rollback", {
                "host":            f"{ssh_user}@{ssh_host}",
                "port":            ssh_port,
                "container_name":  container_name,
                "image":           previous_image,
            })
        except ValidationError as exc:
            return RollbackResult(
                kind=RollbackKind.REMOTE_DEPLOY,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message=f"CommandRegistry validation failed: {exc}",
            )

        # Execute via paramiko (lazy import to avoid hard dep on import)
        try:
            import paramiko  # type: ignore
        except ImportError:
            return RollbackResult(
                kind=RollbackKind.REMOTE_DEPLOY,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message="paramiko not installed — remote rollback unavailable",
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs: dict[str, Any] = {
                "hostname": ssh_host,
                "username": ssh_user,
                "port":     ssh_port,
                "timeout":  15,
            }
            if ssh_key_path:
                connect_kwargs["key_filename"] = ssh_key_path
            client.connect(**connect_kwargs)

            # Strip the leading "ssh ... '...'" so we send the inner command
            inner_cmd = _strip_ssh_wrapper(command)
            _stdin, stdout, stderr = client.exec_command(inner_cmd, timeout=60)
            rc = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            ok = rc == 0
            return RollbackResult(
                kind=RollbackKind.REMOTE_DEPLOY,
                outcome=RollbackOutcome.SUCCEEDED if ok else RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message=err.strip() if not ok else f"Remote container {container_name} restored to {previous_image}",
                details={"stdout": out, "stderr": err, "exit_code": rc},
            )
        except Exception as exc:  # noqa: BLE001
            return RollbackResult(
                kind=RollbackKind.REMOTE_DEPLOY,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=previous_image,
                message=f"SSH rollback failed: {exc}",
            )
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    # 4) Env Rollback — env 변경 전 snapshot 적용
    # ==================================================================

    def snapshot_env(
        self,
        snapshot_id: str,
        env: dict[str, str],
        meta: Optional[dict[str, Any]] = None,
    ) -> Path:
        """env 변경 직전에 호출. snapshot 을 JSON 으로 저장하고 경로 반환."""
        path = self._ENV_SNAPSHOT_BASE / f"{snapshot_id}.json"
        payload = {
            "snapshot_id": snapshot_id,
            "created_at":  datetime.now(timezone.utc).isoformat(),
            "env":         dict(env),
            "meta":        dict(meta or {}),
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # 민감 정보 — 0600 권한
        except Exception:  # noqa: BLE001
            pass
        return path

    def assess_env_rollback(
        self,
        snapshot_id: str,
        approval_level: int = 4,  # env 변경은 Level 4 (민감 설정)
    ) -> RollbackFeasibility:
        path = self._ENV_SNAPSHOT_BASE / f"{snapshot_id}.json"
        if not path.exists():
            return RollbackFeasibility(
                can_rollback=False,
                kind=RollbackKind.ENV,
                reasons=[f"Env snapshot not found: {snapshot_id}"],
            )

        return RollbackFeasibility(
            can_rollback=True,
            kind=RollbackKind.ENV,
            reasons=[f"Env snapshot present: {path}"],
            requires_user_approval=not self.should_auto_trigger(approval_level),
            rollback_target=str(path),
        )

    def execute_env_rollback(
        self,
        snapshot_id: str,
        target_env_file: str,
    ) -> RollbackResult:
        path = self._ENV_SNAPSHOT_BASE / f"{snapshot_id}.json"
        if not path.exists():
            return RollbackResult(
                kind=RollbackKind.ENV,
                outcome=RollbackOutcome.INFEASIBLE,
                finished_at=datetime.now(timezone.utc),
                message=f"Snapshot {snapshot_id} not found",
            )

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            env = payload.get("env", {})
            if not isinstance(env, dict):
                raise ValueError("snapshot env is not a dict")

            lines = [f"{k}={v}" for k, v in env.items()]
            target = Path(target_env_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                os.chmod(target, 0o600)
            except Exception:  # noqa: BLE001
                pass

            return RollbackResult(
                kind=RollbackKind.ENV,
                outcome=RollbackOutcome.SUCCEEDED,
                finished_at=datetime.now(timezone.utc),
                target=snapshot_id,
                message=f"Restored {len(env)} env var(s) to {target_env_file}",
                details={"snapshot_id": snapshot_id, "var_count": len(env)},
            )
        except Exception as exc:  # noqa: BLE001
            return RollbackResult(
                kind=RollbackKind.ENV,
                outcome=RollbackOutcome.FAILED,
                finished_at=datetime.now(timezone.utc),
                target=snapshot_id,
                message=f"Env rollback failed: {exc}",
            )

    # ==================================================================
    # Convenience — record DeploymentRecord JSON for later rollback
    # ==================================================================

    def record_deployment(self, deployment_record: dict[str, Any]) -> Path:
        """Persist a DeploymentRecord as JSON for future rollback lookup."""
        deployment_id = deployment_record.get("deployment_id") or \
                        deployment_record.get("id") or "unknown"
        path = self._DEPLOYMENT_BASE / f"{deployment_id}.json"
        try:
            path.write_text(
                json.dumps(deployment_record, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to record deployment: %s", exc)
        return path

    def load_deployment(self, deployment_id: str) -> Optional[dict[str, Any]]:
        path = self._DEPLOYMENT_BASE / f"{deployment_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load deployment %s: %s", deployment_id, exc)
            return None


# ===========================================================================
# Helpers
# ===========================================================================


def _strip_ssh_wrapper(ssh_command: str) -> str:
    """
    ssh_docker_rollback 의 command_pattern 은
        ssh -p {port} {host} '<inner>'
    형태. paramiko 로 실행할 때는 inner 부분만 필요하다.
    """
    # Look for the last quoted segment
    if "'" in ssh_command:
        first = ssh_command.find("'")
        last = ssh_command.rfind("'")
        if first != -1 and last > first:
            return ssh_command[first + 1:last]
    # Fallback — return as-is (assume already inner)
    return ssh_command


# ===========================================================================
# Singleton
# ===========================================================================


_default_policy: Optional[RollbackPolicy] = None


def get_rollback_policy() -> RollbackPolicy:
    """프로세스 전역 RollbackPolicy 싱글톤."""
    global _default_policy
    if _default_policy is None:
        _default_policy = RollbackPolicy()
    return _default_policy
