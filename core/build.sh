#!/usr/bin/env bash
# Compatibility entry point. Both build paths now produce recoder-core.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .venv/bin/activate ]]; then
  source .venv/bin/activate
elif [[ -f .venv/Scripts/activate ]]; then
  source .venv/Scripts/activate
fi
python -m PyInstaller recoder-core.spec --noconfirm "$@"
