#!/usr/bin/env bash
# ReCoder Discord Bot — 로컬 실행 스크립트
#
# 사용법:
#   ./run.sh           # 봇 실행
#   ./run.sh test      # 단위 테스트만 실행
#   ./run.sh check     # 정적 검증 (pyflakes + import dry-run)
#
# 사전 조건:
#   - Python 3.10+ (3.11 권장)
#   - .env 파일에 DISCORD_BOT_TOKEN 설정
#
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ── 가상환경 자동 생성 ────────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "▶ 가상환경 생성 중..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 의존성 설치 ────────────────────────────────────────────────────────────
if [ ! -f "$VENV_DIR/.deps_installed" ] || [ requirements.txt -nt "$VENV_DIR/.deps_installed" ]; then
    echo "▶ 의존성 설치 중..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    touch "$VENV_DIR/.deps_installed"
fi

CMD="${1:-run}"

case "$CMD" in
    run)
        if [ ! -f ".env" ]; then
            echo "❌ .env 파일이 없습니다. .env.example 을 복사해서 만들어주세요." >&2
            exit 1
        fi
        if ! grep -q '^DISCORD_BOT_TOKEN=.\+' .env; then
            echo "❌ .env 에 DISCORD_BOT_TOKEN 이 비어있습니다." >&2
            exit 1
        fi
        echo "▶ ReCoder Discord Bot 시작..."
        exec python bot.py
        ;;

    test)
        pip install -q pytest pytest-asyncio
        exec python -m pytest tests/ -v
        ;;

    check)
        pip install -q pyflakes
        echo "▶ pyflakes ..."
        python -m pyflakes . | grep -v "tests/" || echo "  ✅ 정적 검증 통과"
        echo "▶ import dry-run ..."
        DISCORD_BOT_TOKEN=dummy python -c "
import sys; sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('bot', 'bot.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print('  ✅ bot.py 로딩 성공:', mod.RecoderBot.__name__)
"
        ;;

    *)
        echo "사용법: $0 [run|test|check]" >&2
        exit 1
        ;;
esac
