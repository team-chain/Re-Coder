import asyncio
import pytest
from fastapi import HTTPException
from api.routes import security_checks as route
import vuln_recheck


def test_code_scan_scope_and_excluded_dependencies(tmp_path):
    (tmp_path / 'app.ts').write_text('const x = 1', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'huge.ts').write_text('ignored')
    result = asyncio.run(route.code_scan(route.CodeScanRequest(workspace_path=str(tmp_path))))
    assert result['files_checked'] == 1
    assert result['engine'] == 'recoder-rules'
    assert result['score'] is None


def test_code_scan_limit_is_not_silently_successful(tmp_path, monkeypatch):
    monkeypatch.setattr(route, '_MAX_FILES', 1)
    for name in ('a.ts', 'b.ts'):
        (tmp_path / name).write_text('const x = 1')
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.code_scan(route.CodeScanRequest(workspace_path=str(tmp_path))))
    assert caught.value.status_code == 400


def test_recheck_without_sbom_is_not_a_pass(monkeypatch):
    monkeypatch.setattr(vuln_recheck, 'records_from_store', lambda: [])
    assert asyncio.run(route.vulns_check())['status'] == 'not_run'


def test_recheck_network_failure_is_visible(monkeypatch):
    monkeypatch.setattr(vuln_recheck, 'records_from_store', lambda: [{}])
    monkeypatch.setattr(vuln_recheck, 'deployments', lambda _: [{'sbom': 'test'}])
    monkeypatch.setattr(vuln_recheck, 'purls', lambda _: [{}])
    def fail(_):
        raise ConnectionError('unavailable')
    monkeypatch.setattr(vuln_recheck, 'run', fail)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(route.vulns_check())
    assert caught.value.status_code == 502


@pytest.mark.parametrize('response', [{}, {'results': []}, {'results': [None]}, {'results': [{'vulns': 'bad'}]}])
def test_incomplete_osv_response_cannot_resolve_existing_findings(response):
    with pytest.raises(ValueError):
        vuln_recheck.query([{'purl': 'pkg:npm/example@1.0.0'}], post=lambda *_: response)
