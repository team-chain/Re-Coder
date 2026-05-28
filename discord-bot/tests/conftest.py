"""
discord-bot/tests/conftest.py — pytest 공통 설정

- discord-bot/ 디렉터리를 sys.path 에 추가하여 `import bot`, `import guild_store`,
  `from middleware import auth` 등 봇 모듈을 그대로 임포트할 수 있게 한다.
- 각 테스트마다 guild_store 의 DB_PATH 를 임시 파일로 격리해 디스크 상의
  실제 guild_config.db 를 건드리지 않는다.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# discord-bot/ 루트를 sys.path 에 추가 (conftest.py 가 tests/ 안에 있음)
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# 부팅 시 DISCORD_BOT_TOKEN 체크에서 sys.exit 가 발생하지 않도록 더미 토큰을 세팅.
os.environ.setdefault("DISCORD_BOT_TOKEN", "dummy-test-token")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """guild_store.DB_PATH 를 테스트 격리용 임시 파일로 교체한다."""
    import guild_store

    db = tmp_path / "test_guild_config.db"
    monkeypatch.setattr(guild_store, "DB_PATH", db)
    guild_store.init_db()
    yield db
