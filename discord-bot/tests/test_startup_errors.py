"""봇이 Discord 에 붙지 못할 때 원인과 조치를 알려 주고 종료하는지 검증한다.

실제 Discord 에는 접속하지 않는다. discord.Client.run 을 예외로 바꿔 끼운다.
"""
from __future__ import annotations

import runpy
from pathlib import Path

import discord
import pytest

BOT = Path(__file__).resolve().parent.parent / "bot.py"


class _Resp:
    status = 401
    reason = "Unauthorized"


@pytest.mark.parametrize(
    ("error", "code", "hint"),
    [
        (lambda: discord.errors.LoginFailure("Improper token has been passed."), 2, "DISCORD_BOT_TOKEN"),
        (lambda: discord.errors.PrivilegedIntentsRequired(None), 3, "MESSAGE CONTENT INTENT"),
    ],
)
def test_login_errors_explain_the_fix(monkeypatch, tmp_path, caplog, error, code, hint):
    secret = "MTAwMDAwMDAwMDAwMDAwMDAwMA.fixture.not-a-real-token"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", secret)
    monkeypatch.chdir(tmp_path)  # recoder-bot.log 가 저장소에 생기지 않게

    def fail(self, *args, **kwargs):
        raise error()

    monkeypatch.setattr(discord.Client, "run", fail)
    monkeypatch.setattr("guild_store.init_db", lambda: None)
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(BOT), run_name="__main__")
    assert exited.value.code == code
    assert hint in caplog.text
    assert secret not in caplog.text
