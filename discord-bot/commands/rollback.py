"""
discord-bot/commands/rollback.py — /recoder rollback 슬래시 커맨드 (§37.3)

ECS 이전 리비전으로 롤백을 실행한다.
"""

import logging

import discord
from discord import app_commands, Interaction, ui

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


class RollbackConfirmView(ui.View):
    """롤백 확인 버튼 View."""

    def __init__(
        self,
        cluster: str,
        service: str,
        target_revision: int | None,
        requester_id: int,
        guild_id: int,
    ):
        super().__init__(timeout=60)
        self._cluster = cluster
        self._service = service
        self._target_revision = target_revision
        self._requester_id = requester_id
        self._guild_id = guild_id

    @ui.button(label="✅ 롤백 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: ui.Button) -> None:
        if interaction.user.id != self._requester_id:
            await interaction.response.send_message(
                "⚠️ 롤백을 요청한 사용자만 확인할 수 있습니다.", ephemeral=True
            )
            return

        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore

        await interaction.response.defer()

        try:
            client = get_client_for_guild(self._guild_id)
            result = await client.rollback(
                cluster=self._cluster,
                service=self._service,
                target_revision=self._target_revision,
            )

            embed = discord.Embed(
                title="↩️ 롤백 실행됨",
                description=(
                    f"**서비스**: `{self._service}`\n"
                    f"**클러스터**: `{self._cluster}`\n"
                    f"**타겟 리비전**: `{self._target_revision or '이전 버전'}`"
                ),
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="🆔 롤백 ID",
                value=f"`{result.get('rollback_id', 'N/A')}`",
                inline=True,
            )
            embed.add_field(
                name="📊 상태",
                value=f"`{result.get('status', 'in_progress')}`",
                inline=True,
            )
            embed.set_footer(text=f"실행자: {interaction.user.display_name} | §37")
            await interaction.edit_original_response(embed=embed, view=self)

        except GuildNotConfiguredError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
        except Exception as exc:
            log.error("Rollback failed: %s", exc)
            await interaction.edit_original_response(
                content=f"❌ 롤백 실패:\n```\n{exc}\n```",
                view=None,
            )

    @ui.button(label="❌ 취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="롤백이 취소되었습니다.", embed=None, view=None
        )


class RollbackCommands(app_commands.Group):

    @app_commands.command(name="rollback", description="ECS 서비스를 이전 리비전으로 롤백합니다")
    @app_commands.describe(
        cluster="ECS 클러스터 이름",
        service="ECS 서비스 이름",
        target_revision="롤백할 리비전 번호 (생략 시 이전 버전)",
    )
    @require_auth
    async def rollback(
        self,
        interaction: Interaction,
        cluster: str,
        service: str,
        target_revision: int | None = None,
    ) -> None:
        rev_str = str(target_revision) if target_revision else "이전 버전"
        embed = discord.Embed(
            title="⚠️ 롤백 확인",
            description=(
                f"**서비스**: `{service}`를 `{rev_str}`으로 롤백하려 합니다.\n\n"
                "이 작업은 현재 트래픽에 즉시 영향을 줄 수 있습니다.\n"
                "계속하시겠습니까?"
            ),
            color=discord.Color.yellow(),
        )

        view = RollbackConfirmView(
            cluster=cluster,
            service=service,
            target_revision=target_revision,
            requester_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
