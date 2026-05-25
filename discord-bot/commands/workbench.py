"""
discord-bot/commands/workbench.py — Discord 안의 ReCoder Workbench GUI.

VSCode Workbench (Build / Ship / Operate / Recover 4탭) 와 동일한 인터랙션을
Discord embed + 인터랙티브 버튼으로 제공한다.

설계:
  - single source of truth = Core 의 SQLite (3-Layer)
  - Discord 봇은 Core REST API 만 호출 (recoder_client)
  - 모든 액션 결과는 SQLite 에 저장 → VSCode 도 polling 으로 같은 state 봄
  - WorkbenchMainView 가 메인 menu (4탭)
  - 각 탭마다 sub-view 가 액션 버튼 제공

UI 흐름:
    /recoder workbench
        ↓ embed + Build/Ship/Operate/Recover 4개 버튼
    [Ship] 클릭
        ↓ Ship 모드 embed + Preflight 실행 / 배포 시작 / Rollback 버튼
    [Preflight 실행] 클릭
        ↓ Core /workbench/preflight/run POST → 결과 embed (blockers/warnings/score)
    [VSCode 도 같은 state 받음] (polling)
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import discord
from discord import app_commands

from recoder_client import GuildNotConfiguredError, get_client_for_guild
from middleware.auth import _get_deny_message, is_allowed

log = logging.getLogger(__name__)


Mode = Literal["home", "build", "ship", "operate", "recover"]


# ---------------------------------------------------------------------------
# Embed 빌더
# ---------------------------------------------------------------------------


_COLOR_MODE: dict[Mode, discord.Color] = {
    "home":    discord.Color.blurple(),
    "build":   discord.Color.red(),       # 에러 분석
    "ship":    discord.Color.blue(),      # 배포
    "operate": discord.Color.green(),     # 운영
    "recover": discord.Color.orange(),    # rollback
}


def _build_state_embed(state: dict, mode: Mode = "home") -> discord.Embed:
    """state 응답 → embed."""
    color = _COLOR_MODE.get(mode, discord.Color.blurple())

    if mode == "home":
        title = "ReCoder Workbench"
        desc = "Discord 에서 직접 검증 · 배포 · 롤백 · 분석을 실행. VSCode 사이드바와 실시간 동기화됩니다."
    elif mode == "build":
        title = "Workbench · Build"
        desc = "에러 분석 + 코드 수정 제안. 마지막 검증 결과를 확인하세요."
    elif mode == "ship":
        title = "Workbench · Ship"
        desc = "Preflight → Remediation → 배포 흐름. blocker 가 있으면 자동 수정 제안."
    elif mode == "operate":
        title = "Workbench · Operate"
        desc = "운영 모니터링 · CV (5분 감시) · auto-rollback 제안."
    else:  # recover
        title = "Workbench · Recover"
        desc = "Rollback 후보 + IncidentMemory 매칭 + 과거 fix 자동 제안."

    embed = discord.Embed(title=title, description=desc, color=color)

    # 상태 요약
    blockers = state.get("blockers_count", 0)
    warnings = state.get("warnings_count", 0)
    deps_24h = state.get("deployments_24h", 0)
    rollback_avail = state.get("rollback_available", False)

    embed.add_field(
        name="검증 상태",
        value=(
            f"Blockers: **{blockers}** · Warnings: **{warnings}**\n"
            f"24h 배포: **{deps_24h}** · Rollback: **{'O' if rollback_avail else 'X'}**"
        ),
        inline=False,
    )

    # 마지막 Preflight
    last_pre = state.get("last_preflight")
    if last_pre:
        status = last_pre.get("status", "unknown")
        score = last_pre.get("score", 0)
        embed.add_field(
            name="마지막 Preflight",
            value=f"{status} · score {score}/100 · `{last_pre.get('preflight_run_id', '?')[:12]}`",
            inline=True,
        )

    # 마지막 Deployment
    last_dep = state.get("last_deployment")
    if last_dep:
        status = last_dep.get("status", "unknown")
        embed.add_field(
            name="마지막 배포",
            value=f"{status} · `{last_dep.get('deployment_id', '?')[:12]}`",
            inline=True,
        )

    # 최근 이벤트 (양방향 sync 보여주기)
    events = state.get("recent_events", [])[-3:]
    if events:
        evt_lines = []
        for e in events:
            kind = e.get("kind", "?")
            src = e.get("source", "?")
            evt_lines.append(f"`{kind}` ← {src}")
        embed.add_field(name="최근 이벤트", value="\n".join(evt_lines), inline=False)

    embed.set_footer(text=f"Single source of truth: Core SQLite · as_of {state.get('as_of', '')[:19]}")
    return embed


# ---------------------------------------------------------------------------
# Main View — 4탭 메뉴
# ---------------------------------------------------------------------------


class WorkbenchMainView(discord.ui.View):
    """홈 화면. 4탭 버튼."""

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id

    @discord.ui.button(label="Build", style=discord.ButtonStyle.danger, emoji="🔨", row=0)
    async def build_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await _switch_mode(interaction, "build", self.guild_id)

    @discord.ui.button(label="Ship", style=discord.ButtonStyle.primary, emoji="📦", row=0)
    async def ship_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await _switch_mode(interaction, "ship", self.guild_id)

    @discord.ui.button(label="Operate", style=discord.ButtonStyle.success, emoji="📊", row=0)
    async def operate_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await _switch_mode(interaction, "operate", self.guild_id)

    @discord.ui.button(label="Recover", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def recover_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await _switch_mode(interaction, "recover", self.guild_id)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, emoji="🔁", row=1)
    async def refresh_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await _refresh_home(interaction, self.guild_id)


# ---------------------------------------------------------------------------
# Sub Views — 각 모드별 액션 버튼
# ---------------------------------------------------------------------------


class _BackButton(discord.ui.Button):
    def __init__(self, guild_id: int):
        super().__init__(label="홈으로", style=discord.ButtonStyle.secondary, emoji="🏠", row=2)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        await _switch_mode(interaction, "home", self.guild_id)


class BuildModeView(discord.ui.View):
    """Build 모드 — 에러 분석 + 코드 수정."""

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="코드 분석", style=discord.ButtonStyle.danger, emoji="🧠", row=0)
    async def analyze_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_message(
            "코드 분석은 `/recoder code <prompt>` 명령을 사용하세요. (모달 입력 권장)",
            ephemeral=True,
        )

    @discord.ui.button(label="최근 PatchProposal", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def recent_patches_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_message(
            "최근 PatchProposal 조회는 v2 에서 지원. 현재는 VSCode 사이드바에서 확인하세요.",
            ephemeral=True,
        )


class ShipModeView(discord.ui.View):
    """Ship 모드 — Preflight → Remediation → 배포."""

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="Preflight 실행", style=discord.ButtonStyle.primary, emoji="🛫", row=0)
    async def preflight_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            res = await client.workbench_preflight_run(source="discord")
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Preflight 실패: `{e}`", ephemeral=True)
            return

        status = res.get("status", "?")
        score = res.get("score", 0)
        blockers = res.get("blockers", [])
        warnings = res.get("warnings", [])

        color = (
            discord.Color.red() if status == "BLOCKED"
            else discord.Color.yellow() if status == "WARN"
            else discord.Color.green()
        )
        embed = discord.Embed(
            title=f"Preflight 결과 · {status}",
            description=f"Score **{score}/100** · `{res.get('preflight_run_id', '?')[:12]}`",
            color=color,
        )
        if blockers:
            blines = [f"• {b.get('code', '?')} ({b.get('severity', '?')})" for b in blockers[:5]]
            embed.add_field(name=f"Blockers ({len(blockers)})", value="\n".join(blines), inline=False)
        if warnings:
            wlines = [f"• {w.get('code', '?')} ({w.get('severity', '?')})" for w in warnings[:5]]
            embed.add_field(name=f"Warnings ({len(warnings)})", value="\n".join(wlines), inline=False)
        embed.set_footer(text="VSCode 사이드바에도 동일 결과가 표시됩니다 (single source of truth)")
        await interaction.followup.send(embed=embed)

    @discord.ui.button(label="배포 시작", style=discord.ButtonStyle.success, emoji="🚀", row=0)
    async def deploy_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            res = await client.workbench_deployment_start(source="discord")
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"배포 시작 실패: `{e}`", ephemeral=True)
            return

        dep_id = res.get("deployment_id", "?")
        embed = discord.Embed(
            title="배포 시작",
            description=f"Deployment `{dep_id[:12]}` (status: deploying)",
            color=discord.Color.blue(),
        )
        embed.set_footer(text="진행 상황은 Operate 모드에서 모니터링 · CV 5분 감시 자동 시작")
        await interaction.followup.send(embed=embed)


class OperateModeView(discord.ui.View):
    """Operate 모드 — 모니터링 / CV 결과 / incident."""

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="최근 배포 (10건)", style=discord.ButtonStyle.success, emoji="📋", row=0)
    async def list_deployments_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            deps = await client.workbench_list_deployments(limit=10)
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"조회 실패: `{e}`", ephemeral=True)
            return

        if not deps:
            await interaction.followup.send("최근 배포가 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(title="최근 배포 (10건)", color=discord.Color.green())
        lines = []
        for d in deps[:10]:
            status = d.get("status", "?")
            did = d.get("deployment_id", "?")[:12]
            created = d.get("created_at", "")[:19]
            lines.append(f"`{did}` · {status} · {created}")
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)


class RecoverModeView(discord.ui.View):
    """Recover 모드 — rollback + IncidentMemory."""

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="마지막 배포 Rollback", style=discord.ButtonStyle.danger, emoji="⏪", row=0)
    async def rollback_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            state = await client.workbench_state()
            last_dep = state.get("last_deployment")
            if not last_dep:
                await interaction.followup.send("rollback 대상 배포가 없습니다.", ephemeral=True)
                return
            dep_id = last_dep.get("deployment_id")
            res = await client.workbench_rollback(dep_id, source="discord")
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Rollback 실패: `{e}`", ephemeral=True)
            return

        embed = discord.Embed(
            title="Rollback 실행",
            description=f"Deployment `{res.get('deployment_id', '?')[:12]}` → ROLLED_BACK",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="VSCode 사이드바에도 동일 결과 반영")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_MODE_VIEW: dict[Mode, type[discord.ui.View]] = {
    "build":   BuildModeView,
    "ship":    ShipModeView,
    "operate": OperateModeView,
    "recover": RecoverModeView,
}


async def _switch_mode(interaction: discord.Interaction, mode: Mode, guild_id: int) -> None:
    """모드 전환 — Core 에 알리고 embed/view 업데이트."""
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    try:
        client = get_client_for_guild(guild_id)
        await client.workbench_change_mode(mode, source="discord")
        state = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("workbench mode 전환 실패")
        await interaction.response.send_message(f"오류: `{e}`", ephemeral=True)
        return

    embed = _build_state_embed(state, mode=mode)
    if mode == "home":
        view = WorkbenchMainView(guild_id=guild_id)
    else:
        view_cls = _MODE_VIEW[mode]
        view = view_cls(guild_id=guild_id)

    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


async def _refresh_home(interaction: discord.Interaction, guild_id: int) -> None:
    """홈 새로고침 — state 다시 받음."""
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    try:
        client = get_client_for_guild(guild_id)
        state = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(f"오류: `{e}`", ephemeral=True)
        return

    embed = _build_state_embed(state, mode="home")
    view = WorkbenchMainView(guild_id=guild_id)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Slash command entry
# ---------------------------------------------------------------------------


async def workbench_command(interaction: discord.Interaction) -> None:
    """/recoder workbench — Discord 안의 ReCoder Workbench 시작."""
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=False)

    try:
        client = get_client_for_guild(interaction.guild_id)
        state = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("workbench 초기 state 조회 실패")
        await interaction.followup.send(f"Core API 호출 실패: `{e}`", ephemeral=True)
        return

    embed = _build_state_embed(state, mode="home")
    view = WorkbenchMainView(guild_id=interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view)
