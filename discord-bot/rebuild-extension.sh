#!/usr/bin/env bash
# ReCoder VSCode 확장 재빌드 + 가이드
#
# 봇 측 (make_handler.py / recoder_bridge.py) 만 수정하고 확장의 BridgeClient.ts
# 를 재컴파일하지 않으면, VSCode 는 옛 코드를 그대로 사용해 결과 파일이 race
# condition 으로 깨진다. 이 스크립트는 그걸 한 번에 해결한다.
#
# 사용법:
#   ./rebuild-extension.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
EXT_DIR="$(cd "$HERE/../extension" && pwd)"

echo "▶ 확장 디렉터리: $EXT_DIR"

cd "$EXT_DIR"

# 의존성 설치 (한 번만)
if [ ! -d node_modules ]; then
    echo "▶ npm install ..."
    npm install
fi

# TypeScript → out/ 컴파일
echo "▶ TypeScript 컴파일 ..."
npm run compile

# 결과 확인
if [ -f out/bridge/BridgeClient.js ]; then
    echo "✅ 확장 재빌드 완료: out/bridge/BridgeClient.js"
else
    echo "❌ 빌드 실패 — out/bridge/BridgeClient.js 가 생성되지 않음" >&2
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "다음 단계 (VSCode 에서 직접 수행):"
echo ""
echo "  1. VSCode 명령 팔레트 (Cmd+Shift+P)"
echo "  2. \"Developer: Reload Window\" 검색해 실행"
echo "  3. ReCoder Bridge 상태바에 \$(check) 표시 확인"
echo ""
echo "그 다음 Discord 에서:"
echo "  • \"테트리스 만들고 실행해줘\"  → 파일 생성 후 브라우저로 자동 열림"
echo "  • \"파이썬 계산기 만들어 실행\"  → 통합 터미널에서 python3 실행"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
