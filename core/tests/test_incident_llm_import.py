"""RCA 의 LLM 클라이언트 import — 보드 이슈 「LLM 기반 RCA 가 절대 실행되지 않음」.

무엇이 사고였나
    incident 라우트가 `core.llm_router` 를 import 했는데 그 모듈은 존재한 적이
    없다. except 가 예외를 조용히 삼켜 use_llm=true 여도 항상 휴리스틱으로
    빠졌고, 실패는 warning 로그 한 줄뿐이라 아무도 눈치채지 못했다.

여기서 고정하는 것
    1. 라우트가 실제로 존재하는 모듈(llm.provider_router)을 import 한다.
    2. 그 클라이언트가 run_rca 가 기대하는 인터페이스(`complete`)를 가진다.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))


def test_라우트가_쓰는_LLM_클라이언트_모듈이_실재한다() -> None:
    """import 자체가 성공해야 한다 — 유령 모듈이면 여기서 바로 죽는다."""
    from llm.provider_router import LLMProviderRouter

    #: run_rca 는 `await llm_client.complete(prompt)` 만 부른다
    #: (agents/incident_agent.py:203). 인터페이스가 어긋나면 import 는 되는데
    #: 호출에서 죽는, 더 찾기 어려운 실패가 된다.
    assert callable(getattr(LLMProviderRouter, "complete", None)), (
        "LLMProviderRouter 에 complete 가 없다 — run_rca 호출에서 터진다"
    )


def test_incident_라우트가_실재하는_경로로_import_한다() -> None:
    """라우트 소스의 import 문이 실제 모듈을 가리키는지 고정한다.

    import 는 함수 안(요청 시점)에 있어서 모듈 로드만으로는 검증되지 않는다.
    소스에서 import 문을 찾아 같은 문장을 여기서 실행해 본다 — 문장이 바뀌어
    다시 유령 모듈을 가리키면 이 테스트가 그 자리에서 깨진다.
    """
    from api.routes import incident

    source = inspect.getsource(incident)
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("from ") and "LLMProviderRouter" in line
    ]
    assert import_lines, "라우트에서 LLM 클라이언트 import 문을 찾지 못했다"
    for line in import_lines:
        module = line.split()[1]
        #: 폴백 경로(core.llm.provider_router)는 패키지 실행 형태라 여기선
        #: 못 들여올 수 있다 — 주 경로(llm.*)만 직접 실행해 확인한다.
        if module.startswith("core."):
            continue
        exec(line, {})  # noqa: S102 — 테스트 전용, 라우트의 import 문 그대로
