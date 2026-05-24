"""
discord-bot/middleware/auth.py — 서버별 역할 기반 인증 (SaaS 봇 모드)

인증 정책:
  1. Discord 서버 관리자(manage_guild 권한) → 항상 허용
  2. 서버에 허용 역할이 설정되어 있으면 → 해당 역할 보유 시 허용
  3. 서버 API 설정이 되어 있지 않으면 → 관리자에게만 /recoder setup 안내
  4. 위 조건 모두 불충족 → 거부

기존 하드코딩 ALLOWED_USER_IDS 방식은 제거되었습니다.
서버 관리자는 /recoder setup role <역할> 로 허용 역할을 추가할 수 있습니다.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

import discord
from discord import Interaction

import guild_store

log = logging.getLogger(__name__)


# ── 핵심 판별 함수 ──────────────────────────────────────────────────────────

def is_guild_configured(guild_id: int) -> bool:
    """서버의 ReCoder API 설정이 완료되었는지 확인한다."""
    return guild_store.get_api(guild_id) is not None


def is_allowed(interaction: Interaction) -> bool:
    """
    주어진 Interaction의 사용자가 이 서버에서 봇을 사용할 수 있는지 확인한다.

    DM (guild=None)은 지원하지 않으므로 False 반환.
    """
    if interaction.guild is None:
        return False

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        # 캐시 미스: 보수적으로 거부
        return False

    # 1) 서버 관리자는 항상 허용
    if member.guild_permissions.manage_guild:
        return True

    # 2) 허용 역할 확인
    allowed_role_ids = set(guild_store.get_roles(interaction.guild.id))
    if allowed_role_ids:
        member_role_ids = {r.id for r in member.roles}
        return bool(allowed_role_ids & member_role_ids)

    # 3) 허용 역할 미설정 → 관리자만 사용 가능 (이미 1에서 처리됨)
    return False


# ── 에러 메시지 생성 ────────────────────────────────────────────────────────

def _get_deny_message(interaction: Interaction) -> str:
    """사용자에게 보여줄 거부 이유 메시지를 반환한다."""
    if interaction.guild is None:
        return "🚫 이 봇은 DM에서 사용할 수 없습니다. Discord 서버 채널에서 사용해주세요."

    guild_id = interaction.guild.id

    if not is_guild_configured(guild_id):
        return (
            "⚙️ **이 서버에서 ReCoder 봇이 아직 설정되지 않았습니다.**\n\n"
            "서버 관리자가 먼저 `/recoder setup api` 명령으로 초기 설정을 완료해야 합니다."
        )

    return (
        "🚫 **접근 거부**: 이 봇을 사용할 권한이 없습니다.\n\n"
        "서버 관리자에게 `/recoder setup role` 로 사용 가능한 역할을 추가해달라고 요청하세요."
    )


# ── 데코레이터 ──────────────────────────────────────────────────────────────

def require_auth(func: Callable) -> Callable:
    """
    슬래시 커맨드에 붙이는 인증 데코레이터.

    인증 실패 시 Ephemeral 거부 메시지를 보내고 함수 실행을 중단한다.
    서버 미설정 / 역할 미보유 등 상황별 안내 메시지를 제공한다.
    """
    @wraps(func)
    async def wrapper(self_or_interaction, interaction_or_first_arg=None, *args, **kwargs):
        # app_commands.Group 메서드 vs 일반 함수 구분
        if isinstance(self_or_interaction, Interaction):
            interaction: Interaction = self_or_interaction
            call_args = (interaction_or_first_arg, *args) if interaction_or_first_arg else args
        else:
            interaction = interaction_or_first_arg
            call_args = args

        if not is_allowed(interaction):
            log.warning(
                "인증 거부: user=%s (%d), guild=%s, command=%s",
                interaction.user.name,
                interaction.user.id,
                interaction.guild.name if interaction.guild else "DM",
                interaction.command.name if interaction.command else "unknown",
            )
            msg = _get_deny_message(interaction)
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        if interaction_or_first_arg is None or isinstance(self_or_interaction, Interaction):
            return await func(self_or_interaction, *call_args, **kwargs)
        return await func(self_or_interaction, interaction, *call_args, **kwargs)

    return wrapper


# 하위 호환성을 위해 require_whitelist도 require_auth로 연결
require_whitelist = require_auth


# ── 유틸리티 ────────────────────────────────────────────────────────────────

def get_whitelist_count() -> int:
    """(레거시 호환) 항상 0 반환 — 역할 기반으로 전환됨."""
    return 0
