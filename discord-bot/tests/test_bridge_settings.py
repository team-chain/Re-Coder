"""bridge_settings.py — ReCoder Bridge 채널 설정 영속화 테스트."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def fresh_bridge(monkeypatch, tmp_path):
    """bridge_settings.json 경로를 임시 파일로 격리한다."""
    import bridge_settings

    fake_path = tmp_path / "bridge_settings.json"
    monkeypatch.setattr(bridge_settings, "_SETTINGS_PATH", fake_path)
    # 모듈 내부 _cache 도 초기화
    monkeypatch.setattr(bridge_settings, "_cache", None)
    return bridge_settings, fake_path


def test_initial_make_channel_id_is_zero(fresh_bridge):
    bs, _ = fresh_bridge
    assert bs.get_make_channel_id() == 0


def test_set_then_get_make_channel_id(fresh_bridge):
    bs, path = fresh_bridge
    bs.set_make_channel_id(123456789)

    assert bs.get_make_channel_id() == 123456789
    assert path.exists()

    # 디스크에 저장된 값 확인
    data = json.loads(path.read_text())
    assert data.get("make_channel_id") == 123456789


def test_set_zero_clears_channel(fresh_bridge):
    bs, _ = fresh_bridge
    bs.set_make_channel_id(123)
    bs.set_make_channel_id(0)
    assert bs.get_make_channel_id() == 0


def test_get_settings_snapshot_shape(fresh_bridge):
    bs, _ = fresh_bridge
    bs.set_make_channel_id(42)
    snap = bs.get_settings_snapshot()
    assert isinstance(snap, dict)
    assert snap.get("make_channel_id") == 42


def test_atomic_write_does_not_leave_tmp(fresh_bridge):
    """_save() 가 tmp → replace 패턴이어서 중간 상태가 남으면 안 됨."""
    bs, path = fresh_bridge
    bs.set_make_channel_id(1)

    parent = path.parent
    tmp_files = [p for p in parent.iterdir() if p.name.endswith(".tmp")]
    assert tmp_files == []
