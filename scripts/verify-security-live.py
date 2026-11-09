"""Run packaged Core against existing AI credentials with a synthetic source fixture.

Explicit --live-ai enables bounded paid model calls. No CloudFormation/IAM/resource
creation, user source upload, saved credential modification, or deployment occurs.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-ai', action='store_true', required=True)
    parser.add_argument('--source-core', action='store_true', help='Validate a fix before rebuilding the native artifact')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = root / '.test-artifacts' / ('live-security-' + time.strftime('%Y%m%d-%H%M%S'))
    work.mkdir(parents=True, exist_ok=False)
    home = work / 'isolated-home'
    home.mkdir()
    load_dotenv(root / 'core' / '.env')  # same configured provider; never print or copy secrets to disk
    env = dict(os.environ)
    # Keep the same SDK credential chain while isolating only application state.
    # Changing HOME must not accidentally switch off the user's existing profile.
    for key, filename in [('AWS_SHARED_CREDENTIALS_FILE', 'credentials'), ('AWS_CONFIG_FILE', 'config')]:
        original = Path.home() / '.aws' / filename
        if key not in env and original.is_file():
            env[key] = str(original)
    env.update(USERPROFILE=str(home), HOME=str(home), AWS_EC2_METADATA_DISABLED='true')
    binary = root / 'extension' / 'bin-dist' / 'win32-x64' / 'recoder-core.exe'
    runtime = home / '.recoder' / 'runtime.json'
    metadata = None
    log = (work / 'core.log').open('w', encoding='utf-8')
    command = [sys.executable, str(root / 'core/main.py')] if args.source_core else [str(binary)]
    process = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(route, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{metadata['port']}{route}",
            data=None if data is None else json.dumps(data).encode(),
            headers={'X-Session-Token': metadata['session_token'], 'Content-Type': 'application/json'})
        with opener.open(req, timeout=30) as response:
            return json.load(response)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Packaged Core exited during startup; inspect the task-local core.log')
            if runtime.exists():
                try:
                    metadata = json.loads(runtime.read_text(encoding='utf-8'))
                    health = api('/api/health')
                    if health.get('status') == 'ok':
                        break
                except (OSError, ValueError):
                    pass
            time.sleep(1)
        else:
            raise RuntimeError('Packaged Core startup timeout')
        before = api('/api/aws/managed/session')
        print('Packaged Core ready; existing AWS connected=' + str(before.get('connected')), flush=True)
        fixture = work / 'fixture'
        child_env = dict(env)
        child_env.update(RECODER_LIVE_AUDIT='1', RECODER_LIVE_FIXTURE=str(fixture),
                         RECODER_LIVE_PORT=str(metadata['port']), RECODER_LIVE_SESSION=metadata['session_token'])
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node is required for the verification harness')
        result = subprocess.run([node, str(root / 'extension/harness/source-audit-live.js')],
                                cwd=root / 'extension', env=child_env, timeout=18*60,
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        (work / 'audit.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, flush=True)
        after = api('/api/aws/managed/session')
        if before != after:
            raise RuntimeError('Existing AWS connection changed during audit')
        evidence = {'coreVersion': health.get('version'), 'nativeCore': not args.source_core, 'existingAwsUnchanged': True,
                    'auditExitCode': result.returncode, 'syntheticSourceOnly': True,
                    'cloudResourcesCreated': False}
        (work / 'verification.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence), flush=True)
        if result.returncode:
            raise RuntimeError('Live source audit did not pass; inspect audit-result.json')
    finally:
        if metadata:
            try:
                api('/api/shutdown', {})
            except Exception:
                pass
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        log.close()


if __name__ == '__main__':
    main()
