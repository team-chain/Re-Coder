"""
discord-bot/commands/setup.py — 서버 관리자용 초기 설정 커맨드

서버에 봇을 초대한 관리자가 ReCoder 연동을 설정하는 명령들.
모든 커맨드는 manage_guild 권한을 가진 관리자만 실행할 수 있다.

지원 커맨드:
  /recoder setup api <url> <token>           — ReCoder API 엔드포인트 설정
  /recoder setup channel <type> <#channel>   — 알림 채널 설정
  /recoder setup role add <@역할>            — 봇 사용 가능 역할 추가
  /recoder setup role remove <@역할>         — 봇 사용 가능 역할 제거
  /recoder setup status                      — 현재 설정 현황 확인
  /recoder invite                            — 봇 초대 URL 출력
"""


import logging
import os
from typing import Literal

import discord
from discord import app_commands, Interaction

import guild_store

log = logging.getLogger(__name__)

# 봇 초대에 필요한 권한 비트마스크
# bot + applications.commands scope, 권한: Send Messages, Embed Links, Read Message History, Use Slash Commands
_INVITE_PERMISSIONS = 2147485696
_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")


def _make_invite_url(client_id: str) -> str:
    return (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&permissions={_INVITE_PERMISSIONS}"
        f"&scope=bot%20applications.commands"
    )


# ── Setup 서브그룹 ──────────────────────────────────────────────────────────

class SetupGroup(app_commands.Group):
    """
    /recoder setup — 서버별 봇 초기 설정 그룹.
    manage_guild 권한 없으면 Discord가 자동으로 차단한다.
    """

    def __init__(self):
        super().__init__(
            name="setup",
            description="ReCoder 봇 초기 설정 (서버 관리자 전용)",
            default_permissions=discord.Permissions(manage_guild=True),
        )

    # ── /recoder setup api ─────────────────────────────────────────────────

    @app_commands.command(name="api", description="ReCoder API 엔드포인트와 인증 토큰을 설정합니다")
    @app_commands.describe(
        url="ReCoder Core API URL (예: http://your-server.com:17894)",
        token="ReCoder 세션 토큰",
    )
    async def setup_api(self, interaction: Interaction, url: str, token: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        # 토큰은 로그에 남기지 않는다
        guild_store.set_api(interaction.guild.id, url, token)
        log.info("Guild %d API 설정 완료: %s", interaction.guild.id, url)

        embed = discord.Embed(
            title="✅ API 설정 완료",
            description=f"**엔드포인트**: `{url}`\n**토큰**: `{'*' * min(len(token), 8)}...` (저장됨)",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="다음 단계",
            value=(
                "• `/recoder setup channel` — 알림 채널 설정\n"
                "• `/recoder setup role add` — 봇 사용 가능 역할 설정\n"
                "• `/recoder status` — 연결 상태 확인"
            ),
            inline=False,
        )
        embed.set_footer(text="설정은 이 서버에만 적용됩니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /recoder setup channel ─────────────────────────────────────────────

    @app_commands.command(name="channel", description="ReCoder 알림을 받을 채널을 설정합니다")
    @app_commands.describe(
        channel_type="알림 종류 (deploy: 배포, incident: 인시던트, standup: 데일리 스탠드업)",
        channel="알림을 받을 텍스트 채널",
    )
    async def setup_channel(
        self,
        interaction: Interaction,
        channel_type: Literal["deploy", "incident", "standup"],
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        guild_store.set_channel(interaction.guild.id, channel_type, channel.id)
        log.info("Guild %d 채널 설정: %s → %d", interaction.guild.id, channel_type, channel.id)

        type_label = {"deploy": "배포 알림", "incident": "인시던트 알림", "standup": "데일리 스탠드업"}
        embed = discord.Embed(
            title="✅ 채널 설정 완료",
            description=f"**{type_label[channel_type]}** 채널이 {channel.mention}으로 설정되었습니다.",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /recoder setup role ────────────────────────────────────────────────

    @app_commands.command(name="role", description="봇 사용 가능 역할을 추가하거나 제거합니다")
    @app_commands.describe(
        action="add: 역할 추가, remove: 역할 제거",
        role="추가/제거할 Discord 역할",
    )
    async def setup_role(
        self,
        interaction: Interaction,
        action: Literal["add", "remove"],
        role: discord.Role,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        if action == "add":
            guild_store.add_role(guild_id, role.id)
            log.info("Guild %d 역할 추가: %s (%d)", guild_id, role.name, role.id)
            msg = f"✅ **{role.mention}** 역할이 봇 사용 가능 목록에 추가되었습니다."
        else:
            guild_store.remove_role(guild_id, role.id)
            log.info("Guild %d 역할 제거: %s (%d)", guild_id, role.name, role.id)
            msg = f"🗑️ **{role.mention}** 역할이 봇 사용 가능 목록에서 제거되었습니다."

        embed = discord.Embed(description=msg, color=discord.Color.green())
        embed.add_field(
            name="💡 참고",
            value="서버 관리자(manage_guild 권한)는 역할 설정에 상관없이 항상 봇을 사용할 수 있습니다.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /recoder setup status ──────────────────────────────────────────────

    @app_commands.command(name="status", description="이 서버의 ReCoder 봇 설정 현황을 확인합니다")
    async def setup_status(self, interaction: Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        summary = guild_store.get_guild_summary(interaction.guild.id)

        if not summary["configured"]:
            embed = discord.Embed(
                title="⚙️ ReCoder 봇 — 설정 필요",
                description=(
                    "이 서버에서 아직 ReCoder API 설정이 완료되지 않았습니다.\n\n"
                    "**시작하기**: `/recoder setup api <url> <token>`"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="⚙️ ReCoder 봇 설정 현황",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="🔗 API 엔드포인트",
            value=f"`{summary['api_base']}`",
            inline=False,
        )

        # 채널 현황
        channel_labels = {"deploy": "배포 알림", "incident": "인시던트", "standup": "스탠드업"}
        channels = summary["channels"]
        if channels:
            channel_lines = "\n".join(
                f"• {channel_labels.get(ctype, ctype)}: <#{cid}>"
                for ctype, cid in channels.items()
            )
        else:
            channel_lines = "미설정 (`/recoder setup channel`로 설정)"
        embed.add_field(name="📣 알림 채널", value=channel_lines, inline=False)

        # 역할 현황
        roles = summary["roles"]
        if roles:
            role_lines = "\n".join(f"• <@&{rid}>" for rid in roles)
        else:
            role_lines = "미설정 — 관리자만 사용 가능 (`/recoder setup role add`로 추가)"
        embed.add_field(name="👥 허용 역할", value=role_lines, inline=False)

        embed.set_footer(text="설정 변경: /recoder setup api | channel | role")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Invite 커맨드 (최상위) ──────────────────────────────────────────────────

async def invite_command(interaction: Interaction) -> None:
    """봇 초대 URL을 Ephemeral 메시지로 전송한다."""
    client_id = _CLIENT_ID or (str(interaction.client.user.id) if interaction.client.user else "")

    if not client_id:
        await interaction.response.send_message(
            "❌ CLIENT_ID가 설정되지 않았습니다. 관리자에게 문의하세요.", ephemeral=True
        )
        return

    invite_url = _make_invite_url(client_id)
    embed = discord.Embed(
        title="🤖 ReCoder Bot 초대",
        description=(
            "아래 링크로 봇을 Discord 서버에 초대하세요.\n\n"
            f"**[👉 봇 초대하기]({invite_url})**"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="초대 후 설정 방법",
        value=(
            "1. 봇을 서버에 초대\n"
            "2. `/recoder setup api <url> <token>` — ReCoder API 연결\n"
            "3. `/recoder setup channel` — 알림 채널 지정\n"
            "4. `/recoder setup role add` — 사용 가능 역할 설정\n"
            "5. `/recoder status` — 연결 확인"
        ),
        inline=False,
    )
    embed.set_footer(text="초대 링크는 이 메시지를 보는 본인에게만 보입니다.")
    await interaction.response.send_message(embed=embed, ephemeral=True)
