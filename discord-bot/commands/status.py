"""
discord-bot/commands/status.py — /recoder status 슬래시 커맨드 (§37.3)

현재 배포 상태, 오케스트레이터 상태, 최근 인시던트를 조회한다.
"""

import logging

import discord
from discord import app_commands, Interaction

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


class StatusCommands(app_commands.Group):

    @app_commands.command(name="status", description="ReCoder 현재 상태 및 최근 배포 현황을 조회합니다")
    @app_commands.describe(session_id="특정 세션 ID 조회 (선택 — 생략 시 전체 현황)")
    @require_auth
    async def status(
        self,
        interaction: Interaction,
        session_id: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            client = get_client_for_guild(interaction.guild_id)
            data = await client.status(session_id=session_id)
            embed = _build_status_embed(data, session_id)
            await interaction.followup.send(embed=embed)

        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            log.error("Status check failed: %s", exc)
            await interaction.followup.send(
                f"❌ 상태 조회 실패:\n```\n{exc}\n```",
                ephemeral=True,
            )


def _build_status_embed(data: dict, session_id: str | None) -> discord.Embed:
    core_ok = data.get("status") == "ok"
    color = discord.Color.green() if core_ok else discord.Color.orange()

    embed = discord.Embed(
        title="📊 ReCoder 상태",
        color=color,
    )

    embed.add_field(
        name="🖥️ Local Core",
        value=f"`{'정상' if core_ok else '이상'}` — 포트 {data.get('port', 17894)}",
        inline=True,
    )

    if version := data.get("version"):
        embed.add_field(name="📦 버전", value=f"`{version}`", inline=True)

    if session_id:
        embed.add_field(name="🔍 세션 ID", value=f"`{session_id}`", inline=True)

    if last_deploy := data.get("last_deploy"):
        status_icon = "✅" if last_deploy.get("success") else "❌"
        embed.add_field(
            name="🚀 최근 배포",
            value=(
                f"{status_icon} `{last_deploy.get('service', 'unknown')}`\n"
                f"시각: `{last_deploy.get('deployed_at', 'N/A')}`"
            ),
            inline=False,
        )

    incidents = data.get("active_incidents", [])
    if incidents:
        incident_lines = "\n".join(
            f"🔴 `{i.get('id', '?')}` — {i.get('title', '')}"
            for i in incidents[:3]
        )
        embed.add_field(name="🚨 활성 인시던트", value=incident_lines, inline=False)
    else:
        embed.add_field(name="🚨 인시던트", value="활성 인시던트 없음 ✅", inline=False)

    embed.set_footer(text="ReCoder Status §37 | /recoder deploy로 배포 시작")
    return embed
