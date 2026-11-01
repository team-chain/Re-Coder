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
    # 2.0.33 CPython 라이선스는 Windows 는 설치 폴더, Linux·macOS 는 표준 라이브러리 폴더에 있다(전에는 Windows 만 들어갔다).
    import ssl
    import sysconfig
    candidates = [Path(sys.base_prefix) / 'LICENSE.txt', Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt',
                  Path(sys.base_prefix) / 'share' / 'doc' / f'python{sys.version_info[0]}.{sys.version_info[1]}' / 'copyright']
    python_license = next((c for c in candidates if c.is_file()), None)
    if python_license is None:
        raise SystemExit('CPython LICENSE.txt 를 찾지 못했습니다 — 배포물에 넣어야 합니다.')
    lines += [f'CPython {platform.python_version()}', python_license.read_text(encoding='utf-8', errors='replace'), '']
    lines += [f'OpenSSL ({ssl.OPENSSL_VERSION}) — bundled with the Python runtime',
              'Licensed under the Apache License 2.0 (OpenSSL 3.x). https://www.openssl.org/source/license.html', '']
    # 확장(webpack 번들)에 들어가는 npm 패키지.
    for package in ('react', 'react-dom', 'scheduler', 'three', 'ws'):
        folder = EXT / 'node_modules' / package
        manifest = folder / 'package.json'
        if not manifest.is_file():
            raise SystemExit(f'{package} 가 node_modules 에 없습니다 — npm ci 뒤에 실행하세요.')
        info = json.loads(manifest.read_text(encoding='utf-8'))
        lines += [f"{package} {info.get('version', '')} (npm)", str(info.get('license', ''))]
        for license_file in sorted(folder.glob('LICEN[CS]E*')):
            lines += [license_file.read_text(encoding='utf-8', errors='replace')]
        lines += ['']
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
    run([sys.executable, '-m', 'PyInstaller', str(CORE / 'recoder-core.spec'), '--noconfirm', '--clean',
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
