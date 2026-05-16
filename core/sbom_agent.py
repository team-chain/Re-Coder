"""
sbom_agent.py — SBOM 공급망 보안 에이전트 (설계서 §Q3 Must)

Syft 를 Docker 일회성 컨테이너로 실행해 CycloneDX JSON 형식 SBOM 을 생성한다.
Docker 미설치 시 graceful fallback (SBOM 없음 표시, OPA 게이트가 차단).

생성물:
  - {sbom_dir}/{image_digest}.cdx.json   (CycloneDX JSON)
  - 반환: SBOMResult (sbom_path, sbom_version, package_count, image_digest)

설계서 데이터 분류 정책:
  - sensitive metadata (내부 패키지명, 버전) → 로컬 저장 기본값
  - Control Plane 에는 sbom_hash / image_digest / package_count / vulnerability_summary 만 업로드
  - 전체 SBOM 업로드는 opt-in
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 상수 ──────────────────────────────────────────────────────────────
_SYFT_IMAGE       = "anchore/syft:latest"
_SYFT_TIMEOUT     = 300   # 5분
_SBOM_DIR_DEFAULT = Path.home() / ".recoder" / "sbom"


# ── 결과 타입 ──────────────────────────────────────────────────────────

@dataclass
class SBOMResult:
    """SBOM 생성 결과."""
    success:       bool
    sbom_path:     str = ""       # 로컬 CycloneDX JSON 경로
    sbom_version:  str = ""       # CycloneDX spec version
    sbom_hash:     str = ""       # SHA-256 of SBOM JSON
    image_digest:  str = ""       # 이미지 digest (sha256:...)
    package_count: int = 0        # 감지된 패키지 수
    error:         str = ""
    logs:          list[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        """Control Plane 업로드용 요약 (민감 메타데이터 제외)."""
        return {
            "sbom_hash":     self.sbom_hash,
            "image_digest":  self.image_digest,
            "package_count": self.package_count,
            "sbom_version":  self.sbom_version,
            "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
        }


# ── SBOM 에이전트 ─────────────────────────────────────────────────────

class SBOMAgent:
    """
    Syft 기반 SBOM 생성 에이전트.

    모든 public 메서드는 동기. server.py 에서 asyncio.to_thread 로 호출.
    """

    def __init__(self, sbom_dir: Optional[Path] = None):
        self._sbom_dir = sbom_dir or _SBOM_DIR_DEFAULT
        self._docker_available = shutil.which("docker") is not None

    def _ensure_sbom_dir(self) -> Path:
        self._sbom_dir.mkdir(parents=True, exist_ok=True)
        return self._sbom_dir

    def _run(self, args: list[str], timeout: int = _SYFT_TIMEOUT) -> tuple[int, str, str]:
        """subprocess.run 래퍼."""
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=os.environ.copy(),
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"Syft 타임아웃 ({timeout}s)"
        except FileNotFoundError as e:
            return -1, "", f"명령 없음: {e}"
        except Exception as e:
            return -1, "", str(e)

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def generate(
        self,
        image_uri: str,
        tag: str = "",
        log_fn=None,
    ) -> SBOMResult:
        """
        Docker 이미지에 대한 CycloneDX JSON SBOM 생성.

        Args:
            image_uri: ECR URI 또는 로컬 이미지명 (예: 123.dkr.ecr.../app:v1)
            tag:       이미지 태그 (파일명 생성에 사용)
            log_fn:    진행 로그 콜백

        Returns:
            SBOMResult
        """
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)
            logger.info(msg)
            if log_fn:
                log_fn(msg)

        if not self._docker_available:
            return SBOMResult(
                success=False,
                error="Docker 미설치 — SBOM 생성 불가",
                logs=logs,
            )

        sbom_dir = self._ensure_sbom_dir()

        # 안전한 파일명 생성
        safe_tag = (tag or "latest").replace(":", "_").replace("/", "_")
        safe_name = image_uri.split("/")[-1].split(":")[0]
        ts = time.strftime("%Y%m%d_%H%M%S")
        sbom_filename = f"{safe_name}_{safe_tag}_{ts}.cdx.json"
        sbom_path = sbom_dir / sbom_filename

        _log(f"[SBOM] Syft 스캔 시작: {image_uri}")

        # Syft Docker 일회성 컨테이너 실행
        # docker run --rm -v /var/run/docker.sock:/var/run/docker.sock
        #   anchore/syft:latest {image_uri} -o cyclonedx-json
        rc, out, err = self._run(
            [
                "docker", "run", "--rm",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                _SYFT_IMAGE,
                image_uri,
                "-o", "cyclonedx-json",
            ],
            timeout=_SYFT_TIMEOUT,
        )

        if rc != 0:
            error_msg = f"Syft 실행 실패 (rc={rc}): {err or out}"
            _log(f"[SBOM] {error_msg}")
            return SBOMResult(success=False, error=error_msg, logs=logs)

        # stdout 이 CycloneDX JSON
        try:
            sbom_data = json.loads(out)
        except json.JSONDecodeError as e:
            return SBOMResult(
                success=False,
                error=f"SBOM JSON 파싱 실패: {e}",
                logs=logs,
            )

        # 파일 저장
        try:
            sbom_path.write_text(out, encoding="utf-8")
        except Exception as e:
            return SBOMResult(
                success=False,
                error=f"SBOM 파일 저장 실패: {e}",
                logs=logs,
            )

        # 메타데이터 추출
        spec_version = sbom_data.get("specVersion", "1.4")
        components   = sbom_data.get("components", [])
        package_count = len(components)

        # image digest 추출 (metadata.component.version 또는 hashes)
        image_digest = ""
        metadata = sbom_data.get("metadata", {})
        meta_comp = metadata.get("component", {})
        for h in meta_comp.get("hashes", []):
            if h.get("alg", "").upper() in ("SHA-256", "SHA256"):
                image_digest = f"sha256:{h.get('content', '')}"
                break
        if not image_digest:
            # fallback: image_uri 에서 digest 힌트
            if "@sha256:" in image_uri:
                image_digest = "sha256:" + image_uri.split("@sha256:")[-1]

        sbom_hash = self._sha256_file(sbom_path)

        _log(f"[SBOM] 생성 완료 — 패키지 {package_count}개, hash={sbom_hash[:16]}...")

        return SBOMResult(
            success=True,
            sbom_path=str(sbom_path),
            sbom_version=spec_version,
            sbom_hash=sbom_hash,
            image_digest=image_digest,
            package_count=package_count,
            logs=logs,
        )

    def load(self, sbom_path: str) -> Optional[dict]:
        """저장된 SBOM JSON 파일 로드."""
        try:
            return json.loads(Path(sbom_path).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[sbom] SBOM 로드 실패: {e}")
            return None

    def list_recent(self, limit: int = 10) -> list[dict]:
        """최근 생성된 SBOM 파일 목록 반환."""
        sbom_dir = self._sbom_dir
        if not sbom_dir.exists():
            return []
        files = sorted(sbom_dir.glob("*.cdx.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        result = []
        for f in files[:limit]:
            stat = f.stat()
            result.append({
                "filename":    f.name,
                "path":        str(f),
                "size_bytes":  stat.st_size,
                "created_at":  time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
            })
        return result


# ── 싱글턴 ────────────────────────────────────────────────────────────

_instance: Optional[SBOMAgent] = None


def get_sbom_agent() -> SBOMAgent:
    global _instance
    if _instance is None:
        _instance = SBOMAgent()
    return _instance


__all__ = ["SBOMAgent", "SBOMResult", "get_sbom_agent"]
