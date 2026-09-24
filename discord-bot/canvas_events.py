"""Opt-in canvas notifications through the existing authenticated bot connection."""
from collections import OrderedDict
import asyncio

from aiohttp import web

_sent: OrderedDict[tuple[int, str], None] = OrderedDict()
_lock = asyncio.Lock()


async def send_canvas_event(request, *, authorized, bot, channel_id):
    if not authorized:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        event_id = str(body.get("event_id", ""))
        title = str(body.get("title", ""))
        detail = str(body.get("detail", ""))
        if not event_id or len(event_id) > 200 or not title or len(title) > 160 or len(detail) > 1000:
            raise ValueError()
    except (ValueError, AttributeError):
        return web.json_response({"error": "invalid event"}, status=400)
    channel = bot.get_channel(channel_id) if bot and channel_id else None
    if channel is None:
        return web.json_response({"error": "알림 채널을 연결하고 봇 권한을 확인하세요."}, status=409)
    key = (channel_id, event_id)
    async with _lock:
        if key in _sent:
            return web.json_response({"ok": True, "duplicate": True, "event_id": event_id})
        import discord
        embed = discord.Embed(title=title, description=detail, colour=0x7985ED)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            return web.json_response({"error": "Discord 전송 실패. 채널 쓰기 권한을 확인하세요."}, status=502)
        _sent[key] = None
        if len(_sent) > 1000:
            _sent.popitem(last=False)
    return web.json_response({"ok": True, "event_id": event_id})
