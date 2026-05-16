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
_SBOM_TIMEOUT = 120   # 초


class SBOMGenerator:
    """
    Syft를 사용한 SBOM 생성기.
    Control Plane에는 metadata만 전송하고 전체 SBOM은 로컬 저장.
    """

    def __init__(self) -> None:
        _SBOM_DIR.mkdir(parents=True, exist_ok=True)

    async def generate(self, image: str) -> SBOMRecord:
        """
        Syft로 CycloneDX JSON SBOM 생성.
        Syft 미설치 시 빈 레코드 반환 (배포는 계속).
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_image = image.replace("/", "_").replace(":", "_")
        output_path = _SBOM_DIR / f"sbom-{safe_image}-{timestamp}.json"

        cmd = [
            "syft", image,
            "-o", f"cyclonedx-json={output_path}",
            "--quiet",
        ]

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

            return SBOMRecord(
                image=image,
                sbom_path=str(output_path),
                sbom_format="cyclonedx-json",
                package_count=package_count,
                generated_at=datetime.now(timezone.utc),
            )

        except FileNotFoundError:
            logger.warning("syft not found — SBOM generation skipped")
            return self._empty_record(image, error="syft not installed")

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
        if error:
            logger.debug("SBOM empty record: %s", error)
        return SBOMRecord(
            image=image,
            sbom_path="",
            package_count=0,
        )


# 싱글톤
sbom_generator = SBOMGenerator()
