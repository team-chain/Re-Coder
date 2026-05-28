"""
discord-bot/bot.py — ReCoder Discord Bot 메인 진입점 (설계서 §37)

"VSCode 밖에서도 작동하는 ReCoder" — 시장 차별화 핵심.

[SaaS 봇 모드]
  개발자가 봇 하나를 운영하고, 사용자는 서버에 초대하는 방식.
  - 봇 토큰 하나만으로 여러 서버 동시 지원
  - 서버별 ReCoder API 설정은 /recoder setup api 로 관리
  - 서버별 허용 역할은 /recoder setup role 로 관리
  - 서버 설정은 guild_config.db (SQLite) 에 저장

지원 기능:
  § 37.3  슬래시 커맨드: /recoder preflight, /status, /deploy, /rollback, /code
  § 37.3  관리 커맨드: /recoder setup api | channel | role | status
  § 37.4  시나리오 1: 출근길 자동 브리핑
  § 37.5  시나리오 2: 새벽 인시던트 알림 + RCA 자동 생성
  § 37.6  시나리오 3: 팀 협업 배포 승인 워크플로
  § 39    Daily Standup 자동 브리핑

실행 방법:
  1. .env 파일 설정 (.env.example 참고) — DISCORD_BOT_TOKEN 필수
  2. pip install -r requirements.txt
  3. python bot.py
  4. 봇을 서버에 초대: /recoder invite 로 초대 URL 확인
  5. 각 서버 관리자가 /recoder setup api 로 설정
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import discord
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import guild_store
# NOTE: PreflightCommands / StatusCommands / DeployCommands / RollbackCommands /
# CodeCommands 클래스는 미래 리팩토링(Group 등록 단순화) 용으로 commands/ 에
# 정의돼 있지만, 본 봇은 _register_commands() 안에서 데코레이터로 직접
# 등록하므로 import 하지 않는다.
from commands import (
    SetupGroup,
    invite_command,
)
from scenarios.commute import send_commute_briefing
from recoder_client import get_client_for_guild, GuildNotConfiguredError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("recoder-bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("recoder-bot")

# ── 필수 설정 ────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
if not DISCORD_TOKEN:
    log.critical("DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
    sys.exit(1)

# 개발용: 특정 서버에만 커맨드를 즉시 동기화할 때 설정 (선택)
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0)

STANDUP_CRON = os.getenv("STANDUP_CRON", "0 9 * * 1-5")  # 평일 오전 9시
BOT_HTTP_PORT = int(os.getenv("BOT_HTTP_PORT", "8765"))  # VSCode 자동 등록 API 포트

# ── Intents ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

_scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


# ── Bot 클래스 ────────────────────────────────────────────────────────────────
class RecoderBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        """슬래시 커맨드 등록 및 스케줄러 시작 (§37.3)."""

        # 슬래시 커맨드 전역 에러 핸들러 등록 — discord.py 2.x 정식 API.
        # 데코레이터로 정의된 모든 /recoder * 명령에서 발생한 예외 (인증 실패,
        # AppCommandError, 그리고 우리가 핸들러 안에서 followup 으로 안내하지
        # 못한 예상치 못한 예외) 가 모두 여기로 흘러들어와 사용자에게
        # 보이는 ephemeral 메시지로 전달된다.
        async def _tree_error_handler(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            log.error(
                "슬래시 커맨드 오류: %s (user=%s, guild=%s)",
                error,
                getattr(interaction.user, "id", "?"),
                interaction.guild.id if interaction.guild else "DM",
            )
            msg = f"❌ 오류 발생: `{error}`"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException as exc:
                log.warning("에러 메시지 전송 실패: %s", exc)

        self.tree.on_error = _tree_error_handler

        # recoder 최상위 그룹 생성
        recoder_group = app_commands.Group(name="recoder", description="ReCoder ChatOps 명령")

        # 기능 커맨드 등록
        _register_commands(recoder_group)

        # setup 서브그룹 등록
        recoder_group.add_command(SetupGroup())

        # /recoder invite 커맨드 등록
        @recoder_group.command(name="invite", description="ReCoder 봇 초대 URL을 확인합니다")
        async def _invite(interaction: discord.Interaction) -> None:
            await invite_command(interaction)

        self.tree.add_command(recoder_group)

        # 커맨드 동기화
        # DEV_GUILD_ID 설정 시 해당 서버에만 즉시 동기화 (개발 편의)
        # 운영 환경에서는 전역 동기화 (최대 1시간 소요)
        if DEV_GUILD_ID:
            dev_guild = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=dev_guild)
            await self.tree.sync(guild=dev_guild)
            log.info("슬래시 커맨드를 개발 서버 %d에 즉시 동기화했습니다.", DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("슬래시 커맨드를 전역 동기화했습니다 (모든 서버, 최대 1시간 소요).")

        # Daily Standup 스케줄러 시작
        _setup_standup_scheduler(self)
        _scheduler.start()
        log.info("Standup 스케줄러 시작: %s (Asia/Seoul)", STANDUP_CRON)

        # VSCode 자동 등록 API 서버 시작 (bot 인스턴스 주입 — GitHub Webhook 전송용)
        from api_server import start_api_server
        await start_api_server(BOT_HTTP_PORT, bot=self)

        # ReCoder Bridge (모바일 → 노트북 VSCode 실시간 코드 스트리밍) 시작
        from recoder_bridge import hub as bridge_hub
        await bridge_hub.start()

    async def on_message(self, message: discord.Message) -> None:
        """지정 채널 메시지를 받으면 Bedrock 스트리밍 → VSCode 확장에 실시간 삽입."""
        try:
            from make_handler import handle_make_message
            await handle_make_message(self, message)
        except Exception as exc:
            log.exception("on_message handler error: %s", exc)

    async def on_ready(self) -> None:
        log.info(
            "ReCoder Bot 준비 완료: %s (ID: %d) | %d개 서버에서 활성화됨",
            self.user,
            self.user.id,
            len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="배포 상태 | /recoder",
            )
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """봇이 새 서버에 초대될 때 관리자에게 설정 안내를 보낸다."""
        log.info("새 서버 참가: %s (%d)", guild.name, guild.id)

        # 시스템 메시지 채널 또는 첫 번째 텍스트 채널에 안내 전송
        channel = guild.system_channel
        if channel is None:
            channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )

        if channel is None:
            return

        embed = discord.Embed(
            title="👋 ReCoder Bot이 서버에 참가했습니다!",
            description=(
                "안녕하세요! ReCoder Bot은 Discord에서 바로 **ECS 배포, 롤백, 코드 분석**을 "
                "할 수 있는 ChatOps 봇입니다.\n\n"
                "시작하려면 **서버 관리자**가 아래 단계를 따라 설정해주세요."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="⚙️ 초기 설정 (관리자 전용)",
            value=(
                "```\n"
                "/recoder setup api <ReCoder_URL> <Token>\n"
                "/recoder setup channel deploy #배포-알림\n"
                "/recoder setup role add @개발팀\n"
                "```"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 지원 커맨드",
            value=(
                "`/recoder preflight` — 배포 사전 점검\n"
                "`/recoder deploy` — ECS 배포 실행\n"
                "`/recoder rollback` — 이전 버전으로 롤백\n"
                "`/recoder status` — 현재 상태 조회\n"
                "`/recoder code` — 코드 분석/생성"
            ),
            inline=False,
        )
        embed.set_footer(text="/recoder setup status 로 설정 현황을 확인할 수 있습니다.")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("서버 %d에 웰컴 메시지 전송 실패 (권한 없음)", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """봇이 서버에서 제거될 때 해당 서버 설정을 삭제한다."""
        log.info("서버 제거: %s (%d) — 설정 삭제", guild.name, guild.id)
        guild_store.delete_guild(guild.id)

    # NOTE: discord.Client 에는 `on_application_command_error` 이벤트가 없다.
    # discord.py 2.x 에서 슬래시 커맨드 에러는 CommandTree.on_error 로만 잡힌다.
    # 실제 핸들러는 setup_hook 안에서 `self.tree.on_error = ...` 로 등록된다.


def _register_commands(group: app_commands.Group) -> None:
    """§37.3 슬래시 커맨드 5종을 recoder 그룹에 등록한다."""

    @group.command(name="preflight", description="ECS 배포 전 AWS 리소스 사전 점검")
    @app_commands.describe(
        cluster="ECS 클러스터 이름",
        service="ECS 서비스 이름",
        region="AWS 리전 (기본: ap-northeast-2)",
    )
    async def preflight_cmd(
        interaction: discord.Interaction,
        cluster: str,
        service: str,
        region: str = "ap-northeast-2",
    ) -> None:
        from commands.preflight import _build_preflight_embed
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(interaction.guild_id)
            result = await client.preflight(cluster=cluster, service=service, region=region)
            embed = _build_preflight_embed(result, cluster, service, region)
            await interaction.followup.send(embed=embed)
        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Preflight 실패: `{exc}`", ephemeral=True)

    @group.command(name="status", description="ReCoder 현재 상태 및 최근 배포 현황 조회")
    async def status_cmd(
        interaction: discord.Interaction,
        session_id: str = "",
    ) -> None:
        from commands.status import _build_status_embed
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(interaction.guild_id)
            data = await client.status(session_id=session_id or None)
            embed = _build_status_embed(data, session_id or None)
            await interaction.followup.send(embed=embed)
        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ 상태 조회 실패: `{exc}`", ephemeral=True)

    @group.command(name="deploy", description="ECS 서비스 배포 실행")
    @app_commands.describe(
        cluster="ECS 클러스터",
        service="ECS 서비스",
        image_tag="Docker 이미지 태그",
    )
    async def deploy_cmd(
        interaction: discord.Interaction,
        cluster: str,
        service: str,
        image_tag: str = "latest",
    ) -> None:
        from commands.deploy import DeployConfirmModal
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        modal = DeployConfirmModal(
            cluster=cluster,
            service=service,
            image_tag=image_tag,
            region="ap-northeast-2",
            guild_id=interaction.guild_id,
        )
        await interaction.response.send_modal(modal)

    @group.command(name="rollback", description="ECS 서비스를 이전 리비전으로 롤백")
    @app_commands.describe(
        cluster="ECS 클러스터",
        service="ECS 서비스",
    )
    async def rollback_cmd(
        interaction: discord.Interaction,
        cluster: str,
        service: str,
    ) -> None:
        from commands.rollback import RollbackConfirmView
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        embed = discord.Embed(
            title="⚠️ 롤백 확인",
            description=f"`{service}`를 이전 버전으로 롤백하시겠습니까?",
            color=discord.Color.yellow(),
        )
        view = RollbackConfirmView(
            cluster=cluster,
            service=service,
            target_revision=None,
            requester_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @group.command(
        name="code",
        description="코드 분석/생성 요청 (모바일에서도 사용 가능)",
    )
    @app_commands.describe(prompt="코드 관련 질문 또는 요청")
    async def code_cmd(
        interaction: discord.Interaction,
        prompt: str,
    ) -> None:
        from commands.code import _build_code_embeds
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(interaction.guild_id)
            result = await client.code(prompt=prompt)
            embeds = _build_code_embeds(prompt, result)
            await interaction.followup.send(embeds=embeds)
        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ 코드 분석 실패: `{exc}`", ephemeral=True)

    @group.command(
        name="workbench",
        description="Workbench GUI — VSCode 와 실시간 동기화되는 인터랙티브 대시보드",
    )
    async def workbench_cmd(interaction: discord.Interaction) -> None:
        from commands.workbench import workbench_command
        await workbench_command(interaction)

    # ── §41 Deploy Forecast ───────────────────────────────────────────────
    @group.command(
        name="forecast",
        description="배포 일기예보 — 지금 배포해도 안전한지 한눈에 확인합니다",
    )
    @app_commands.describe(
        service="대상 서비스명 (선택 — 생략 시 전체)",
        window_days="분석 기간 (기본 30일)",
    )
    async def forecast_cmd(
        interaction: discord.Interaction,
        service: str = "",
        window_days: int = 30,
    ) -> None:
        from commands.forecast import build_forecast_embed
        from middleware.auth import is_allowed

        if not is_allowed(interaction):
            from middleware.auth import _get_deny_message
            await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(interaction.guild_id)
            data = await client.get_deploy_forecast(
                service=service,
                window_days=window_days,
            )
            embed = build_forecast_embed(data)
            await interaction.followup.send(embed=embed)
        except GuildNotConfiguredError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ 배포 일기예보 조회 실패: `{exc}`", ephemeral=True
            )


def _setup_standup_scheduler(bot: RecoderBot) -> None:
    """Daily Standup을 STANDUP_CRON 스케줄에 맞게 각 서버에 전송한다 (§39)."""

    async def _send_standup() -> None:
        # 설정된 모든 서버에 standup 전송
        for guild in bot.guilds:
            channel_id = guild_store.get_channel(guild.id, "standup")
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                log.warning("Standup 채널 %d를 찾을 수 없습니다. (서버: %s)", channel_id, guild.name)
                continue

            try:
                # core/ 디렉터리를 sys.path 에 한 번만 추가 (cron 매 호출마다
                # 중복 insert 되어 모듈 캐시가 오염되는 것을 방지)
                _core_path = str(Path(__file__).parent.parent / "core")
                if _core_path not in sys.path:
                    sys.path.insert(0, _core_path)
                from standup.generator import StandupGenerator  # noqa: PLC0415
                gen = StandupGenerator()
                client = get_client_for_guild(guild.id)
                standup_data = await client.get_standup_data()
                report = await gen.generate(standup_data)

                # §41 Forecast 한 줄 통합 — Core 에 forecast 엔드포인트가 없거나
                # 실패해도 standup 자체는 항상 발송되도록 best-effort 로 처리.
                forecast_line: str = ""
                try:
                    forecast_data = await client.get_deploy_forecast()
                    from commands.forecast import build_forecast_oneline  # noqa: PLC0415
                    forecast_line = build_forecast_oneline(forecast_data)
                except Exception as fc_exc:  # noqa: BLE001
                    log.debug("Forecast 조회 실패 (서버: %s): %s", guild.name, fc_exc)

                embed = _build_standup_embed(report, forecast_line=forecast_line)
                await channel.send(embed=embed)
            except GuildNotConfiguredError:
                pass  # 미설정 서버는 건너뜀
            except Exception as exc:
                log.error("Standup 전송 실패 (서버: %s): %s", guild.name, exc)
                await send_commute_briefing(channel, guild.id)

    parts = STANDUP_CRON.split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
        _scheduler.add_job(
            _send_standup,
            "cron",
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )


def _build_standup_embed(report, forecast_line: str = "") -> discord.Embed:
    # StandupReport dataclass 또는 dict 어느 쪽이든 받을 수 있게 정규화.
    if hasattr(report, "to_dict"):
        r = report.to_dict()
    elif isinstance(report, dict):
        r = report
    else:
        r = {"summary": str(report)}

    embed = discord.Embed(
        title="📅 Daily Standup 브리핑",
        description=r.get("summary", "오늘의 운영 브리핑입니다."),
        color=discord.Color.blue(),
    )

    # §41 Forecast 한 줄 — best-effort, 실패하면 누락됨
    if forecast_line:
        embed.add_field(name="🌤️ 오늘 배포 일기예보", value=forecast_line, inline=False)

    if items := r.get("yesterday"):
        embed.add_field(name="✅ 어제 완료", value="\n".join(f"• {i}" for i in items[:5]), inline=False)
    if items := r.get("today"):
        embed.add_field(name="🎯 오늘 예정", value="\n".join(f"• {i}" for i in items[:5]), inline=False)
    if items := r.get("blockers"):
        embed.add_field(name="🚧 블로커", value="\n".join(f"• {i}" for i in items[:3]), inline=False)

    embed.set_footer(text="ReCoder Daily Standup §39 | Haiku 자동 요약 + §41 Forecast")
    return embed


# ── 진입점 ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # DB 초기화 (테이블 없으면 생성)
    guild_store.init_db()
    log.info("ReCoder Discord Bot 시작 중... (SaaS 멀티 서버 모드)")
    bot = RecoderBot()
    bot.run(DISCORD_TOKEN, log_handler=None)
