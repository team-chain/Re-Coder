"""로컬 Docker 배포: PC 의 .env 를 컨테이너 환경변수로 넘긴다(값은 저장하지 않는다)."""
from __future__ import annotations

from pathlib import Path

from deployment_inputs import read_env_file, workspace_env_files
from api.routes.deploy import _env_file_args, _resolved_env_files


def test_reads_env_like_dotenv(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# 주석\nexport GREETING='hello world'\nDATABASE_URL=\"postgres://u:p@db/x\"\n"
        "PORT=9999\nTOKEN=abc # 꼬리 주석\nbad-key=1\nEMPTY=\n",
        encoding="utf-8",
    )
    values = read_env_file(env)
    assert values == {"GREETING": "hello world", "DATABASE_URL": "postgres://u:p@db/x", "TOKEN": "abc", "EMPTY": ""}


def test_finds_root_and_subproject_env_but_not_examples(tmp_path: Path):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("A=\n", encoding="utf-8")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / ".env").write_text("B=2\n", encoding="utf-8")
    assert workspace_env_files(str(tmp_path), ["server", "client"]) == [".env", "server/.env"]


def test_env_args_keep_plan_values_and_never_leave_the_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("A=from-file\nB=2\n", encoding="utf-8")
    (tmp_path / "outside.env").write_text("SECRET=x\n", encoding="utf-8")
    paths = _resolved_env_files(str(ws), [".env", "../outside.env", "missing/.env"])
    assert paths == [str((ws / ".env").resolve())]
    #: 계획에서 이미 정한 A 는 덮지 않는다.
    assert _env_file_args(paths, {"A"}) == ["-e", "B=2"]
