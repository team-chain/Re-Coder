"""Core 와 함께 배포되는 데이터 파일(템플릿·고정 소스·지식 문서)의 실제 위치.

실행 파일(PyInstaller onefile)은 데이터를 임시 폴더(_MEI…)의 맨 위에 푼다(registry/…, starter_templates/…).
그런데 같은 모듈이 `core.agents.ecs_agent` 처럼 core 패키지 이름으로 불리면 __file__ 이 _MEI…/core/… 라
`Path(__file__).parent.parent / "registry"` 가 없는 _MEI…/core/registry 를 가리켰다
(실기기 2.0.11 ECS 배포: 태스크 정의 단계에서 ecs-task-definition.json.template 이 없다며 실패).
소스 실행·실행 파일·두 가지 import 이름 모두에서 같은 파일을 찾는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent


def resource(*parts: str) -> Path:
    """core/ 기준 상대 경로의 실제 파일·폴더. 어디에도 없으면 소스 기준 경로(오류 메시지가 가리킬 곳)."""
    candidates = [_CORE_DIR.joinpath(*parts)]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates += [Path(bundle).joinpath(*parts), Path(bundle, "core").joinpath(*parts)]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]
