"""CI windows-latest 에서만 드러난 것들 — 리눅스에서도 같은 조건을 만들어 고정한다.

1. 같은 시각에 저장된 기록(Windows 시계는 약 15ms 단위)의 최근순이 뒤섞였다 → 저장 순서로 끊는다.
2. CRLF 로 받은 같은 내용의 파일을 "이미 있음(충돌)"으로 봤다 → 줄바꿈만 다르면 같은 파일.
3. 의존성 자동 수정 안내에 OS 구분자(backend\\package.json)가 나왔다 → 화면에는 언제나 / 로.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import api.routes.deploy as deploy_route
import vuln_recheck as vr
from persistence import RecoderDB, list_preflight_runs, save_preflight_run
from schemas import PreflightRun, PreflightStatus


def test_same_timestamp_runs_list_most_recently_saved_first(tmp_path):
    db = RecoderDB(tmp_path / "t.db", check_same_thread=True)
    same = datetime(2026, 10, 17, 9, 0, 0)
    ids = [save_preflight_run(db, PreflightRun(project_id="p", status=PreflightStatus.PASSED, score=70 + i, created_at=same))
           for i in range(4)]
    assert [r.preflight_run_id for r in list_preflight_runs(db)] == list(reversed(ids))


def test_crlf_copy_of_the_same_dockerfile_is_not_a_conflict(tmp_path):
    (tmp_path / "Dockerfile").write_bytes(b"FROM node:22-alpine\r\nCMD [\"node\", \"server.js\"]\r\n")
    proposal = SimpleNamespace(target_path="Dockerfile", workspace_path=str(tmp_path),
                               content='FROM node:22-alpine\nCMD ["node", "server.js"]\n', file_type="dockerfile")
    result = deploy_route._write_proposal_to_workspace(proposal, "", "p1")
    assert result["status"] == "saved"
    assert (tmp_path / "Dockerfile").read_bytes() == b'FROM node:22-alpine\nCMD ["node", "server.js"]\n', "LF 로 맞춘다"
    # 내용이 실제로 다르면 여전히 충돌
    (tmp_path / "Dockerfile").write_bytes(b"FROM node:20\r\n")
    assert deploy_route._write_proposal_to_workspace(proposal, "", "p2")["status"] == "exists"


def test_fix_message_uses_forward_slashes(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    (ws / "backend").mkdir(parents=True)
    (ws / "backend" / "package.json").write_text(json.dumps({"dependencies": {"lodash": "4.17.20"}}), encoding="utf-8")
    out = vr.apply_fix({"key": "k", "workspace": str(ws)}, {"ecosystem": "npm", "name": "lodash", "to": "4.17.21"}, npm_lock=False)
    assert "backend/package.json" in out["message"] and "\\" not in out["message"]
