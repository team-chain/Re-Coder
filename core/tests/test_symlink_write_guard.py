"""자동 수정·인프라 파일 저장이 심볼릭 링크를 따라 워크스페이스 밖에 쓰지 않는다 (3.0.2)."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import build_readiness as br
import api.routes.deploy as deploy_route

pytestmark = pytest.mark.skipif(os.name != "posix", reason="symlink creation needs POSIX in CI")


@pytest.fixture
def outside(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    victim = target / "victim.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    return victim


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    return root


def test_apply_file_plan_refuses_dockerfile_symlink_outside(ws, outside):
    (ws / "Dockerfile").symlink_to(outside)
    with pytest.raises(ValueError, match="심볼릭 링크"):
        br.apply_file_plan(ws, {"Dockerfile": "FROM evil\n", "main.js": "ok"}, [])
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (ws / "main.js").exists(), "하나라도 거부되면 아무것도 쓰지 않는다"


def test_apply_file_plan_refuses_symlinked_directory(ws, outside):
    (ws / "src").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        br.apply_file_plan(ws, {"src/victim.txt": "pwned"}, [])
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_apply_file_plan_refuses_escaping_paths(ws, outside):
    with pytest.raises(ValueError):
        br.apply_file_plan(ws, {"../outside/victim.txt": "pwned"}, [])
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_backup_refuses_symlinked_backup_folder(ws, outside):
    (ws / ".recoder").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        br._backup(ws, "Dockerfile", "FROM a\n")
    assert sorted(p.name for p in outside.parent.iterdir()) == ["victim.txt"]


def test_write_raw_refuses_final_symlink_even_without_root(ws, outside):
    link = ws / "Dockerfile"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        br._write_raw(link, "FROM evil\n")
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_apply_file_plan_still_writes_regular_files(ws):
    (ws / "Dockerfile").write_text("FROM a\n", encoding="utf-8")
    changed = br.apply_file_plan(ws, {"Dockerfile": "FROM b\n"}, [])
    assert (ws / "Dockerfile").read_text(encoding="utf-8") == "FROM b\n"
    assert "Dockerfile" in changed and any(c.startswith(".recoder/backups/") for c in changed)


def _proposal(root, content="FROM new\n", target="Dockerfile"):
    return SimpleNamespace(target_path=target, workspace_path=str(root), content=content, file_type="dockerfile")


def test_infra_save_refuses_dockerfile_symlink_outside(ws, outside):
    (ws / "Dockerfile").symlink_to(outside)
    with pytest.raises(HTTPException) as err:
        deploy_route._write_proposal_to_workspace(_proposal(ws), "", "p1", overwrite=True)
    assert err.value.status_code == 400 and "심볼릭 링크" in err.value.detail
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (outside.parent / "victim.txt.recoder-prev").exists()


def test_infra_save_refuses_symlinked_parent_directory(ws, outside):
    (ws / ".github").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(HTTPException) as err:
        deploy_route._write_proposal_to_workspace(_proposal(ws, "on: push\n", ".github/deploy.yml"), "", "p1")
    assert err.value.status_code == 400
    assert not (outside.parent / "deploy.yml").exists()


def test_infra_save_refuses_absolute_target_outside_workspace(ws, outside):
    with pytest.raises(HTTPException):
        deploy_route._write_proposal_to_workspace(_proposal(ws, target=str(outside)), "", "p1", overwrite=True)
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_infra_save_still_writes_normal_file(ws):
    result = deploy_route._write_proposal_to_workspace(_proposal(ws), "", "p1")
    assert result["status"] == "saved"
    assert (ws / "Dockerfile").read_text(encoding="utf-8") == "FROM new\n"
