"""프로덕션 점검에서 확인한 가장자리 경우(Windows 인코딩·CRLF·포트·커버리지 리포트)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import build_readiness as br
from api.routes import deploy as d


def test_UTF8_이_아닌_기존_Dockerfile_은_충돌로_보고_원본_바이트로_백업한다(tmp_path):
    target = tmp_path / "Dockerfile"
    original = "# 한글 주석\nFROM node:22\n".encode("cp949")
    target.write_bytes(original)
    proposal = SimpleNamespace(content="FROM node:22-alpine\n", target_path="Dockerfile", workspace_path=str(tmp_path),
                               file_type="dockerfile", risk_reasons=[])
    conflict = d._existing_file_conflict(proposal, target)
    assert conflict is not None and "한글" in conflict["existing_content"]
    result = d._write_proposal_to_workspace(proposal, str(tmp_path), "p1", overwrite=False)
    assert result["status"] == "exists" and target.read_bytes() == original
    result = d._write_proposal_to_workspace(proposal, str(tmp_path), "p1", overwrite=True)
    assert Path(result["backup_path"]).read_bytes() == original


def test_BOM_이_붙은_package_json_도_스택과_시작_명령을_읽는다(tmp_path):
    (tmp_path / "package.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"name": "a", "scripts": {"start": "node server/app.js"}, "dependencies": {"express": "^4"}}).encode())
    assert d._detect_stack(str(tmp_path)).value.startswith("node")


def test_CRLF_파일에도_pg_숫자_파서를_넣는다():
    text = "const { Pool } = require('pg');\r\nconst x = 1;\r\n"
    out = br.pg_numeric_parser_rewrite(text)
    assert "setTypeParser" in out and "\r\n" in out and "\n" not in out.replace("\r\n", "")


def test_커버리지_리포트의_index_html_은_앱_화면이_아니다(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "index.html").write_text("<html></html>")
    assert br.expects_screen(tmp_path) is False


def test_PC_포트는_게시_목록에서_직접_읽는다(monkeypatch):
    out = "web\t0.0.0.0:3131->3001/tcp, [::]:3131->3001/tcp\nother\t0.0.0.0:8081->3000/tcp\ndb\t5432/tcp\n"
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, ""))
    assert d._docker_port_publishers(3131) == ["web"]
    assert d._docker_port_publishers(3000) == []   # 컨테이너 안 포트는 PC 포트가 아니다
    assert d._docker_port_publishers(8081) == ["other"]
