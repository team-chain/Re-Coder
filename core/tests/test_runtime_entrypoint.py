"""
회귀: runtime.json 이 자기 entrypoint 를 적는가

배경
    확장은 개발 모드(ReCoder 저장소를 연 경우)에서 "지금 떠 있는 Core 를
    재사용해도 되는가" 를 판단해야 한다. 예전 VSIX 가 남긴 **번들** Core 면
    재사용하면 안 되고(옛날 API 로 조용히 동작한다), 개발자가 직접 띄운
    워크스페이스 Core 면 재사용해야 한다.

    그 구분 근거가 없어서 확장은 "개발 모드면 재사용 시도 자체를 안 한다" 로
    처리했고, 결과적으로 코어를 직접 띄워 둔 환경에서 연결이 끊기면 복구되지
    않았다(칸반 「코어 연결이 끊긴 뒤 복구되지 않음」).

    이제 Core 가 runtime.json 에 자기 실행 파일 절대경로를 적는다.

DoD 근거: 칸반 「코어 연결이 끊긴 뒤 복구되지 않음」(P1)
"""
import json

import pytest

from schemas import RuntimeConfig
from singleton import CoreSingleton


@pytest.fixture()
def runtime_file(tmp_path, monkeypatch):
    target = tmp_path / "runtime.json"
    monkeypatch.setattr(CoreSingleton, "RUNTIME_FILE", target)
    monkeypatch.setattr("singleton._RECODER_DIR", tmp_path)
    return target


def test_runtime_json_에_entrypoint_가_기록된다(runtime_file):
    CoreSingleton.write_runtime(port=17894, token="t" * 32, pid=1234)

    data = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert "entrypoint" in data, "확장이 재사용 여부를 판단할 근거가 없다"
    assert data["entrypoint"], "entrypoint 가 비었다"


def test_entrypoint_는_절대경로다(runtime_file):
    """확장이 워크스페이스의 main.py 경로와 **문자열로** 비교한다."""
    import os

    CoreSingleton.write_runtime(port=17894, token="t" * 32, pid=1234)
    entrypoint = json.loads(runtime_file.read_text(encoding="utf-8"))["entrypoint"]
    assert os.path.isabs(entrypoint), f"상대경로다: {entrypoint}"


def test_구버전_runtime_json_도_읽힌다():
    """
    entrypoint 필드가 없던 시절의 파일이 남아 있어도 파싱이 죽으면 안 된다.
    죽으면 확장이 코어를 아예 못 찾아 지금 고치는 버그가 그대로 재현된다.
    """
    legacy = {"port": 17894, "session_token": "t" * 32, "pid": 1234}
    config = RuntimeConfig(**legacy)
    assert config.port == 17894
    assert config.entrypoint is None


def test_음성대조_entrypoint_는_고정값이_아니다(runtime_file, monkeypatch):
    """
    항상 같은 문자열을 적는다면 확장의 비교는 무의미하다.
    실행 주체가 달라지면 값도 달라져야 한다.
    """
    import sys

    monkeypatch.setattr(sys, "argv", ["/proj/Re-Coder/core/main.py"])
    CoreSingleton.write_runtime(port=17894, token="t" * 32, pid=1)
    first = json.loads(runtime_file.read_text(encoding="utf-8"))["entrypoint"]

    monkeypatch.setattr(sys, "argv", ["/home/u/.recoder/bin/recoder-core"])
    CoreSingleton.write_runtime(port=17894, token="t" * 32, pid=1)
    second = json.loads(runtime_file.read_text(encoding="utf-8"))["entrypoint"]

    assert first != second, "실행 주체가 달라도 같은 값을 적는다"
    assert first.endswith("main.py")
    assert second.endswith("recoder-core")
