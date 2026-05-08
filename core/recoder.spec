# -*- mode: python ; coding: utf-8 -*-
# recoder.spec — PyInstaller 빌드 명세 (§S-1)
# 사용법: pyinstaller recoder.spec --noconfirm
#
# 출력: dist/recoder  (macOS/Linux)  또는  dist/recoder.exe  (Windows)

import sys
from pathlib import Path

block_cipher = None

# ── 경로 기준 ─────────────────────────────────────────────────────────
CORE_DIR = Path(SPECPATH)       # recoder.spec 이 위치한 디렉토리 = core/
ROOT_DIR = CORE_DIR.parent

# ── Hidden Imports ─────────────────────────────────────────────────────
# PyInstaller 정적 분석으로 감지되지 않는 동적 import 전부 명시
hidden_imports = [
    # FastAPI / uvicorn 내부
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "fastapi",
    "pydantic",
    "pydantic.v1",
    "starlette",
    "anyio",
    "anyio._backends._asyncio",
    # AWS
    "boto3",
    "botocore",
    "botocore.loaders",
    "botocore.handlers",
    "s3transfer",
    # Gemini
    "google.genai",
    "google.auth",
    "google.auth.transport",
    # 기타
    "httpx",
    "requests",
    "psutil",
    "dotenv",
    "sqlite3",
    # ReCoder 모듈 (동적 import 사용)
    "analyzer",
    "code_agent",
    "infra_agent",
    "git_agent",
    "local_deploy_agent",
    "quality_runner",
    "session_logger",
    "project_scanner",
    "first_run",
    "schemas",
    "trigger_detector",
    "risk_validator",
    "command_safety",
    "context_gate",
    "orchestrator",
    "llm.router",
    "llm.base",
    "llm.bedrock",
    "llm.gemini",
    "registries.command_registry",
    "collectors",
]

# ── 데이터 파일 ───────────────────────────────────────────────────────
datas = [
    # botocore endpoint 데이터 (필수)
    (str(CORE_DIR / ".venv" / "Lib" / "site-packages" / "botocore" / "data"), "botocore/data"),
]

# venv 경로가 없으면 system site-packages에서 탐색
import importlib.util
_botocore_spec = importlib.util.find_spec("botocore")
if _botocore_spec:
    _botocore_data = Path(_botocore_spec.origin).parent / "data"
    if _botocore_data.exists():
        datas = [( str(_botocore_data), "botocore/data" )]

# ── Analysis ──────────────────────────────────────────────────────────
a = Analysis(
    [str(CORE_DIR / "main.py")],
    pathex=[str(CORE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 테스트/개발 전용 — 바이너리에서 제외
        "pytest",
        "pytest_asyncio",
        "black",
        "mypy",
        "ruff",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── EXE ───────────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="recoder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # Local Core는 콘솔 모드 (백그라운드 서비스)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows: 아이콘 (있을 경우)
    icon=str(ROOT_DIR / "extension" / "media" / "icon.ico") if sys.platform == "win32" and (ROOT_DIR / "extension" / "media" / "icon.ico").exists() else None,
)
