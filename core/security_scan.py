"""
Local Core — Q3: 보안 스캐너 (Trivy / Hadolint / gitleaks)

설계서 §Q3 Trivy/Hadolint/gitleaks OPA 게이트:
- Trivy critical → 차단
- Trivy high → 기본 경고 (조직 정책으로 차단 전환 가능)
- Hadolint error → 차단
- Hadolint warning → 경고만 표시
- gitleaks 시크릿 → 항상 차단, 원문 LLM 미전달

override 승인: Approval Level 4 격상 + AuditLog 사유 기록
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

try:
    from core.schemas import (
        SecurityFinding,
        SecurityScanResult,
        SecurityScanSeverity,
        SecurityScanTool,
    )
except ImportError:  # core/ 가 직접 sys.path 에 있는 실행 환경
    from schemas import (
        SecurityFinding,
        SecurityScanResult,
        SecurityScanSeverity,
        SecurityScanTool,
    )

logger = logging.getLogger(__name__)

_SCAN_TIMEOUT = 120   # 초
_TRIVY_TIMEOUT = 600  # 초 — 이미지 스캔은 DB 다운로드·레이어 분석으로 길다

# ---------------------------------------------------------------------------
# Docker 폴백 — 바이너리가 없으면 같은 도구를 컨테이너로 돌린다
# ---------------------------------------------------------------------------
#
# 실기기(검증 C3)에서 로컬 Docker 배포는 `quality_runner` 가 `docker run` 으로
# trivy 를 돌려 잘 검사했는데, ECS 경로는 이 모듈이 **네이티브 바이너리만**
# 찾아서 trivy·gitleaks·hadolint 셋 다 "미설치" 로 차단됐다. 같은 컴퓨터,
# 같은 Docker 인데 경로에 따라 검사 가능 여부가 갈리는 건 도구 문제가 아니라
# 우리 문제다. 바이너리가 없고 docker 가 있으면 공식 이미지로 대신 돈다.
# 둘 다 없으면 예전처럼 `<tool>_not_installed` 로 남겨 게이트가 막는다.
_DOCKER_IMAGES = {
    "trivy": "aquasec/trivy:latest",
    "hadolint": "hadolint/hadolint:latest",
    "gitleaks": "zricethezav/gitleaks:latest",
}
#: trivy 컨테이너에 넘겨 줄 AWS 환경변수 — 값은 argv 에 싣지 않고 `-e NAME` 으로
#: docker 가 현재 환경에서 읽게 한다(프로세스 목록에 비밀이 안 보인다).
_AWS_ENV_PASSTHROUGH = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
)


def _which(binary: str) -> bool:
    return shutil.which(binary) is not None


def _docker_fallback_available(tool: str) -> bool:
    """도구 바이너리는 없고 docker 는 있는가 — 그때만 컨테이너로 대신 돈다."""
    return not _which(tool) and _which("docker")



class SecurityScanner:
    """Trivy / Hadolint / gitleaks 통합 보안 스캐너"""

    async def scan_all(
        self,
        image: Optional[str] = None,
        dockerfile_path: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> SecurityScanResult:
        """세 도구를 병렬 실행하고 결과를 통합한다."""
        result = SecurityScanResult(
            image=image,
            dockerfile_path=dockerfile_path,
            repo_path=repo_path,
        )
        findings: list[SecurityFinding] = []

        tasks = []
        if image:
            tasks.append(self._run_trivy(image))
        if dockerfile_path:
            tasks.append(self._run_hadolint(dockerfile_path))
        if repo_path:
            tasks.append(self._run_gitleaks(repo_path))
            tasks.append(self._run_builtin_secrets(repo_path))

        scan_results = await asyncio.gather(*tasks, return_exceptions=True)

        for scan in scan_results:
            if isinstance(scan, Exception):
                logger.warning("Security scan error: %s", scan)
            elif isinstance(scan, list):
                findings.extend(scan)

        findings = self._dedupe_secret_findings(findings)
        result.findings = findings
        result.compute_pass()
        logger.info(
            "Security scan: critical=%d high=%d hadolint_err=%d secrets=%d passed=%s",
            result.critical_count, result.high_count,
            result.hadolint_error_count, result.secret_count, result.scan_passed,
        )
        return result

    # ------------------------------------------------------------------
    # Trivy
    # ------------------------------------------------------------------

    async def _run_trivy(self, image: str) -> list[SecurityFinding]:
        """
        trivy image --format json 실행.
        CRITICAL → 차단. HIGH → 경고.
        """
        out_dir = tempfile.mkdtemp(prefix="recoder-trivy-")
        output_path = os.path.join(out_dir, "trivy.json")

        trivy_args = [
            "image",
            "--format", "json",
            "--exit-code", "0",    # 취약점 있어도 exit 0 (결과는 JSON으로 판단)
            "--quiet",
        ]
        if _docker_fallback_available("trivy"):
            # 로컬 데몬의 이미지도 보게 소켓을 물리고, 결과 파일은 마운트한
            # 디렉터리로 받는다. ECR 이미지를 끌어와야 하면 AWS 자격증명이
            # 필요하므로 환경변수 이름만 넘기고 ~/.aws 는 읽기 전용으로 준다.
            # 취약점 DB 캐시를 호스트에 두지 않으면 매번 수백 MB 를 다시 받는다.
            cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "recoder-trivy")
            os.makedirs(cache_dir, exist_ok=True)
            cmd = [
                "docker", "run", "--rm",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{out_dir}:/out",
                "-v", f"{cache_dir}:/root/.cache/",
            ]
            for name in _AWS_ENV_PASSTHROUGH:
                if os.environ.get(name):
                    cmd.extend(["-e", name])
            aws_dir = os.path.join(os.path.expanduser("~"), ".aws")
            if os.path.isdir(aws_dir):
                cmd.extend(["-v", f"{aws_dir}:/root/.aws:ro"])
            cmd.append(_DOCKER_IMAGES["trivy"])
            cmd.extend(trivy_args + ["--output", "/out/trivy.json", image])
            logger.info("trivy 바이너리가 없어 %s 컨테이너로 검사합니다", _DOCKER_IMAGES["trivy"])
        else:
            cmd = ["trivy"] + trivy_args + ["--output", output_path, image]
        findings: list[SecurityFinding] = []
        try:
            # 첫 실행은 취약점 DB(수백 MB)를 받느라 2분을 넘길 수 있다.
            await self._run_cmd(cmd, timeout=_TRIVY_TIMEOUT)
            raw = json.loads(Path(output_path).read_text())
            for result in raw.get("Results", []):
                for vuln in result.get("Vulnerabilities") or []:
                    sev_str = vuln.get("Severity", "UNKNOWN").upper()
                    sev = self._trivy_severity(sev_str)
                    findings.append(SecurityFinding(
                        tool=SecurityScanTool.TRIVY,
                        severity=sev,
                        title=vuln.get("VulnerabilityID", "Unknown"),
                        description=vuln.get("Title") or vuln.get("Description") or "",
                        location=f"{result.get('Target', '')}:{vuln.get('PkgName', '')}@{vuln.get('InstalledVersion', '')}",
                        fix_suggestion=f"업그레이드: {vuln.get('FixedVersion', '버전 정보 없음')}",
                    ))
        except FileNotFoundError:
            logger.warning("trivy not found — skipping Trivy scan")
            findings.append(SecurityFinding(
                tool=SecurityScanTool.TRIVY,
                severity=SecurityScanSeverity.INFO,
                title="trivy_not_installed",
                description="trivy가 설치되지 않아 스캔을 건너뜁니다",
            ))
        except Exception as exc:
            # **조용히 넘어가면 안 된다.**
            #
            # 여기서 아무 흔적도 안 남기면 findings 가 빈 채로 돌아가고,
            # 호출부는 "취약점 0건 = 스캔 통과"로 읽는다. 즉 **이미지를 한 번도
            # 들여다보지 않은 배포가 보안 게이트를 통과**한다.
            #
            # 실제로 그렇게 되는 경로가 있다 — 최소권한 정책에 ECR **pull**
            # 권한(`ecr:BatchGetImage`)이 없으면 trivy 가 원격 이미지를 못 받아
            # 여기로 떨어진다. 권한 하나가 보안 게이트 무력화로 이어진다.
            #
            # 위 FileNotFoundError 갈래와 똑같이 흔적을 남겨, 결과만 보고도
            # 스캔이 실제로 수행됐는지 알 수 있게 한다.
            logger.warning("Trivy scan failed: %s", exc)
            findings.append(SecurityFinding(
                tool=SecurityScanTool.TRIVY,
                severity=SecurityScanSeverity.INFO,
                title="trivy_scan_failed",
                description=(
                    f"Trivy 스캔이 실패해 이미지를 검사하지 못했습니다: {exc} — "
                    f"이 결과의 '취약점 없음'은 검사 결과가 아닙니다. "
                    f"ECR 이미지를 받아오지 못한 경우라면 권한표의 "
                    f"ecr:BatchGetImage · ecr:GetDownloadUrlForLayer 를 확인하세요."
                ),
            ))
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        return findings

    # ------------------------------------------------------------------
    # Hadolint
    # ------------------------------------------------------------------

    async def _run_hadolint(self, dockerfile_path: str) -> list[SecurityFinding]:
        """
        hadolint --format json 실행.
        error → 차단. warning → 경고.
        """
        findings: list[SecurityFinding] = []
        stdin_data: Optional[bytes] = None
        if _docker_fallback_available("hadolint"):
            # 파일을 마운트하지 않고 표준입력으로 넘긴다 — 경로에 공백·한글이
            # 있어도 안전하고, 컨테이너가 워크스페이스를 볼 필요도 없다.
            try:
                stdin_data = Path(dockerfile_path).read_bytes()
            except OSError as exc:
                return [SecurityFinding(
                    tool=SecurityScanTool.HADOLINT,
                    severity=SecurityScanSeverity.INFO,
                    title="hadolint_scan_failed",
                    description=f"Dockerfile 을 읽지 못했습니다: {exc}",
                )]
            cmd = ["docker", "run", "--rm", "-i", _DOCKER_IMAGES["hadolint"],
                   "hadolint", "--format", "json", "-"]
            logger.info("hadolint 바이너리가 없어 %s 컨테이너로 검사합니다", _DOCKER_IMAGES["hadolint"])
        else:
            cmd = ["hadolint", "--format", "json", dockerfile_path]
        try:
            stdout = await self._run_cmd(cmd, allow_nonzero=True, stdin_data=stdin_data)
            if not stdout.strip():
                return findings
            issues = json.loads(stdout)
            for issue in issues:
                level = issue.get("level", "warning").lower()
                sev = SecurityScanSeverity.CRITICAL if level == "error" else SecurityScanSeverity.MEDIUM
                findings.append(SecurityFinding(
                    tool=SecurityScanTool.HADOLINT,
                    severity=sev,
                    title=issue.get("code", "DL????"),
                    description=issue.get("message", ""),
                    location=f"{dockerfile_path}:{issue.get('line', '?')}",
                    fix_suggestion=f"https://github.com/hadolint/hadolint/wiki/{issue.get('code','')}",
                ))
        except FileNotFoundError:
            logger.warning("hadolint not found — skipping Hadolint scan")
            findings.append(SecurityFinding(
                tool=SecurityScanTool.HADOLINT,
                severity=SecurityScanSeverity.INFO,
                title="hadolint_not_installed",
                description="hadolint가 설치되지 않아 스캔을 건너뜁니다",
            ))
        except Exception as exc:
            # 조용히 넘어가면 findings 가 빈 채로 돌아가고, 호출부는
            # "위반 0건 = 통과"로 읽는다. Dockerfile 을 한 번도 안 본 배포가
            # 게이트를 통과하게 된다 — trivy 쪽과 같은 이유로 흔적을 남긴다.
            logger.warning("Hadolint scan failed: %s", exc)
            findings.append(SecurityFinding(
                tool=SecurityScanTool.HADOLINT,
                severity=SecurityScanSeverity.INFO,
                title="hadolint_scan_failed",
                description=(
                    f"Hadolint 검사가 실패해 Dockerfile 을 보지 못했습니다: {exc} — "
                    f"이 결과의 '위반 없음'은 검사 결과가 아닙니다."
                ),
            ))
        return findings

    # ------------------------------------------------------------------
    # gitleaks
    # ------------------------------------------------------------------

    async def _run_gitleaks(self, repo_path: str) -> list[SecurityFinding]:
        """
        gitleaks detect --report-format json 실행.
        시크릿 발견 → 항상 차단. 원문은 LLM에 미전달 (redacted=True).
        """
        out_dir = tempfile.mkdtemp(prefix="recoder-gitleaks-")
        output_path = os.path.join(out_dir, "gitleaks.json")

        gitleaks_args = ["detect", "--no-git", "--report-format", "json", "--exit-code", "0"]
        if _docker_fallback_available("gitleaks"):
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{os.path.abspath(repo_path)}:/repo:ro",
                "-v", f"{out_dir}:/out",
                _DOCKER_IMAGES["gitleaks"],
            ] + gitleaks_args + ["--source", "/repo", "--report-path", "/out/gitleaks.json"]
            logger.info("gitleaks 바이너리가 없어 %s 컨테이너로 검사합니다", _DOCKER_IMAGES["gitleaks"])
        else:
            cmd = ["gitleaks"] + gitleaks_args + ["--source", repo_path, "--report-path", output_path]
        findings: list[SecurityFinding] = []
        try:
            # `--exit-code 0` 이라 시크릿이 있어도 0 이다. 그래서 0 이 아니면
            # 검사 자체가 실패한 것(잘못된 플래그·마운트 실패 등) — 비어 있는
            # 보고서를 "시크릿 없음" 으로 읽으면 안 된다.
            await self._run_cmd(cmd)
            raw_text = Path(output_path).read_text() if os.path.exists(output_path) else ""
            if not raw_text.strip() or raw_text.strip() == "null":
                return findings
            leaks = json.loads(raw_text)
            for leak in (leaks or []):
                findings.append(SecurityFinding(
                    tool=SecurityScanTool.GITLEAKS,
                    severity=SecurityScanSeverity.CRITICAL,
                    title=f"secret_leak:{leak.get('RuleID', 'unknown')}",
                    description=f"시크릿 감지: {leak.get('Description', '')} — 파일: {leak.get('File', '?')}:{leak.get('StartLine', '?')}",
                    location=f"{leak.get('File', '?')}:{leak.get('StartLine', '?')}",
                    fix_suggestion="해당 파일에서 시크릿을 제거하고 git history를 정리하세요. AWS Secrets Manager 또는 .env 파일을 사용하세요.",
                    redacted=True,  # 원문 미포함
                ))
        except FileNotFoundError:
            logger.warning("gitleaks not found — skipping gitleaks scan")
            findings.append(SecurityFinding(
                tool=SecurityScanTool.GITLEAKS,
                severity=SecurityScanSeverity.INFO,
                title="gitleaks_not_installed",
                description="gitleaks가 설치되지 않아 스캔을 건너뜁니다",
            ))
        except Exception as exc:
            logger.warning("gitleaks scan failed: %s", exc)
            findings.append(SecurityFinding(
                tool=SecurityScanTool.GITLEAKS,
                severity=SecurityScanSeverity.INFO,
                title="gitleaks_scan_failed",
                description=(
                    f"gitleaks 검사가 실패해 저장소를 보지 못했습니다: {exc} — "
                    f"이 결과의 '시크릿 없음'은 검사 결과가 아닙니다."
                ),
            ))
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        return findings

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_cmd(
        cmd: list[str], allow_nonzero: bool = False, stdin_data: Optional[bytes] = None,
        timeout: int = _SCAN_TIMEOUT,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Command timed out after {timeout}s: {cmd[0]}")

        if proc.returncode != 0 and not allow_nonzero:
            raise RuntimeError(f"{cmd[0]} failed (rc={proc.returncode}): {stderr.decode()[:500]}")

        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _trivy_severity(sev_str: str) -> SecurityScanSeverity:
        return {
            "CRITICAL": SecurityScanSeverity.CRITICAL,
            "HIGH": SecurityScanSeverity.HIGH,
            "MEDIUM": SecurityScanSeverity.MEDIUM,
            "LOW": SecurityScanSeverity.LOW,
        }.get(sev_str, SecurityScanSeverity.INFO)

    # ──────────────────────────────────────────────────────────────────
    # 내장 시크릿 스캐너 (gitleaks 바이너리 없이도 동작 — 학생 PC 폴백)
    # ──────────────────────────────────────────────────────────────────

    # (이름, 정규식, 심각도) — 값 원문은 절대 로그/응답에 싣지 않는다.
    _SECRET_PATTERNS = [
        ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"), "critical"),
        ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"), "critical"),
        ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "critical"),
        ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
        ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), "high"),
        ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "critical"),
        ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "medium"),
        ("generic_secret_assignment", re.compile(
            r"(?i)(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?key|auth[_-]?token|password|passwd|client[_-]?secret)"
            r"\s*[:=]\s*[\'\"]([^\'\"\s]{16,})[\'\"]"), "high"),
    ]
    _PLACEHOLDER_RE = re.compile(r"(?i)(your[_-]?|xxx|change[_-]?me|example|placeholder|dummy|<.*>|\{\{.*\}\}|\$\{)")
    _SECRET_SCAN_SKIP_DIRS = {
        ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
        "dist", "build", ".next", "out", "target", "coverage", ".mypy_cache",
        ".pytest_cache", ".ruff_cache",
    }
    _SECRET_SCAN_MAX_FILES = 2000
    _SECRET_SCAN_MAX_BYTES = 1_000_000

    async def _run_builtin_secrets(self, repo_path: str) -> list[SecurityFinding]:
        """순수 파이썬 정규식 기반 시크릿 스캔. 파일:라인 보고, 값은 마스킹."""
        findings: list[SecurityFinding] = []
        try:
            root = Path(repo_path)
            if not root.exists():
                return findings
            scanned = 0
            for fp in root.rglob("*"):
                if scanned >= self._SECRET_SCAN_MAX_FILES:
                    break
                if fp.is_dir():
                    continue
                if any(part in self._SECRET_SCAN_SKIP_DIRS for part in fp.parts):
                    continue
                try:
                    if fp.stat().st_size > self._SECRET_SCAN_MAX_BYTES:
                        continue
                    text = fp.read_text(encoding="utf-8", errors="ignore")
                except (OSError, ValueError):
                    continue
                scanned += 1
                try:
                    rel = str(fp.relative_to(root)).replace("\\", "/")
                except ValueError:
                    rel = str(fp)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if len(line) > 4000:
                        line = line[:4000]
                    for name, rx, sev in self._SECRET_PATTERNS:
                        m = rx.search(line)
                        if not m:
                            continue
                        # generic 패턴은 placeholder/예시 값 제외 (오탐 감소)
                        if name == "generic_secret_assignment":
                            val = m.group(1)
                            if self._PLACEHOLDER_RE.search(val):
                                continue
                        raw = m.group(0)
                        masked = (raw[:4] + "***") if len(raw) > 7 else "***"
                        findings.append(SecurityFinding(
                            tool=SecurityScanTool.GITLEAKS,
                            severity=self._trivy_severity(sev.upper()),
                            title=f"secret_leak:{name}",
                            description=f"시크릿 의심 패턴({name}) 감지 — 파일: {rel}:{lineno} (값 마스킹: {masked})",
                            location=f"{rel}:{lineno}",
                            fix_suggestion="해당 값을 코드에서 제거하고 .env 또는 AWS Secrets Manager로 옮기세요.",
                            redacted=True,
                        ))
                        break  # 한 줄에 한 건만
        except Exception as exc:
            logger.warning("builtin secret scan failed: %s", exc)
        return findings

    @staticmethod
    def _dedupe_secret_findings(findings: list[SecurityFinding]) -> list[SecurityFinding]:
        """동일 location 의 시크릿 중복 제거(gitleaks 우선). 비시크릿은 그대로 유지."""
        seen: set[str] = set()
        out: list[SecurityFinding] = []
        # gitleaks 실제 탐지(title이 secret_leak: 로 시작) 먼저 처리되도록 정렬은 불필요 —
        # 동일 location 이면 먼저 들어온 것을 유지하되, 위치 키로만 판단.
        for f in findings:
            is_secret = (f.title or "").startswith("secret_leak:")
            if not is_secret or not f.location:
                out.append(f)
                continue
            key = f.location
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out


# 싱글톤
security_scanner = SecurityScanner()


# ════════════════════════════════════════════════════════════════════════════
# 독립 시크릿 스캐너 (바이너리·scan_all 불필요) — 코드 에이전트 적용 전 검사 +
# 프로젝트 전체 스캔 라우트가 직접 사용한다. 값 원문은 절대 반환하지 않는다.
# ════════════════════════════════════════════════════════════════════════════

_STANDALONE_SECRET_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"), "critical"),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"), "critical"),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "critical"),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), "high"),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "critical"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "medium"),
    ("generic_secret_assignment", re.compile(
        r"(?i)(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?key|auth[_-]?token|password|passwd|client[_-]?secret)"
        r"\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]"), "high"),
]
_STANDALONE_PLACEHOLDER = re.compile(
    r"(?i)(your[_-]?|xxx|change[_-]?me|example|placeholder|dummy|<.*>|\{\{.*\}\}|\$\{)")
_STANDALONE_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build",
    ".next", "out", "target", "coverage", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


def scan_text_for_secrets(text: str, filename: str = "") -> list[dict]:
    """문자열에서 시크릿 의심 패턴 탐지. 반환 항목은 값 원문 미포함(마스킹)."""
    out: list[dict] = []
    if not text:
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            line = line[:4000]
        for name, rx, sev in _STANDALONE_SECRET_PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            if name == "generic_secret_assignment" and _STANDALONE_PLACEHOLDER.search(m.group(1)):
                continue
            raw = m.group(0)
            masked = (raw[:4] + "***") if len(raw) > 7 else "***"
            out.append({
                "rule": name,
                "severity": sev,
                "file": filename,
                "line": lineno,
                "masked": masked,
                "fix": "해당 값을 코드에서 제거하고 .env 또는 AWS Secrets Manager로 옮기세요.",
            })
            break  # 한 줄 한 건
    return out


def scan_project_for_secrets(repo_path: str, max_files: int = 2000, max_bytes: int = 1_000_000) -> list[dict]:
    """프로젝트 전체(작업 트리)를 순회하며 시크릿 탐지. 커밋 여부와 무관."""
    out: list[dict] = []
    root = Path(repo_path)
    if not root.exists():
        return out
    scanned = 0
    for fp in root.rglob("*"):
        if scanned >= max_files:
            break
        if fp.is_dir() or any(part in _STANDALONE_SKIP_DIRS for part in fp.parts):
            continue
        try:
            if fp.stat().st_size > max_bytes:
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        scanned += 1
        try:
            rel = str(fp.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(fp)
        out.extend(scan_text_for_secrets(text, rel))
    return out
