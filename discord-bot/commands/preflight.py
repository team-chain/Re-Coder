"""
discord-bot/commands/preflight.py — /recoder preflight 슬래시 커맨드 (§37.3)

사용 예:
  /recoder preflight cluster:my-cluster service:my-service region:ap-northeast-2
"""

import logging

import discord
from discord import app_commands, Interaction

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)

_RISK_EMOJI = {
    "LOW": "🟢",
    "MEDIUM": "🟡",
    "HIGH": "🔴",
    "CRITICAL": "🚨",
}


class PreflightCommands(app_commands.Group):
    """§37.3 preflight 커맨드 그룹."""

    @app_commands.command(name="preflight", description="ECS 배포 전 AWS 리소스 사전 점검을 실행합니다")
    @app_commands.describe(
        cluster="ECS 클러스터 이름",
        service="ECS 서비스 이름",
        region="AWS 리전 (기본: ap-northeast-2)",
        task_definition="태스크 정의 패밀리 이름 (선택)",
    )
    @require_auth
    async def preflight(
        self,
        interaction: Interaction,
        cluster: str,
        service: str,
        region: str = "ap-northeast-2",
        task_definition: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            client = get_client_for_guild(interaction.guild_id)
            result = await client.preflight(
                cluster=cluster,
                service=service,
                region=region,
                task_definition=task_definition or "",
            )
            embed = _build_preflight_embed(result, cluster, service, region)
            await interaction.followup.send(embed=embed)

        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            log.error("Preflight failed: %s", exc)
            await interaction.followup.send(
                f"❌ Preflight 실행 중 오류가 발생했습니다:\n```\n{exc}\n```\n"
                "ReCoder Core가 실행 중인지 확인하세요.",
                ephemeral=True,
            )


def _build_preflight_embed(
    result: dict, cluster: str, service: str, region: str
) -> discord.Embed:
    checks = result.get("checks", [])
    passed = sum(1 for c in checks if c.get("passed"))
    total = len(checks)

    overall_ok = result.get("overall_passed", passed == total)
    color = discord.Color.green() if overall_ok else discord.Color.red()
    status_icon = "✅" if overall_ok else "❌"

    embed = discord.Embed(
        title=f"{status_icon} Preflight 결과 — {service}",
        description=f"**클러스터**: `{cluster}` | **리전**: `{region}`\n"
                    f"**통과**: {passed}/{total}",
        color=color,
    )

    for check in checks[:10]:
        icon = "✅" if check.get("passed") else "❌"
        name = check.get("name", "unknown")
        message = check.get("message", "")
        risk = check.get("risk_level", "LOW")
        risk_icon = _RISK_EMOJI.get(risk, "⚪")

        embed.add_field(
            name=f"{icon} {name} {risk_icon}",
            value=message or "OK",
            inline=False,
        )

    if not overall_ok:
        embed.add_field(
            name="⚠️ 권장 조치",
            value="배포 진행 전 위 항목을 확인하세요.\n"
                  "`/recoder deploy`는 preflight 통과 후 사용하세요.",
            inline=False,
        )

    embed.set_footer(text="ReCoder Preflight §37 | 읽기 전용 IAM 점검")
    return embed
