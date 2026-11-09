"""Install a VSIX in an isolated real VS Code profile and exercise its commands.

The test driver is a separate development extension: ReCoder must remain an
installed production extension so CoreManager selects its shipped executable.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = root / '.test-artifacts' / 'vscode-3.0.7'
    work.mkdir(exist_ok=True)
    code = Path(os.environ['LOCALAPPDATA']) / 'Programs/Microsoft VS Code/Code.exe'
    cli = next(code.parent.glob('*/resources/app/out/cli.js'), None)
    direct = code.parent / 'resources/app/out/cli.js'
    if direct.is_file():
        cli = direct
    if not cli:
        raise RuntimeError('Installed VS Code CLI not found')
    vsix = root / 'extension/dist' / f'recoder-{args.version}-win32-x64.vsix'
    assert vsix.is_file(), vsix
    settings = work / 'user-data/User/settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    existing['editor.fontSize'] = 15
    settings.write_text(json.dumps(existing, indent=2), encoding='utf-8')
    profile_args = ['--user-data-dir', str(work / 'user-data'), '--extensions-dir', str(work / 'extensions')]
    env = dict(os.environ)
    env['ELECTRON_RUN_AS_NODE'] = '1'
    result = subprocess.run([str(code), str(cli), *profile_args, '--install-extension', str(vsix), '--force'],
                            env=env, capture_output=True, timeout=120)
    (work / f'install-{args.version}.log').write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('VSIX installation failed; see profile install log')
    driver = work / 'test-driver'
    driver.mkdir(exist_ok=True)
    (driver / 'package.json').write_text(json.dumps({
        'name': 'recoder-installed-verification', 'publisher': 'recoder-test',
        'version': '0.0.1', 'engines': {'vscode': '^1.100.0'},
        'main': './extension.js', 'activationEvents': ['onStartupFinished'],
    }), encoding='utf-8')
    (driver / 'extension.js').write_text('exports.activate = () => {};', encoding='utf-8')
    fixture = work / 'fixture'
    fixture.mkdir(exist_ok=True)
    (fixture / 'app.js').write_text('console.log("isolated verification fixture");\n', encoding='utf-8')
    (work / 'home').mkdir(exist_ok=True)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    env.update(USERPROFILE=str(work / 'home'), HOME=str(work / 'home'),
               RECODER_VSCODE_EVIDENCE=str(work / f'verification-{args.version}.json'),
               RECODER_EXPECTED_VERSION=args.version)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    result = subprocess.run([str(code), *profile_args,
        '--extensionDevelopmentPath', str(driver),
        '--extensionTestsPath', str(root / 'extension/harness/vscode-installed-smoke.js'),
        '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust',
        '--new-window', str(fixture)], env=env, capture_output=True, timeout=240, startupinfo=startup)
    (work / f'host-{args.version}.log').write_bytes(result.stdout + result.stderr)
    evidence = json.loads((work / f'verification-{args.version}.json').read_text(encoding='utf-8'))
    print(json.dumps(evidence, ensure_ascii=True))
    if result.returncode or evidence.get('error'):
        raise RuntimeError('Installed extension verification failed')


if __name__ == '__main__':
    main()
