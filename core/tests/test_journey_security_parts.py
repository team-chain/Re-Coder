"""진행 흐름의 「보안 검사」 는 코드·시크릿·Dockerfile·이미지 결과를 합친다 — 이미지에 위험이 있는데 「완료」 로 보이던 회귀."""
import journey


def test_parts_combine_into_one_honest_step(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "app"; ws.mkdir()
    journey.security_part(ws, "code", True)
    assert journey.load(ws)["security"]["status"] == "done"
    journey.security_part(ws, "trivy", True, critical=1, high=0, medium=8)
    journey.security_part(ws, "gitleaks", True)
    sec = journey.load(ws)["security"]
    assert sec["status"] == "failed" and sec["summary"].startswith("심각·높음 1건 · 중간 8건")
    assert "코드·시크릿·이미지" in sec["summary"]
    journey.security_part(ws, "trivy", True)
    journey.security_part(ws, "hadolint", False)
    sec = journey.load(ws)["security"]
    assert sec["status"] == "unverified" and "못 한 검사: Dockerfile" in sec["summary"]


def test_unknown_tool_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("RECODER_HOME", str(tmp_path / "home"))
    ws = tmp_path / "app"; ws.mkdir()
    journey.security_part(ws, "nmap", True, high=3)
    assert "security" not in journey.load(ws)
