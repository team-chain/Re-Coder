"""
Quality Runner — Trivy·Hadolint·gitleaks 보안 스캔 (Stage 2).

설계서 §9.2 기준:
- critical/high 필터링 후 Haiku로 요약
- Docker 컨테이너로 일회성 실행
- Docker 미설치 시 graceful 처리
- §15 원칙: gitleaks에서 secret 원문 제거, 파일 경로·라인·타입·rule_id만 반환
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from llm.router import get_router
from llm.base import LLMRequest

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """개별 스캔 도구 결과."""

    tool: str  # "trivy" | "hadolint" | "gitleaks"
    passed: bool
    critical_count: int = 0
    high_count: int = 0
    findings: list[dict] = field(default_factory=list)
    summary: str = ""  # Haiku가 생성한 한국어 요약
    raw_output: str = ""
    error: str = ""


class QualityRunner:
    """보안 품질 스캔 실행기."""

    _DOCKER_UNAVAILABLE_CODE = -127

    def __init__(self):
        self._docker_available = shutil.which("docker") is not None

    def _docker_skip_result(self, tool: str) -> ScanResult:
        return ScanResult(
            tool=tool,
            passed=True,
            summary=f"Docker unavailable - {tool} scan skipped",
        )

    # Docker 데몬 미실행 시 stderr 에 포함되는 패턴들
    _DOCKER_DAEMON_PATTERNS = (
        "failed to connect to the docker api",
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "error during connect",
        "open //./pipe/",
        "npipe:////./pipe/",
        "system cannot find the file specified",
    )

    @classmethod
    def _is_docker_daemon_error(cls, stderr: str) -> bool:
        """Docker CLI 는 있지만 데몬이 실행되지 않은 경우 감지."""
        low = stderr.lower()
        return any(p in low for p in cls._DOCKER_DAEMON_PATTERNS)

    def _run_docker_command(
        self, args: list[str], timeout: int = 300
    ) -> tuple[int, str, str]:
        """
        Docker 명령 실행 헬퍼.

        Returns:
            (return_code, stdout, stderr)
        """
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            # Docker CLI 는 있지만 데몬이 꺼져 있는 경우 — UNAVAILABLE 로 처리
            if proc.returncode != 0 and self._is_docker_daemon_error(proc.stderr):
                logger.warning("[quality_runner] Docker 데몬 미실행 — 스캔 건너뜀")
                return self._DOCKER_UNAVAILABLE_CODE, "", proc.stderr
            return proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as e:
            self._docker_available = False
            return self._DOCKER_UNAVAILABLE_CODE, "", str(e)
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except Exception as e:
            return -1, "", str(e)

    def run_trivy(self, image_name: str) -> ScanResult:
        """
        Trivy 이미지 스캔.

        설계서 §9.2:
        - docker run --rm aquasec/trivy:latest image {image_name} --format json
        - CRITICAL/HIGH 취약점만 필터링
        - Docker 없으면 passed=True, summary="Docker 미설치 - 스캔 건너뜀"

        Args:
            image_name: Docker 이미지 이름 (예: myapp:latest)

        Returns:
            ScanResult: Trivy 스캔 결과
        """
        if not self._docker_available:
            return ScanResult(
                tool="trivy",
                passed=True,
                summary="Docker 미설치 - Trivy 스캔 건너뜀",
            )

        cmd = [
            "docker",
            "run",
            "--rm",
            "aquasec/trivy:latest",
            "image",
            image_name,
            "--format",
            "json",
        ]

        returncode, stdout, stderr = self._run_docker_command(cmd, timeout=600)

        if returncode == self._DOCKER_UNAVAILABLE_CODE:
            return self._docker_skip_result("trivy")

        result = ScanResult(tool="trivy", passed=returncode == 0, raw_output=stdout)

        if returncode != 0:
            result.error = stderr or "Trivy scan failed"
            logger.warning(f"[trivy] scan failed (non-fatal): {result.error[:200]}")
            return result

        try:
            data = json.loads(stdout) if stdout.strip() else {}
            results = data.get("Results", [])

            findings = []
            critical_count = 0
            high_count = 0

            for result_item in results:
                vulns = result_item.get("Vulnerabilities", [])
                for vuln in vulns:
                    severity = vuln.get("Severity", "").upper()
                    if severity in ("CRITICAL", "HIGH"):
                        if severity == "CRITICAL":
                            critical_count += 1
                        else:
                            high_count += 1

                        findings.append(
                            {
                                "id": vuln.get("VulnerabilityID", ""),
                                "severity": severity,
                                "title": vuln.get("Title", ""),
                                "package": vuln.get("PkgName", ""),
                                "version": vuln.get("InstalledVersion", ""),
                            }
                        )

            result.critical_count = critical_count
            result.high_count = high_count
            result.findings = findings[:50]  # 최대 50개만

            if critical_count == 0 and high_count == 0:
                result.passed = True

            logger.info(
                f"[trivy] critical={critical_count}, high={high_count}, passed={result.passed}"
            )
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {str(e)}"
            logger.error(f"[trivy] {result.error}")

        return result

    def run_hadolint(self, dockerfile_path: str) -> ScanResult:
        """
        Hadolint Dockerfile 스캔.

        설계서 §9.2:
        - docker run --rm -i hadolint/hadolint < {dockerfile_path}
        - error/warning 파싱
        - Docker 없으면 passed=True, summary="Docker 미설치 - 스캔 건너뜀"

        Args:
            dockerfile_path: Dockerfile 경로

        Returns:
            ScanResult: Hadolint 스캔 결과
        """
        if not self._docker_available:
            return ScanResult(
                tool="hadolint",
                passed=True,
                summary="Docker 미설치 - Hadolint 스캔 건너뜀",
            )

        dockerfile = Path(dockerfile_path)
        if not dockerfile.exists():
            return ScanResult(
                tool="hadolint",
                passed=True,
                summary=f"Dockerfile 없음: {dockerfile_path}",
            )

        try:
            content = dockerfile.read_text(encoding="utf-8")
        except Exception as e:
            return ScanResult(
                tool="hadolint",
                passed=False,
                error=f"Failed to read Dockerfile: {str(e)}",
            )

        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "hadolint/hadolint",
        ]

        returncode, stdout, stderr = self._run_docker_command(
            cmd, timeout=120
        )  # stdin 사용하려면 다른 처리 필요
        if returncode == self._DOCKER_UNAVAILABLE_CODE:
            return self._docker_skip_result("hadolint")

        # 실제로는 stdin으로 파일 전달해야 하므로 subprocess.Popen 사용
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(input=content, timeout=120)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            return ScanResult(tool="hadolint", passed=False, error="Timeout")
        except FileNotFoundError:
            self._docker_available = False
            return self._docker_skip_result("hadolint")
        except Exception as e:
            return ScanResult(tool="hadolint", passed=False, error=str(e))

        result = ScanResult(tool="hadolint", passed=returncode == 0, raw_output=stdout)

        if returncode != 0 or stderr:
            output = stdout + "\n" + stderr
            findings = []
            errors = []
            warnings = []

            for line in output.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Hadolint 출력 형식: file:line LEVEL CODE MESSAGE
                if " " in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        level = parts[1].upper()
                        if level in ("ERROR", "WARNING"):
                            findings.append({"line": line})
                            if level == "ERROR":
                                errors.append(line)
                            else:
                                warnings.append(line)

            result.findings = findings
            result.critical_count = len(errors)
            result.high_count = len(warnings)
            if len(errors) > 0:
                result.passed = False

            logger.info(
                f"[hadolint] errors={len(errors)}, warnings={len(warnings)}, passed={result.passed}"
            )

        return result

    def run_gitleaks(self, workspace_path: str) -> ScanResult:
        """
        Gitleaks 시크릿 스캔.

        설계서 §15 원칙:
        - secret 원문은 결과에서 제거
        - 파일 경로·라인·타입·rule_id만 반환
        - findings에서 'Secret', 'Match' 필드 제거 후 반환

        Args:
            workspace_path: 스캔할 디렉토리 경로

        Returns:
            ScanResult: Gitleaks 스캔 결과
        """
        if not self._docker_available:
            return ScanResult(
                tool="gitleaks",
                passed=True,
                summary="Docker 미설치 - Gitleaks 스캔 건너뜀",
            )

        workspace = Path(workspace_path)
        if not workspace.exists():
            return ScanResult(
                tool="gitleaks",
                passed=True,
                summary=f"경로 없음: {workspace_path}",
            )

        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workspace}:/path",
            "zricethezav/gitleaks:latest",
            "detect",
            "--source",
            "/path",
            "--report-format",
            "json",
        ]

        returncode, stdout, stderr = self._run_docker_command(cmd, timeout=600)

        if returncode == self._DOCKER_UNAVAILABLE_CODE:
            return self._docker_skip_result("gitleaks")

        result = ScanResult(tool="gitleaks", passed=returncode == 0, raw_output=stdout)

        if returncode not in (0, 1):  # 0 = no leaks, 1 = leaks found
            result.error = stderr or "Gitleaks scan failed"
            logger.error(f"[gitleaks] {result.error}")
            return result

        try:
            data = json.loads(stdout) if stdout.strip() else {}
            leaks = data if isinstance(data, list) else data.get("leaks", [])

            findings = []
            for leak in leaks:
                # 시크릿 원문 제거
                leak_sanitized = {
                    "file": leak.get("File", ""),
                    "line": leak.get("StartLine", leak.get("LineNumber", 0)),
                    "rule_id": leak.get("RuleID", ""),
                    "rule": leak.get("Rule", ""),
                    "type": leak.get("Type", ""),
                    "verified": leak.get("Verified", False),
                }
                # 민감한 필드 명시적으로 제외
                for sensitive_field in ("Secret", "Match", "Substring", "Password"):
                    leak_sanitized.pop(sensitive_field, None)

                findings.append(leak_sanitized)

            result.findings = findings
            result.critical_count = len(findings)  # gitleaks는 모두 critical로 취급
            if len(findings) > 0:
                result.passed = False

            logger.info(f"[gitleaks] secrets_found={len(findings)}, passed={result.passed}")
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {str(e)}"
            logger.error(f"[gitleaks] {result.error}")

        return result

    async def run_all(
        self,
        workspace_path: str,
        image_name: str,
        dockerfile_path: str = "Dockerfile",
    ) -> list[ScanResult]:
        """
        Trivy + Hadolint + Gitleaks 순서로 실행.

        Args:
            workspace_path: 프로젝트 경로
            image_name: Docker 이미지 이름
            dockerfile_path: Dockerfile 경로 (기본값: "Dockerfile")

        Returns:
            list[ScanResult]: 3개 스캔 결과
        """
        results = []

        # 1. Trivy
        logger.info("[quality_runner] Starting Trivy scan...")
        trivy_result = self.run_trivy(image_name)
        results.append(trivy_result)

        # 2. Hadolint
        logger.info("[quality_runner] Starting Hadolint scan...")
        dockerfile_full_path = str(Path(workspace_path) / dockerfile_path)
        hadolint_result = self.run_hadolint(dockerfile_full_path)
        results.append(hadolint_result)

        # 3. Gitleaks
        logger.info("[quality_runner] Starting Gitleaks scan...")
        gitleaks_result = self.run_gitleaks(workspace_path)
        results.append(gitleaks_result)

        # 4. LLM 요약 생성
        self._summarize_with_llm(results)

        return results

    def _summarize_with_llm(self, results: list[ScanResult]) -> None:
        """
        Haiku 모델로 스캔 결과 한국어 요약 생성.

        설계서 §9.2:
        - Fast 모델(Haiku) 사용
        - critical/high 필터링
        - 각 결과에 summary 필드 채우기

        Args:
            results: ScanResult 리스트 (in-place 수정)
        """
        if not results:
            return

        # 요약할 데이터 준비
        summary_data = []
        for result in results:
            if not result.passed or result.critical_count > 0 or result.high_count > 0:
                summary_data.append(
                    {
                        "tool": result.tool,
                        "critical": result.critical_count,
                        "high": result.high_count,
                        "findings": result.findings[:5],  # 상위 5개만
                    }
                )

        if not summary_data:
            for result in results:
                result.summary = "모든 스캔 통과"
            return

        prompt = f"""다음 보안 스캔 결과를 한국어로 간결하게 요약해주세요.
중요한 취약점(Critical/High)에만 집중하세요. 한 문단, 최대 200자.

스캔 결과:
{json.dumps(summary_data, ensure_ascii=False, indent=2)}

요약 (한국어):
"""

        try:
            llm_resp = get_router().call(
                LLMRequest(prompt=prompt, max_tokens=256, temperature=0.0),
                agent="quality_runner",
                operation="summarize_scan_results",
            )
            summary = llm_resp.text.strip()

            # 모든 결과에 동일한 요약 할당
            for result in results:
                result.summary = summary

            logger.info(f"[quality_runner] LLM summary: {summary[:100]}...")
        except Exception as e:
            logger.error(f"[quality_runner] LLM summary failed: {str(e)}")
            # 실패 시 기본 요약
            critical_total = sum(r.critical_count for r in results)
            high_total = sum(r.high_count for r in results)
            for result in results:
                if critical_total > 0:
                    result.summary = f"Critical {critical_total}건, High {high_total}건 발견됨. 즉시 해결 필요."
                elif high_total > 0:
                    result.summary = f"High 취약점 {high_total}건 발견됨. 해결 권장."
                else:
                    result.summary = "주요 취약점 없음."


# ── 싱글턴 접근 ────────────────────────────────────────────────────────

_instance: Optional[QualityRunner] = None


def get_quality_runner() -> QualityRunner:
    """QualityRunner 싱글턴 반환."""
    global _instance
    if _instance is None:
        _instance = QualityRunner()
    return _instance


__all__ = [
    "QualityRunner",
    "ScanResult",
    "get_quality_runner",
]
