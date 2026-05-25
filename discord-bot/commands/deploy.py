"""
discord-bot/commands/deploy.py — /recoder deploy 슬래시 커맨드 (§37.3)

ECS 배포를 Discord에서 트리거한다.
Modal 기반 확인창을 통해 실수 배포를 방지한다.
"""


import logging

import discord
from discord import app_commands, Interaction, ui

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


class DeployConfirmModal(ui.Modal, title="배포 확인"):
    """배포 전 최종 확인 Modal — 실수 배포 방지."""

    confirm = ui.TextInput(
        label="'DEPLOY'를 입력하여 배포를 확인하세요",
        placeholder="DEPLOY",
        max_length=6,
    )

    def __init__(self, cluster: str, service: str, image_tag: str, region: str, guild_id: int):
        super().__init__()
        self._cluster = cluster
        self._service = service
        self._image_tag = image_tag
        self._region = region
        self._guild_id = guild_id

    async def on_submit(self, interaction: Interaction) -> None:
        if self.confirm.value.strip().upper() != "DEPLOY":
            await interaction.response.send_message(
                "❌ 'DEPLOY'를 정확히 입력해야 배포가 진행됩니다. 취소되었습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self._guild_id)
            result = await client.deploy({
                "cluster": self._cluster,
                "service": self._service,
                "image_tag": self._image_tag,
                "region": self._region,
            })

            embed = discord.Embed(
                title="🚀 배포 시작됨",
                description=(
                    f"**서비스**: `{self._service}`\n"
                    f"**클러스터**: `{self._cluster}`\n"
                    f"**이미지 태그**: `{self._image_tag}`\n"
                    f"**리전**: `{self._region}`"
                ),
                color=discord.Color.blue(),
            )

            deploy_id = result.get("deploy_id", "N/A")
            embed.add_field(name="🆔 배포 ID", value=f"`{deploy_id}`", inline=True)
            embed.add_field(
                name="📌 상태 확인",
                value="`/recoder status`로 진행 상황을 확인하세요",
                inline=False,
            )
            embed.set_footer(text=f"요청자: {interaction.user.display_name} | §37")

            await interaction.followup.send(embed=embed)

        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            log.error("Deploy failed: %s", exc)
            await interaction.followup.send(
                f"❌ 배포 실패:\n```\n{exc}\n```",
                ephemeral=True,
            )


class DeployCommands(app_commands.Group):

    @app_commands.command(name="deploy", description="ECS 서비스 배포를 실행합니다")
    @app_commands.describe(
        cluster="ECS 클러스터 이름",
        service="ECS 서비스 이름",
        image_tag="Docker 이미지 태그 (예: v1.2.3, latest)",
        region="AWS 리전 (기본: ap-northeast-2)",
    )
    @require_auth
    async def deploy(
        self,
        interaction: Interaction,
        cluster: str,
        service: str,
        image_tag: str = "latest",
        region: str = "ap-northeast-2",
    ) -> None:
        """배포 전 Modal 확인창을 띄운다."""
        modal = DeployConfirmModal(
            cluster=cluster,
            service=service,
            image_tag=image_tag,
            region=region,
            guild_id=interaction.guild_id,
        )
        await interaction.response.send_modal(modal)
