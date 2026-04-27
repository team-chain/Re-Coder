"""User-approved AWS EC2 Docker deployment helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-")
    return result or "project"


@dataclass
class AwsDeployConfig:
    host: str
    user: str = "ec2-user"
    ssh_key_path: str = ""
    container_port: str = "8000"
    host_port: str = "8000"
    container_name: str = ""
    image_name: str = ""

    @classmethod
    def from_env(cls, project_name: str) -> "AwsDeployConfig":
        port = os.getenv("AWS_DEPLOY_PORT", "8000").strip() or "8000"
        slug = _slug(project_name)
        return cls(
            host=os.getenv("AWS_EC2_HOST", "").strip(),
            user=os.getenv("AWS_EC2_USER", "ec2-user").strip() or "ec2-user",
            ssh_key_path=os.getenv("AWS_SSH_KEY_PATH", "").strip(),
            container_port=os.getenv("AWS_CONTAINER_PORT", port).strip() or port,
            host_port=os.getenv("AWS_HOST_PORT", port).strip() or port,
            container_name=os.getenv("AWS_CONTAINER_NAME", f"recoder-{slug}").strip() or f"recoder-{slug}",
            image_name=os.getenv("AWS_IMAGE_NAME", f"recoder-{slug}:latest").strip() or f"recoder-{slug}:latest",
        )


def _run(args: list[str], cwd: Path | None = None, timeout: int = 300, stdin=None) -> dict[str, Any]:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip())
    return {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "output": output[-12000:],
    }


def _ssh_args(config: AwsDeployConfig, remote_command: str) -> list[str]:
    args = ["ssh"]
    if config.ssh_key_path:
        args.extend(["-i", config.ssh_key_path])
    args.extend([
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{config.user}@{config.host}",
        remote_command,
    ])
    return args


def _validate_config(config: AwsDeployConfig) -> list[str]:
    errors: list[str] = []
    if not config.host:
        errors.append("AWS_EC2_HOST 또는 배포 화면의 EC2 host가 필요합니다.")
    if config.ssh_key_path and not Path(config.ssh_key_path).expanduser().exists():
        errors.append(f"SSH key 파일을 찾지 못했습니다: {config.ssh_key_path}")
    if shutil.which("docker") is None:
        errors.append("로컬 Docker CLI를 찾지 못했습니다.")
    if shutil.which("ssh") is None:
        errors.append("OpenSSH ssh 명령을 찾지 못했습니다.")
    return errors


def aws_deploy_status(project_root: Path) -> dict[str, Any]:
    config = AwsDeployConfig.from_env(project_root.name)
    return {
        "configured": not _validate_config(config),
        "errors": _validate_config(config),
        "host": config.host,
        "user": config.user,
        "ssh_key_path": config.ssh_key_path,
        "container_port": config.container_port,
        "host_port": config.host_port,
        "container_name": config.container_name,
        "image_name": config.image_name,
    }


def deploy_to_ec2(project_root: Path, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = AwsDeployConfig.from_env(project_root.name)
    overrides = overrides or {}
    for field in config.__dataclass_fields__:
        value = overrides.get(field)
        if value is not None and str(value).strip():
            setattr(config, field, str(value).strip())

    errors = _validate_config(config)
    if errors:
        return {"status": "failed", "errors": errors, "steps": []}

    steps: list[dict[str, Any]] = []

    build = _run(["docker", "build", "-t", config.image_name, "."], project_root, timeout=900)
    steps.append(build)
    if build["returncode"] != 0:
        return {"status": "failed", "stage": "docker_build", "steps": steps}

    docker_check = _run(_ssh_args(config, "docker --version && docker ps >/dev/null"), timeout=60)
    steps.append(docker_check)
    if docker_check["returncode"] != 0:
        return {
            "status": "failed",
            "stage": "remote_docker_check",
            "message": "EC2에 Docker가 설치되어 있고 현재 사용자가 docker 권한을 갖고 있어야 합니다.",
            "steps": steps,
        }

    save_proc = subprocess.Popen(
        ["docker", "save", config.image_name],
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    load_proc = subprocess.run(
        _ssh_args(config, f"docker load && docker image ls {config.image_name}"),
        stdin=save_proc.stdout,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if save_proc.stdout:
        save_proc.stdout.close()
    save_stderr = save_proc.stderr.read().decode(errors="replace") if save_proc.stderr else ""
    save_code = save_proc.wait(timeout=60)
    load_output = "\n".join(part.strip() for part in (load_proc.stdout, load_proc.stderr, save_stderr) if part and part.strip())
    load_step = {
        "command": f"docker save {config.image_name} | ssh {config.user}@{config.host} docker load",
        "returncode": load_proc.returncode or save_code,
        "output": load_output[-12000:],
    }
    steps.append(load_step)
    if load_step["returncode"] != 0:
        return {"status": "failed", "stage": "image_transfer", "steps": steps}

    remote_run = (
        f"docker rm -f {config.container_name} >/dev/null 2>&1 || true; "
        f"docker run -d --restart unless-stopped --name {config.container_name} "
        f"-p {config.host_port}:{config.container_port} {config.image_name}"
    )
    run_step = _run(_ssh_args(config, remote_run), timeout=120)
    steps.append(run_step)
    if run_step["returncode"] != 0:
        return {"status": "failed", "stage": "remote_docker_run", "steps": steps}

    return {
        "status": "ok",
        "host": config.host,
        "user": config.user,
        "image_name": config.image_name,
        "container_name": config.container_name,
        "container_port": config.container_port,
        "host_port": config.host_port,
        "url": f"http://{config.host}:{config.host_port}",
        "steps": steps,
    }
