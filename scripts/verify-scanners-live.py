"""Opt-in real Docker scanners, bundled Rego and public OSV checks. Synthetic inputs only."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', action='store_true', required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = root / '.test-artifacts' / ('live-scanners-' + time.strftime('%Y%m%d-%H%M%S'))
    work.mkdir(parents=True, exist_ok=False)
    os.environ['RECODER_HOME'] = str(work / 'state')
    os.environ['XDG_CACHE_HOME'] = str(root / '.test-artifacts/live-scanner-cache')
    sys.path.insert(0, str(root / 'core'))
    from security_scan import SecurityScanner
    import vuln_recheck
    scanner = SecurityScanner()
    fixture = work / 'fixture'
    fixture.mkdir()
    (fixture / 'Dockerfile').write_text('FROM alpine:latest\nRUN echo hello\n', encoding='utf-8')
    # Deliberately synthetic token; never a credential and never sent to an AI.
    (fixture / 'sample.js').write_text("const npm_token = 'npm_" + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8' + "';\n", encoding='utf-8')
    evidence = {}
    failures = []
    async def check(name, call, positive=False):
        print('RUN: ' + name, flush=True)
        try:
            findings = await call
            errors = [f.title for f in findings if any(v in f.title for v in ('_failed', '_not_installed', '_timeout'))]
            if errors:
                raise AssertionError(', '.join(errors))
            if positive and not findings:
                raise AssertionError('Synthetic issue was not found')
            evidence[name] = {'status': 'passed', 'findingCount': len(findings),
                              'titles': [f.title for f in findings[:8]]}
            print('PASS: ' + name + ' findings=' + str(len(findings)), flush=True)
        except Exception as exc:
            evidence[name] = {'status': 'failed', 'error': str(exc)}
            failures.append(name)
            print('FAIL: ' + name + ' ' + str(exc), flush=True)
    await check('hadolint', scanner._run_hadolint(str(fixture / 'Dockerfile')), True)
    await check('gitleaks', scanner._run_gitleaks(str(fixture)), True)
    await check('trivy', scanner._run_trivy('docker:29.3.1-dind'))
    opa = 'openpolicyagent/opa@sha256:906bfc9904b2693733e7959d71773dd073e6c135cf06f26d955abe94603e82b3'
    print('RUN: OPA repository policies', flush=True)
    try:
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--mount',
            'type=bind,src=' + str(root / 'policies') + ',dst=/policies,readonly', opa,
            'test', '/policies', '-v'], capture_output=True, text=True, encoding='utf-8', timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW)
        (work / 'opa.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise AssertionError('OPA exited ' + str(result.returncode))
        evidence['opa'] = {'status': 'passed', 'output': result.stdout.strip()}
        print('PASS: OPA repository policies', flush=True)
    except Exception as exc:
        evidence['opa'] = {'status': 'failed', 'error': str(exc)}
        failures.append('opa')
    print('RUN: real OSV SBOM recheck / persisted history / repeated-check deduplication', flush=True)
    try:
        sbom = work / 'sbom.json'
        sbom.write_text(json.dumps({'bomFormat': 'CycloneDX', 'specVersion': '1.4', 'components': [
            {'type': 'library', 'name': 'lodash', 'version': '4.17.20', 'purl': 'pkg:npm/lodash@4.17.20'}]}), encoding='utf-8')
        records = [{'status': 'succeeded', 'sbom_path': str(sbom), 'region': 'local',
                    'cluster': 'verification', 'service': 'synthetic', 'deployment_id': 'synthetic-only',
                    'completed_at': '2026-11-04T00:00:00Z'}]
        events = []
        def capture(alert):
            events.append(alert)
            return alert
        raised = vuln_recheck.run(records, store=capture)
        history = vuln_recheck.history()
        known = history['local/verification/synthetic']['known']
        assert raised and known and any(v['fixed'] for v in known.values())
        assert not vuln_recheck.run(records, store=capture)
        assert len(events) == 1
        evidence['osv'] = {'status': 'passed', 'vulnerabilities': len(known),
                           'fixedVersions': sorted({v['fixed'] for v in known.values() if v['fixed']}),
                           'historySaved': True, 'duplicateAlerts': False, 'externalMessagesSent': False}
        print('PASS: real OSV query, fixed versions and history', flush=True)
    except Exception as exc:
        evidence['osv'] = {'status': 'failed', 'error': str(exc)}
        failures.append('osv')
    (work / 'verification.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding='utf-8')
    print('Evidence: ' + str(work), flush=True)
    if failures:
        raise RuntimeError('Live checks failed: ' + ', '.join(failures))


if __name__ == '__main__':
    asyncio.run(main())
