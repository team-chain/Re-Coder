"""make_handler._infer_should_auto_run — '실행' 의도 감지 단위 테스트."""
from __future__ import annotations

import pytest

from make_handler import _infer_should_auto_run


@pytest.mark.parametrize("text", [
    "테트리스 만들고 실행해줘",
    "snake 게임 만들어서 돌려",
    "tetris.html 만들어 열어줘",
    "calculator 만들어줘 켜줘",
    "make a snake game and run it",
    "build tetris and open it",
    "스네이크 만들어서 보여줘",
    "todo 앱 만들고 launch",
])
def test_run_keyword_detected(text):
    assert _infer_should_auto_run(text) is True


@pytest.mark.parametrize("text", [
    "테트리스 만들어줘",
    "snake 게임 코드 작성해줘",
    "todo 앱 만들어",
    "make a tetris game",
    "calculator html code please",
    "",
])
def test_no_run_keyword(text):
    assert _infer_should_auto_run(text) is False


def test_case_insensitive():
    assert _infer_should_auto_run("Build tetris and RUN it") is True
    assert _infer_should_auto_run("OPEN the file") is True
