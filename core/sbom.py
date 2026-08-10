"""
Local Core — Q3: SBOM 생성 (Syft CycloneDX JSON)

설계서 §Q3 SBOM 공급망 보안:
- Syft로 CycloneDX JSON 형식 SBOM 생성 (일회성 컨테이너처럼 실행)
- DeploymentRecord에 sbom_path, sbom_version 추가
- Control Plane에는 sbom_hash, image_digest, package_count, vulnerability_summary만 업로드
  (전체 SBOM 업로드는 opt-in)
- sensitive metadata 정책: Free/Pro는 로컬 저장 기본값
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.schemas import SBOMRecord

logger = logging.getLogger(__name__)

_SBOM_DIR = Path(os.environ.get("RECODER_SBOM_DIR", Path.home() / ".recoder" / "sbom"))
#: syft 바이너리가 없을 때 대신 쓰는 컨테이너 이미지.
_SYFT_IMAGE = "anchore/syft:latest"
_SBOM_TIMEOUT = 120   # 초


class SBOMGenerator:
    """
    Syft를 사용한 SBOM 생성기.
    Control Plane에는 metadata만 전송하고 전체 SBOM은 로컬 저장.
    """

    def __init__(self) -> None:
        _SBOM_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _syft_command(image: str, output_path: Path) -> list[str]:
        """syft 실행 명령. **바이너리가 없으면 도커로 돌린다.**

        `syft` 를 맨 이름으로 부르던 것이 문제였다. 이 프로젝트가 사용자에게
        요구하는 준비물은 Python·Node·**Docker Desktop** 뿐이고(SETUP.md),
        syft 는 설치 안내도 CI 설치도 없다. 즉 **아무 개발자 PC 에도 없다.**
        그 상태에서 SBOM 실패를 배포 실패로 올리면 모든 배포가 막힌다.

        도커는 어차피 있어야 이미지를 빌드하므로, 없는 바이너리 대신
        컨테이너로 돌린다. 같은 저장소의 `sbom_agent.py` 가 이미 쓰는 방식이다.
        """
        if shutil.which("syft"):
            return ["syft", image, "-o", f"cyclonedx-json={output_path}", "--quiet"]

        # 컨테이너 안에서 결과를 쓰고 호스트 경로로 마운트해 받는다.
        # 이미지 자체는 로컬 도커 데몬에서 읽으므로 소켓도 함께 넘긴다.
        return [
            "docker", "run", "--rm",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{output_path.parent}:/out",
            _SYFT_IMAGE,
            f"docker:{image}",
            "-o", f"cyclonedx-json=/out/{output_path.name}",
            "--quiet",
        ]

    async def generate(self, image: str) -> SBOMRecord:
        """Syft 로 CycloneDX JSON SBOM 생성.

        실패하면 **원인을 담은** 빈 레코드를 돌려준다. 배포를 세울지 말지는
        호출부가 정한다 (`ECSAgent._step_sbom` 이 정책상 세운다).
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_image = image.replace("/", "_").replace(":", "_")
        output_path = _SBOM_DIR / f"sbom-{safe_image}-{timestamp}.json"

        cmd = self._syft_command(image, output_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SBOM_TIMEOUT)

            if proc.returncode != 0:
                logger.warning("Syft failed (rc=%d): %s", proc.returncode, stderr.decode()[:200])
                return self._empty_record(image, error=stderr.decode()[:200])

            if not output_path.exists():
                return self._empty_record(image, error="SBOM 파일이 생성되지 않았습니다")

            # SBOM 파싱
            sbom_data = json.loads(output_path.read_text())
            package_count = len(sbom_data.get("components", []))

            logger.info("SBOM generated: %s (%d packages) → %s", image, package_count, output_path)

            # `generated_at` 은 str 필드다. datetime 을 넣으면 pydantic 이
            # ValidationError 를 내고, 그걸 아래 `except Exception` 이 삼켜
            # **성공한 SBOM 이 매번 빈 결과로 둔갑**했다. `sbom_format` 은
            # 모델에 없는 이름이라 조용히 버려지고 있었다.
            return SBOMRecord(
                image=image,
                sbom_path=str(output_path),
                package_count=package_count,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )

        except FileNotFoundError:
            # syft 도 docker 도 없다.
            return self._empty_record(
                image,
                error="syft 도 docker 도 실행할 수 없습니다. Docker Desktop 이 "
                      "켜져 있는지 확인하세요.",
            )

        except asyncio.TimeoutError:
            logger.warning("Syft timed out after %ds", _SBOM_TIMEOUT)
            return self._empty_record(image, error=f"syft timeout ({_SBOM_TIMEOUT}s)")

        except Exception as exc:
            logger.warning("SBOM generation failed: %s", exc)
            return self._empty_record(image, error=str(exc))

    def get_upload_metadata(self, record: SBOMRecord) -> dict:
        """
        Control Plane에 업로드할 metadata만 추출.
        설계서 §SBOM — sensitive metadata:
        전체 SBOM은 opt-in. 기본은 hash/digest/count/summary만.
        """
        sbom_hash = ""
        if record.sbom_path and Path(record.sbom_path).exists():
            content = Path(record.sbom_path).read_bytes()
            sbom_hash = hashlib.sha256(content).hexdigest()

        return {
            "sbom_hash": sbom_hash,
            "image": record.image,
            "image_digest": record.image_digest,
            "package_count": record.package_count,
            "vulnerability_summary": record.vulnerability_summary,
            "sbom_format": record.sbom_format,
            "generated_at": record.generated_at.isoformat(),
        }

    @staticmethod
    def _empty_record(image: str, error: str = "") -> SBOMRecord:
        """실패 기록. **원인을 반드시 실어 보낸다.**

        예전에는 `error` 를 받아서 debug 로그로만 흘리고 모델에는 안 넣었다.
        `SBOMRecord.error` 필드가 있는데도 항상 None 이었다. 그래서 호출부가
        "syft 가 결과 파일을 안 남겼습니다"라는 **기본 문구밖에 못 보여줬고**,
        진짜 원인(타임아웃·권한 거부·도커 소켓 접근 실패)은 사용자에게
        닿지 않았다. 이미 설치된 syft 를 다시 설치하러 가게 된다.
        """
        if error:
            logger.warning("SBOM 생성 실패: %s", error)
        return SBOMRecord(
            image=image,
            sbom_path="",
            package_count=0,
            error=error or None,
        )


# 싱글톤
sbom_generator = SBOMGenerator()
