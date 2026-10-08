"""Scanner failures cannot be converted to a clean deployment verdict."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.infra_agent import InfraAgent


def agent_with_report(monkeypatch, report, code=0):
    agent=object.__new__(InfraAgent)
    async def run(*args,**kwargs):return code,report,''
    async def summary(*args):return 'checked'
    monkeypatch.setattr(agent,'_run_subprocess',run)
    monkeypatch.setattr(agent,'_summarize_scan_results',summary)
    return agent


@pytest.mark.parametrize('report',['','broken','null','[]','{}','{"Results":"invalid"}','{"Results":{}}','{"Results":0}','{"Results":false}'])
def test_trivy_requires_a_complete_json_report(monkeypatch,report):
    agent=agent_with_report(monkeypatch,report)
    assert asyncio.run(agent.run_trivy_scan('app:v1'))['success'] is False


@pytest.mark.parametrize('report',['','broken','null','{}','["invalid"]'])
def test_gitleaks_missing_or_invalid_report_is_unverified(monkeypatch,report):
    agent=agent_with_report(monkeypatch,report)
    assert asyncio.run(agent.run_gitleaks_scan('/workspace'))['success'] is False


def test_gitleaks_scanner_error_cannot_hide_behind_empty_findings(monkeypatch):
    agent=agent_with_report(monkeypatch,'[]',code=2)
    assert asyncio.run(agent.run_gitleaks_scan('/workspace'))['success'] is False


def test_valid_empty_reports_still_pass(monkeypatch):
    agent=agent_with_report(monkeypatch,'{"Results":[]}')
    assert asyncio.run(agent.run_trivy_scan('app:v1'))['success'] is True
    agent=agent_with_report(monkeypatch,'[]')
    assert asyncio.run(agent.run_gitleaks_scan('/workspace'))['success'] is True


def test_hadolint_docker_error_is_not_a_clean_scan(tmp_path,monkeypatch):
    dockerfile=tmp_path/'Dockerfile'
    dockerfile.write_text('FROM nginx:alpine\n')
    agent=object.__new__(InfraAgent)
    async def communicate(**kwargs):return b'',b'Docker daemon unavailable'
    async def start(*args,**kwargs):return SimpleNamespace(returncode=125,communicate=communicate)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',start)
    result=asyncio.run(agent.run_hadolint_scan(str(dockerfile)))
    assert result['success'] is False
