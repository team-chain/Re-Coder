"""스캐너(trivy·hadolint·gitleaks)를 ``docker run`` 으로 돌릴 때의 정리 도구.

docker CLI 프로세스만 죽이면 컨테이너는 계속 돈다. 이름을 붙여 띄우고, 시간 초과나
취소 때 ``docker rm -f`` 로 함께 지운다.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

def named_docker_run(cmd: list[str]) -> tuple[list[str], Optional[str]]:
    """``docker run`` 이면 지울 수 있게 이름을 붙인다. 아니면 그대로."""
    if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "run" and "--name" not in cmd:
        name = f"recoder-scan-{uuid.uuid4().hex[:12]}"
        return [cmd[0], cmd[1], "--name", name, *cmd[2:]], name
    return cmd, None


async def stop_scan_process(proc, name: Optional[str]) -> None:
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
    if name:
        try:
            killer = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=20)
        except Exception:  # noqa: BLE001
            logger.warning("scan container %s could not be removed", name)

