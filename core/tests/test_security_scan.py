"""
test_security_scan.py -- S-3 security scan unit tests

Verifies that ScanResult.passed == True when Docker is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from quality_runner import QualityRunner


class TestSecurityScanDockerNotInstalled:
    """Docker not-installed behavior validation (S-3)."""

    def test_run_gitleaks_docker_not_found_returns_passed(self, tmp_path):
        """
        run_gitleaks() should return passed=True when docker is not found.
        Missing Docker is treated as skip (pass), not a scan failure.
        """
        runner = QualityRunner()

        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            result = runner.run_gitleaks(str(tmp_path))

        assert result.tool == "gitleaks"
        assert result.passed is True

    def test_run_gitleaks_docker_not_found_no_findings(self, tmp_path):
        """When Docker is missing, findings list must be empty."""
        runner = QualityRunner()

        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            result = runner.run_gitleaks(str(tmp_path))

        findings = result.findings or []
        assert len(findings) == 0

    @pytest.mark.asyncio
    async def test_run_all_docker_not_found_overall_passed(self, tmp_path):
        """
        run_all() should return all passed=True results even when all scans
        are skipped because Docker is not installed.
        """
        runner = QualityRunner()

        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            results = await runner.run_all(
                image_name="recoder-app:latest",
                workspace_path=str(tmp_path),
                dockerfile_path="",
            )

        assert isinstance(results, list)
        assert len(results) > 0
        assert all(r.passed for r in results)
