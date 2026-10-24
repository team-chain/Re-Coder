"""ECS 서비스별 '마지막으로 성공(정상)한 배포'의 태스크 정의 기록 (3.0.2).

롤백 대상은 **실제로 정상 배포가 끝난 버전**이어야 한다. 예전에는 새 태스크 정의를 등록하기 직전
서비스가 가리키던 태스크 정의를 '이전 버전'으로 잡았다. 그 값은 직전 배포가 실패했으면 **실패한 버전**이다
(v2 정상 → v3 실패 → v4 배포가 v3 를 롤백 대상으로 잡음). 24시간 감시자의 stable_td 도 같은 값을 써서,
밤사이 롤백 링크가 망가진 버전으로 되돌릴 수 있었다.

이제 배포가 SUCCEEDED 로 끝날 때만 여기에 적고, 롤백 대상·감시자 기준은 여기서만 읽는다.
기록은 이 PC 의 ~/.recoder/ecs_stable_versions.json(테스트는 RECODER_ECS_STABLE_STORE)에 남는다.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

#: 서비스마다 최근 성공 기록을 이만큼만 남긴다.
HISTORY = 20

_LOCK = threading.Lock()


def _state_io():
    try:
        import state_io  # type: ignore
    except ImportError:  # pragma: no cover
        from core import state_io  # type: ignore
    return state_io


def store_path() -> Path:
    configured = os.environ.get("RECODER_ECS_STABLE_STORE")
    if configured:
        return Path(configured)
    return Path(os.environ.get("RECODER_HOME") or Path.home() / ".recoder") / "ecs_stable_versions.json"


def _key(region: str, cluster: str, service: str) -> str:
    return f"{region or ''}/{cluster or ''}/{service or ''}"


def _rev(arn: str) -> str:
    """`.../task-definition/app:7` → `app:7` (같은 리비전인지 비교용)."""
    return str(arn or "").rsplit("/", 1)[-1]


def _read() -> dict:
    data = _state_io().read_json(store_path(), {})
    return data if isinstance(data, dict) else {}


def history(region: str, cluster: str, service: str) -> list[str]:
    """성공한 배포의 태스크 정의 — 오래된 것부터."""
    entries = _read().get(_key(region, cluster, service)) or []
    return [str(e.get("td")) for e in entries if isinstance(e, dict) and e.get("td")]


def record_success(region: str, cluster: str, service: str, task_definition: str) -> None:
    """정상 배포가 끝난 태스크 정의를 적는다(같은 리비전이 이미 있으면 맨 뒤로 옮긴다)."""
    if not task_definition:
        return
    with _LOCK:
        data = _read()
        key = _key(region, cluster, service)
        entries = [e for e in (data.get(key) or []) if isinstance(e, dict) and _rev(str(e.get("td"))) != _rev(task_definition)]
        entries.append({"td": task_definition, "at": int(time.time())})
        data[key] = entries[-HISTORY:]
        _state_io().write_json(store_path(), data)


def last_success(region: str, cluster: str, service: str, *, exclude: str = "") -> str:
    """가장 최근에 성공한 태스크 정의(exclude 와 같은 리비전은 건너뛴다). 없으면 ""."""
    for td in reversed(history(region, cluster, service)):
        if not exclude or _rev(td) != _rev(exclude):
            return td
    return ""


def is_known_success(region: str, cluster: str, service: str, task_definition: str) -> bool:
    return any(_rev(td) == _rev(task_definition) for td in history(region, cluster, service))


def steady_task_definition(service: Optional[dict]) -> str:
    """기록이 없을 때(이 PC 에서 처음 배포하는 기존 서비스)의 대안 — 서비스가 **안정 상태**일 때만 그 태스크 정의.

    PRIMARY 배포의 rolloutState 가 COMPLETED 이거나(서킷 브레이커가 켜진 서비스), rolloutState 가 없으면
    배포가 하나뿐이고 원하는 수만큼 떠 있을 때만 믿는다. 실패·진행 중인 롤아웃의 태스크 정의는 쓰지 않는다.
    """
    if not service or service.get("status") not in (None, "ACTIVE"):
        return ""
    td = str(service.get("taskDefinition") or "")
    if not td:
        return ""
    deployments = service.get("deployments") or []
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if primary is None:
        return ""
    if str(primary.get("taskDefinition") or td) != td:
        return ""
    state = primary.get("rolloutState")
    if state:
        return td if state == "COMPLETED" else ""
    desired = int(primary.get("desiredCount") or service.get("desiredCount") or 0)
    running = int(primary.get("runningCount") or service.get("runningCount") or 0)
    if len(deployments) == 1 and desired > 0 and running >= desired:
        return td
    return ""
