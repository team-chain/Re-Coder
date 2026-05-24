"""
discord-bot/scenarios/dawn_incident.py — 시나리오 2: 새벽 인시던트 (§37.5)

새벽 3시 알림 → Discord 알림 → 모바일에서 즉시 대응하는 시나리오.

흐름:
  1. Watchdog이 인시던트 감지 → Local Core → Discord Bot에 Webhook 전송
  2. Bot이 on-call 팀원에게 @멘션 알림
  3. /recoder status 로 상황 확인
  4. /recoder rollback 으로 즉시 롤백 (모바일에서!)
  5. 자동 RCA 생성 → Discord에 요약 게시
"""

import logging
from datetime import datetime, timezone
from typing import Any

import discord

from recoder_client import get_client_for_guild, GuildNotConfiguredError

log = logging.getLogger(__name__)


async def send_incident_alert(
    channel: discord.TextChannel,
    incident: dict[str, Any],
    oncall_role: discord.Role | None = None,
) -> discord.Message:
    """
    새벽 인시던트 알림 전송 (§37.5).

    Watchdog이 감지한 인시던트를 Discord 채널에 긴급 알림으로 전송한다.
    on-call 역할(@oncall)을 멘션하여 즉각 응답을 유도한다.
    """
    severity = incident.get("severity", "UNKNOWN")
    severity_color = {
        "CRITICAL": discord.Color.red(),
        "HIGH": discord.Color.orange(),
        "MEDIUM": discord.Color.yellow(),
        "LOW": discord.Color.green(),
    }.get(severity, discord.Color.dark_gray())

    severity_icon = {
        "CRITICAL": "🚨",
        "HIGH": "🔴",
        "MEDIUM": "🟡",
        "LOW": "🟢",
    }.get(severity, "⚪")

    embed = discord.Embed(
        title=f"{severity_icon} [{severity}] 인시던트 감지",
        description=incident.get("title", "알 수 없는 인시던트"),
        color=severity_color,
        timestamp=datetime.now(tz=timezone.utc),
    )

    embed.add_field(
        name="🆔 인시던트 ID",
        value=f"`{incident.get('id', 'N/A')}`",
        inline=True,
    )
    embed.add_field(
        name="🎯 영향 서비스",
        value=f"`{incident.get('service', 'unknown')}`",
        inline=True,
    )
    embed.add_field(
        name="⏰ 감지 시각",
        value=f"`{incident.get('detected_at', 'N/A')}`",
        inline=True,
    )

    # 영향 지표
    if metrics := incident.get("metrics"):
        metric_lines = "\n".join(
            f"• **{k}**: `{v}`" for k, v in list(metrics.items())[:5]
        )
        embed.add_field(name="📊 영향 지표", value=metric_lines, inline=False)

    # 즉각 대응 가이드
    service = incident.get("service", "my-service")
    cluster = incident.get("cluster", "my-cluster")
    embed.add_field(
        name="⚡ 즉각 대응 (모바일에서 바로 실행)",
        value=(
            f"```\n"
            f"/recoder status\n"
            f"/recoder rollback cluster:{cluster} service:{service}\n"
            f"```"
        ),
        inline=False,
    )

    embed.set_footer(
        text="ReCoder 새벽 인시던트 시나리오 §37.5 | 모바일에서 바로 롤백 가능"
    )

    # on-call 멘션
    mention_text = f"{oncall_role.mention} " if oncall_role else "@here "
    mention_text += "**긴급 인시던트 발생!** 즉각 확인이 필요합니다."

    return await channel.send(content=mention_text, embed=embed)


async def send_rca_summary(
    channel: discord.TextChannel,
    incident_id: str,
    guild_id: int,
    thread: discord.Thread | None = None,
) -> None:
    """
    인시던트 처리 후 자동 RCA 요약을 Discord에 게시한다 (§37.5).
    thread가 있으면 스레드에, 없으면 채널에 게시.
    """
    target = thread or channel

    try:
        client = get_client_for_guild(guild_id)
        timeline = await client.get_replay_timeline(incident_id)
        events = timeline.get("events", [])

        embed = discord.Embed(
            title="📋 자동 RCA 요약",
            description=f"인시던트 `{incident_id}` 처리 완료",
            color=discord.Color.blue(),
            timestamp=datetime.now(tz=timezone.utc),
        )

        # 타임라인 요약
        if events:
            timeline_text = "\n".join(
                f"`{e.get('ts', '?')}` {e.get('kind', '')} — {e.get('title', '')}"
                for e in events[:8]
            )
            embed.add_field(
                name="📈 인시던트 타임라인",
                value=timeline_text,
                inline=False,
            )

        # 원인 분석
        root_cause = timeline.get("root_cause", "분석 중...")
        embed.add_field(name="🔍 근본 원인", value=root_cause, inline=False)

        # 재발 방지
        prevention = timeline.get("prevention", "조치 검토 중...")
        embed.add_field(name="🛡️ 재발 방지", value=prevention, inline=False)

        embed.add_field(
            name="🎬 Deploy Replay",
            value="`/recoder replay` 명령으로 전체 타임라인을 재생할 수 있습니다.",
            inline=False,
        )
        embed.set_footer(text="ReCoder 자동 RCA §37.5 | Postmortem 자동 생성 §38.4")

        await target.send(embed=embed)

    except GuildNotConfiguredError as exc:
        await target.send(str(exc))
    except Exception as exc:
        log.error("RCA summary failed: %s", exc)
        await target.send(f"⚠️ RCA 요약 생성 실패: `{exc}`")


async def create_incident_thread(
    message: discord.Message,
    incident_id: str,
) -> discord.Thread:
    """인시던트 알림 메시지에 전용 토론 스레드를 생성한다."""
    thread = await message.create_thread(
        name=f"🚨 인시던트 #{incident_id[:8]}",
        auto_archive_duration=1440,  # 24시간 후 자동 아카이브
    )
    await thread.send(
        "이 스레드에서 인시던트 대응 내용을 기록하세요.\n"
        "처리 완료 후 자동으로 RCA 요약이 게시됩니다."
    )
    return thread
