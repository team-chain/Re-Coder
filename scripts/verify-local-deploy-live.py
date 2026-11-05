"""Deploy an owned disposable HTTP fixture through the packaged Core SSE API."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--docker', action='store_true', required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = root / '.test-artifacts' / ('live-deploy-' + time.strftime('%Y%m%d-%H%M%S'))
    work.mkdir(parents=True, exist_ok=False)
    home, fixture = work / 'home', work / 'fixture'
    home.mkdir()
    fixture.mkdir()
    name = 'recoder-live-check-' + uuid.uuid4().hex[:12]
    image = name + ':test'
    marker = 'owned-recorder-http-fixture-' + uuid.uuid4().hex
    (fixture / 'index.html').write_text(marker, encoding='utf-8')
    (fixture / 'health').write_text('ok', encoding='utf-8')
    (fixture / 'Dockerfile').write_text(
        'FROM busybox:1.37.0\nWORKDIR /www\nCOPY index.html health ./\n'
        'USER 65534:65534\nEXPOSE 8080\nCMD ["httpd", "-f", "-p", "8080", "-h", "/www"]\n', encoding='utf-8')
    (fixture / '.dockerignore').write_text('.recoder\n', encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AWS_', 'BEDROCK_', 'RECODER_', 'GEMINI_', 'OPENAI_', 'ANTHROPIC_'))}
    env.update(USERPROFILE=str(home), HOME=str(home), AWS_EC2_METADATA_DISABLED='true',
               XDG_CACHE_HOME=str(root / '.test-artifacts/live-scanner-cache'))
    # Reuse the installed Docker context while isolating ReCoder's own state.
    env['DOCKER_CONFIG'] = os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))
    def docker(*args, check=True):
        return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8',
                              timeout=90, check=check, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    before = set(docker('ps', '-aq').splitlines())
    assert not docker('ps', '-aq', '--filter', 'name=^/' + name + '$')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = (work / 'core.log').open('w', encoding='utf-8')
    process = subprocess.Popen([str(root / 'extension/bin-dist/win32-x64/recoder-core.exe')],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    metadata = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(route, data=None, stream=False):
        req = urllib.request.Request(f"http://127.0.0.1:{metadata['port']}{route}",
            data=None if data is None else json.dumps(data).encode(),
            headers={'X-Session-Token': metadata['session_token'], 'Content-Type': 'application/json'})
        response = opener.open(req, timeout=600)
        if stream:
            return response
        with response:
            return json.load(response)
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError('Core exited during startup')
            try:
                metadata = json.loads((home / '.recoder/runtime.json').read_text(encoding='utf-8'))
                health = request('/api/health')
                break
            except (OSError, ValueError):
                time.sleep(1)
        else:
            raise RuntimeError('Core startup timeout')
        print('RUN: real security gate and local deployment plan', flush=True)
        plan = request('/api/deploy/plan', {
            'workspace_path': str(fixture), 'method': 'local_docker',
            'image': image, 'container_name': name, 'host_port': port, 'container_port': 8080,
            'health_check_path': '/health', 'enable_continuous_verification': False,
        })
        (work / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        # Core can resolve a host-port collision while preparing the approval plan.
        approved_port = int(next(iter(plan['ports'])))
        events, times = [], []
        started = time.monotonic()
        print('RUN: approved fixture build / scan / deploy SSE', flush=True)
        with request('/api/deploy/execute/stream', {'plan_id': plan['plan_id'], 'approved': True}, stream=True) as response:
            assert 'text/event-stream' in response.headers.get('content-type', '')
            for line in response:
                if line.startswith(b'data: '):
                    event = json.loads(line[6:])
                    events.append(event)
                    times.append(round(time.monotonic() - started, 3))
                    print('EVENT: ' + str(event.get('step')), flush=True)
                    if event.get('step') in ('scan', 'start'):
                        inspected = docker('image', 'inspect', image, check=False)
                        (work / ('image-at-' + event['step'] + '.json')).write_text(inspected, encoding='utf-8')
        (work / 'events.json').write_text(json.dumps({'events': events, 'elapsedSeconds': times}, indent=2), encoding='utf-8')
        final = events[-1].get('result', {})
        assert final.get('status') == 'success', final
        steps = [event.get('step') for event in events]
        assert 'build' in steps and 'scan' in steps and steps[-1] == 'done', steps
        assert times[steps.index('build')] < times[-1], 'progress was buffered until completion'
        assert final.get('security_scan', {}).get('status') == 'passed', final
        with opener.open(f'http://127.0.0.1:{approved_port}/', timeout=10) as response:
            assert response.read().decode() == marker
        evidence = {'coreVersion': health.get('version'), 'status': 'passed', 'syntheticOnly': True,
                    'securityGateSkipped': False, 'postBuildScan': 'passed', 'steps': steps,
                    'progressDeliveredBeforeCompletion': True, 'httpFixtureVerified': True,
                    'requestedPort': port, 'approvedPort': approved_port}
        (work / 'verification.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence), flush=True)
    finally:
        # Exact random names created by this harness; never prune shared resources.
        if docker('ps', '-aq', '--filter', 'name=^/' + name + '$'):
            docker('rm', '-f', name)
        for tag in docker('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}', '--filter', 'reference=' + name + ':*').splitlines():
            assert tag.startswith(name + ':'), 'refusing unrelated image cleanup'
            docker('image', 'rm', tag, check=False)
        assert before.issubset(set(docker('ps', '-aq').splitlines())), 'pre-existing container disappeared'
        if metadata:
            try:
                request('/api/shutdown', {})
            except Exception:
                pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        log.close()


if __name__ == '__main__':
    main()
