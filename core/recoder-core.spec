# -*- mode: python ; coding: utf-8 -*-
# ReCoder Local Core → recoder-core(.exe) PyInstaller 빌드 스펙
#
# 빌드 (core/ 에서, venv 활성 + pip install pyinstaller):
#   pyinstaller recoder-core.spec --noconfirm
# 산출물(onefile): dist/recoder-core.exe   (Windows)  /  dist/recoder-core (mac/linux)
#
# FastAPI + uvicorn + boto3 는 동적 import/데이터 파일이 많아, 첫 실행에서
# "ModuleNotFoundError" 가 나면 그 모듈명을 아래 hidden 리스트에 추가하면 됩니다.

from pathlib import Path
import runpy
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

CORE_DIR = Path(SPECPATH)

hidden = []
datas = []
datas += [(str(CORE_DIR / 'grounded_repair' / 'sources.json'), 'grounded_repair')]
binaries = []

# ── uvicorn 런타임(동적 로딩) ─────────────────────────────────────────
hidden += collect_submodules('uvicorn')
hidden += [
    'anyio._backends._asyncio',
    'uvicorn.lifespan.on', 'uvicorn.lifespan.off',
    'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.wsproto_impl',
]

# ── 앱 내부 패키지(try/except 동적 import 대비 전체 수집) ───────────────
for pkg in [
    'agents', 'api', 'preflight', 'remediation', 'persistence', 'incident_memory',
    'cv', 'llm', 'registries', 'registry', 'relay', 'observability', 'chunker',
    'forecast', 'standup', 'replay', 'visual_diff', 'collectors',
]:
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

# ── 데이터·바이너리·hidden 가 많은 패키지는 collect_all ─────────────────
for pkg in ['botocore', 'boto3']:
    datas += collect_data_files(pkg)

# APScheduler resolves plugins through package metadata, not static imports.
hidden += collect_submodules('apscheduler')
datas += copy_metadata('APScheduler')
hidden += list(runpy.run_path(str(CORE_DIR / 'release_check.py'))['RUNTIME_IMPORTS'])
hidden += ['release_check']

# ── FileTemplate 데이터(Dockerfile.* 등) ───────────────────────────────
datas += [(str(file), 'registry/file_templates')
          for file in (CORE_DIR / 'registry' / 'file_templates').iterdir()
          if file.is_file() and file.suffix not in ('.py', '.pyc')]
datas += [(str(CORE_DIR / 'registry' / 'command_templates.json'), 'registry')]

a = Analysis(
    [str(CORE_DIR / 'main.py')],
    pathex=[str(CORE_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pytest', 'pytest_asyncio', 'tests', 'moto', 'eval', 'tkinter'],
    noarchive=False,
)
# ── 시작 속도: 한 파일 실행 파일은 시작할 때마다 모든 데이터 파일을 임시 폴더에 푼다 ──
# botocore 서비스 정의 1,900개·googleapiclient 발견 문서 580개를 매번 풀고 Windows 백신이
# 검사해 Core 시작이 12~50초 걸렸다(실기기 로그). 쓰는 AWS 서비스와 botocore 공용 파일만 남긴다.
_release = runpy.run_path(str(CORE_DIR / 'release_check.py'))
_services = set(_release['BUNDLED_AWS_SERVICES'])


def _keep_data(dest):
    parts = dest.replace('\\', '/').split('/')
    if parts[:2] == ['botocore', 'data'] and len(parts) > 3:
        return parts[2] in _services
    if parts[:3] == ['googleapiclient', 'discovery_cache', 'documents']:
        return parts[-1].startswith('generativelanguage')
    return True


a.datas = [entry for entry in a.datas if _keep_data(entry[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='recoder-core',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,            # 로그를 stdout 으로 (확장이 읽음). GUI 숨김 원하면 False
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
