"""
discord-bot/scenarios/commute.py — 시나리오 1: 출근길 (§37.4)

"VSCode 밖에서도 작동하는 ReCoder" — 핵심 차별화.
출근 중 모바일 Discord에서 배포 상태를 확인하고
긴급 코드 리뷰를 요청하는 시나리오.

흐름:
  1. 매일 출근 시간(기본 08:30)에 자동 상태 요약 전송
  2. /recoder status 로 현재 배포 상태 확인
  3. /recoder code 로 모바일에서 코드 분석 요청
  4. PR 리뷰 코멘트를 Discord에서 직접 승인/거부
"""

import logging
from datetime import datetime, timezone

import discord

from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


async def send_commute_briefing(
    channel: discord.TextChannel,
    guild_id: int,
) -> None:
    """
    출근길 자동 브리핑 — 배포 상태 + 야간 인시던트 요약.

    APScheduler 또는 Daily Standup과 연계하여 매일 08:30에 호출된다.
    """
    now = datetime.now(tz=timezone.utc)

    try:
        client = get_client_for_guild(guild_id)
        data = await client.status()
        incidents = data.get("active_incidents", [])
        last_deploy = data.get("last_deploy", {})

        embed = discord.Embed(
            title="🌅 출근길 브리핑",
            description=(
                f"**{now.strftime('%Y-%m-%d')} 오전 브리핑**\n"
                "모바일에서 ReCoder 상태를 확인하세요."
            ),
            color=discord.Color.gold(),
            timestamp=now,
        )

        # 배포 상태
        if last_deploy:
            ok = last_deploy.get("success", True)
            icon = "✅" if ok else "❌"
            embed.add_field(
                name=f"{icon} 최근 배포",
                value=(
                    f"`{last_deploy.get('service', 'unknown')}`\n"
                    f"시각: `{last_deploy.get('deployed_at', 'N/A')}`\n"
                    f"태그: `{last_deploy.get('image_tag', 'N/A')}`"
                ),
                inline=True,
            )

        # 인시던트
        if incidents:
            inc_text = "\n".join(
                f"🔴 `{i.get('id', '?')}` {i.get('title', '')}"
                for i in incidents[:3]
            )
            embed.add_field(
                name="🚨 주의! 활성 인시던트",
                value=inc_text,
                inline=False,
            )
            embed.color = discord.Color.red()
        else:
            embed.add_field(
                name="✅ 인시던트",
                value="활성 인시던트 없음",
                inline=True,
            )

        # 모바일 사용 안내
        embed.add_field(
            name="📱 모바일 명령",
            value=(
                "`/recoder status` — 상태 확인\n"
                "`/recoder code [질문]` — 코드 분석\n"
                "`/recoder preflight` — 배포 점검"
            ),
            inline=False,
        )

        embed.set_footer(text="ReCoder 출근길 시나리오 §37.4 | VSCode 없이 Discord에서 바로 사용")
        await channel.send(embed=embed)

    except GuildNotConfiguredError as exc:
        await channel.send(str(exc))
    except Exception as exc:
        log.error("Commute briefing failed: %s", exc)
        await channel.send(
            f"⚠️ 출근길 브리핑 생성 실패: `{exc}`\n"
            "Local Core가 실행 중인지 확인하세요."
        )


async def handle_mobile_code_review(
    channel: discord.TextChannel,
    user: discord.Member,
    pr_url: str,
    review_request: str,
    guild_id: int,
) -> None:
    """
    출근길 모바일 코드 리뷰 요청 처리.

    PR URL과 리뷰 요청 내용을 받아 ReCoder가 분석한 결과를 채널에 전송한다.
    """
    embed = discord.Embed(
        title="📱 모바일 코드 리뷰 요청",
        description=(
            f"**요청자**: {user.mention}\n"
            f"**PR**: {pr_url}\n\n"
            f"**요청 내용**: {review_request}"
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text="ReCoder 출근길 시나리오 §37.4")

    try:
        client = get_client_for_guild(guild_id)
        result = await client.code(
            prompt=f"PR 리뷰: {pr_url}\n{review_request}",
        )
        analysis = result.get("analysis", "분석 결과를 불러올 수 없습니다.")
        embed.add_field(
            name="🤖 ReCoder 분석",
            value=analysis[:1000],
            inline=False,
        )
        embed.color = discord.Color.green()
    except GuildNotConfiguredError as exc:
        embed.add_field(name="⚠️ 설정 필요", value=str(exc), inline=False)
        embed.color = discord.Color.orange()
    except Exception as exc:
        embed.add_field(name="❌ 분석 실패", value=str(exc), inline=False)
        embed.color = discord.Color.red()

    await channel.send(embed=embed)
