"""골든패스 스모크의 파일 적용 안전장치 회귀 테스트."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "golden_path_smoke.py"
SPEC = importlib.util.spec_from_file_location("golden_path_smoke", SCRIPT_PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_형제_폴더로_탈출하는_생성_파일은_기록하지_않는다(tmp_path: Path):
    """문자열 접두사가 같은 형제 폴더는 워크스페이스 내부가 아니다."""
    workspace = tmp_path / "recoder-smoke-ws-abc"
    workspace.mkdir()
    escaped = tmp_path / "recoder-smoke-ws-abc-escaped" / "leak.txt"

    with pytest.raises(smoke.SmokeFailure, match="워크스페이스 밖"):
        smoke.step5_apply(
            workspace,
            {"ops": [{"file": "../recoder-smoke-ws-abc-escaped/leak.txt", "content": "no"}]},
        )

    assert not escaped.exists(), "라이브 스모크가 임시 워크스페이스 밖에 파일을 썼다"
