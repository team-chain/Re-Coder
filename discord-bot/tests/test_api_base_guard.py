"""봇이 세션 토큰을 붙여 부를 API 주소 — 메타데이터·링크 로컬 주소로 내부를 찌르지 못하게 한다(SSRF)."""
import pytest

import guild_store


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/",
    "http://0.0.0.0:8080",
    "not a url",
])
def test_위험한_주소는_저장하지_않는다(url):
    assert guild_store.api_base_problem(url)


@pytest.mark.parametrize("url", ["https://core.example.com", "http://127.0.0.1:17894", "http://192.168.0.10:17894"])
def test_음성대조_공개_주소와_직접_운영하는_로컬_주소는_허용한다(url, monkeypatch):
    monkeypatch.delenv("RECODER_BLOCK_PRIVATE_API_BASE", raising=False)
    assert guild_store.api_base_problem(url) is None


def test_공개_호스팅_모드에서는_사설_주소도_막는다(monkeypatch):
    monkeypatch.setenv("RECODER_BLOCK_PRIVATE_API_BASE", "1")
    assert guild_store.api_base_problem("http://127.0.0.1:17894")
    assert guild_store.api_base_problem("http://10.0.0.5:17894")


def test_set_api_는_위험한_주소를_거절한다(tmp_path, monkeypatch):
    monkeypatch.setattr(guild_store, "DB_PATH", tmp_path / "g.db")
    guild_store.init_db() if hasattr(guild_store, "init_db") else None
    with pytest.raises(ValueError):
        guild_store.set_api(1, "http://169.254.169.254/", "t")
