"""Build a native VSIX from an isolated Python environment; never publish it."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'core'
EXT = ROOT / 'extension'


def run(args: list[str], *, cwd: Path = ROOT):
    return subprocess.run(args, cwd=cwd, check=True,
                          env={**os.environ, 'PYTHONUTF8': '1'})


def notices() -> None:
    lines = ['ReCoder third-party notices',
             'Python runtime and build dependencies; versions in the release manifest.', '']
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        lines += ['CPython', python_license.read_text(encoding='utf-8', errors='replace'), '']
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        if name.lower() == 'pip':
            continue
        lines += [f'{name} {dist.version}', dist.metadata.get('License-Expression') or dist.metadata.get('License', ''),
                  dist.metadata.get('Home-page', '')]
        for file in dist.files or []:
            # License text only; never include configuration or credential files.
            if '.dist-info/' in str(file).replace('\\', '/') and Path(file).name.upper().startswith(('LICENSE', 'LICENCE', 'NOTICE', 'COPYING')):
                path = Path(dist.locate_file(file))
                if path.is_file():
                    lines += [path.read_text(encoding='utf-8', errors='replace')]
        lines += ['']
    (EXT / 'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(lines), encoding='utf-8')


def main() -> None:
    if sys.prefix == sys.base_prefix:
        raise SystemExit('Use an isolated release virtual environment.')
    os_name = {'Windows': 'win32', 'Linux': 'linux', 'Darwin': 'darwin'}[platform.system()]
    arch = {'AMD64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch:
        raise SystemExit(f'Unsupported architecture: {platform.machine()}')
    target = f'{os_name}-{arch}'
    binary_name = 'recoder-core.exe' if os_name == 'win32' else 'recoder-core'
    node = shutil.which('node')
    if not node:
        raise SystemExit('Node.js is required to package the extension.')
    run([sys.executable, '-m', 'pip', 'check'])
    run([sys.executable, '-m', 'PyInstaller', str(CORE / 'recoder-core.spec'), '--noconfirm',
         '--distpath', str(CORE / 'dist'), '--workpath', str(CORE / 'build')], cwd=CORE)
    binary = CORE / 'dist' / binary_name
    with tempfile.TemporaryDirectory(prefix='recoder-release-check-') as isolated:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('AWS_', 'RECODER_', 'GEMINI_', 'BEDROCK_', 'GOOGLE_'))}
        env.update(HOME=isolated, USERPROFILE=isolated, AWS_EC2_METADATA_DISABLED='true', PYTHONUTF8='1')
        output = subprocess.run([str(binary), '--self-check'], cwd=isolated, env=env,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        if output.returncode:
            raise RuntimeError(f'Packaged Core self-check failed:\n{output.stdout}\n{output.stderr}')
        result = json.loads(output.stdout.strip().splitlines()[-1])
        if not result.get('ok') or not result.get('frozen'):
            raise RuntimeError('Expected a successful self-check from a frozen Core binary.')
    version = json.loads((EXT / 'package.json').read_text(encoding='utf-8'))['version']
    if result['version'] != version:
        raise RuntimeError('Core and extension versions do not match.')
    notices()
    archive_dir = EXT / 'bin-dist' / target
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, archive_dir / binary_name)
    run([node, str(EXT / 'scripts' / 'package-release.js'), target])
    package = EXT / 'dist' / f'recoder-{version}-{target}.vsix'
    sha = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix('.vsix.sha256').write_text(f'{sha}  {package.name}\n', encoding='ascii')
    manifest = {
        'version': version, 'target': target, 'python': platform.python_version(),
        'coreSha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'vsixSha256': sha, 'vsixBytes': package.stat().st_size, 'selfCheck': result,
        'dependencies': {d.metadata['Name']: d.version for d in metadata.distributions()},
    }
    package.with_suffix('.release.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Release ready: {package}\nSHA256: {sha}', flush=True)


if __name__ == '__main__':
    main()
