"""
discord-bot/commands/code.py — /recoder code 슬래시 커맨드 (§37.3)

Discord에서 ReCoder 코드 분석/생성을 요청한다.
모바일(출근길)에서도 사용 가능한 핵심 차별화 기능.
"""


import logging

import discord
from discord import app_commands, Interaction

from middleware.auth import require_auth
from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)

_MAX_RESPONSE_LEN = 1800  # Discord 메시지 한도 2000자 미만


class CodeCommands(app_commands.Group):

    @app_commands.command(
        name="code",
        description="ReCoder에 코드 분석 또는 생성을 요청합니다 (VSCode 없이 모바일에서도 사용 가능)",
    )
    @app_commands.describe(
        prompt="코드 관련 질문 또는 요청 (예: 'main.py의 성능 문제 분석해줘')",
        project_path="프로젝트 경로 (생략 시 현재 활성 프로젝트)",
    )
    @require_auth
    async def code(
        self,
        interaction: Interaction,
        prompt: str,
        project_path: str = ".",
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            client = get_client_for_guild(interaction.guild_id)
            result = await client.code(prompt=prompt, project_path=project_path)
            embeds = _build_code_embeds(prompt, result)
            await interaction.followup.send(embeds=embeds)

        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            log.error("Code command failed: %s", exc)
            await interaction.followup.send(
                f"❌ 코드 분석 실패:\n```\n{exc}\n```",
                ephemeral=True,
            )


def _build_code_embeds(prompt: str, result: dict) -> list[discord.Embed]:
    embeds = []

    req_embed = discord.Embed(
        title="💻 코드 분석 요청",
        description=f"```\n{prompt[:500]}\n```",
        color=discord.Color.blue(),
    )
    req_embed.set_footer(text="ReCoder Code Agent §37 | VSCode 없이 모바일에서도 사용 가능")
    embeds.append(req_embed)

    analysis = result.get("analysis") or result.get("result") or str(result)
    if len(analysis) > _MAX_RESPONSE_LEN:
        chunks = [
            analysis[i: i + _MAX_RESPONSE_LEN]
            for i in range(0, len(analysis), _MAX_RESPONSE_LEN)
        ]
        for idx, chunk in enumerate(chunks[:3], 1):
            resp_embed = discord.Embed(
                title=f"📋 분석 결과 ({idx}/{min(len(chunks), 3)})",
                description=chunk,
                color=discord.Color.green(),
            )
            embeds.append(resp_embed)
    else:
        resp_embed = discord.Embed(
            title="📋 분석 결과",
            description=analysis,
            color=discord.Color.green(),
        )
        embeds.append(resp_embed)

    if cost := result.get("cost"):
        embeds[-1].add_field(
            name="💰 LLM 비용",
            value=f"`${cost:.4f}`",
            inline=True,
        )

    return embeds
