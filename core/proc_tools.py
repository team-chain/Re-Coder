"""ReCoder Core — 시간 제한이 실제로 지켜지는 subprocess.run (2.0.32)

Windows 에서 subprocess.run(timeout=…) 은 시간이 지나면 **바로 그 프로세스만** 죽이고, 출력 파이프를 다 읽을 때까지 다시
기다린다(시간 제한 없이). npm.cmd → node.exe, docker.exe → docker-buildx.exe 처럼 손주 프로세스가 파이프를 쥐고 있으면
그 손주가 끝날 때까지 멈춘 채 기다렸다. 여기서는 프로세스 **나무 전체**를 끝낸다(Windows: taskkill /T /F, 그 밖: 프로세스 그룹).

시험이 subprocess.run 을 바꿔 끼운 경우(monkeypatch)에는 그대로 그것을 부른다.
"""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Sequence

_ORIGINAL_RUN = subprocess.run


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            _ORIGINAL_RUN(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - 이미 끝났거나 권한 없음
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def run(args: Sequence[str], *, timeout: float | None = None, capture_output: bool = False, input: Any = None,
        check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    if subprocess.run is not _ORIGINAL_RUN:  # 시험이 바꿔 끼움
        return subprocess.run(args, timeout=timeout, capture_output=capture_output, input=input, check=check, **kwargs)
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if input is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs.setdefault("start_new_session", True)
    with subprocess.Popen(args, **kwargs) as proc:
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        stream and stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                out = err = None
            raise subprocess.TimeoutExpired(args, timeout or 0, output=out, stderr=err) from exc
        except BaseException:
            kill_tree(proc)
            raise
        code = proc.poll()
    result = subprocess.CompletedProcess(args, code if code is not None else proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result
