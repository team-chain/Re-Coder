"""
/api/analyze/approve — 워크스페이스 경계(fail-closed) + 훅 매칭 회귀 테스트.

Codex P1 2건:
  1. 승인된 패치가 workspace_root 밖의 절대경로를 수정할 수 있었다
     (환각·프롬프트 주입 경로).
  2. 훅 매칭이 모든 따옴표 문자열을 와일드카드로 바꿔, 문자열로만 구분되는
     블록에서 **엉뚱한 블록**에 패치가 적용됐다.
"""
import asyncio
import sys
from pathlib import Path

import pytest

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from fastapi import HTTPException            # noqa: E402
from api.routes import analyze as az         # noqa: E402
from schemas import FilePatch, PatchProposal  # noqa: E402


def _register(proposal: PatchProposal) -> str:
    az._proposals[proposal.proposal_id] = proposal
    return proposal.proposal_id


def _approve(pid: str):
    return asyncio.run(az.approve_patch(pid, True))


@pytest.fixture(autouse=True)
def _clean_store():
    az._proposals.clear()
    yield
    az._proposals.clear()


# ── 1. 워크스페이스 경계 ────────────────────────────────────────────────

def _simple_diff(old: str, new: str) -> str:
    return f"@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_approve_rejects_patch_outside_workspace(tmp_path):
    """[Codex P1 회귀] 경계 밖 절대경로는 승인해도 절대 쓰이지 않는다."""
    ws = tmp_path / "ws"; ws.mkdir()
    victim = tmp_path / "outside.txt"
    victim.write_text("원본\n", encoding="utf-8")

    pid = _register(PatchProposal(
        summary="s", workspace_root=str(ws),
        patches=[FilePatch(file=str(victim), unified_diff=_simple_diff("원본", "변조"), reason="r")],
    ))
    with pytest.raises(HTTPException) as exc:
        _approve(pid)
    assert exc.value.status_code == 422
    assert "워크스페이스 밖" in exc.value.detail
    assert victim.read_text(encoding="utf-8") == "원본\n", "경계 밖 파일이 수정됐다"


def test_approve_rejects_traversal_and_symlink_escape(tmp_path):
    """`ws/../outside` 처럼 어휘상 안쪽처럼 보이는 우회도 resolve 로 막는다."""
    ws = tmp_path / "ws"; ws.mkdir()
    victim = tmp_path / "outside.txt"
    victim.write_text("원본\n", encoding="utf-8")
    sneaky = str(ws / ".." / "outside.txt")

    pid = _register(PatchProposal(
        summary="s", workspace_root=str(ws),
        patches=[FilePatch(file=sneaky, unified_diff=_simple_diff("원본", "변조"), reason="r")],
    ))
    with pytest.raises(HTTPException) as exc:
        _approve(pid)
    assert exc.value.status_code == 422
    assert victim.read_text(encoding="utf-8") == "원본\n"


def test_approve_applies_inside_workspace(tmp_path):
    """[음성 대조] 경계 **안** 파일은 정상적으로 적용된다."""
    ws = tmp_path / "ws"; ws.mkdir()
    target = ws / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")

    pid = _register(PatchProposal(
        summary="s", workspace_root=str(ws),
        patches=[FilePatch(file=str(target), unified_diff=_simple_diff("x = 1", "x = 2"), reason="r")],
    ))
    out = _approve(pid)
    assert out["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_approve_without_workspace_root_fails_closed(tmp_path):
    """경계를 모르는 제안은 적용을 거부한다 — 모르면 여는 게 아니라 닫는다."""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    pid = _register(PatchProposal(
        summary="s", workspace_root="",
        patches=[FilePatch(file=str(target), unified_diff=_simple_diff("x = 1", "x = 2"), reason="r")],
    ))
    with pytest.raises(HTTPException) as exc:
        _approve(pid)
    assert exc.value.status_code == 422
    assert target.read_text(encoding="utf-8") == "x = 1\n"


# ── 2. 훅 매칭 — 문자열 리터럴은 구분자다 ──────────────────────────────

def test_patch_lands_on_the_block_named_in_the_diff(tmp_path):
    """[Codex P1 회귀] "/a"·"/b" 처럼 문자열로만 구분되는 블록에서
    diff 가 지목한 블록("/b")에 적용돼야 한다 — 첫 블록이 아니라."""
    ws = tmp_path / "ws"; ws.mkdir()
    target = ws / "routes.py"
    target.write_text(
        '@app.get("/a")\n'
        'def handler():\n'
        '    return ok()\n'
        '\n'
        '@app.get("/b")\n'
        'def handler():\n'
        '    return ok()\n', encoding="utf-8")

    diff = (
        '@@ -5,3 +5,3 @@\n'
        ' @app.get("/b")\n'
        ' def handler():\n'
        '-    return ok()\n'
        '+    return fixed()\n'
    )
    pid = _register(PatchProposal(
        summary="s", workspace_root=str(ws),
        patches=[FilePatch(file=str(target), unified_diff=diff, reason="r")],
    ))
    out = _approve(pid)
    assert out["status"] == "applied"
    text = target.read_text(encoding="utf-8")
    assert '"/a"' in text and 'return ok()' in text.split('"/b"')[0], (
        "패치가 /a 블록(첫 블록)에 잘못 적용됐다"
    )
    assert 'return fixed()' in text.split('"/b"')[1], "패치가 /b 블록에 적용되지 않았다"


def test_masked_secret_line_still_matches_real_secret(tmp_path):
    """[음성 대조] 마스킹된 시크릿 줄은 여전히 실제 값과 매칭된다 —
    구분자 보존이 마스크 관용까지 없애면 안 된다."""
    ws = tmp_path / "ws"; ws.mkdir()
    target = ws / "config.py"
    target.write_text('KEY = "AKIA1234SECRET"\nMODE = "prod"\n', encoding="utf-8")

    diff = (
        '@@ -1,2 +1,2 @@\n'
        '-KEY = "[MASKED]"\n'
        '+KEY = os.environ["KEY"]\n'
        ' MODE = "prod"\n'
    )
    pid = _register(PatchProposal(
        summary="s", workspace_root=str(ws),
        patches=[FilePatch(file=str(target), unified_diff=diff, reason="r")],
    ))
    out = _approve(pid)
    assert out["status"] == "applied"
    assert 'os.environ["KEY"]' in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("diff_line,file_line,expected", [
    ('app.get("/a")', 'app.get("/b")', False),
    ('app.get("/a")', 'app.get("/a")', True),
    ('KEY = "[MASKED]"', 'KEY = "AKIA99"', True),
    ('aws_secret_access_key=[MASKED]', 'aws_secret_access_key=ab/c+1', True),
    ('token = "[MASKED_JWT]"', 'token = "eyJa.b.c"', True),
    ('cfg("/a", key="[MASKED]")', 'cfg("/a", key="s3cr3t")', True),
    ('cfg("/a", key="[MASKED]")', 'cfg("/b", key="s3cr3t")', False),
])
def test_line_matcher_semantics(diff_line, file_line, expected):
    assert az._lines_match(diff_line, file_line) is expected
