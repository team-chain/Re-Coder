"""
discord-bot/commands/workbench.py — Discord 안의 ReCoder Workbench GUI.

VSCode Workbench (Build / Ship / Operate / Recover / Replay 5탭) 와 동일한
인터랙션을 Discord embed + 인터랙티브 버튼으로 제공한다.

탭별 기능 (VSCode 동일):
  Build   — 에러 로그 모달 입력 → /api/analyze → PatchProposal embed
  Ship    — Preflight 실행 → 배포 시작
  Operate — 인시던트 조회 → AI 분석 → 원격 명령 승인
  Recover — 마지막 배포 Rollback
  Replay  — Deploy ID 입력 → 타임라인 이벤트 embed (§38)
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import discord
from discord import app_commands

from recoder_client import GuildNotConfiguredError, get_client_for_guild
from middleware.auth import _get_deny_message, is_allowed

log = logging.getLogger(__name__)

Mode = Literal["home", "build", "ship", "operate", "recover", "replay", "github"]


# ---------------------------------------------------------------------------
# Embed 빌더
# ---------------------------------------------------------------------------

_COLOR_MODE: dict[Mode, discord.Color] = {
    "home":    discord.Color.blurple(),
    "build":   discord.Color.red(),
    "ship":    discord.Color.blue(),
    "operate": discord.Color.green(),
    "recover": discord.Color.orange(),
    "replay":  discord.Color.purple(),
    "github":  discord.Color.from_rgb(36, 41, 47),  # GitHub dark
}


def _build_state_embed(state: dict, mode: Mode = "home") -> discord.Embed:
    color = _COLOR_MODE.get(mode, discord.Color.blurple())

    titles = {
        "home":    ("ReCoder Workbench",        "Discord에서 직접 검증 · 배포 · 롤백 · 분석 · 재생. VSCode와 실시간 동기화."),
        "build":   ("Workbench · Build 🔨",      "에러 로그를 붙여넣으면 AI가 원인 분석 + 코드 수정안을 제안합니다."),
        "ship":    ("Workbench · Ship 📦",        "Preflight → Remediation → 배포. blocker가 있으면 자동 수정 제안."),
        "operate": ("Workbench · Operate 📊",     "인시던트 조회 → AI 분석 → 승인 기반 원격 명령 실행."),
        "recover": ("Workbench · Recover 🔄",     "Rollback 후보 + IncidentMemory 매칭 + 과거 fix 자동 제안."),
        "replay":  ("Workbench · Replay 🎬",      "배포 이벤트를 타임라인으로 재생. Deploy ID를 입력하세요. (§38)"),
        "github":  ("Workbench · GitHub 🐙",      "Rollback PR 자동 생성 · 마지막 배포 커밋으로 빠른 롤백."),
    }
    title, desc = titles.get(mode, ("ReCoder", ""))
    embed = discord.Embed(title=title, description=desc, color=color)

    blockers  = state.get("blockers_count", 0)
    warnings  = state.get("warnings_count", 0)
    deps_24h  = state.get("deployments_24h", 0)
    rollback  = state.get("rollback_available", False)

    embed.add_field(
        name="현재 상태",
        value=(
            f"Blockers: **{blockers}** · Warnings: **{warnings}**\n"
            f"24h 배포: **{deps_24h}** · Rollback: **{'가능' if rollback else '없음'}**"
        ),
        inline=False,
    )

    last_pre = state.get("last_preflight")
    if last_pre:
        embed.add_field(
            name="마지막 Preflight",
            value=f"{last_pre.get('status','?')} · {last_pre.get('score',0)}/100 · `{str(last_pre.get('preflight_run_id','?'))[:12]}`",
            inline=True,
        )

    last_dep = state.get("last_deployment")
    if last_dep:
        embed.add_field(
            name="마지막 배포",
            value=f"{last_dep.get('status','?')} · `{str(last_dep.get('deployment_id','?'))[:12]}`",
            inline=True,
        )

    events = state.get("recent_events", [])[-3:]
    if events:
        embed.add_field(
            name="최근 이벤트",
            value="\n".join(f"`{e.get('kind','?')}` ← {e.get('source','?')}" for e in events),
            inline=False,
        )

    embed.set_footer(text=f"Single source of truth: Core SQLite · {state.get('as_of','')[:19]}")
    return embed


# ---------------------------------------------------------------------------
# §6.x GitHub Mode — 전용 임베드 빌더
#
# Discord 워크밴치를 VSCode 워크밴치 범위와 정합시키기 위해 P0/P1 핵심 흐름인
# "Rollback PR" 중심으로 단순화. ArgoCD 동기화 / 동기화 이력 카드는 P2 확장
# 카드로 분리됨(설계서 단락 367).
#
# 임베드 구성:
#   ① 마지막 배포 컨텍스트 — Rollback PR 모달의 사전입력 후보
#   ② 액션별 친절한 설명 — 무엇이 일어나는지 한 문장
# ---------------------------------------------------------------------------

_GITHUB_COLOR = discord.Color.from_rgb(36, 41, 47)   # GitHub 다크 헤더


def _build_github_embed(state: dict) -> discord.Embed:
    """GitHub 모드 전용 임베드 — 마지막 배포 컨텍스트 + Rollback PR 액션 가이드."""
    embed = discord.Embed(
        title="🐙  GitHub · Rollback Workspace",
        description=(
            "배포 직후 문제가 생겼나요?\n"
            "마지막 배포의 커밋을 자동으로 가져와 **Rollback PR** 한 번에 생성합니다."
        ),
        color=_GITHUB_COLOR,
    )

    # ── ① 마지막 배포 컨텍스트 ───────────────────────────────────────────
    last_dep = state.get("last_deployment") or {}
    service  = last_dep.get("service") or last_dep.get("service_name") or "—"
    dep_id   = str(last_dep.get("deployment_id", "")) or "—"
    status   = last_dep.get("status", "—")
    commit   = (
        last_dep.get("commit_sha")
        or last_dep.get("metadata", {}).get("commit_sha", "")
        or ""
    )
    repo = (
        last_dep.get("repo")
        or last_dep.get("metadata", {}).get("repo", "")
        or ""
    )

    commit_line = f"`{commit[:7]}`" if commit else "_(미저장 — 모달에서 직접 입력 필요)_"
    repo_line   = f"`{repo}`"      if repo   else "_(미저장 — 모달에서 직접 입력 필요)_"

    embed.add_field(
        name="📦  마지막 배포 컨텍스트",
        value=(
            f"**서비스** ·  {service}\n"
            f"**상태** ·  {status}\n"
            f"**배포 ID** ·  `{dep_id[:14]}`\n"
            f"**커밋** ·  {commit_line}\n"
            f"**리포** ·  {repo_line}"
        ),
        inline=False,
    )

    # ── ② 액션 가이드 ─────────────────────────────────────────────────────
    embed.add_field(
        name="⚡  가능한 액션",
        value=(
            "🔀  **Rollback PR**  →  위 커밋을 자동 revert 하는 PR 생성 (ADR-005)\n"
            "⚡  **빠른 롤백**  →  사전정보 자동 매핑 가이드 (모달 입력 부담 ↓)\n"
            "🔗  **GitHub에서 열기**  →  마지막 배포 커밋 페이지로 점프"
        ),
        inline=False,
    )

    embed.set_footer(text="ADR-005 · single source of truth: Core SQLite")
    return embed


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ErrorLogModal(discord.ui.Modal, title="에러 로그 입력"):
    """BuildMode — 에러 로그를 입력받아 AI 분석 요청."""

    error_log: discord.ui.TextInput = discord.ui.TextInput(
        label="에러 로그 / 스택 트레이스",
        style=discord.TextStyle.paragraph,
        placeholder="터미널 에러 메시지 또는 스택 트레이스를 붙여넣으세요...",
        required=True,
        max_length=3000,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            res = await client.code(prompt=self.error_log.value)
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"분석 실패: `{e}`", ephemeral=True)
            return

        # 결과 파싱
        proposal = res.get("proposal") or res.get("patch_proposal") or {}
        explanation = res.get("explanation") or res.get("summary") or str(res)[:800]
        diff = proposal.get("diff") or proposal.get("content") or ""
        risk = proposal.get("risk_level") or "unknown"

        color_map = {"low": discord.Color.green(), "medium": discord.Color.yellow(),
                     "high": discord.Color.red(), "critical": discord.Color(0xb71c1c)}
        color = color_map.get(str(risk).lower(), discord.Color.blurple())

        embed = discord.Embed(title="🔨 Build · 에러 분석 결과", color=color)
        embed.add_field(name="분석 요약", value=explanation[:1000], inline=False)
        if diff:
            # diff가 길면 잘라냄
            diff_preview = diff[:800] + ("\n…(생략)" if len(diff) > 800 else "")
            embed.add_field(
                name=f"수정 제안 (Risk: {risk})",
                value=f"```diff\n{diff_preview}\n```",
                inline=False,
            )
        embed.add_field(
            name="Proposal ID",
            value=f"`{proposal.get('proposal_id','N/A')}`" if proposal else "N/A",
            inline=True,
        )
        embed.set_footer(text="VSCode 사이드바에도 동일 결과 표시됩니다 (single source of truth)")
        await interaction.followup.send(embed=embed)


class RollbackPRModal(discord.ui.Modal, title="Rollback PR 생성"):
    """GitHub 탭 — Rollback PR 자동 생성 (ADR-005)."""

    repo: discord.ui.TextInput = discord.ui.TextInput(
        label="레포지토리 (owner/repo)",
        placeholder="예: team-chain/Re-Coder",
        required=True,
        max_length=100,
    )
    commit_sha: discord.ui.TextInput = discord.ui.TextInput(
        label="되돌릴 커밋 SHA",
        placeholder="예: a1b2c3d4e5f6... (최소 7자)",
        required=True,
        max_length=40,
    )
    github_token: discord.ui.TextInput = discord.ui.TextInput(
        label="GitHub Token (repo 권한 필요)",
        placeholder="ghp_xxxxxxxxxxxx",
        required=True,
        max_length=100,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        raw = self.repo.value.strip()
        if "/" not in raw:
            await interaction.followup.send("❌ 레포지토리 형식이 잘못됐어요. `owner/repo` 형식으로 입력해주세요.", ephemeral=True)
            return

        owner, repo_name = raw.split("/", 1)
        try:
            client = get_client_for_guild(self.guild_id)
            record = await client.gitops_rollback_pr(
                repo_owner=owner,
                repo_name=repo_name,
                target_commit_sha=self.commit_sha.value.strip(),
                github_token=self.github_token.value.strip(),
            )
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Rollback PR 생성 실패: `{e}`", ephemeral=True)
            return

        pr_id   = record.get("pr_id", "?")
        status  = record.get("status", "pending")
        pr_url  = record.get("pr_url") or ""
        branch  = record.get("revert_branch", "?")

        embed = discord.Embed(
            title="🔀 Rollback PR 생성 요청",
            color=discord.Color.from_rgb(36, 41, 47),
        )
        embed.add_field(name="레포지토리", value=f"`{raw}`", inline=True)
        embed.add_field(name="커밋 SHA", value=f"`{self.commit_sha.value[:12]}`", inline=True)
        embed.add_field(name="Revert 브랜치", value=f"`{branch}`", inline=True)
        embed.add_field(name="상태", value=f"`{status}`", inline=True)
        embed.add_field(name="PR ID", value=f"`{pr_id}`", inline=True)
        if pr_url:
            embed.add_field(name="PR URL", value=f"[바로가기]({pr_url})", inline=True)
        embed.set_footer(text="ADR-005: 프로덕션 rollback = Git revert PR 기본 경로 | Level 3 승인(2인) 필요")
        await interaction.followup.send(embed=embed)


# NOTE: ArgoCDSyncModal 은 P2 확장 카드로 분리되어 Discord UI 에서 제거되었습니다.
#       Core 측 `gitops_agent.py` 및 `recoder_client.gitops_argocd_sync*` 메서드는
#       나중에 ArgoCD 통합을 재도입할 때를 대비하여 그대로 보존합니다.
#       설계서 단락 367 — "ArgoCD 연동을 통한 GitOps 통합 ... 은 시장 확장
#       단계의 과제로 분류된다." (P2)


class ReplayModal(discord.ui.Modal, title="Deploy Replay"):
    """Replay 모드 — Deploy ID 입력."""

    deploy_id: discord.ui.TextInput = discord.ui.TextInput(
        label="Deploy ID",
        placeholder="예: dep_e09cdf77",
        required=True,
        max_length=64,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        deploy_id = self.deploy_id.value.strip()
        try:
            client = get_client_for_guild(self.guild_id)
            timeline = await client.get_replay_timeline(deploy_id)
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"타임라인 조회 실패: `{e}`", ephemeral=True)
            return

        events = timeline.get("events", [])
        duration = timeline.get("duration_seconds", 0)
        service  = timeline.get("service", "unknown")

        KIND_ICON = {
            "DEPLOY_START": "🚀", "APPROVAL": "✅", "ROLLBACK": "↩️",
            "INCIDENT": "🚨", "LLM_CALL": "🤖", "GIT_COMMIT": "📝",
            "METRIC_SPIKE": "📈",
        }

        embed = discord.Embed(
            title=f"🎬 Deploy Replay · {service}",
            description=(
                f"**Deploy ID**: `{deploy_id}`\n"
                f"**소요**: {int(duration)}초 · **이벤트**: {len(events)}개\n"
                f"OTel: {'✅' if timeline.get('otel_available') else '⚪ 미연결'}"
            ),
            color=discord.Color.purple(),
        )

        # 이벤트 타임라인 (최대 15개)
        if events:
            lines = []
            for ev in events[:15]:
                icon = KIND_ICON.get(ev.get("kind", ""), "•")
                ts   = str(ev.get("ts", ""))[:19].split("T")[-1]
                title_text = ev.get("title", "")[:50]
                sev  = ev.get("severity", "INFO")
                sev_mark = {"CRITICAL": "🔴", "ERROR": "🟠", "WARN": "🟡"}.get(sev, "⚪")
                lines.append(f"`{ts}` {sev_mark} {icon} {title_text}")
            if len(events) > 15:
                lines.append(f"… 외 {len(events) - 15}개")
            embed.add_field(name="타임라인", value="\n".join(lines), inline=False)

        # Postmortem 요약 (앞 300자)
        postmortem = timeline.get("postmortem_md", "")
        if postmortem:
            embed.add_field(
                name="Postmortem 요약 (§38.4)",
                value=postmortem[:300] + ("…" if len(postmortem) > 300 else ""),
                inline=False,
            )

        embed.set_footer(text="VSCode의 Replay 탭에서 0.5x/1x/2x 속도로 전체 재생 가능")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Main View — 5탭 메뉴
# ---------------------------------------------------------------------------

class WorkbenchMainView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id

    @discord.ui.button(label="Build", style=discord.ButtonStyle.danger, emoji="🔨", row=0)
    async def build_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "build", self.guild_id)

    @discord.ui.button(label="Ship", style=discord.ButtonStyle.primary, emoji="📦", row=0)
    async def ship_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "ship", self.guild_id)

    @discord.ui.button(label="Operate", style=discord.ButtonStyle.success, emoji="📊", row=0)
    async def operate_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "operate", self.guild_id)

    @discord.ui.button(label="Recover", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def recover_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "recover", self.guild_id)

    @discord.ui.button(label="Replay", style=discord.ButtonStyle.secondary, emoji="🎬", row=1)
    async def replay_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "replay", self.guild_id)

    @discord.ui.button(label="GitHub", style=discord.ButtonStyle.secondary, emoji="🐙", row=1)
    async def github_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _switch_mode(interaction, "github", self.guild_id)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, emoji="🔁", row=1)
    async def refresh_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _refresh_home(interaction, self.guild_id)


# ---------------------------------------------------------------------------
# 공통 Back 버튼
# ---------------------------------------------------------------------------

class _BackButton(discord.ui.Button):
    def __init__(self, guild_id: int):
        super().__init__(label="홈으로", style=discord.ButtonStyle.secondary, emoji="🏠", row=2)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        await _switch_mode(interaction, "home", self.guild_id)


# ---------------------------------------------------------------------------
# Build Mode View — 에러 분석 (VSCode BuildMode와 동일)
# ---------------------------------------------------------------------------

class BuildModeView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="에러 로그 분석", style=discord.ButtonStyle.danger, emoji="🧠", row=0)
    async def analyze_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        """모달로 에러 로그 입력 → /api/analyze → PatchProposal embed."""
        await interaction.response.send_modal(ErrorLogModal(self.guild_id))

    @discord.ui.button(label="빠른 코드 분석", style=discord.ButtonStyle.secondary, emoji="⚡", row=0)
    async def quick_code_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "빠른 분석: `/recoder code <질문>` 명령어를 사용하세요.\n"
            "예) `/recoder code 왜 ImportError가 나는지 설명해줘`",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Ship Mode View — Preflight + 배포 (VSCode ShipMode와 동일)
# ---------------------------------------------------------------------------

class ShipModeView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="Preflight 실행", style=discord.ButtonStyle.primary, emoji="🛫", row=0)
    async def preflight_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
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

        status   = res.get("status", "?")
        score    = res.get("score", 0)
        blockers = res.get("blockers", [])
        warnings = res.get("warnings", [])

        color = (discord.Color.red() if status == "BLOCKED"
                 else discord.Color.yellow() if status == "WARN"
                 else discord.Color.green())
        embed = discord.Embed(
            title=f"🛫 Preflight · {status}",
            description=f"Score **{score}/100** · `{str(res.get('preflight_run_id','?'))[:12]}`",
            color=color,
        )
        if blockers:
            embed.add_field(
                name=f"🚫 Blockers ({len(blockers)})",
                value="\n".join(f"• `{b.get('code','?')}` ({b.get('severity','?')})" for b in blockers[:5]),
                inline=False,
            )
        if warnings:
            embed.add_field(
                name=f"⚠️ Warnings ({len(warnings)})",
                value="\n".join(f"• `{w.get('code','?')}` ({w.get('severity','?')})" for w in warnings[:5]),
                inline=False,
            )
        embed.set_footer(text="VSCode 사이드바에도 동일 결과 표시 (single source of truth)")
        await interaction.followup.send(embed=embed)

    @discord.ui.button(label="배포 시작", style=discord.ButtonStyle.success, emoji="🚀", row=0)
    async def deploy_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
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
            title="🚀 배포 시작",
            description=f"Deployment `{str(dep_id)[:12]}` · status: **deploying**",
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Operate 모드에서 진행 상황 모니터링 가능")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Operate Mode View — 인시던트 조회 + AI 분석 (VSCode OperateMode와 동일)
# ---------------------------------------------------------------------------

class OperateModeView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="인시던트 조회", style=discord.ButtonStyle.success, emoji="🔍", row=0)
    async def incidents_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            res = await client.status()
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"인시던트 조회 실패: `{e}`", ephemeral=True)
            return

        alerts = res.get("active_alerts") or res.get("incidents") or res.get("alerts") or []
        if not alerts:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="📊 Operate · 인시던트",
                    description="✅ 현재 활성 인시던트가 없습니다.",
                    color=discord.Color.green(),
                )
            )
            return

        embed = discord.Embed(
            title=f"📊 Operate · 인시던트 ({len(alerts)}건)",
            color=discord.Color.orange(),
        )
        for alert in alerts[:5]:
            alert_id = str(alert.get("alert_id") or alert.get("id") or "?")[:12]
            title_txt = alert.get("title") or alert.get("message") or "인시던트"
            severity  = alert.get("severity", "UNKNOWN")
            sev_icon  = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "⚪"}.get(severity, "❓")
            embed.add_field(
                name=f"{sev_icon} [{severity}] `{alert_id}`",
                value=str(title_txt)[:200],
                inline=False,
            )
        embed.set_footer(text="AI 분석 버튼으로 원인 분석 + 원격 명령 제안을 받으세요")
        await interaction.followup.send(embed=embed, view=OperateAnalyzeView(self.guild_id, alerts[:5]))

    @discord.ui.button(label="최근 배포 목록", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def deployments_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
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

        embed = discord.Embed(title="📋 최근 배포 (10건)", color=discord.Color.green())
        lines = []
        for d in deps[:10]:
            did     = str(d.get("deployment_id", "?"))[:12]
            status  = d.get("status", "?")
            created = str(d.get("created_at", ""))[:19]
            lines.append(f"`{did}` · **{status}** · {created}")
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)


class OperateAnalyzeView(discord.ui.View):
    """인시던트 AI 분석 버튼 (Operate 서브뷰)."""

    def __init__(self, guild_id: int, alerts: list, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.alerts   = alerts

    @discord.ui.button(label="첫 번째 인시던트 AI 분석", style=discord.ButtonStyle.danger, emoji="🧠", row=0)
    async def analyze_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        alert = self.alerts[0] if self.alerts else {}
        alert_id = str(alert.get("alert_id") or alert.get("id") or "unknown")
        try:
            client = get_client_for_guild(self.guild_id)
            # /api/analyze로 인시던트 내용 분석
            prompt = (
                f"인시던트 분석 요청:\n"
                f"ID: {alert_id}\n"
                f"제목: {alert.get('title') or alert.get('message','')}\n"
                f"심각도: {alert.get('severity','?')}\n"
                f"상세: {str(alert.get('detail') or alert.get('description',''))[:500]}\n\n"
                "근본 원인과 즉각 조치 방법을 알려주세요."
            )
            res = await client.code(prompt=prompt)
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"AI 분석 실패: `{e}`", ephemeral=True)
            return

        explanation = res.get("explanation") or res.get("summary") or str(res)[:800]
        embed = discord.Embed(
            title=f"🧠 인시던트 AI 분석 · `{alert_id[:12]}`",
            description=explanation[:1500],
            color=discord.Color.red(),
        )
        proposal = res.get("proposal") or {}
        if proposal.get("diff"):
            embed.add_field(
                name="수정 제안",
                value=f"```diff\n{proposal['diff'][:600]}\n```",
                inline=False,
            )
        embed.set_footer(text="VSCode Operate 탭에서 승인 기반 원격 명령 실행 가능")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Recover Mode View — Rollback
# ---------------------------------------------------------------------------

class RecoverModeView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="마지막 배포 Rollback", style=discord.ButtonStyle.danger, emoji="⏪", row=0)
    async def rollback_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client = get_client_for_guild(self.guild_id)
            state   = await client.workbench_state()
            last_dep = state.get("last_deployment")
            if not last_dep:
                await interaction.followup.send("Rollback 대상 배포가 없습니다.", ephemeral=True)
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
            title="⏪ Rollback 실행",
            description=f"Deployment `{str(res.get('deployment_id','?'))[:12]}` → **ROLLED_BACK**",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="VSCode 사이드바에도 동일 결과 반영")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Replay Mode View — Deploy ID 입력 → 타임라인 (§38)
# ---------------------------------------------------------------------------

class ReplayModeView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    @discord.ui.button(label="Deploy ID 입력 → 재생", style=discord.ButtonStyle.primary, emoji="▶️", row=0)
    async def replay_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ReplayModal(self.guild_id))

    @discord.ui.button(label="마지막 배포 재생", style=discord.ButtonStyle.secondary, emoji="⏮️", row=0)
    async def last_deploy_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            client   = get_client_for_guild(self.guild_id)
            state    = await client.workbench_state()
            last_dep = state.get("last_deployment")
            if not last_dep:
                await interaction.followup.send("재생할 배포가 없습니다.", ephemeral=True)
                return
            dep_id   = last_dep.get("deployment_id", "")
            timeline = await client.get_replay_timeline(dep_id)
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"타임라인 조회 실패: `{e}`", ephemeral=True)
            return

        await _send_replay_embed(interaction, dep_id, timeline, followup=True)


# ---------------------------------------------------------------------------
# GitHub Mode View — Rollback PR + 빠른 롤백 + GitHub 점프 (P0/P1 — VSCode 워크밴치 범위와 정합)
# ---------------------------------------------------------------------------

class GitHubModeView(discord.ui.View):
    """
    GitHub · Rollback Workspace 뷰 — VSCode 워크밴치 범위와 정합 (P0/P1).

    버튼 배치:
      Row 0 ─  핵심 (Rollback PR · 빠른 롤백)
      Row 1 ─  편의 (GitHub에서 열기)
      Row 2 ─  네비게이션 (홈으로 — _BackButton)

    NOTE: ArgoCD Sync / 동기화 이력 버튼은 설계서 단락 367 의 P2 확장 카드로
    분리되었습니다. Core 측 `gitops_argocd_sync*` 메서드는 그대로 보존돼
    있어 P2 단계에서 즉시 복원 가능합니다.
    """

    def __init__(self, guild_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.add_item(_BackButton(guild_id))

    # ── Row 0 : 핵심 액션 ─────────────────────────────────────────────────
    @discord.ui.button(label="Rollback PR", style=discord.ButtonStyle.danger, emoji="🔀", row=0)
    async def rollback_pr_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        """ADR-005 · Git revert PR 자동 생성 (모달)."""
        await interaction.response.send_modal(RollbackPRModal(self.guild_id))

    @discord.ui.button(label="빠른 롤백", style=discord.ButtonStyle.secondary, emoji="⚡", row=0)
    async def quick_rollback_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        """마지막 배포의 commit SHA·repo를 자동 매핑한 Rollback PR 가이드."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            client   = get_client_for_guild(self.guild_id)
            state    = await client.workbench_state()
            last_dep = state.get("last_deployment") or {}
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 상태 조회 실패: `{e}`", ephemeral=True)
            return

        if not last_dep:
            embed = discord.Embed(
                title="⚡  빠른 롤백 — 대상 없음",
                description="아직 배포 이력이 없습니다. 먼저 **Ship 모드**에서 배포를 실행해 주세요.",
                color=discord.Color.dark_grey(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        dep_id  = str(last_dep.get("deployment_id", ""))[:14]
        service = last_dep.get("service") or last_dep.get("service_name") or "—"
        commit  = (
            last_dep.get("commit_sha")
            or last_dep.get("metadata", {}).get("commit_sha", "")
            or ""
        )
        repo = (
            last_dep.get("repo")
            or last_dep.get("metadata", {}).get("repo", "")
            or ""
        )

        embed = discord.Embed(
            title="⚡  빠른 롤백 — 사전 정보",
            description=(
                "아래 정보를 **Rollback PR** 버튼의 모달에 그대로 복사·붙여넣기 하세요."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="📦 서비스",  value=f"**{service}**",        inline=True)
        embed.add_field(name="🆔 배포 ID", value=f"`{dep_id}`",          inline=True)

        if repo:
            embed.add_field(name="📂 리포",  value=f"`{repo}`",          inline=False)
        else:
            embed.add_field(
                name="📂 리포",
                value="_(미저장)_ — 모달에 직접 입력 필요",
                inline=False,
            )

        if commit:
            embed.add_field(
                name="🔖 되돌릴 커밋 SHA",
                value=f"```\n{commit}\n```",
                inline=False,
            )
            embed.set_footer(text="✅ 자동 매핑 완료 — Rollback PR 버튼을 눌러 모달을 여세요")
        else:
            embed.add_field(
                name="🔖 되돌릴 커밋 SHA",
                value="_(저장된 SHA 없음)_ — 7자 이상의 커밋 SHA 직접 입력 필요",
                inline=False,
            )
            embed.set_footer(text="⚠️ SHA 미저장 — 수동 입력 필요")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="GitHub에서 열기", style=discord.ButtonStyle.secondary, emoji="🔗", row=1)
    async def open_github_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        """마지막 배포 repo를 GitHub에서 열기 — 브라우저로 점프."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            client = get_client_for_guild(self.guild_id)
            state  = await client.workbench_state()
        except GuildNotConfiguredError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ 상태 조회 실패: `{e}`", ephemeral=True)
            return

        last_dep = state.get("last_deployment") or {}
        repo = (
            last_dep.get("repo")
            or last_dep.get("metadata", {}).get("repo", "")
            or ""
        )
        commit = (
            last_dep.get("commit_sha")
            or last_dep.get("metadata", {}).get("commit_sha", "")
            or ""
        )

        if not repo:
            await interaction.followup.send(
                "ℹ️ 마지막 배포에 repo 정보가 저장돼 있지 않습니다.\n"
                "Ship 모드에서 배포 시 `metadata.repo` 를 함께 기록해 주세요.",
                ephemeral=True,
            )
            return

        # owner/repo 형태가 아니면 그대로 노출
        repo_url = (
            f"https://github.com/{repo}/commit/{commit}"
            if commit and "/" in repo
            else (f"https://github.com/{repo}" if "/" in repo else repo)
        )

        embed = discord.Embed(
            title="🔗  GitHub에서 열기",
            description=f"**[{repo}]({repo_url})**",
            color=_GITHUB_COLOR,
        )
        if commit:
            embed.add_field(name="🔖 커밋", value=f"`{commit[:12]}`", inline=True)
        embed.set_footer(text="새 탭에서 GitHub UI 로 점프합니다")
        await interaction.followup.send(embed=embed, ephemeral=True)


# NOTE: _build_sync_history_embed 및 _PHASE_ICON/_PHASE_LABEL_KO 상수는
#       ArgoCD 동기화 UI 와 함께 P2 확장 카드로 분리되어 제거되었습니다.
#       Core 측 gitops_list_syncs 엔드포인트는 그대로 살아있어 P2 단계에서
#       즉시 복원 가능합니다.


async def _send_replay_embed(
    interaction: discord.Interaction,
    deploy_id: str,
    timeline: dict,
    followup: bool = False,
) -> None:
    KIND_ICON = {
        "DEPLOY_START": "🚀", "APPROVAL": "✅", "ROLLBACK": "↩️",
        "INCIDENT": "🚨", "LLM_CALL": "🤖", "GIT_COMMIT": "📝",
        "METRIC_SPIKE": "📈",
    }
    events   = timeline.get("events", [])
    duration = timeline.get("duration_seconds", 0)
    service  = timeline.get("service", "unknown")

    embed = discord.Embed(
        title=f"🎬 Deploy Replay · {service}",
        description=(
            f"**Deploy ID**: `{deploy_id}`\n"
            f"**소요**: {int(duration)}초 · **이벤트**: {len(events)}개\n"
            f"OTel: {'✅' if timeline.get('otel_available') else '⚪ 미연결'}"
        ),
        color=discord.Color.purple(),
    )

    if events:
        lines = []
        for ev in events[:15]:
            icon     = KIND_ICON.get(ev.get("kind", ""), "•")
            ts       = str(ev.get("ts", ""))[:19].split("T")[-1]
            title_tx = ev.get("title", "")[:50]
            sev      = ev.get("severity", "INFO")
            sev_mark = {"CRITICAL": "🔴", "ERROR": "🟠", "WARN": "🟡"}.get(sev, "⚪")
            lines.append(f"`{ts}` {sev_mark} {icon} {title_tx}")
        if len(events) > 15:
            lines.append(f"… 외 {len(events) - 15}개")
        embed.add_field(name="타임라인", value="\n".join(lines), inline=False)

    postmortem = timeline.get("postmortem_md", "")
    if postmortem:
        embed.add_field(
            name="Postmortem 요약 (§38.4)",
            value=postmortem[:300] + ("…" if len(postmortem) > 300 else ""),
            inline=False,
        )

    embed.set_footer(text="VSCode Replay 탭에서 0.5x/1x/2x 속도로 전체 재생 가능")

    if followup:
        await interaction.followup.send(embed=embed)
    else:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MODE_VIEW: dict[Mode, type[discord.ui.View]] = {
    "build":   BuildModeView,
    "ship":    ShipModeView,
    "operate": OperateModeView,
    "recover": RecoverModeView,
    "replay":  ReplayModeView,
    "github":  GitHubModeView,
}


async def _switch_mode(interaction: discord.Interaction, mode: Mode, guild_id: int) -> None:
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    # Core API가 허용하는 모드만 전달 ("replay", "github"는 Discord 전용 UI)
    _CORE_MODES = {"build", "ship", "operate", "recover", "home"}

    try:
        client = get_client_for_guild(guild_id)
        if mode in _CORE_MODES:
            await client.workbench_change_mode(mode, source="discord")
        state = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("workbench mode 전환 실패")
        await interaction.response.send_message(f"오류: `{e}`", ephemeral=True)
        return

    # GitHub 모드는 전용 임베드 — 마지막 배포 컨텍스트 + Rollback PR 액션 가이드.
    if mode == "github":
        embed = _build_github_embed(state)
    else:
        embed = _build_state_embed(state, mode=mode)

    view = WorkbenchMainView(guild_id=guild_id) if mode == "home" else _MODE_VIEW[mode](guild_id=guild_id)

    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


async def _refresh_home(interaction: discord.Interaction, guild_id: int) -> None:
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    try:
        client = get_client_for_guild(guild_id)
        state  = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(f"오류: `{e}`", ephemeral=True)
        return

    embed = _build_state_embed(state, mode="home")
    view  = WorkbenchMainView(guild_id=guild_id)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def workbench_command(interaction: discord.Interaction) -> None:
    """/recoder workbench — Discord Workbench 시작."""
    if not is_allowed(interaction):
        await interaction.response.send_message(_get_deny_message(interaction), ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        client = get_client_for_guild(interaction.guild_id)
        state  = await client.workbench_state()
    except GuildNotConfiguredError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    except Exception as e:
        log.exception("workbench 초기 state 조회 실패")
        await interaction.followup.send(f"Core API 호출 실패: `{e}`", ephemeral=True)
        return

    embed = _build_state_embed(state, mode="home")
    view  = WorkbenchMainView(guild_id=interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view)
