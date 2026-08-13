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
from aiohttp import web

from guild_store import (
    set_api,
    set_channel,
    get_guild_summary,
    get_channel,
    _conn as _guild_conn,
)
from bridge_settings import (
    get_make_channel_id,
    set_make_channel_id,
    get_settings_snapshot,
)
from recoder_bridge import hub as bridge_hub

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
    """등록·브리지 관리 요청 인증.

    키 미설정을 "인증 통과"로 바꾸면 안 된다 — 이 서버는 0.0.0.0 에 뜨므로
    그 상태에서는 네트워크의 아무나 길드 자격증명을 덮어쓰고 브리지 채널을
    바꿀 수 있다. 키가 없으면: (1) start_api_server 가 루프백에만 바인드하고
    (2) 여기서도 루프백 밖 요청은 전부 거부한다 — 이중 방어다.
    """
    if not REGISTRATION_KEY:
        peer = request.remote or ""
        if peer in ("127.0.0.1", "::1", "localhost"):
            return True
        log.warning("BOT_REGISTRATION_KEY 미설정 상태에서 외부(%s) 요청 거부", peer)
        return False
    supplied = request.headers.get("X-Registration-Key") or ""
    return hmac.compare_digest(supplied, REGISTRATION_KEY)


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

    # 모든 guild_id 조회 — guild_store 의 컨텍스트 매니저로 connection 누수 방지
    try:
        with _guild_conn() as c:
            rows = c.execute("SELECT guild_id FROM guild_config").fetchall()
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

async def handle_bridge_status(request: web.Request) -> web.Response:
    """ReCoder Bridge 현재 설정 + 연결 상태 조회 (Workbench UI 용)."""
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    snapshot = get_settings_snapshot()
    active_channel_id = get_make_channel_id()

    # 봇이 알고 있는 채널 정보로 이름까지 채워준다 (UI 표시용)
    channel_name = None
    channel_guild_name = None
    if _bot is not None and active_channel_id:
        try:
            ch = _bot.get_channel(active_channel_id)
            if ch is not None:
                channel_name = getattr(ch, "name", None)
                channel_guild_name = getattr(getattr(ch, "guild", None), "name", None)
        except Exception:
            pass

    return web.json_response({
        "active_channel_id": str(active_channel_id) if active_channel_id else "",
        "channel_name": channel_name,
        "guild_name": channel_guild_name,
        "connected_clients": bridge_hub.connected_count,
        "settings": snapshot,
    })


async def handle_bridge_invite_url(request: web.Request) -> web.Response:
    """봇 OAuth 초대 URL 생성. DISCORD_CLIENT_ID 환경변수 → 봇 user.id 순으로 fallback."""
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    # setup.py 와 동일 권한 비트마스크 사용 (Send/Embed/History/Slash)
    invite_permissions = 2147485696

    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id and _bot is not None:
        try:
            if _bot.user is not None:
                client_id = str(_bot.user.id)
        except Exception:
            pass

    if not client_id:
        return web.json_response(
            {"error": "DISCORD_CLIENT_ID가 설정되지 않았고 봇 user 정보도 없습니다."},
            status=503,
        )

    invite_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&permissions={invite_permissions}"
        f"&scope=bot%20applications.commands"
    )

    bot_name = None
    bot_avatar = None
    if _bot is not None and _bot.user is not None:
        try:
            bot_name = str(_bot.user)
            bot_avatar = _bot.user.display_avatar.url if _bot.user.display_avatar else None
        except Exception:
            pass

    return web.json_response({
        "invite_url": invite_url,
        "client_id": client_id,
        "bot_name": bot_name,
        "bot_avatar": bot_avatar,
    })


async def handle_bridge_guilds(request: web.Request) -> web.Response:
    """봇이 접속해 있는 길드 목록 (Workbench dropdown 용).

    응답:
      { guilds: [
          { id, name, icon_url, channel_count, text_channel_count, registered }
        ]
      }
    봇이 아직 init 안된 경우 비어있는 리스트 반환.
    """
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    guilds: list[dict] = []
    if _bot is not None:
        try:
            for g in list(getattr(_bot, "guilds", []) or []):
                icon_url = None
                try:
                    if g.icon is not None:
                        icon_url = g.icon.url
                except Exception:
                    icon_url = None

                text_count = 0
                try:
                    for ch in getattr(g, "channels", []) or []:
                        if type(ch).__name__ == "TextChannel":
                            text_count += 1
                except Exception:
                    pass

                # 이 guild가 /recoder setup 으로 등록된 적 있는지
                registered = False
                try:
                    summary = get_guild_summary(g.id)
                    registered = bool(summary and summary.get("api_base"))
                except Exception:
                    pass

                guilds.append({
                    "id": str(g.id),
                    "name": str(getattr(g, "name", "") or ""),
                    "icon_url": icon_url,
                    "channel_count": len(list(getattr(g, "channels", []) or [])),
                    "text_channel_count": text_count,
                    "registered": registered,
                })
        except Exception as exc:
            log.warning("길드 목록 조회 실패: %s", exc)

    return web.json_response({"guilds": guilds})


async def handle_bridge_guild_channels(request: web.Request) -> web.Response:
    """특정 길드의 text 채널 목록 (Workbench dropdown 용).

    URL: GET /api/v1/bridge/guilds/{guild_id}/channels

    응답:
      { guild_id, channels: [
          { id, name, type, category, position }
        ]
      }
    """
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    raw_guild = request.match_info.get("guild_id", "")
    try:
        guild_id = int(raw_guild)
    except (TypeError, ValueError):
        return web.json_response({"error": "guild_id는 숫자여야 합니다."}, status=400)

    if _bot is None:
        return web.json_response({"guild_id": str(guild_id), "channels": []})

    try:
        guild = _bot.get_guild(guild_id)
    except Exception:
        guild = None

    if guild is None:
        return web.json_response(
            {
                "error": f"봇이 guild {guild_id} 에 접속해 있지 않습니다.",
                "guild_id": str(guild_id),
                "channels": [],
            },
            status=404,
        )

    channels: list[dict] = []
    try:
        for ch in getattr(guild, "channels", []) or []:
            if type(ch).__name__ != "TextChannel":
                continue

            category_name = None
            try:
                if getattr(ch, "category", None) is not None:
                    category_name = str(ch.category.name)
            except Exception:
                category_name = None

            channels.append({
                "id": str(ch.id),
                "name": str(getattr(ch, "name", "") or ""),
                "type": "text",
                "category": category_name,
                "position": int(getattr(ch, "position", 0) or 0),
            })
    except Exception as exc:
        log.warning("Guild %d 채널 목록 조회 실패: %s", guild_id, exc)

    # category 우선, position 보조 정렬
    channels.sort(key=lambda c: ((c["category"] or "~"), c["position"], c["name"]))

    return web.json_response({"guild_id": str(guild_id), "channels": channels})


async def handle_bridge_set_channel(request: web.Request) -> web.Response:
    """채널 ID 설정. body: { channel_id: "123..." }  또는 "" 로 해제."""
    if not _check_auth(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    raw = body.get("channel_id", "")
    if raw in ("", None):
        set_make_channel_id(0)
        return web.json_response({"ok": True, "active_channel_id": ""})

    try:
        channel_id = int(str(raw).strip())
    except ValueError:
        return web.json_response(
            {"error": "channel_id는 숫자여야 합니다 (Discord 채널 우클릭 → ID 복사)."},
            status=400,
        )

    # 봇이 해당 채널을 실제로 보고 있는지 best-effort 검증 (실패해도 저장은 함)
    channel_name = None
    if _bot is not None:
        try:
            ch = _bot.get_channel(channel_id)
            if ch is not None:
                channel_name = getattr(ch, "name", None)
        except Exception:
            pass

    set_make_channel_id(channel_id)
    return web.json_response({
        "ok": True,
        "active_channel_id": str(channel_id),
        "channel_name": channel_name,
    })


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/v1/health",                  handle_health)
    app.router.add_post("/api/v1/register",               handle_register)
    app.router.add_delete("/api/v1/register/{guild_id}",  handle_unregister)
    app.router.add_post("/api/v1/github/webhook",         handle_github_webhook)
    # ReCoder Bridge (Discord → VSCode 실시간 코드 삽입) 설정 API
    app.router.add_get("/api/v1/bridge/status",           handle_bridge_status)
    app.router.add_put("/api/v1/bridge/channel",          handle_bridge_set_channel)
    app.router.add_get("/api/v1/bridge/invite-url",       handle_bridge_invite_url)
    app.router.add_get("/api/v1/bridge/guilds",           handle_bridge_guilds)
    app.router.add_get(
        "/api/v1/bridge/guilds/{guild_id}/channels",
        handle_bridge_guild_channels,
    )
    return app


async def start_api_server(port: int = 8765, bot=None) -> web.AppRunner:
    """봇과 함께 API 서버를 시작한다.

    BOT_REGISTRATION_KEY 가 없으면 **루프백에만** 바인드한다. 키 없이
    0.0.0.0 으로 열면 등록·브리지 관리 API 전부가 무인증으로 노출된다 —
    비밀이 없다는 것이 곧 권한이 되면 안 된다.
    """
    if bot is not None:
        set_bot(bot)
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    host = "0.0.0.0" if REGISTRATION_KEY else "127.0.0.1"
    if not REGISTRATION_KEY:
        log.warning(
            "BOT_REGISTRATION_KEY 미설정 — API 서버를 루프백(127.0.0.1)에만 "
            "바인드합니다. 외부에서 쓰려면 키를 설정하세요.")
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Bot 등록 API 서버 시작: http://%s:%d", host, port)
    return runner
