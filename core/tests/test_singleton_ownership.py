"""Regression: competing installed/development Cores must not share ownership."""
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import singleton
from singleton import CoreSingleton


def configure(directory):
    directory = Path(directory)
    singleton._RECODER_DIR = directory
    CoreSingleton.RECODER_HOME = directory
    CoreSingleton.LOCK_FILE = directory / 'core.lock'
    CoreSingleton.RUNTIME_FILE = directory / 'runtime.json'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, '_RECODER_DIR', tmp_path)
    monkeypatch.setattr(CoreSingleton, 'RECODER_HOME', tmp_path)
    monkeypatch.setattr(CoreSingleton, 'LOCK_FILE', tmp_path / 'core.lock')
    monkeypatch.setattr(CoreSingleton, 'RUNTIME_FILE', tmp_path / 'runtime.json')
    return tmp_path


def publish(pid):
    CoreSingleton.write_runtime(port=17894, token='test-token-only', pid=pid)


def test_losing_process_exit_preserves_owner_and_token(isolated):
    owner = os.getpid()
    assert CoreSingleton.acquire_lock(owner)
    publish(owner)
    before = CoreSingleton.RUNTIME_FILE.read_bytes()
    assert not CoreSingleton.acquire_lock(owner + 100000)
    CoreSingleton.release_lock(owner + 100000)
    assert CoreSingleton.RUNTIME_FILE.read_bytes() == before
    lock = json.loads(CoreSingleton.LOCK_FILE.read_text())
    assert lock['pid'] == owner
    assert lock['windows'] == [owner]


def test_old_finally_and_atexit_do_not_delete_replacement(isolated):
    previous, replacement = os.getpid(), os.getpid() + 100000
    CoreSingleton.LOCK_FILE.write_text(json.dumps({'pid': replacement, 'windows': [replacement]}))
    publish(replacement)
    CoreSingleton.release_lock(previous)
    CoreSingleton.release_lock(previous)
    assert json.loads(CoreSingleton.LOCK_FILE.read_text())['pid'] == replacement
    assert CoreSingleton.read_runtime().pid == replacement


def test_owner_release_and_repeated_cleanup(isolated):
    owner = os.getpid()
    assert CoreSingleton.acquire_lock(owner)
    publish(owner)
    CoreSingleton.release_lock(owner)
    CoreSingleton.release_lock(owner)
    assert not CoreSingleton.LOCK_FILE.exists()
    assert not CoreSingleton.RUNTIME_FILE.exists()


def test_missing_lock_does_not_replace_live_runtime(isolated):
    publish(os.getpid())
    before = CoreSingleton.RUNTIME_FILE.read_bytes()
    assert not CoreSingleton.acquire_lock(os.getpid() + 100000)
    assert CoreSingleton.RUNTIME_FILE.read_bytes() == before
    assert not CoreSingleton.LOCK_FILE.exists()


@pytest.mark.parametrize('has_runtime', [True, False])
def test_duplicate_asgi_startup_never_serves(monkeypatch, isolated, has_runtime):
    import main
    monkeypatch.setattr(CoreSingleton, 'acquire_lock', lambda pid: False)
    existing = SimpleNamespace(port=17894, session_token='existing-token') if has_runtime else None
    monkeypatch.setattr(CoreSingleton, 'read_runtime', lambda: existing)
    monkeypatch.setattr(CoreSingleton, 'find_available_port', lambda: 17895)
    writes = []
    monkeypatch.setattr(CoreSingleton, 'write_runtime', lambda **kwargs: writes.append(kwargs))
    async def attempt():
        with pytest.raises(RuntimeError, match='already running'):
            async with main.lifespan(main.create_app()):
                pytest.fail('a second ASGI server reached serving state')
    asyncio.run(attempt())
    assert writes == []


def contender(directory, barrier, release, result):
    configure(directory)
    pid = os.getpid()
    barrier.wait(timeout=15)
    acquired = CoreSingleton.acquire_lock(pid)
    if acquired:
        publish(pid)
    result.put((pid, acquired))
    try:
        if acquired:
            release.wait(timeout=15)
    finally:
        # Both a winner and a startup loser run finally/atexit in main.py.
        CoreSingleton.release_lock(pid)


def test_simultaneous_processes_have_one_owner(isolated):
    context = multiprocessing.get_context('spawn')
    barrier, release, result = context.Barrier(3), context.Event(), context.Queue()
    workers = [context.Process(target=contender, args=(str(isolated), barrier, release, result)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        barrier.wait(timeout=15)
        outcomes = [result.get(timeout=15) for _ in workers]
        winners = [pid for pid, acquired in outcomes if acquired]
        assert len(winners) == 1
        # Wait for losing process's cleanup before checking the owner's files.
        loser = next(worker for worker in workers if worker.pid != winners[0])
        loser.join(timeout=5)
        assert loser.exitcode == 0
        assert CoreSingleton.read_runtime().pid == winners[0]
        assert json.loads(CoreSingleton.LOCK_FILE.read_text())['pid'] == winners[0]
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        result.close()
        result.join_thread()
    assert all(worker.exitcode == 0 for worker in workers)
    assert not CoreSingleton.RUNTIME_FILE.exists()


def test_crashed_owner_can_be_replaced(isolated):
    program = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import singleton
from singleton import CoreSingleton
d = Path(sys.argv[2])
singleton._RECODER_DIR = d
CoreSingleton.LOCK_FILE = d / 'core.lock'
CoreSingleton.RUNTIME_FILE = d / 'runtime.json'
assert CoreSingleton.acquire_lock(os.getpid())
CoreSingleton.write_runtime(17894, 'dummy-token', os.getpid())
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve().parents[1]), str(isolated)], check=True, timeout=15)
    assert CoreSingleton.acquire_lock(os.getpid())
    publish(os.getpid())
    assert CoreSingleton.read_runtime().pid == os.getpid()
    CoreSingleton.release_lock(os.getpid())
