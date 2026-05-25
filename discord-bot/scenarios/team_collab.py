"""
discord-bot/scenarios/team_collab.py — 시나리오 3: 팀 협업 (§37.6)

팀 전체가 Discord에서 ReCoder를 통해 배포를 공동으로 관리하는 시나리오.

흐름:
  1. 팀원 A가 /recoder preflight 로 점검
  2. 팀원 B(TL)가 Discord에서 배포 승인 버튼 클릭
  3. /recoder deploy 실행 → 전체 채널에 진행 상황 브로드캐스트
  4. 배포 완료 후 /recoder status 로 전체 팀이 결과 확인
  5. 실패 시 자동 @oncall 멘션 + 롤백 버튼 제공
"""

import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord import ui

from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


class TeamDeployApprovalView(ui.View):
    """
    팀 배포 승인 View (§37.6).

    TL 또는 승인 권한이 있는 팀원이 Discord에서 직접 배포를 승인/거부한다.
    """

    def __init__(
        self,
        deploy_payload: dict[str, Any],
        approver_ids: list[int],
        channel: discord.TextChannel,
        guild_id: int,
    ):
        super().__init__(timeout=300)  # 5분 타임아웃
        self._payload = deploy_payload
        self._approver_ids = approver_ids
        self._channel = channel
        self._guild_id = guild_id
        self._approved = False

    @ui.button(label="✅ 배포 승인", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if self._approver_ids and interaction.user.id not in self._approver_ids:
            await interaction.response.send_message(
                "⚠️ 배포 승인 권한이 없습니다. TL에게 문의하세요.", ephemeral=True
            )
            return

        self._approved = True
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore

        await interaction.response.defer()

        # 승인 알림
        approve_embed = discord.Embed(
            title="✅ 배포 승인됨",
            description=(
                f"**승인자**: {interaction.user.mention}\n"
                f"**서비스**: `{self._payload.get('service')}`\n"
                f"**이미지**: `{self._payload.get('image_tag')}`"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        await interaction.edit_original_response(embed=approve_embed, view=self)

        # 배포 실행
        await _execute_team_deploy(self._channel, self._payload, interaction.user, self._guild_id)

    @ui.button(label="❌ 거부", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if self._approver_ids and interaction.user.id not in self._approver_ids:
            await interaction.response.send_message(
                "⚠️ 배포 거부 권한이 없습니다.", ephemeral=True
            )
            return

        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore

        reject_embed = discord.Embed(
            title="❌ 배포 거부됨",
            description=(
                f"**거부자**: {interaction.user.mention}\n"
                f"**서비스**: `{self._payload.get('service')}`"
            ),
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=reject_embed, view=self)

    async def on_timeout(self) -> None:
        if not self._approved:
            for child in self.children:
                child.disabled = True  # type: ignore
            await self._channel.send(
                "⏰ 배포 승인 요청이 5분 내에 처리되지 않아 자동 취소되었습니다."
            )


async def request_team_deploy_approval(
    channel: discord.TextChannel,
    requester: discord.Member,
    deploy_payload: dict[str, Any],
    approver_ids: list[int],
    guild_id: int,
    approver_role: discord.Role | None = None,
) -> None:
    """
    팀 배포 승인 요청을 채널에 게시한다 (§37.6).

    설계서:
    - 승인자(approver_ids)만 버튼 조작 가능
    - 5분 내 미승인 시 자동 취소
    - 승인 후 즉시 배포 실행 + 팀 브로드캐스트
    """
    service = deploy_payload.get("service", "unknown")
    image_tag = deploy_payload.get("image_tag", "latest")
    cluster = deploy_payload.get("cluster", "unknown")

    embed = discord.Embed(
        title="🔔 배포 승인 요청",
        description=(
            f"**요청자**: {requester.mention}이(가) 배포 승인을 요청합니다.\n\n"
            f"**서비스**: `{service}`\n"
            f"**클러스터**: `{cluster}`\n"
            f"**이미지 태그**: `{image_tag}`"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(tz=timezone.utc),
    )

    if approver_role:
        embed.add_field(
            name="👤 승인 권한",
            value=f"{approver_role.mention} 역할 보유자",
            inline=True,
        )

    embed.add_field(
        name="⏰ 유효 시간",
        value="5분 (이후 자동 취소)",
        inline=True,
    )
    embed.set_footer(text="ReCoder 팀 협업 시나리오 §37.6")

    mention = approver_role.mention if approver_role else ""
    view = TeamDeployApprovalView(
        deploy_payload=deploy_payload,
        approver_ids=approver_ids,
        channel=channel,
        guild_id=guild_id,
    )
    await channel.send(content=mention, embed=embed, view=view)


async def _execute_team_deploy(
    channel: discord.TextChannel,
    payload: dict[str, Any],
    approver: discord.Member,
    guild_id: int,
) -> None:
    """승인된 배포를 실행하고 팀 전체에 진행 상황을 브로드캐스트한다."""
    service = payload.get("service", "unknown")

    # 시작 알림
    start_embed = discord.Embed(
        title="🚀 배포 시작",
        description=f"`{service}` 배포를 시작합니다...",
        color=discord.Color.blue(),
        timestamp=datetime.now(tz=timezone.utc),
    )
    await channel.send(embed=start_embed)

    try:
        client = get_client_for_guild(guild_id)
        result = await client.deploy(payload)
        deploy_id = result.get("deploy_id", "N/A")

        success_embed = discord.Embed(
            title="✅ 배포 완료",
            description=(
                f"**서비스**: `{service}`\n"
                f"**배포 ID**: `{deploy_id}`\n"
                f"**승인자**: {approver.mention}"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        success_embed.add_field(
            name="📊 상태 확인",
            value="`/recoder status`로 배포 결과를 확인하세요.",
            inline=False,
        )
        success_embed.set_footer(text="ReCoder 팀 협업 §37.6 | Deploy Replay §38에서 재생 가능")
        await channel.send(embed=success_embed)

    except GuildNotConfiguredError as exc:
        await channel.send(str(exc))
    except Exception as exc:
        log.error("Team deploy execution failed: %s", exc)
        fail_embed = discord.Embed(
            title="❌ 배포 실패",
            description=f"`{service}` 배포 중 오류가 발생했습니다.\n```\n{exc}\n```",
            color=discord.Color.red(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        fail_embed.add_field(
            name="⚡ 즉각 조치",
            value=f"`/recoder rollback cluster:{payload.get('cluster')} service:{service}`",
            inline=False,
        )
        await channel.send(embed=fail_embed)


async def broadcast_deploy_progress(
    channel: discord.TextChannel,
    deploy_id: str,
    stage: str,
    message: str,
    success: bool | None = None,
) -> None:
    """배포 진행 단계를 팀 채널에 브로드캐스트한다."""
    color_map = {
        True: discord.Color.green(),
        False: discord.Color.red(),
        None: discord.Color.blue(),
    }
    icon_map = {
        True: "✅",
        False: "❌",
        None: "⏳",
    }

    embed = discord.Embed(
        title=f"{icon_map[success]} 배포 진행 — {stage}",
        description=message,
        color=color_map[success],
        timestamp=datetime.now(tz=timezone.utc),
    )
    embed.add_field(name="🆔 배포 ID", value=f"`{deploy_id}`", inline=True)
    embed.set_footer(text="ReCoder 팀 협업 §37.6")
    await channel.send(embed=embed)
