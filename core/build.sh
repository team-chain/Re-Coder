#!/usr/bin/env bash
# build.sh — ReCoder Local Core PyInstaller 빌드 스크립트 (§S-1)
#
# 사용법:
#   cd core/
#   bash build.sh            # 기본 빌드 (dist/recoder)
#   bash build.sh --clean    # dist/ build/ 삭제 후 빌드
#
# 전제조건:
#   - Python 3.11+
#   - .venv/ 가상환경 또는 전역 환경에 requirements.txt 설치 완료
#   - pyinstaller 설치: pip install pyinstaller

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 색상 출력 ──────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[build]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()  { echo -e "${RED}[error]${NC} $*" >&2; }

# ── 옵션 파싱 ─────────────────────────────────────────────────────────
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --clean) CLEAN=1 ;;
    *) warn "알 수 없는 인수: $arg" ;;
  esac
done

# ── Clean ─────────────────────────────────────────────────────────────
if [[ $CLEAN -eq 1 ]]; then
  log "dist/ build/ 삭제 중..."
  rm -rf dist/ build/
fi

# ── 가상환경 활성화 ───────────────────────────────────────────────────
if [[ -f ".venv/bin/activate" ]]; then
  log ".venv 가상환경 활성화..."
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -f ".venv/Scripts/activate" ]]; then
  log ".venv 가상환경 활성화 (Windows)..."
  source .venv/Scripts/activate
else
  warn ".venv 없음 — 전역 Python 사용"
fi

# ── Python / PyInstaller 버전 확인 ───────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
PY_VER=$("$PYTHON" --version 2>&1)
log "Python: $PY_VER"

if ! command -v pyinstaller &>/dev/null; then
  err "pyinstaller 를 찾을 수 없습니다."
  err "pip install pyinstaller 를 실행하세요."
  exit 1
fi

PI_VER=$(pyinstaller --version 2>&1)
log "PyInstaller: $PI_VER"

# ── requirements 설치 확인 ────────────────────────────────────────────
log "의존성 확인 중..."
"$PYTHON" -c "import fastapi, uvicorn, boto3, pydantic" 2>/dev/null || {
  warn "일부 패키지 누락 — requirements.txt 설치 중..."
  pip install -r requirements.txt --quiet
}

# ── 빌드 실행 ─────────────────────────────────────────────────────────
log "PyInstaller 빌드 시작..."
pyinstaller recoder.spec \
  --noconfirm \
  --log-level WARN

# ── 결과 확인 ─────────────────────────────────────────────────────────
if [[ -f "dist/recoder" ]] || [[ -f "dist/recoder.exe" ]]; then
  log "빌드 완료 ✓"
  ls -lh dist/recoder* 2>/dev/null || ls -lh dist/
else
  err "빌드 결과물을 찾을 수 없습니다."
  exit 1
fi

log "dist/recoder 를 extension/bin/ 에 복사하려면:"
log "  cp dist/recoder ../extension/bin/recoder"
