"""
discord-bot/commands/forecast.py — /recoder forecast 슬래시 커맨드 (§41)

배포 일기예보 — 과거 30일 배포 기록에서 시간대/요일별 성공률 + 활성
인시던트를 종합해 현재 배포 위험도를 ☀️/⛅/🌧️/⛈️/🌫️ 한 문자로 표현하고,
Haiku 권고문 + 최적 배포 시간대를 임베드로 띄운다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import discord
from discord import app_commands, Interaction

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


_WEATHER_COLOR = {
    "CLEAR": discord.Color.green(),
    "CLOUDY": discord.Color.gold(),
    "RAINY": discord.Color.orange(),
    "STORM": discord.Color.red(),
    "FOGGY": discord.Color.dark_grey(),
}


class ForecastCommands(app_commands.Group):
    """/recoder forecast — §41 Deploy Forecast."""

    @app_commands.command(
        name="forecast",
        description="배포 일기예보 — 지금 배포해도 안전한지 한눈에 확인합니다",
    )
    @app_commands.describe(
        service="대상 서비스명 (선택 — 생략 시 전체)",
        window_days="분석 기간 (기본 30일)",
    )
    @require_auth
    async def forecast(
        self,
        interaction: Interaction,
        service: str | None = None,
        window_days: int = 30,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            client = get_client_for_guild(interaction.guild_id)
            data = await client.get_deploy_forecast(
                service=service or "",
                window_days=window_days,
            )
            embed = build_forecast_embed(data)
            await interaction.followup.send(embed=embed)

        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            log.error("Forecast fetch failed: %s", exc)
            await interaction.followup.send(
                f"❌ 배포 일기예보 조회 실패:\n```\n{exc}\n```",
                ephemeral=True,
            )


def build_forecast_embed(data: Dict[str, Any]) -> discord.Embed:
    """ForecastReport dict 를 Discord embed 로 변환."""
    weather = (data.get("current_weather") or "FOGGY").upper()
    icon = data.get("weather_icon") or "🌫️"
    color = _WEATHER_COLOR.get(weather, discord.Color.dark_grey())

    rate_now = data.get("success_rate_now", -1.0)
    rate_overall = data.get("success_rate_overall", -1.0)
    confidence = data.get("confidence", 0.0)
    total = data.get("total_deploys_analyzed", 0)
    active_inc = data.get("active_incidents", 0)

    def fmt_rate(v: float) -> str:
        return f"{v * 100:.0f}%" if v >= 0 else "데이터 부족"

    embed = discord.Embed(
        title=f"{icon} 배포 일기예보 — {weather}",
        description=data.get("recommendation") or "권고문 없음",
        color=color,
    )

    embed.add_field(
        name="📈 현재 시간대 성공률",
        value=f"`{fmt_rate(rate_now)}`",
        inline=True,
    )
    embed.add_field(
        name="📊 전체 성공률",
        value=f"`{fmt_rate(rate_overall)}`",
        inline=True,
    )
    embed.add_field(
        name="🎯 신뢰도",
        value=f"`{confidence * 100:.0f}%` ({total}건)",
        inline=True,
    )

    if active_inc > 0:
        embed.add_field(
            name="🚨 활성 인시던트",
            value=f"`{active_inc}건` — 배포 자제 권장",
            inline=False,
        )

    if best := data.get("best_deploy_window"):
        embed.add_field(name="✨ 최적 배포 시간", value=f"`{best}`", inline=True)
    if worst := data.get("worst_deploy_window"):
        embed.add_field(name="⚠️ 위험 배포 시간", value=f"`{worst}`", inline=True)

    risks = data.get("risk_factors") or []
    if risks:
        embed.add_field(
            name="📋 리스크 요인",
            value="\n".join(f"• {r}" for r in risks[:5]),
            inline=False,
        )

    embed.set_footer(text="ReCoder Forecast §41 | /recoder deploy로 배포 시작")
    return embed


def build_forecast_oneline(data: Dict[str, Any]) -> str:
    """Standup 에 한 줄로 끼워넣기 위한 짧은 요약 — §39 통합 보조."""
    weather = (data.get("current_weather") or "FOGGY").upper()
    icon = data.get("weather_icon") or "🌫️"
    rate_now = data.get("success_rate_now", -1.0)
    rate_str = f"{rate_now * 100:.0f}%" if rate_now >= 0 else "—"
    return f"{icon} **{weather}** (현재 시간대 성공률 {rate_str})"
