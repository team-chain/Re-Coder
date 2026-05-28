"""commands/* — 임베드 빌더 함수 단위 테스트.

discord.py 의 슬래시 커맨드 라이프사이클은 mock 하기 까다로워서, 본 테스트는
임베드 빌더(_build_*_embed) 만 직접 호출해 Discord 표시 제한 (필드 최대 25개,
title/description/field name/value 길이 제한) 안에서 동작하는지 검증한다.
"""

from __future__ import annotations

import discord

from commands.code import _build_code_embeds
from commands.forecast import build_forecast_embed, build_forecast_oneline
from commands.preflight import _build_preflight_embed
from commands.status import _build_status_embed


# ── Discord embed 제한 ──────────────────────────────────────────────────────

EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELDS_MAX = 25
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_TOTAL_MAX = 6000


def _check_embed_limits(e: discord.Embed) -> None:
    assert e.title is None or len(e.title) <= EMBED_TITLE_MAX
    assert e.description is None or len(e.description) <= EMBED_DESCRIPTION_MAX
    assert len(e.fields) <= EMBED_FIELDS_MAX
    for f in e.fields:
        assert len(f.name) <= EMBED_FIELD_NAME_MAX
        assert len(f.value) <= EMBED_FIELD_VALUE_MAX
    # 대략적인 total length 추정
    total = sum(len(str(x)) for x in [
        e.title or "", e.description or "",
        *(f.name + f.value for f in e.fields),
    ])
    assert total <= EMBED_TOTAL_MAX


# ── preflight ───────────────────────────────────────────────────────────────

def test_preflight_embed_all_passed():
    result = {
        "overall_passed": True,
        "checks": [
            {"name": "ECS Cluster", "passed": True, "risk_level": "LOW", "message": "OK"},
            {"name": "Task Definition", "passed": True, "risk_level": "LOW", "message": "OK"},
        ],
    }
    embed = _build_preflight_embed(result, "my-cluster", "my-svc", "ap-northeast-2")
    assert embed.color == discord.Color.green()
    assert "my-svc" in embed.title
    assert "my-cluster" in embed.description
    assert len(embed.fields) == 2
    _check_embed_limits(embed)


def test_preflight_embed_with_failures():
    result = {
        "overall_passed": False,
        "checks": [
            {"name": "ECS Cluster", "passed": False, "risk_level": "HIGH",
             "message": "Cluster not found"},
        ],
    }
    embed = _build_preflight_embed(result, "c", "s", "r")
    assert embed.color == discord.Color.red()
    # 권장 조치 필드 추가됨
    field_names = [f.name for f in embed.fields]
    assert any("권장 조치" in n for n in field_names)
    _check_embed_limits(embed)


def test_preflight_embed_truncates_to_10_checks():
    """Discord embed field 25개 한계 — 본 빌더는 10개로 자르도록 구현됨."""
    result = {
        "overall_passed": True,
        "checks": [
            {"name": f"check-{i}", "passed": True, "risk_level": "LOW", "message": "ok"}
            for i in range(20)
        ],
    }
    embed = _build_preflight_embed(result, "c", "s", "r")
    assert len(embed.fields) <= 10
    _check_embed_limits(embed)


def test_preflight_embed_with_empty_checks():
    embed = _build_preflight_embed({"checks": []}, "c", "s", "r")
    assert embed.title is not None
    _check_embed_limits(embed)


# ── code ────────────────────────────────────────────────────────────────────

def test_code_embed_basic():
    result = {
        "analysis": "이 코드는 ImportError 가 발생합니다.",
        "patches": [],
    }
    embeds = _build_code_embeds("ImportError: foo", result)
    assert len(embeds) >= 1
    for e in embeds:
        _check_embed_limits(e)


# ── status ──────────────────────────────────────────────────────────────────

def test_status_embed_with_health():
    data = {"status": "healthy", "uptime": "1h"}
    embed = _build_status_embed(data, None)
    assert embed.title is not None
    _check_embed_limits(embed)


# ── forecast ────────────────────────────────────────────────────────────────

def test_forecast_embed_clear_weather():
    """current_weather=CLEAR 면 _WEATHER_COLOR 매핑에 의해 녹색."""
    data = {
        "current_weather": "CLEAR",
        "weather_icon": "☀️",
        "success_rate_now": 0.95,
        "success_rate_overall": 0.90,
        "active_incidents": 0,
        "recommendation": "지금 배포해도 안전합니다.",
        "best_window": "오후 2시~4시",
    }
    embed = build_forecast_embed(data)
    assert embed.color == discord.Color.green()
    _check_embed_limits(embed)


def test_forecast_embed_storm():
    data = {
        "current_weather": "STORM",
        "weather_icon": "⛈️",
        "success_rate_now": 0.40,
        "active_incidents": 3,
        "recommendation": "지금은 배포를 피하세요.",
    }
    embed = build_forecast_embed(data)
    assert embed.color == discord.Color.red()
    _check_embed_limits(embed)


def test_forecast_embed_unknown_weather_defaults_to_foggy_color():
    """current_weather 누락 시 안전한 dark_grey 폴백."""
    embed = build_forecast_embed({})
    assert embed.color == discord.Color.dark_grey()
    _check_embed_limits(embed)


def test_forecast_oneline_clear():
    line = build_forecast_oneline({
        "current_weather": "CLEAR",
        "weather_icon": "☀️",
        "success_rate_now": 0.95,
    })
    assert isinstance(line, str)
    assert len(line) > 0


def test_forecast_oneline_fallback_on_missing_keys():
    """필수 키가 빠져도 죽지 말고 적당한 문자열 반환."""
    line = build_forecast_oneline({})
    assert isinstance(line, str)
