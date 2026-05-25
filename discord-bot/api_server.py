"""
discord-bot/api_server.py — 봇 내장 HTTP 등록 서버

VSCode 확장이 Discord 설정을 저장할 때 자동으로 guild 설정을 등록한다.
사용자가 /recoder setup api 명령을 입력할 필요가 없어진다.

엔드포인트:
  POST /api/v1/register
    Body: { guild_id, api_base, api_token, channels?, standup_cron? }
    Header: X-Registration-Key: <BOT_REGISTRATION_KEY>

  GET  /api/v1/health
    봇 서버 상태 확인
"""

import logging
import os
from aiohttp import web

from guild_store import (
    set_api,
    set_channel,
    get_guild_summary,
)

log = logging.getLogger(__name__)

REGISTRATION_KEY = os.getenv("BOT_REGISTRATION_KEY", "")


def _check_auth(request: web.Request) -> bool:
    """X-Registration-Key 헤더로 인증을 확인한다."""
    if not REGISTRATION_KEY:
        # 키가 설정되지 않으면 로컬 개발 환경으로 간주 — 허용
        log.warning("BOT_REGISTRATION_KEY가 설정되지 않아 인증을 건너뜁니다.")
        return True
    return request.headers.get("X-Registration-Key") == REGISTRATION_KEY


async def handle_health(request: web.Request) -> web.Response:
    """GET /api/v1/health — 봇 서버 상태 확인."""
    return web.json_response({"status": "ok", "service": "recoder-discord-bot"})


async def handle_register(request: web.Request) -> web.Response:
    """
    POST /api/v1/register — Guild API 설정 자동 등록.

    VSCode 확장이 Discord 설정 저장 시 이 엔드포인트를 호출한다.
    사용자가 /recoder setup api 를 입력할 필요가 없어진다.
    """
    if not _check_auth(request):
        return web.json_response(
            {"ok": False, "error": "인증 실패: X-Registration-Key 헤더를 확인하세요."},
            status=401,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON 파싱 실패"}, status=400)

    guild_id = body.get("guild_id")
    api_base = body.get("api_base")
    api_token = body.get("api_token")

    if not guild_id or not api_base or not api_token:
        return web.json_response(
            {"ok": False, "error": "guild_id, api_base, api_token은 필수입니다."},
            status=400,
        )

    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "guild_id는 정수여야 합니다."}, status=400)

    # API 설정 저장
    set_api(guild_id, api_base.rstrip("/"), api_token)
    log.info("Guild %d API 자동 등록 완료: %s", guild_id, api_base)

    # 채널 설정 (선택 사항)
    channels = body.get("channels") or {}
    for ch_type in ("deploy", "incident", "standup"):
        ch_id = channels.get(ch_type)
        if ch_id:
            try:
                set_channel(guild_id, ch_type, int(ch_id))
                log.info("Guild %d 채널 설정: %s → %s", guild_id, ch_type, ch_id)
            except Exception as e:
                log.warning("채널 설정 실패 (%s): %s", ch_type, e)

    # 현재 설정 상태 반환
    summary = get_guild_summary(guild_id)
    return web.json_response({
        "ok": True,
        "guild_id": guild_id,
        "message": "Guild 설정이 자동으로 완료되었습니다. 이제 /recoder 커맨드를 사용할 수 있습니다.",
        "summary": summary,
    })


async def handle_unregister(request: web.Request) -> web.Response:
    """
    DELETE /api/v1/register/{guild_id} — Guild 설정 삭제.

    VSCode에서 Discord 연동 해제 시 호출된다.
    """
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "인증 실패"}, status=401)

    guild_id_str = request.match_info.get("guild_id", "")
    try:
        guild_id = int(guild_id_str)
    except ValueError:
        return web.json_response({"ok": False, "error": "잘못된 guild_id"}, status=400)

    from guild_store import delete_guild
    delete_guild(guild_id)
    log.info("Guild %d 설정 삭제 (VSCode 연동 해제)", guild_id)
    return web.json_response({"ok": True, "guild_id": guild_id})


def create_app() -> web.Application:
    """aiohttp 앱 생성."""
    app = web.Application()
    app.router.add_get("/api/v1/health", handle_health)
    app.router.add_post("/api/v1/register", handle_register)
    app.router.add_delete("/api/v1/register/{guild_id}", handle_unregister)
    return app


async def start_api_server(port: int = 8765) -> web.AppRunner:
    """봇과 함께 API 서버를 시작한다."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Bot 등록 API 서버 시작: http://0.0.0.0:%d", port)
    return runner
