import os
from pathlib import Path

import code_agent


def test_skips_dependencies_before_descending(tmp_path, monkeypatch):
    for folder in ('src', 'node_modules', '.git', 'dist'):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'file.ts').write_text('example', encoding="utf-8")
    visited = []
    original = os.scandir
    def scan(path):
        visited.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(os, 'scandir', scan)
    assert code_agent._list_project_files(tmp_path) == ['src/file.ts']
    assert not set(visited) & {'node_modules', '.git', 'dist'}


def test_stops_at_limit_without_visiting_remaining_directories(tmp_path, monkeypatch):
    for name in ('b.ts', 'a.ts'):
        (tmp_path / name).write_text('example', encoding="utf-8")
    (tmp_path / 'nested').mkdir()
    visited = []
    original = os.scandir
    def scan(path):
        visited.append(Path(path))
        return original(path)
    monkeypatch.setattr(os, 'scandir', scan)
    #: 얕은 파일을 고르려고 한도보다 넉넉히 훑지만, 훑는 양에는 상한이 있다(_TREE_SCAN_CAP).
    monkeypatch.setattr(code_agent, '_TREE_SCAN_CAP', 2)
    assert code_agent._list_project_files(tmp_path, limit=1) == ['a.ts']
    assert visited == [tmp_path]
    assert code_agent._list_project_files(tmp_path, limit=0) == []
