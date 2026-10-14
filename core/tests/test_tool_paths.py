import os

import tool_paths


def test_adds_existing_docker_dir_when_missing(tmp_path, monkeypatch):
    bin_dir = tmp_path / "Docker" / "resources" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(tool_paths, "_candidates", lambda: [str(bin_dir), str(tmp_path / "missing")])
    monkeypatch.setattr(tool_paths.shutil, "which", lambda tool: None)
    monkeypatch.setenv("PATH", "/usr/bin")
    added = tool_paths.augment_path()
    assert added == [str(bin_dir)]
    assert os.environ["PATH"].split(os.pathsep) == ["/usr/bin", str(bin_dir)]
    # 두 번 불러도 중복으로 붙지 않는다
    assert tool_paths.augment_path() == []


def test_leaves_path_alone_when_tools_found(monkeypatch):
    monkeypatch.setattr(tool_paths.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert tool_paths.augment_path() == []
    assert os.environ["PATH"] == "/usr/bin"
