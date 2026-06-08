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
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_path = f.name

        cmd = [
            "trivy", "image",
            "--format", "json",
            "--output", output_path,
            "--exit-code", "0",    # 취약점 있어도 exit 0 (결과는 JSON으로 판단)
            "--quiet",
            image,
        ]
        findings: list[SecurityFinding] = []
        try:
            await self._run_cmd(cmd)
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
            logger.warning("Trivy scan failed: %s", exc)
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass
        return findings

    # ------------------------------------------------------------------
    # Hadolint
    # ------------------------------------------------------------------

    async def _run_hadolint(self, dockerfile_path: str) -> list[SecurityFinding]:
        """
        hadolint --format json 실행.
        error → 차단. warning → 경고.
        """
        cmd = ["hadolint", "--format", "json", dockerfile_path]
        findings: list[SecurityFinding] = []
        try:
            stdout = await self._run_cmd(cmd, allow_nonzero=True)
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
            logger.warning("Hadolint scan failed: %s", exc)
        return findings

    # ------------------------------------------------------------------
    # gitleaks
    # ------------------------------------------------------------------

    async def _run_gitleaks(self, repo_path: str) -> list[SecurityFinding]:
        """
        gitleaks detect --report-format json 실행.
        시크릿 발견 → 항상 차단. 원문은 LLM에 미전달 (redacted=True).
        """
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_path = f.name

        cmd = [
            "gitleaks", "detect",
            "--source", repo_path,
            "--no-git",
            "--report-format", "json",
            "--report-path", output_path,
            "--exit-code", "0",
            "--quiet",
        ]
        findings: list[SecurityFinding] = []
        try:
            await self._run_cmd(cmd, allow_nonzero=True)
            raw_text = Path(output_path).read_text()
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
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass
        return findings

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_cmd(cmd: list[str], allow_nonzero: bool = False) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SCAN_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Command timed out after {_SCAN_TIMEOUT}s: {cmd[0]}")

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
