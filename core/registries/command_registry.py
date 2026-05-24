"""
ReCoder v6.4 CommandTemplate Registry (§14.2)
LLM은 직접 명령을 생성하지 않고, 이 Registry에서만 명령을 생성한다.
설계서 §14.2 기준.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from schemas import CommandTemplate, RiskLevel


# ── 파라미터 검증 규칙 ────────────────────────────────────────────────

class ValidationError(ValueError):
    """파라미터 검증 실패 시 발생."""
    pass


def _validate_image_name(value: str) -> None:
    """이미지 이름: 정규식 ^[a-z0-9][a-z0-9._/-]*:[a-z0-9._-]+$ 또는 ^[a-z0-9][a-z0-9._/-]*$"""
    pattern_with_tag = r'^[a-z0-9][a-z0-9._/-]*:[a-z0-9._-]+$'
    pattern_without_tag = r'^[a-z0-9][a-z0-9._/-]*$'

    if not (re.match(pattern_with_tag, value) or re.match(pattern_without_tag, value)):
        raise ValidationError(
            f"Invalid image name '{value}'. Must match pattern: "
            "[a-z0-9][a-z0-9._/-]*:[a-z0-9._-]+ or [a-z0-9][a-z0-9._/-]*"
        )


def _validate_port(value: int | str) -> None:
    """포트: 1~65535 정수"""
    try:
        port = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"Port must be an integer, got {value}")

    if not (1 <= port <= 65535):
        raise ValidationError(f"Port must be between 1 and 65535, got {port}")


def _validate_container_name(value: str) -> None:
    """container_name: 영숫자·하이픈·언더스코어만 허용"""
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        raise ValidationError(
            f"Invalid container name '{value}'. Only alphanumeric, hyphens, and underscores allowed."
        )


def _validate_lines(value: int | str) -> None:
    """lines: 1~1000 정수"""
    try:
        lines = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"Lines must be an integer, got {value}")

    if not (1 <= lines <= 1000):
        raise ValidationError(f"Lines must be between 1 and 1000, got {lines}")


def _validate_path(value: str) -> None:
    """path: 절대경로 금지 (시작이 / 또는 드라이브 문자: 금지)"""
    if value.startswith('/') or (len(value) > 1 and value[1] == ':'):
        raise ValidationError(f"Absolute paths are not allowed: {value}")


def _validate_no_injection(value: str) -> None:
    """shell injection 방지: ;, &&, ||, |, >, <, `, $() 등 포함 시 거부"""
    dangerous_chars = [';', '&&', '||', '|', '>', '<', '`', '$()']
    for char in dangerous_chars:
        if char in value:
            raise ValidationError(
                f"Shell injection detected in '{value}': forbidden character '{char}'"
            )


# ── CommandTemplate Registry ──────────────────────────────────────────

class CommandRegistry:
    """CommandTemplate Registry — 검증 및 명령 생성."""

    def __init__(self):
        self._templates: dict[str, CommandTemplate] = {}
        self._init_templates()

    def _init_templates(self) -> None:
        """CommandTemplate 초기화 (Docker · SSH · ECR · ECS · SBOM)."""
        self._templates = {
            # ── Docker 기본 명령들 (Level 2) ─────────────────────────
            "docker_build": CommandTemplate(
                template_id="docker_build",
                action_type="docker_build",
                allowed_params=["image", "context_path"],
                command_pattern="docker build -t {image} {context_path}",
                risk_level=RiskLevel.MEDIUM,
                approval_level=2,
            ),
            "docker_run": CommandTemplate(
                template_id="docker_run",
                action_type="docker_run",
                allowed_params=["container_name", "host_port", "container_port", "image"],
                command_pattern="docker run -d --name {container_name} -p {host_port}:{container_port} {image}",
                risk_level=RiskLevel.MEDIUM,
                approval_level=2,
            ),
            "docker_stop": CommandTemplate(
                template_id="docker_stop",
                action_type="docker_stop",
                allowed_params=["container_name"],
                command_pattern="docker stop {container_name}",
                risk_level=RiskLevel.MEDIUM,
                approval_level=2,
            ),
            "docker_restart": CommandTemplate(
                template_id="docker_restart",
                action_type="docker_restart",
                allowed_params=["container_name"],
                command_pattern="docker restart {container_name}",
                risk_level=RiskLevel.MEDIUM,
                approval_level=2,
            ),
            "docker_logs": CommandTemplate(
                template_id="docker_logs",
                action_type="docker_logs",
                allowed_params=["lines", "container_name"],
                command_pattern="docker logs --tail {lines} {container_name}",
                risk_level=RiskLevel.LOW,
                approval_level=2,
            ),
            "docker_remove": CommandTemplate(
                template_id="docker_remove",
                action_type="docker_remove",
                allowed_params=["container_name"],
                command_pattern="docker rm -f {container_name}",
                risk_level=RiskLevel.HIGH,
                approval_level=2,
            ),

            # ── SSH 원격 명령들 (Level 3, 2학기용) ───────────────────
            "ssh_docker_restart": CommandTemplate(
                template_id="ssh_docker_restart",
                action_type="ssh_docker_restart",
                allowed_params=["host", "port", "container_name"],
                command_pattern="ssh -p {port} {host} 'docker restart {container_name}'",
                risk_level=RiskLevel.HIGH,
                approval_level=3,
            ),
            "ssh_docker_rollback": CommandTemplate(
                template_id="ssh_docker_rollback",
                action_type="ssh_docker_rollback",
                allowed_params=["host", "port", "container_name", "image"],
                command_pattern="ssh -p {port} {host} 'docker stop {container_name} && docker rm {container_name} && docker run -d --name {container_name} {image}'",
                risk_level=RiskLevel.HIGH,
                approval_level=3,
            ),
            "ssh_env_update": CommandTemplate(
                template_id="ssh_env_update",
                action_type="ssh_env_update",
                allowed_params=["host", "port", "env_file_path", "key", "value"],
                command_pattern="ssh -p {port} {host} 'echo {key}={value} >> {env_file_path}'",
                risk_level=RiskLevel.HIGH,
                approval_level=3,
            ),

            # ── ECR 푸시 명령들 (Level 4) ────────────────────────────
            "ecr_login": CommandTemplate(
                template_id="ecr_login",
                action_type="ecr_login",
                allowed_params=["region", "registry_url"],
                command_pattern="aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {registry_url}",
                risk_level=RiskLevel.HIGH,
                approval_level=4,
            ),
            "ecr_push": CommandTemplate(
                template_id="ecr_push",
                action_type="ecr_push",
                allowed_params=["image", "registry_url"],
                command_pattern="docker tag {image} {registry_url}/{image} && docker push {registry_url}/{image}",
                risk_level=RiskLevel.HIGH,
                approval_level=4,
            ),

            # ── ECS Fargate 전용 (Q3-A, Level 3~4) ───────────────────
            "ecr_get_login_password": CommandTemplate(
                template_id="ecr_get_login_password",
                action_type="ecr_get_login_password",
                allowed_params=["region"],
                command_pattern="aws ecr get-login-password --region {region}",
                risk_level=RiskLevel.HIGH,
                approval_level=4,
            ),
            "docker_tag_ecr": CommandTemplate(
                template_id="docker_tag_ecr",
                action_type="docker_tag_ecr",
                allowed_params=["local_image", "ecr_uri"],
                command_pattern="docker tag {local_image} {ecr_uri}",
                risk_level=RiskLevel.MEDIUM,
                approval_level=3,
            ),
            "docker_push_ecr": CommandTemplate(
                template_id="docker_push_ecr",
                action_type="docker_push_ecr",
                allowed_params=["ecr_uri"],
                command_pattern="docker push {ecr_uri}",
                risk_level=RiskLevel.HIGH,
                approval_level=4,
            ),
            "ecs_update_service": CommandTemplate(
                template_id="ecs_update_service",
                action_type="ecs_update_service",
                allowed_params=["cluster", "service", "task_definition"],
                command_pattern="aws ecs update-service --cluster {cluster} --service {service} --task-definition {task_definition} --force-new-deployment",
                risk_level=RiskLevel.HIGH,
                approval_level=4,
            ),
            "ecs_describe_service": CommandTemplate(
                template_id="ecs_describe_service",
                action_type="ecs_describe_service",
                allowed_params=["cluster", "service"],
                command_pattern="aws ecs describe-services --cluster {cluster} --services {service}",
                risk_level=RiskLevel.LOW,
                approval_level=2,
            ),
            "syft_sbom": CommandTemplate(
                template_id="syft_sbom",
                action_type="syft_sbom",
                allowed_params=["image_uri"],
                command_pattern="docker run --rm -v /var/run/docker.sock:/var/run/docker.sock anchore/syft:latest {image_uri} -o cyclonedx-json",
                risk_level=RiskLevel.LOW,
                approval_level=2,
            ),
        }

    def get(self, template_id: str) -> Optional[CommandTemplate]:
        """template_id로 CommandTemplate 조회."""
        return self._templates.get(template_id)

    def build_command(self, template_id: str, params: dict) -> str:
        """params 검증 후 command_pattern에 format 적용해 최종 명령 반환."""
        template = self.get(template_id)
        if not template:
            raise ValueError(f"Unknown template_id: {template_id}")

        # 필수 파라미터 확인
        missing = set(template.allowed_params) - set(params.keys())
        if missing:
            raise ValidationError(f"Missing required parameters: {missing}")

        # 파라미터별 검증
        validated_params = {}
        for param_name, param_value in params.items():
            if param_name not in template.allowed_params:
                raise ValidationError(f"Unexpected parameter: {param_name}")

            # 파라미터 타입별 검증
            if "image" in param_name and param_name.endswith("image"):
                _validate_image_name(str(param_value))
            elif "port" in param_name:
                _validate_port(param_value)
            elif "container_name" in param_name:
                _validate_container_name(str(param_value))
            elif "lines" in param_name:
                _validate_lines(param_value)
            elif "path" in param_name:
                _validate_path(str(param_value))
            else:
                # 일반 파라미터도 injection 체크
                _validate_no_injection(str(param_value))

            validated_params[param_name] = param_value

        # 명령 생성
        try:
            command = template.command_pattern.format(**validated_params)
        except KeyError as e:
            raise ValidationError(f"Format error in template: {e}")

        return command

    def list_templates(self) -> list[CommandTemplate]:
        """모든 CommandTemplate 반환."""
        return list(self._templates.values())

    def list_templates_by_approval_level(self, level: int) -> list[CommandTemplate]:
        """특정 approval_level의 CommandTemplate들 반환."""
        return [t for t in self._templates.values() if t.approval_level == level]


# ── 싱글톤 인스턴스 ───────────────────────────────────────────────────

_command_registry_instance: Optional[CommandRegistry] = None


def get_command_registry() -> CommandRegistry:
    """CommandRegistry 싱글톤 인스턴스 반환."""
    global _command_registry_instance
    if _command_registry_instance is None:
        _command_registry_instance = CommandRegistry()
    return _command_registry_instance
