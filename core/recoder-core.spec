# -*- mode: python ; coding: utf-8 -*-
# ReCoder Local Core → recoder-core(.exe) PyInstaller 빌드 스펙
#
# 빌드 (core/ 에서, venv 활성 + pip install pyinstaller):
#   pyinstaller recoder-core.spec --noconfirm
# 산출물(onefile): dist/recoder-core.exe   (Windows)  /  dist/recoder-core (mac/linux)
#
# FastAPI + uvicorn + boto3 는 동적 import/데이터 파일이 많아, 첫 실행에서
# "ModuleNotFoundError" 가 나면 그 모듈명을 아래 hidden 리스트에 추가하면 됩니다.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

hidden = []
datas = []
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
for pkg in ['botocore', 'boto3', 'pydantic', 'pydantic_core']:
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hidden += h
    except Exception:
        pass

# ── FileTemplate 데이터(Dockerfile.* 등) ───────────────────────────────
datas += [('registry/file_templates', 'registry/file_templates')]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pytest', 'tests'],
    noarchive=False,
)
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
