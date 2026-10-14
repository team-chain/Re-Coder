"""Core 가 runtime.json 을 잃거나 확장 호스트가 사라졌을 때의 자가 복구 (1.1.15 실기기 사례)."""
import json
import os

import pytest
import singleton
from singleton import CoreSingleton


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, '_RECODER_DIR', tmp_path)
    monkeypatch.setattr(CoreSingleton, 'RECODER_HOME', tmp_path)
    monkeypatch.setattr(CoreSingleton, 'LOCK_FILE', tmp_path / 'core.lock')
    monkeypatch.setattr(CoreSingleton, 'RUNTIME_FILE', tmp_path / 'runtime.json')
    return tmp_path


def test_락을_가진_Core_는_지워진_runtime_json_을_다시_쓴다(isolated):
    import main
    pid = os.getpid()
    assert CoreSingleton.acquire_lock(pid)
    CoreSingleton.write_runtime(port=17894, token='t-guard', pid=pid)
    CoreSingleton.RUNTIME_FILE.unlink()   # 재시작 중인 확장 창이 파일만 지운 상황
    assert main._guard_tick(pid, 17894, 't-guard', 0, 0) == 0
    data = json.loads(CoreSingleton.RUNTIME_FILE.read_text(encoding='utf-8'))
    assert data['pid'] == pid and data['port'] == 17894 and data['session_token'] == 't-guard'


def test_락을_잃은_Core_는_runtime_json_을_건드리지_않는다(isolated):
    import main
    CoreSingleton.LOCK_FILE.write_text(json.dumps({'pid': 999999, 'windows': [999999]}), encoding='utf-8')
    assert main._guard_tick(os.getpid(), 17894, 't', 0, 0) == 0
    assert not CoreSingleton.RUNTIME_FILE.exists()


def test_확장_호스트가_없으면_연속_두_번_뒤_종료_신호(isolated, monkeypatch):
    import main
    monkeypatch.setattr(CoreSingleton, '_pid_alive', staticmethod(lambda p: False))
    assert main._guard_tick(os.getpid(), 1, 't', 4242, 0) == 1
    assert main._guard_tick(os.getpid(), 1, 't', 4242, 1) == 2
    monkeypatch.setattr(CoreSingleton, '_pid_alive', staticmethod(lambda p: True))
    assert main._guard_tick(os.getpid(), 1, 't', 4242, 1) == 0   # 다시 보이면 초기화
    assert main._guard_tick(os.getpid(), 1, 't', 0, 5) == 0      # 수동 실행(부모 없음)은 대상 아님


def test_부모_PID_환경변수(monkeypatch):
    import main
    monkeypatch.setenv('RECODER_PARENT_PID', '1234')
    assert main._parent_pid() == 1234
    monkeypatch.setenv('RECODER_PARENT_PID', 'x')
    assert main._parent_pid() == 0
    monkeypatch.delenv('RECODER_PARENT_PID')
    assert main._parent_pid() == 0
