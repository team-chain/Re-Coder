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

  POST /api/v1/github/webhook
    GitHub Webhook 수신 — push / pull_request / pull_request_review 이벤트를
    설정된 deploy 채널에 Discord embed로 전송.
    Header: X-Hub-Signature-256: <HMAC-SHA256>  (GITHUB_WEBHOOK_SECRET 설정 시 검증)
            X-GitHub-Event: <event>
"""

import hashlib
import hmac
import logging
import os
import sqlite3
from aiohttp import web

from guild_store import (
    set_api,
    set_channel,
    get_guild_summary,
    get_channel,
    DB_PATH,
)

log = logging.getLogger(__name__)

REGISTRATION_KEY  = os.getenv("BOT_REGISTRATION_KEY", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# bot 인스턴스 — start_api_server() 호출 시 주입
_bot = None


def set_bot(bot) -> None:
    """봇 인스턴스를 주입한다 (Webhook이 채널에 메시지를 보내기 위해 필요)."""
    global _bot
    _bot = bot


# ---------------------------------------------------------------------------
# 인증 헬퍼
# ---------------------------------------------------------------------------

def _check_auth(request: web.Request) -> bool:
    if not REGISTRATION_KEY:
        log.warning("BOT_REGISTRATION_KEY가 설정되지 않아 인증을 건너뜁니다.")
        return True
    return request.headers.get("X-Registration-Key") == REGISTRATION_KEY


def _verify_github_signature(body: bytes, signature: str) -> bool:
    """GitHub Webhook HMAC-SHA256 서명 검증."""
    if not GITHUB_WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET 미설정 — 서명 검증 건너뜀 (개발 모드)")
        return True
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# 기존 엔드포인트
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "recoder-discord-bot"})


async def handle_register(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response(
            {"ok": False, "error": "인증 실패: X-Registration-Key 헤더를 확인하세요."},
            status=401,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON 파싱 실패"}, status=400)

    guild_id  = body.get("guild_id")
    api_base  = body.get("api_base")
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

    set_api(guild_id, api_base.rstrip("/"), api_token)
    log.info("Guild %d API 자동 등록 완료: %s", guild_id, api_base)

    channels = body.get("channels") or {}
    for ch_type in ("deploy", "incident", "standup"):
        ch_id = channels.get(ch_type)
        if ch_id:
            try:
                set_channel(guild_id, ch_type, int(ch_id))
            except Exception as e:
                log.warning("채널 설정 실패 (%s): %s", ch_type, e)

    summary = get_guild_summary(guild_id)
    return web.json_response({
        "ok": True,
        "guild_id": guild_id,
        "message": "Guild 설정이 자동으로 완료되었습니다.",
        "summary": summary,
    })


async def handle_unregister(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return web.json_response({"ok": False, "error": "인증 실패"}, status=401)

    guild_id_str = request.match_info.get("guild_id", "")
    try:
        guild_id = int(guild_id_str)
    except ValueError:
        return web.json_response({"ok": False, "error": "잘못된 guild_id"}, status=400)

    from guild_store import delete_guild
    delete_guild(guild_id)
    log.info("Guild %d 설정 삭제", guild_id)
    return web.json_response({"ok": True, "guild_id": guild_id})


# ---------------------------------------------------------------------------
# GitHub Webhook 핸들러
# ---------------------------------------------------------------------------

async def handle_github_webhook(request: web.Request) -> web.Response:
    """
    POST /api/v1/github/webhook

    GitHub → 이 엔드포인트 → 모든 guild의 deploy 채널에 embed 전송.

    지원 이벤트:
      push              — 브랜치 커밋 푸시
      pull_request      — PR 오픈 / 클로즈 / 머지
      pull_request_review — PR 리뷰 승인 / 코멘트
      create            — 브랜치 / 태그 생성
    """
    body_bytes = await request.read()

    # 서명 검증
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(body_bytes, signature):
        log.warning("GitHub Webhook 서명 검증 실패")
        return web.json_response({"ok": False, "error": "서명 검증 실패"}, status=401)

    event = request.headers.get("X-GitHub-Event", "ping")
    if event == "ping":
        return web.json_response({"ok": True, "message": "pong"})

    try:
        import json
        payload = json.loads(body_bytes)
    except Exception:
        return web.json_response({"ok": False, "error": "JSON 파싱 실패"}, status=400)

    embed_data = _build_github_embed(event, payload)
    if embed_data is None:
        return web.json_response({"ok": True, "message": f"이벤트 '{event}' 무시됨"})

    # 모든 guild의 deploy 채널에 전송
    await _broadcast_to_deploy_channels(embed_data)
    return web.json_response({"ok": True, "event": event})


def _build_github_embed(event: str, payload: dict) -> dict | None:
    """
    GitHub 이벤트 payload → Discord embed dict 변환.
    None 반환 시 무시.
    """
    repo = payload.get("repository", {})
    repo_name = repo.get("full_name", "unknown/repo")
    repo_url  = repo.get("html_url", "")

    if event == "push":
        ref    = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        pusher = payload.get("pusher", {}).get("name", "unknown")
        commits = payload.get("commits", [])

        if not commits:
            return None

        commit_lines = []
        for c in commits[:5]:
            sha     = c.get("id", "")[:7]
            message = c.get("message", "").split("\n")[0][:60]
            author  = c.get("author", {}).get("name", "?")
            url     = c.get("url", "")
            commit_lines.append(f"[`{sha}`]({url}) {message} — {author}")
        if len(commits) > 5:
            commit_lines.append(f"… 외 {len(commits) - 5}개 커밋")

        return {
            "title": f"📦 Push · {repo_name}",
            "description": (
                f"**브랜치**: `{branch}` · **커밋**: {len(commits)}개\n"
                f"**푸시한 사람**: {pusher}\n\n"
                + "\n".join(commit_lines)
            ),
            "color": 0x3b82f6,
            "url": repo_url,
            "footer": f"{repo_name}",
        }

    elif event == "pull_request":
        action = payload.get("action", "")
        if action not in ("opened", "closed", "reopened", "ready_for_review"):
            return None

        pr      = payload.get("pull_request", {})
        pr_num  = pr.get("number", "?")
        pr_url  = pr.get("html_url", "")
        pr_title = pr.get("title", "")
        author  = pr.get("user", {}).get("login", "?")
        base    = pr.get("base", {}).get("ref", "?")
        head    = pr.get("head", {}).get("ref", "?")
        merged  = pr.get("merged", False)

        if action == "closed" and merged:
            icon  = "🔀"
            label = "Merged"
            color = 0x8b5cf6
        elif action == "closed":
            icon  = "🚫"
            label = "Closed"
            color = 0xef4444
        elif action == "opened":
            icon  = "🟢"
            label = "Opened"
            color = 0x22c55e
        else:
            icon  = "🔁"
            label = action.capitalize()
            color = 0xf59e0b

        return {
            "title": f"{icon} PR #{pr_num} {label} · {repo_name}",
            "description": (
                f"**[{pr_title}]({pr_url})**\n"
                f"`{head}` → `{base}` · by **{author}**"
            ),
            "color": color,
            "url": pr_url,
            "footer": f"{repo_name}",
        }

    elif event == "pull_request_review":
        action = payload.get("action", "")
        if action not in ("submitted",):
            return None

        review  = payload.get("review", {})
        state   = review.get("state", "").upper()
        pr      = payload.get("pull_request", {})
        pr_num  = pr.get("number", "?")
        pr_title = pr.get("title", "")
        pr_url  = pr.get("html_url", "")
        reviewer = review.get("user", {}).get("login", "?")
        body    = (review.get("body") or "")[:200]

        icon_map = {"APPROVED": "✅", "CHANGES_REQUESTED": "🔄", "COMMENTED": "💬"}
        color_map = {"APPROVED": 0x22c55e, "CHANGES_REQUESTED": 0xf59e0b, "COMMENTED": 0x64748b}

        return {
            "title": f"{icon_map.get(state,'❓')} PR Review · #{pr_num} {state}",
            "description": (
                f"**[{pr_title}]({pr_url})**\n"
                f"리뷰어: **{reviewer}**"
                + (f"\n> {body}" if body else "")
            ),
            "color": color_map.get(state, 0x888888),
            "url": pr_url,
            "footer": f"{repo_name}",
        }

    elif event == "create":
        ref_type = payload.get("ref_type", "")
        ref      = payload.get("ref", "")
        sender   = payload.get("sender", {}).get("login", "?")
        if ref_type not in ("branch", "tag"):
            return None

        icon = "🌿" if ref_type == "branch" else "🏷️"
        return {
            "title": f"{icon} {ref_type.capitalize()} 생성 · {repo_name}",
            "description": f"`{ref}` — by **{sender}**",
            "color": 0x22c55e,
            "url": repo_url,
            "footer": f"{repo_name}",
        }

    return None


async def _broadcast_to_deploy_channels(embed_data: dict) -> None:
    """DB에 등록된 모든 guild의 deploy 채널에 embed를 전송한다."""
    if _bot is None:
        log.warning("봇 인스턴스 미주입 — Webhook 메시지를 보낼 수 없습니다.")
        return

    # 모든 guild_id 조회
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT guild_id FROM guild_config").fetchall()
        conn.close()
    except Exception as e:
        log.error("guild_id 조회 실패: %s", e)
        return

    import discord

    for (guild_id,) in rows:
        ch_id = get_channel(guild_id, "deploy")
        if not ch_id:
            continue
        try:
            channel = _bot.get_channel(ch_id)
            if channel is None:
                channel = await _bot.fetch_channel(ch_id)

            embed = discord.Embed(
                title=embed_data["title"],
                description=embed_data.get("description", ""),
                color=embed_data.get("color", 0x3b82f6),
                url=embed_data.get("url"),
            )
            footer = embed_data.get("footer", "")
            if footer:
                embed.set_footer(text=footer)

            await channel.send(embed=embed)
            log.info("GitHub Webhook → Guild %d deploy 채널 전송 완료", guild_id)
        except Exception as e:
            log.warning("Guild %d 채널 전송 실패: %s", guild_id, e)


# ---------------------------------------------------------------------------
# 앱 생성 & 시작
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/v1/health",                  handle_health)
    app.router.add_post("/api/v1/register",               handle_register)
    app.router.add_delete("/api/v1/register/{guild_id}",  handle_unregister)
    app.router.add_post("/api/v1/github/webhook",         handle_github_webhook)
    return app


async def start_api_server(port: int = 8765, bot=None) -> web.AppRunner:
    """봇과 함께 API 서버를 시작한다."""
    if bot is not None:
        set_bot(bot)
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Bot 등록 API 서버 시작: http://0.0.0.0:%d", port)
    return runner
