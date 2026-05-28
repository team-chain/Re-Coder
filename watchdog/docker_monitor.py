"""
watchdog/docker_monitor.py — Docker CLI 래퍼.

설계 §3.2.4 — docker SDK 의존성을 추가하지 않고 subprocess 를 통해 docker CLI 를
호출한다 (EC2 에 docker 가 이미 설치되어 있다는 전제).

제공 함수:
  - list_containers()    → docker ps --format json
  - inspect_container()  → docker inspect
  - get_container_stats() → docker stats --no-stream --format json
  - get_container_logs() → docker logs --since 60s --tail 100
  - watch_docker_events() → docker events stream (thread 안전 generator)

docker daemon 이 실행 중이 아닐 때는 DockerUnavailableError 를 던진다 — 호출자가
이를 catch 해 재시도/알림 정책을 결정한다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Generator, List, Optional

log = logging.getLogger(__name__)


DOCKER_BIN = os.environ.get("RECODER_WATCHDOG_DOCKER_BIN", "docker")
_DEFAULT_TIMEOUT_SEC = 15


class DockerUnavailableError(RuntimeError):
    """docker daemon 또는 docker CLI 에 접근할 수 없을 때 발생."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ContainerInfo:
    container_id: str
    name: str
    image: str
    status: str          # "running" / "exited" / "restarting" / ...
    state: str           # docker ps "State" (running/exited/...)
    raw: dict

    @property
    def is_running(self) -> bool:
        s = (self.state or self.status or "").lower()
        return "up" in s or s == "running"


@dataclass
class ContainerStats:
    name: str
    cpu_percent: float
    mem_percent: float
    mem_usage: str
    raw: dict


@dataclass
class DockerEvent:
    """docker events stream 의 한 줄을 파싱한 결과."""
    action: str          # "die", "oom", "kill", ...
    container_name: Optional[str]
    container_id: Optional[str]
    exit_code: Optional[int]
    raw: dict


# ---------------------------------------------------------------------------
# Low-level CLI helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    return shutil.which(DOCKER_BIN) is not None


def _run_docker(args: List[str], timeout: int = _DEFAULT_TIMEOUT_SEC) -> str:
    """`docker <args>` 동기 실행. stderr 가 daemon 오류를 포함하면 예외 변환."""
    if not _docker_available():
        raise DockerUnavailableError(f"docker binary {DOCKER_BIN!r} not found in PATH")

    try:
        proc = subprocess.run(
            [DOCKER_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerUnavailableError(f"docker binary not executable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailableError(f"docker {args!r} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().lower()
        if "cannot connect to the docker daemon" in stderr or "permission denied" in stderr:
            raise DockerUnavailableError(f"docker daemon not reachable: {proc.stderr.strip()}")
        # 그 외 비정상 종료 — 호출자에게 stderr 반환
        raise DockerUnavailableError(
            f"docker {args!r} exited rc={proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout


def _parse_json_lines(blob: str) -> List[dict]:
    """`docker ps --format {{json .}}` 처럼 줄당 JSON 1개인 출력 파싱."""
    out: List[dict] = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.debug("skip non-json line: %r", line[:200])
            continue
    return out


# ---------------------------------------------------------------------------
# Public API — containers
# ---------------------------------------------------------------------------


def list_containers(include_all: bool = False) -> List[ContainerInfo]:
    """현재 컨테이너 목록.

    include_all=True 면 종료된 컨테이너도 포함 (`docker ps -a`).
    """
    args = ["ps", "--format", "{{json .}}"]
    if include_all:
        args.insert(1, "-a")
    blob = _run_docker(args)
    rows = _parse_json_lines(blob)

    containers: List[ContainerInfo] = []
    for row in rows:
        name = row.get("Names") or row.get("Name") or ""
        # docker ps Names 는 콤마로 구분된 별칭 — 첫 번째만 사용
        if isinstance(name, str) and "," in name:
            name = name.split(",", 1)[0]
        containers.append(ContainerInfo(
            container_id=row.get("ID") or row.get("Id") or "",
            name=name or "",
            image=row.get("Image") or "",
            status=row.get("Status") or "",
            state=row.get("State") or "",
            raw=row,
        ))
    return containers


def inspect_container(name_or_id: str) -> dict:
    """`docker inspect <name>` — 단일 컨테이너 상세."""
    blob = _run_docker(["inspect", name_or_id])
    try:
        arr = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise DockerUnavailableError(f"failed to parse inspect output: {exc}") from exc
    if not arr:
        raise DockerUnavailableError(f"inspect returned empty for {name_or_id}")
    return arr[0]


def get_container_stats() -> List[ContainerStats]:
    """`docker stats --no-stream --format json`. 한 번만 샘플링한다."""
    blob = _run_docker(["stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
    rows = _parse_json_lines(blob)
    out: List[ContainerStats] = []
    for row in rows:
        cpu_raw = (row.get("CPUPerc") or "0%").rstrip("%").strip()
        mem_raw = (row.get("MemPerc") or "0%").rstrip("%").strip()
        try:
            cpu = float(cpu_raw)
        except ValueError:
            cpu = 0.0
        try:
            mem = float(mem_raw)
        except ValueError:
            mem = 0.0
        out.append(ContainerStats(
            name=(row.get("Name") or "").strip(),
            cpu_percent=cpu,
            mem_percent=mem,
            mem_usage=row.get("MemUsage") or "",
            raw=row,
        ))
    return out


def get_container_logs(name: str, since_seconds: int = 60, tail: int = 100) -> str:
    """`docker logs --since <s>s --tail <n> <name>` — 마지막 N 줄."""
    if not name:
        return ""
    try:
        blob = _run_docker(
            ["logs", "--since", f"{int(since_seconds)}s", "--tail", str(int(tail)), name],
            timeout=15,
        )
        return blob
    except DockerUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("get_container_logs(%s) failed: %s", name, exc)
        return ""


# ---------------------------------------------------------------------------
# Public API — events stream
# ---------------------------------------------------------------------------


def _spawn_events_proc(filters: List[str]) -> subprocess.Popen:
    if not _docker_available():
        raise DockerUnavailableError(f"docker binary {DOCKER_BIN!r} not found in PATH")
    args = [DOCKER_BIN, "events", "--format", "{{json .}}"]
    for f in filters:
        args.extend(["--filter", f])
    return subprocess.Popen(  # noqa: S603,S607 — args list, controlled binary
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # 라인 버퍼링
    )


class DockerEventStream:
    """`docker events` 출력을 다른 thread 에서 읽어들이는 helper.

    사용:
        with DockerEventStream(["event=oom", "event=die"]) as stream:
            for ev in stream.iter_events():
                if stop_event.is_set():
                    break
                ...
    """

    def __init__(self, filters: List[str]) -> None:
        self.filters = filters
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()

    def __enter__(self) -> "DockerEventStream":
        self._proc = _spawn_events_proc(self.filters)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception as exc:  # noqa: BLE001
                log.debug("event proc close failed: %s", exc)

    def iter_events(self) -> Generator[DockerEvent, None, None]:
        if not self._proc or not self._proc.stdout:
            raise DockerUnavailableError("event stream not initialized")
        for raw_line in self._proc.stdout:
            if self._stop.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield _parse_event(row)


def _parse_event(row: dict) -> DockerEvent:
    actor = row.get("Actor") or {}
    attrs = (actor.get("Attributes") if isinstance(actor, dict) else {}) or {}
    name = attrs.get("name") if isinstance(attrs, dict) else None
    container_id = actor.get("ID") if isinstance(actor, dict) else row.get("id")
    exit_code: Optional[int] = None
    raw_exit = attrs.get("exitCode") if isinstance(attrs, dict) else None
    if raw_exit is not None:
        try:
            exit_code = int(raw_exit)
        except (TypeError, ValueError):
            exit_code = None
    return DockerEvent(
        action=str(row.get("Action") or row.get("status") or "").lower(),
        container_name=name,
        container_id=container_id,
        exit_code=exit_code,
        raw=row,
    )


def docker_is_healthy() -> bool:
    """짧은 ping 으로 daemon 가용 여부 확인."""
    try:
        _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=5)
        return True
    except DockerUnavailableError:
        return False


__all__ = [
    "ContainerInfo",
    "ContainerStats",
    "DockerEvent",
    "DockerEventStream",
    "DockerUnavailableError",
    "docker_is_healthy",
    "get_container_logs",
    "get_container_stats",
    "inspect_container",
    "list_containers",
]
