"""설계 결정 프롬프트가 이미 있는 서비스를 본다 — 실사용(2026-11-21): users 서비스가 있는데 「users 서비스 신설」 을 추천."""
import json

import code_agent


def _project(tmp_path):
    ws = tmp_path / "shop"
    for i in range(40):
        (ws / "_old" / "copy").mkdir(parents=True, exist_ok=True)
        (ws / "_old" / "copy" / f"f{i}.ts").write_text("x", encoding="utf-8")
        (ws / "docs" / "adr").mkdir(parents=True, exist_ok=True)
        (ws / "docs" / "adr" / f"ADR-{i:03d}.md").write_text("x", encoding="utf-8")
    for svc in ("gateway", "users", "orders"):
        src = ws / "services" / svc / "src"
        src.mkdir(parents=True)
        (src / "main.ts").write_text("x", encoding="utf-8")
        (src / f"{svc}.controller.ts").write_text("x", encoding="utf-8")
    (ws / "architecture.json").write_text(json.dumps({
        "services": [{"name": "users", "path": "services/users", "kind": "api", "port": 3001, "purpose": "회원가입·로그인", "databases": ["users-db"]}],
        "databases": [{"name": "users-db", "engine": "postgres", "tables": ["users"]}],
        "endpoints": [{"method": "POST", "path": "/api/auth/login"}]}), encoding="utf-8")
    return ws


def test_service_files_come_before_archives_and_docs(tmp_path):
    files = code_agent._list_project_files(_project(tmp_path), limit=20)
    assert not any(f.startswith("_old/") for f in files)
    assert "services/users/src/users.controller.ts" in files
    assert files.index("services/users/src/main.ts") < len(files) and all(not f.startswith("docs/") for f in files[:7])


def test_plan_prompt_lists_existing_services(tmp_path):
    files = code_agent._list_project_files(_project(tmp_path))
    prompt = code_agent._build_plan_prompt("회원가입과 로그인 기능을 추가해줘", files, None)
    assert "서비스 users (services/users, api, :3001, DB users-db): 회원가입·로그인" in prompt
    assert "DB users-db (postgres): users" in prompt and "새로 만들자고 하지 말고" in prompt


def test_empty_project_has_no_structure_block(tmp_path):
    files = code_agent._list_project_files(tmp_path)
    assert files == [] and "(빈 프로젝트)" in code_agent._build_plan_prompt("x", files, None)
