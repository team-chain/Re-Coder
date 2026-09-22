"""
discord-bot/middleware/auth.py — Hybrid 인증 (§6.1.4 user_id 화이트리스트 +
역할 기반 보조)

설계서 §6.1.4 Discord ChatOps 보안 모델은 "Discord user_id 화이트리스트를
통해 명령 실행 권한을 제한한다"고 명시한다. §6.4.3 Hybrid Cloud Relay 또한
"Discord user_id 매핑" 및 "Discord user_id 화이트리스트와 양방향 토큰 인증의
결합"을 요구한다. 본 모듈은 스펙을 1차로 충족하기 위해 user_id 화이트리스트를
**1차 게이트**로 두고, 운영 편의를 위해 역할 기반 ACL 을 **보조 게이트**로
유지한다.

인증 정책 (OR 결합 — 어느 하나라도 통과하면 허용):
  1. 서버 관리자(manage_guild 권한) → 항상 허용 (운영 안전망)
  2. §6.1.4 user_id ∈ guild_users 화이트리스트 → 허용 (1차 게이트)
  3. user 가 guild_roles 의 허용 역할을 보유 → 허용 (보조 게이트)
  4. 위 셋 모두 불충족 → 거부

DM (guild=None) 은 허용되지 않는다 (서버 단위로 화이트리스트를 분리하는
SaaS 설계 때문). 캐시 미스로 member 객체를 못 가져오면 보수적으로 거부.

레거시 메모: 이전에 'ALLOWED_USER_IDS 환경변수 기반 단일 글로벌 화이트리스트'
구조였던 것을 per-guild SQLite (guild_users 테이블)로 이전했고, 그 과정에서
역할 기반 ACL 만 남기는 잘못된 단계가 있었다 — 본 버전에서 스펙 부합한
hybrid 로 정정한다.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from discord import Interaction

import guild_store

log = logging.getLogger(__name__)


# ── 핵심 판별 함수 ──────────────────────────────────────────────────────────

def is_guild_configured(guild_id: int) -> bool:
    """서버의 ReCoder API 설정이 완료되었는지 확인한다."""
    return guild_store.get_api(guild_id) is not None


def is_allowed(interaction: Interaction) -> bool:
    """
    주어진 Interaction 의 사용자가 이 서버에서 봇을 사용할 수 있는지 확인한다.

    Hybrid 정책 (어느 하나라도 통과하면 True):
      (A) 서버 관리자 (manage_guild) — 항상 허용
      (B) §6.1.4 user_id 화이트리스트 — 1차 게이트
      (C) 역할 기반 보조 — guild_roles 의 허용 역할 보유

    DM 또는 캐시 미스의 경우 보수적으로 False.
    """
    if interaction.guild is None:
        return False

    guild_id = interaction.guild.id
    user_id = interaction.user.id

    # (A) 서버 관리자 — 항상 허용
    member = interaction.guild.get_member(user_id)
    if member is None:
        # 캐시 미스. user_id 화이트리스트만 단독 확인 (member 객체 없이도 가능).
        # 1차 게이트가 살아있어야 모바일/캐시 콜드스타트 시에도 접근 가능.
        return guild_store.is_user_allowed(guild_id, user_id)

    if member.guild_permissions.manage_guild:
        return True

    # (B) §6.1.4 user_id 화이트리스트 — 1차 게이트
    if guild_store.is_user_allowed(guild_id, user_id):
        return True

    # (C) 역할 기반 보조 게이트
    allowed_role_ids = set(guild_store.get_roles(guild_id))
    if allowed_role_ids:
        member_role_ids = {r.id for r in member.roles}
        if allowed_role_ids & member_role_ids:
            return True

    return False


# ── 에러 메시지 생성 ────────────────────────────────────────────────────────

def is_user_allowed_in_guild(guild_id: int, user_id: int) -> bool:
    """Interaction 객체 없이(예: on_message 자동 처리) 인가를 판정한다.

    화이트리스트(§6.1.4)만 확인한다 — 자동 처리 경로에는 member 객체·권한
    비트가 없을 수 있으므로, 명시적으로 등록된 사용자만 통과시킨다. 관리자·
    역할 기반 통과는 슬래시 커맨드(is_allowed)에서만 적용한다.
    """
    if not guild_id or not user_id:
        return False
    try:
        return guild_store.is_user_allowed(guild_id, user_id)
    except Exception:
        return False


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
        "서버 관리자에게 다음 중 한 가지를 요청하세요:\n"
        "• `/recoder setup user add @사용자` — user_id 화이트리스트에 추가 (§6.1.4 권장)\n"
        "• `/recoder setup role add @역할` — 허용 역할을 부여받은 후 사용 (보조 경로)"
    )


# ── 데코레이터 ──────────────────────────────────────────────────────────────

def require_auth(func: Callable) -> Callable:
    """
    슬래시 커맨드에 붙이는 인증 데코레이터.

    인증 실패 시 Ephemeral 거부 메시지를 보내고 함수 실행을 중단한다.
    서버 미설정 / 역할 미보유 / user_id 미등록 등 상황별 안내 메시지를 제공한다.
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


# 하위 호환성: require_whitelist 는 require_auth 의 alias.
# 본래 의도 — '화이트리스트로 보호된 명령' — 가 §6.1.4 hybrid 로 정확히 부활.
require_whitelist = require_auth


# ── 유틸리티 ────────────────────────────────────────────────────────────────

def get_whitelist_count(guild_id: int | None = None) -> int:
    """
    화이트리스트 등록 user_id 개수.

    guild_id 가 주어지면 해당 서버 카운트, 미지정 시 전체 합계.
    """
    if guild_id is not None:
        return guild_store.get_user_whitelist_count(guild_id)

    # 전체 합계 — 모든 길드 스캔. guild_store 의 컨텍스트 매니저를 재사용해
    # connection 누수가 발생하지 않도록 한다.
    try:
        with guild_store._conn() as c:  # noqa: SLF001 — 의도적 내부 헬퍼 재사용
            row = c.execute("SELECT COUNT(*) FROM guild_users").fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0
