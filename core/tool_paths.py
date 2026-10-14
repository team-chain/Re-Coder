"""외부 CLI(docker·trivy 등)를 PATH 에서 못 찾을 때 흔한 설치 위치를 덧붙인다.

Docker Desktop 을 VS Code 보다 **나중에** 설치했거나, macOS 에서 Dock 으로 VS Code 를
띄우면 확장 호스트 → Core 로 물려받은 PATH 에 docker 가 없다. 그러면 Docker 가
멀쩡히 돌고 있어도 "docker 명령이 없습니다" 로 배포·검사가 막혔다.
이미 PATH 에 있으면 아무것도 바꾸지 않고, 있는 디렉터리만 **뒤에** 붙인다
(사용자가 고른 도구 버전보다 앞서지 않게).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _candidates() -> list[str]:
    home = Path.home()
    if sys.platform == "win32":
        dirs = []
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
            if base:
                dirs.append(str(Path(base) / "Docker" / "Docker" / "resources" / "bin"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(str(Path(local) / "Programs" / "Docker" / "Docker" / "resources" / "bin"))
            dirs.append(str(Path(local) / "Microsoft" / "WinGet" / "Links"))
        dirs.append(str(home / "scoop" / "shims"))
        dirs.append(r"C:\ProgramData\chocolatey\bin")
        return dirs
    if sys.platform == "darwin":
        return [
            "/usr/local/bin",
            "/opt/homebrew/bin",
            str(home / ".docker" / "bin"),
            "/Applications/Docker.app/Contents/Resources/bin",
            str(home / ".rd" / "bin"),          # Rancher Desktop
            str(home / ".orbstack" / "bin"),
        ]
    return ["/usr/local/bin", "/usr/bin", "/snap/bin", str(home / ".local" / "bin")]


def augment_path(tools: tuple[str, ...] = ("docker",)) -> list[str]:
    """tools 중 하나라도 PATH 에 없으면 존재하는 후보 디렉터리를 PATH 끝에 붙인다."""
    if all(shutil.which(tool) for tool in tools):
        return []
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    seen = {os.path.normcase(os.path.normpath(p)) for p in parts}
    added: list[str] = []
    for directory in _candidates():
        key = os.path.normcase(os.path.normpath(directory))
        if key in seen or not os.path.isdir(directory):
            continue
        parts.append(directory)
        seen.add(key)
        added.append(directory)
    if added:
        os.environ["PATH"] = os.pathsep.join(parts)
    return added
