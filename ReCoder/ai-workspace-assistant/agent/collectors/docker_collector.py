"""Docker 컨테이너 상태 수집 및 에러 감지 모듈."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime


def is_docker_available() -> bool:
    """Docker CLI 사용 가능 여부 확인."""
    return shutil.which("docker") is not None


def _run(cmd: list[str], timeout: int = 5) -> str:
    """subprocess 실행 후 stdout 반환. 실패 시 빈 문자열."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_container_list() -> list[dict]:
    """실행 중 / 중지된 전체 컨테이너 목록 반환."""
    out = _run([
        "docker", "ps", "-a",
        "--format", "{{json .}}"
    ])
    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return containers


def get_container_logs(name: str, tail: int = 50) -> str:
    """컨테이너 마지막 N줄 로그 반환 (stderr 포함)."""
    try:
        result = subprocess.run(
            ["docker", "logs", name, "--tail", str(tail)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
        )
        return (result.stderr + result.stdout).strip()
    except Exception:
        return ""


def collect_docker_status() -> dict:
    """
    Docker 전체 상태 수집.
    반환값:
        containers: 전체 컨테이너 목록
        errors: 문제 있는 컨테이너 (Exited / 재시작 반복)
    """
    if not is_docker_available():
        return {"available": False, "containers": [], "errors": []}

    containers = get_container_list()
    errors = []

    for c in containers:
        status: str = c.get("Status", "")
        name: str = c.get("Names", "")

        # Exited 상태 감지
        if status.startswith("Exited"):
            logs = get_container_logs(name)
            errors.append({
                "source": "docker",
                "type": "container_exited",
                "container": name,
                "image": c.get("Image", ""),
                "status": status,
                "logs": logs,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        # Restarting 상태 감지 (반복 재시작)
        elif "Restarting" in status:
            logs = get_container_logs(name)
            errors.append({
                "source": "docker",
                "type": "container_restarting",
                "container": name,
                "image": c.get("Image", ""),
                "status": status,
                "logs": logs,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    return {
        "available": True,
        "containers": containers,
        "errors": errors,
    }