"""Kubernetes Pod 상태 수집 및 에러 감지 모듈."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime


# 에러로 간주할 Pod 상태
_ERROR_REASONS = {
    "CrashLoopBackOff",
    "Error",
    "OOMKilled",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerError",
    "RunContainerError",
}

_ERROR_PHASES = {"Failed"}


def is_kubectl_available() -> bool:
    """kubectl CLI 사용 가능 여부 확인."""
    return shutil.which("kubectl") is not None


def _run(cmd: list[str], timeout: int = 10) -> str:
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


def get_pod_logs(name: str, namespace: str, tail: int = 50) -> str:
    """Pod 이전 컨테이너 로그 반환 (--previous 우선, 실패 시 현재 로그)."""
    # 이전 컨테이너 로그 (크래시 원인 파악에 유리)
    result = subprocess.run(
        ["kubectl", "logs", name, "-n", namespace,
         "--tail", str(tail), "--previous"],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
    )
    if result.stdout.strip():
        return result.stdout.strip()

    # 폴백: 현재 로그
    result = subprocess.run(
        ["kubectl", "logs", name, "-n", namespace, "--tail", str(tail)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
    )
    return result.stdout.strip()


def collect_k8s_status() -> dict:
    """
    Kubernetes 전체 Pod 상태 수집.
    반환값:
        pods: 전체 Pod 목록 (요약)
        errors: 문제 있는 Pod 목록
    """
    if not is_kubectl_available():
        return {"available": False, "pods": [], "errors": []}

    raw = _run(["kubectl", "get", "pods", "-A", "-o", "json"])
    if not raw:
        return {"available": True, "pods": [], "errors": []}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": True, "pods": [], "errors": []}

    items = data.get("items", [])
    pods_summary = []
    errors = []

    for pod in items:
        meta = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status = pod.get("status", {})

        name = meta.get("name", "")
        namespace = meta.get("namespace", "default")
        phase = status.get("phase", "Unknown")

        pods_summary.append({
            "name": name,
            "namespace": namespace,
            "phase": phase,
        })

        # Phase 기반 에러 감지
        if phase in _ERROR_PHASES:
            logs = get_pod_logs(name, namespace)
            errors.append({
                "source": "kubernetes",
                "type": "pod_failed",
                "pod": name,
                "namespace": namespace,
                "reason": phase,
                "logs": logs,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        # containerStatuses 기반 에러 감지
        for cs in status.get("containerStatuses", []):
            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")

            if reason in _ERROR_REASONS:
                logs = get_pod_logs(name, namespace)
                restart_count = cs.get("restartCount", 0)
                errors.append({
                    "source": "kubernetes",
                    "type": "pod_error",
                    "pod": name,
                    "namespace": namespace,
                    "container": cs.get("name", ""),
                    "reason": reason,
                    "restart_count": restart_count,
                    "logs": logs,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

    return {
        "available": True,
        "pods": pods_summary,
        "errors": errors,
    }