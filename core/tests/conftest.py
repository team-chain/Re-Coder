"""
pytest 설정 — core 디렉터리를 import path 에 추가.

pytest 가 core/tests 에서 실행될 때 schemas, server, registries 등을
상대경로 없이 import 할 수 있게 한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

# 테스트 중에는 실제 LLM 호출을 막기 위한 환경변수
os.environ.setdefault("RECODER_TEST_MODE", "1")
# infra_agent 의 LLM 커스터마이징도 비활성화 → 템플릿 그대로 반환되도록
os.environ.setdefault("RECODER_INFRA_AI_CUSTOMIZE", "0")
