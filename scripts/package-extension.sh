#!/usr/bin/env bash
# 플랫폼별 VSIX 를 만든다. (회차4 「마켓플레이스 게시 준비」)
#
#   bash scripts/package-extension.sh                 # bin-dist/ 에 있는 모든 플랫폼
#   bash scripts/package-extension.sh darwin-arm64    # 특정 플랫폼만
#   bash scripts/package-extension.sh --no-binary     # Core 바이너리 없는 경량 VSIX
#
# ## 왜 플랫폼별로 나누나
#
# 확장은 Local Core 를 PyInstaller 바이너리로 동봉해 "설치만 하면 동작"을
# 약속한다. 그런데 그 바이너리는 **OS 별로 다른 파일**이다. 하나만 담아
# 배포하면 다른 OS 사용자는 설치는 되는데 Core 가 안 뜬다 — 확장이 조용히
# 아무것도 못 하는 상태가 된다. 실제로 지금까지 만들어진 VSIX 에는 macOS
# 바이너리 하나만 들어 있었다.
#
# VS Code 는 이 문제를 위해 platform-specific extension 을 지원한다.
# `--target <platform>` 으로 만든 VSIX 는 해당 플랫폼 사용자에게만 배포되고,
# 마켓플레이스가 알아서 맞는 것을 내려준다.
#
# ## 바이너리를 어디에 두나
#
# CoreManager 는 `extension/bin/recoder-core[.exe]` 라는 **고정 경로**를 찾는다
# (CoreManager.ts 의 탐색 순서 2번). 그 규칙을 바꾸면 확장 코드를 고쳐야 하므로,
# 대신 이 스크립트가 대상 플랫폼의 바이너리를 그 자리에 복사한 뒤 패키징한다.
# 원본은 아래 구조로 보관한다 (bin/ 과 bin-dist/ 모두 git 추적 대상이 아니다):
#
#   extension/bin-dist/darwin-arm64/recoder-core
#   extension/bin-dist/darwin-x64/recoder-core
#   extension/bin-dist/win32-x64/recoder-core.exe
#   extension/bin-dist/linux-x64/recoder-core
#
# 바이너리 빌드 방법은 extension/PACKAGING.md 참고.

set -euo pipefail

cd "$(dirname "$0")/../extension"

BIN_DIST="bin-dist"
OUT_DIR="dist"
VERSION="$(node -p "require('./package.json').version")"

mkdir -p "$OUT_DIR"

# ── 대상 결정 ───────────────────────────────────────────────────────────
NO_BINARY=0
TARGETS=()
for arg in "$@"; do
  case "$arg" in
    --no-binary) NO_BINARY=1 ;;
    -*) echo "알 수 없는 옵션: $arg" >&2; exit 1 ;;
    *) TARGETS+=("$arg") ;;
  esac
done

# ── 빌드 ────────────────────────────────────────────────────────────────
# vsce 가 부르는 vscode:prepublish 는 npm run build 를 돌리지만, 웹뷰 번들이
# 최신인지 여기서도 한 번 확인해 둔다. 오래된 out/webview/webview.js 로
# 포장되면 "빌드는 됐는데 화면만 옛날 것"이 되어 원인을 찾기 어렵다.
echo "==> 빌드"
npm run build

# ── 바이너리 없는 경량 VSIX ─────────────────────────────────────────────
if [ "$NO_BINARY" -eq 1 ]; then
  echo "==> 경량 VSIX (Core 바이너리 없음)"
  TMP=""
  restore_nobinary_bin() {
    # `set -e` 때문에 vsce가 실패해도 EXIT trap은 실행된다. 복구는 모든 파일을
    # 옮겼는지와 무관하게 시도해야 개발자의 F5용 Core가 임시 폴더에 갇히지 않는다.
    if [ -n "${TMP:-}" ] && [ -d "$TMP" ]; then
      mkdir -p bin
      find "$TMP" -mindepth 1 -maxdepth 1 -exec mv {} bin/ \;
      rmdir "$TMP" 2>/dev/null || true
      TMP=""
    fi
  }
  # 이동하기 **전** 등록한다. vsce 실패·Ctrl+C·예상 밖 오류 모두 bin을 복구한다.
  trap restore_nobinary_bin EXIT HUP INT TERM
  if [ -d bin ] && [ -n "$(ls -A bin 2>/dev/null || true)" ]; then
    TMP="$(mktemp -d)"
    mv bin/* "$TMP/"
  fi
  npx @vscode/vsce package -o "$OUT_DIR/recoder-$VERSION-nobinary.vsix"
  restore_nobinary_bin
  trap - EXIT HUP INT TERM
  echo
  ls -lh "$OUT_DIR"/recoder-"$VERSION"-nobinary.vsix
  echo
  echo "주의: 이 VSIX 는 Core 바이너리가 없다. 사용자가 core/ 를 직접 실행해야 한다."
  exit 0
fi

# ── 플랫폼별 VSIX ───────────────────────────────────────────────────────
if [ ${#TARGETS[@]} -eq 0 ]; then
  if [ ! -d "$BIN_DIST" ]; then
    echo "오류: $PWD/$BIN_DIST 가 없다." >&2
    echo "     플랫폼별 Core 바이너리를 먼저 넣어라 (PACKAGING.md 참고)." >&2
    echo "     바이너리 없이 만들려면: bash scripts/package-extension.sh --no-binary" >&2
    exit 1
  fi
  while IFS= read -r d; do
    TARGETS+=("$(basename "$d")")
  done < <(find "$BIN_DIST" -mindepth 1 -maxdepth 1 -type d | sort)
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "오류: $BIN_DIST 아래에 플랫폼 디렉터리가 하나도 없다." >&2
  exit 1
fi

# 원래 bin/ 내용을 보존했다가 끝나고 되돌린다. 스크립트가 개발자의 작업
# 상태를 바꿔 놓으면 다음 F5 실행이 엉뚱한 바이너리를 쓴다.
RESTORE=""
if [ -d bin ] && [ -n "$(ls -A bin 2>/dev/null || true)" ]; then
  RESTORE="$(mktemp -d)"
  cp -R bin/. "$RESTORE/"
fi

cleanup() {
  rm -rf bin
  mkdir -p bin
  if [ -n "$RESTORE" ]; then
    cp -R "$RESTORE"/. bin/
    rm -rf "$RESTORE"
  fi
}
trap cleanup EXIT

FAILED=0
for target in "${TARGETS[@]}"; do
  src_dir="$BIN_DIST/$target"
  if [ ! -d "$src_dir" ]; then
    echo "건너뜀: $target — $src_dir 없음" >&2
    FAILED=$((FAILED + 1))
    continue
  fi

  # 바이너리 이름은 CoreManager 가 정한다: win32 는 .exe, 나머지는 확장자 없음.
  case "$target" in
    win32-*) binary="recoder-core.exe" ;;
    *)       binary="recoder-core" ;;
  esac

  if [ ! -f "$src_dir/$binary" ]; then
    echo "건너뜀: $target — $src_dir/$binary 없음 (CoreManager 가 찾는 이름이다)" >&2
    FAILED=$((FAILED + 1))
    continue
  fi

  echo
  echo "==> $target"
  rm -rf bin
  mkdir -p bin
  cp "$src_dir/$binary" "bin/$binary"
  chmod +x "bin/$binary"

  npx @vscode/vsce package \
    --target "$target" \
    -o "$OUT_DIR/recoder-$VERSION-$target.vsix"
done

echo
echo "==> 산출물"
ls -lh "$OUT_DIR"/*.vsix

if [ "$FAILED" -gt 0 ]; then
  echo
  echo "경고: $FAILED 개 플랫폼을 건너뛰었다. 위 메시지를 확인할 것." >&2
  exit 1
fi
