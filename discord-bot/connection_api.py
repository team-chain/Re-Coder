"""Discord OAuth login, project bindings and authenticated outbound VS Code sockets.

The operator owns the Discord client secret. VS Code proves possession of a
random verifier when exchanging its short-lived login attempt for a session.
Discord access tokens are used once for identity/guild lookup, never returned
to the extension or persisted. Bot invites do not authenticate a user.
"""
import asyncio
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
from aiohttp import web

import connection_store as store

PREFIX = '/api/v1/connect'


class ConnectionService:
    def __init__(self, get_bot):
        self.get_bot = get_bot
        self.pending = {}
        self.sockets = {}
        self.jobs = set()
        self.sent = OrderedDict()
        self.seen_messages = OrderedDict()
        self.send_lock = asyncio.Lock()

    def config(self):
        base = os.getenv('DISCORD_PUBLIC_URL', 'http://127.0.0.1:8765').rstrip('/')
        url = urlsplit(base)
        if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise web.HTTPServiceUnavailable(text='DISCORD_PUBLIC_URL은 HTTPS 주소여야 합니다.')
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise web.HTTPServiceUnavailable(text='DISCORD_PUBLIC_URL에는 서버의 기본 주소만 설정하세요.')
        return base, os.getenv('DISCORD_CLIENT_ID', ''), os.getenv('DISCORD_CLIENT_SECRET', '')

    def ready(self):
        bot = self.get_bot()
        return bool(bot and bot.is_ready())

    def plain_chat_ready(self):
        bot = self.get_bot()
        return self.ready() and bool(getattr(getattr(bot, 'intents', None), 'message_content', False))

    def prune(self):
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.time()}

    async def info(self, request):
        base, client_id, secret = self.config()
        return web.json_response({'service': 'recoder-discord-connect', 'bot_ready': self.ready(),
                                  'plain_chat_ready': self.plain_chat_ready(),
                                  'oauth_ready': bool(client_id and secret),
                                  'redirect_uri': base + PREFIX + '/callback'})

    async def start(self, request):
        base, client_id, secret = self.config()
        if not client_id or not secret:
            raise web.HTTPServiceUnavailable(text='운영자가 Discord Client ID와 Client Secret을 설정해야 합니다.')
        if not self.ready():
            raise web.HTTPServiceUnavailable(text='봇이 Discord에 로그인하지 못했습니다. 봇 실행 로그와 토큰을 확인하세요.')
        body = await request.json()
        challenge, project_id = str(body.get('challenge', '')), str(body.get('project_id', ''))
        name = str(body.get('project_name', '')).strip()
        if not re.fullmatch(r'[a-f0-9]{64}', challenge) or not re.fullmatch(r'[a-f0-9]{64}', project_id) or not name or len(name) > 100:
            raise web.HTTPBadRequest(text='올바른 프로젝트와 연결 요청이 필요합니다.')
        self.prune()
        if len(self.pending) >= 500 or sum(v['peer'] == request.remote for v in self.pending.values()) >= 10:
            raise web.HTTPTooManyRequests(text='진행 중인 로그인이 많습니다. 잠시 후 다시 연결하세요.')
        attempt = secrets.token_urlsafe(32)
        self.pending[attempt] = dict(challenge=challenge, project_id=project_id, project_name=name,
                                     expires=time.time() + 300, peer=request.remote, status='pending',
                                     code=secrets.token_hex(3).upper(), cookie='', user=None)
        return web.json_response({'attempt': attempt, 'authorize_url': base + PREFIX + '/authorize/' + attempt,
                                  'confirmation_code': self.pending[attempt]['code'], 'expires_in': 300})

    def attempt(self, key):
        self.prune()
        value = self.pending.get(key)
        if value is None:
            raise web.HTTPGone(text='로그인이 만료되었습니다. VS Code에서 다시 연결하세요.')
        return value

    async def authorize(self, request):
        key = request.match_info['attempt']
        attempt = self.attempt(key)
        if attempt['status'] != 'pending' or attempt['cookie']:
            raise web.HTTPConflict(text='이미 시작한 로그인입니다. VS Code에서 다시 연결하세요.')
        base, client_id, _ = self.config()
        cookie = secrets.token_urlsafe(32)
        attempt['cookie'] = store.digest(cookie)
        params = dict(client_id=client_id, response_type='code', scope='identify guilds',
                      redirect_uri=base + PREFIX + '/callback', state=key, prompt='consent')
        response = web.HTTPFound('https://discord.com/oauth2/authorize?' + urlencode(params))
        response.set_cookie('recoder_connect', cookie, max_age=300, httponly=True,
                            secure=base.startswith('https:'), samesite='Lax', path=PREFIX)
        raise response

    def browser_attempt(self, request, key):
        attempt = self.attempt(key)
        cookie = request.cookies.get('recoder_connect', '')
        if not cookie or not hmac.compare_digest(attempt['cookie'], store.digest(cookie)):
            raise web.HTTPForbidden(text='로그인 브라우저가 일치하지 않습니다. VS Code에서 다시 시작하세요.')
        return attempt

    async def discord_identity(self, code):
        base, client_id, secret = self.config()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
            async with client.post('https://discord.com/api/v10/oauth2/token', data={
                'client_id': client_id, 'client_secret': secret, 'grant_type': 'authorization_code',
                'code': code, 'redirect_uri': base + PREFIX + '/callback',
            }) as response:
                if response.status != 200:
                    raise web.HTTPBadGateway(text='Discord 인증 교환에 실패했습니다. Redirect URI와 앱 설정을 확인하세요.')
                token = await response.json()
            headers = {'Authorization': 'Bearer ' + token['access_token']}
            async def get(route):
                async with client.get('https://discord.com/api/v10' + route, headers=headers) as response:
                    if response.status != 200:
                        raise web.HTTPBadGateway(text='Discord 계정 정보를 가져오지 못했습니다.')
                    return await response.json()
            return await get('/users/@me'), await get('/users/@me/guilds')

    async def callback(self, request):
        key = request.query.get('state', '')
        attempt = self.browser_attempt(request, key)
        if attempt['status'] != 'pending':
            raise web.HTTPConflict(text='이미 처리된 로그인입니다.')
        attempt['status'] = 'exchanging'
        if request.query.get('error') or not request.query.get('code'):
            attempt['status'] = 'denied'
            return self.page('연결 취소', 'Discord 연결을 취소했습니다. VS Code로 돌아가세요.')
        try:
            user, guilds = await self.discord_identity(request.query['code'])
        except Exception:
            attempt['status'] = 'failed'
            raise
        attempt.update(user=user, guilds=guilds, status='confirm')
        message = f"<p><b>{html.escape(user.get('global_name') or user['username'])}</b> 계정으로 <b>{html.escape(attempt['project_name'])}</b> 프로젝트를 연결합니다.</p>"
        message += f"<p>VS Code에 표시된 확인 코드와 일치하는지 확인하세요.</p><h2>{attempt['code']}</h2>"
        message += f'<form method="post" action="{PREFIX}/confirm"><input type="hidden" name="state" value="{html.escape(key)}"><button name="decision" value="approve">이 프로젝트 연결</button> <button name="decision" value="deny">취소</button></form>'
        return self.page('ReCoder Discord 연결', message, markup=True)

    @staticmethod
    def page(title, message, markup=False):
        return web.Response(content_type='text/html', text='<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ReCoder Discord 연결</title><body><main><h1>' + html.escape(title) + '</h1>' + (message if markup else html.escape(message)) + '</main></body></html>', headers={
            'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})

    async def confirm(self, request):
        body = await request.post()
        attempt = self.browser_attempt(request, body.get('state', ''))
        if attempt['status'] != 'confirm':
            raise web.HTTPConflict(text='이미 처리된 로그인입니다.')
        attempt['status'] = 'approved' if body.get('decision') == 'approve' else 'denied'
        response = self.page('연결 승인 완료' if attempt['status'] == 'approved' else '연결 취소', 'VS Code로 돌아가 서버와 채널을 선택하세요.' if attempt['status'] == 'approved' else 'VS Code로 돌아가세요.')
        response.del_cookie('recoder_connect', path=PREFIX)
        return response

    async def poll(self, request):
        body = await request.json()
        key, verifier = str(body.get('attempt', '')), str(body.get('verifier', ''))
        attempt = self.attempt(key)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{40,100}', verifier) or not hmac.compare_digest(store.digest(verifier), attempt['challenge']):
            raise web.HTTPForbidden(text='연결 요청을 검증하지 못했습니다.')
        status = attempt['status']
        if status == 'approved':
            token = store.issue(attempt['user'], attempt['project_id'], attempt['project_name'], attempt['guilds'])
            del self.pending[key]
            return web.json_response({'status': 'connected', 'token': token})
        return web.json_response({'status': status})

    def authenticate(self, request):
        auth = request.headers.get('Authorization', '')
        session = store.session(auth[7:] if auth.startswith('Bearer ') else '')
        if session is None:
            raise web.HTTPUnauthorized(text='Discord 연결이 만료되었거나 해제되었습니다. 다시 로그인하세요.')
        return session

    async def member(self, s, guild_id):
        bot = self.get_bot()
        if not bot or not self.ready():
            raise web.HTTPServiceUnavailable(text='Discord 봇이 오프라인입니다.')
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            raise web.HTTPConflict(text='먼저 관리자가 이 서버에 ReCoder 봇을 초대해야 합니다.')
        try:
            member = await guild.fetch_member(int(s['user_id']))
        except discord.NotFound:
            raise web.HTTPForbidden(text='이 Discord 서버의 멤버가 아닙니다.')
        except discord.HTTPException:
            raise web.HTTPBadGateway(text='서버 멤버 권한을 확인하지 못했습니다.')
        return guild, member

    async def channel(self, s, guild_id, channel_id):
        guild, member = await self.member(s, guild_id)
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise web.HTTPBadRequest(text='서버의 텍스트 채널을 선택하세요.')
        user_permissions, bot_permissions = channel.permissions_for(member), channel.permissions_for(guild.me)
        if not (user_permissions.view_channel and user_permissions.send_messages):
            raise web.HTTPForbidden(text='이 채널을 보거나 메시지를 보낼 권한이 없습니다.')
        if not (bot_permissions.view_channel and bot_permissions.send_messages and bot_permissions.embed_links):
            raise web.HTTPForbidden(text='봇에 채널 보기·메시지 보내기·링크 포함 권한을 부여하세요.')
        return channel

    async def status(self, request):
        s = self.authenticate(request)
        p = store.project(s['user_id'], s['project_id'])
        result = dict(mode='oauth', authenticated=True, username=s['username'], project_id=s['project_id'],
                      project_name=s['project_name'], bot_ready=self.ready(), plain_chat_ready=self.plain_chat_ready(),
                      connected_clients=0, active_channel_id='')
        if p:
            channel = await self.channel(s, p['guild_id'], p['channel_id'])
            result.update(active_channel_id=str(channel.id), channel_name=channel.name,
                          guild_id=p['guild_id'], guild_name=channel.guild.name, development=bool(p['development']),
                          connected_clients=int(self.project_connected(p)))
        return web.json_response(result)

    async def guilds(self, request):
        s = self.authenticate(request)
        bot = self.get_bot()
        rows = {str(g['id']): {'id': str(g['id']), 'name': g['name'], 'bot_present': bool(bot and bot.get_guild(int(g['id'])))} for g in json.loads(s['guilds'])}
        return web.json_response({'guilds': list(rows.values())})

    async def channels(self, request):
        s = self.authenticate(request)
        guild, member = await self.member(s, request.match_info['guild_id'])
        channels = []
        for channel in guild.text_channels:
            a, b = channel.permissions_for(member), channel.permissions_for(guild.me)
            if a.view_channel and a.send_messages and b.view_channel and b.send_messages and b.embed_links:
                channels.append({'id': str(channel.id), 'name': channel.name})
        return web.json_response({'channels': channels, 'guild_id': str(guild.id)})

    async def bind(self, request):
        s = self.authenticate(request)
        body = await request.json()
        channel = await self.channel(s, body.get('guild_id', '0'), body.get('channel_id', '0'))
        try:
            store.bind(s, channel.guild.id, channel.id, body.get('development') is True)
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(text='이 채널은 다른 프로젝트에 연결되어 있습니다. 그 연결을 해제하거나 다른 채널을 선택하세요.')
        return await self.status(request)

    async def invite(self, request):
        self.authenticate(request)
        _, client_id, _ = self.config()
        params = dict(client_id=client_id, scope='bot applications.commands', permissions=str(1024 + 2048 + 16384 + 65536))
        if request.query.get('guild_id', '').isdigit():
            params.update(guild_id=request.query['guild_id'], disable_guild_select='true')
        return web.json_response({'invite_url': 'https://discord.com/oauth2/authorize?' + urlencode(params)})

    async def disconnect(self, request):
        s = self.authenticate(request)
        store.revoke(s)
        await self.close_project((s['user_id'], s['project_id']))
        return web.json_response({'ok': True})

    async def event(self, request):
        s = self.authenticate(request)
        body = await request.json()
        p = store.project(s['user_id'], s['project_id'])
        if not p:
            raise web.HTTPConflict(text='먼저 알림 채널을 연결하세요.')
        event_id, title, detail = str(body.get('event_id', '')), str(body.get('title', '')), str(body.get('detail', ''))
        if not event_id or len(event_id) > 200 or not title or len(title) > 160 or len(detail) > 1000:
            raise web.HTTPBadRequest(text='알림 내용이 올바르지 않습니다.')
        channel = await self.channel(s, p['guild_id'], p['channel_id'])
        key = (s['user_id'], s['project_id'], p['channel_id'], event_id)
        async with self.send_lock:
            if key not in self.sent:
                embed = discord.Embed(title=title, description=detail, colour=0x7985ED)
                embed.set_footer(text=s['project_name'])
                try:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    raise web.HTTPBadGateway(text='Discord 알림 전송에 실패했습니다.')
                self.sent[key] = True
                if len(self.sent) > 1000:
                    self.sent.popitem(last=False)
        return web.json_response({'ok': True, 'event_id': event_id})

    async def close_project(self, key, *, restarting=False):
        current = self.sockets.pop(key, None)
        if current:
            await current[0].close(code=1001 if restarting else 4001,
                                   message=b'Server restarting' if restarting else b'Connection replaced or revoked')

    def project_connected(self, p):
        current = self.sockets.get((p['user_id'], p['project_id']))
        return bool(current and not current[0].closed and store.session(current[1]))

    async def websocket(self, request):
        s = self.authenticate(request)
        key = (s['user_id'], s['project_id'])
        await self.close_project(key)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4096)
        await ws.prepare(request)
        token = request.headers['Authorization'][7:]
        self.sockets[key] = (ws, token)
        await ws.send_json({'type': 'hello', 'project_id': s['project_id'], 'msg': '프로젝트 연결 완료'})
        try:
            async for message in ws:
                if not store.session(token):
                    await ws.close(code=4001)
                    break
                if message.type == aiohttp.WSMsgType.TEXT and message.data == '{"type":"ping"}':
                    await ws.send_json({'type': 'pong'})
        finally:
            if self.sockets.get(key, (None,))[0] is ws:
                self.sockets.pop(key, None)
        return ws

    async def develop(self, interaction, prompt):
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message('서버의 연결된 채널에서 사용하세요.', ephemeral=True)
            return
        p = store.channel_project(interaction.user.id, interaction.guild.id, interaction.channel.id)
        if not p or not p['development']:
            await interaction.response.send_message('VS Code의 Discord 연결에서 이 채널을 선택하고 개발 요청을 허용하세요.', ephemeral=True)
            return
        if not self.project_connected(p):
            await interaction.response.send_message('연결된 프로젝트의 VS Code를 열어 주세요.', ephemeral=True)
            return
        key = (p['user_id'], p['project_id'])
        if key in self.jobs:
            await interaction.response.send_message('이 프로젝트의 작업이 진행 중입니다. 완료 후 다시 요청하세요.', ephemeral=True)
            return
        self.jobs.add(key)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            # Membership and channel permissions are checked again at execution.
            await self.channel(p, p['guild_id'], p['channel_id'])
            await interaction.followup.send('연결된 VS Code로 개발 요청을 전달합니다.', ephemeral=True)
            class Message:
                author = interaction.user
                channel = interaction.channel
                guild = interaction.guild
                content = prompt
                async def reply(self, content, **kwargs):
                    return await interaction.followup.send(content, ephemeral=True, wait=True,
                                                           allowed_mentions=discord.AllowedMentions.none())
            await self.generate(p, Message())
        except Exception:
            await interaction.followup.send('개발 요청에 실패했습니다. VS Code 연결과 봇의 AI 설정을 확인하세요.', ephemeral=True)
        finally:
            self.jobs.discard(key)

    async def develop_message(self, message):
        """Consume messages for an OAuth binding; False permits the existing legacy handler."""
        content = (message.content or '').strip()
        if (message.author.bot or message.webhook_id or not message.guild
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)
                or not content or content.startswith('/') or not self.plain_chat_ready()):
            return True
        p = store.channel_project(message.author.id, message.guild.id, message.channel.id)
        if not p:
            return False
        if not p['development']:
            return True
        if message.id in self.seen_messages:
            return True
        self.seen_messages[message.id] = None
        while len(self.seen_messages) > 1000:
            self.seen_messages.popitem(last=False)

        async def reply(text):
            return await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

        key = (p['user_id'], p['project_id'])
        if key in self.jobs:
            await reply('이 프로젝트의 작업이 진행 중입니다. 완료 후 다시 요청하세요.')
            return True
        self.jobs.add(key)
        try:
            await self.channel(p, p['guild_id'], p['channel_id'])
            if not self.project_connected(p):
                await reply('연결된 프로젝트의 VS Code를 열고 Discord 연결 상태를 확인해 주세요.')
                return True

            class Message:
                author = message.author
                channel = message.channel
                guild = message.guild
                # Only the author's new text is a request, not quoted replies or attachments.
                content = message.content

                async def reply(self, text, **kwargs):
                    return await reply(text)

            await self.generate(p, Message())
        except web.HTTPException:
            await reply('이 채널에서 개발할 권한을 확인하지 못했어요. Discord 채널 권한과 프로젝트 연결을 확인해 주세요.')
        except Exception:
            await reply('개발 요청에 실패했습니다. VS Code 연결과 봇의 AI 설정을 확인해 주세요.')
        finally:
            self.jobs.discard(key)
        return True

    async def generate(self, p, message):
        """Both slash commands and chat use the same verified, revocable project route."""
        key = (p['user_id'], p['project_id'])
        original_socket = self.sockets.get(key)
        service = self

        class Route:
            target = p['project_id']

            def connected(self):
                return service.project_connected(p)

            async def send(self, event):
                current = service.sockets.get(key)
                binding = store.project(p['user_id'], p['project_id'])
                if (not current or current is not original_socket or not store.session(current[1])
                        or not binding or not binding['development']
                        or binding['guild_id'] != p['guild_id'] or binding['channel_id'] != p['channel_id']):
                    raise RuntimeError('프로젝트 연결이 해제되었습니다.')
                await current[0].send_json({**event, 'project_id': p['project_id']})
                return 1

        from make_handler import handle_make_message
        await handle_make_message(self.get_bot(), message, route=Route())

    async def cleanup(self, app):
        for key in list(self.sockets):
            await self.close_project(key, restarting=True)

    def install(self, app):
        async def startup(app):
            store.init_db()
        app.on_startup.append(startup)
        app.on_shutdown.append(self.cleanup)
        for method, route, handler in [
            ('GET', '/info', self.info), ('POST', '/start', self.start),
            ('GET', '/authorize/{attempt}', self.authorize), ('GET', '/callback', self.callback),
            ('POST', '/confirm', self.confirm), ('POST', '/poll', self.poll),
            ('GET', '/status', self.status), ('GET', '/guilds', self.guilds),
            ('GET', '/guilds/{guild_id}/channels', self.channels), ('PUT', '/channel', self.bind),
            ('GET', '/invite', self.invite), ('DELETE', '/session', self.disconnect),
            ('POST', '/events', self.event), ('GET', '/ws', self.websocket),
        ]:
            app.router.add_route(method, PREFIX + route, handler)


@web.middleware
async def connection_errors(request, handler):
    if not request.path.startswith(PREFIX + '/'):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        else:
            response = web.json_response({'error': exc.text}, status=exc.status)
    except (ValueError, TypeError, AttributeError, KeyError):
        response = web.json_response({'error': '연결 요청 형식이 올바르지 않습니다.'}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        response = web.json_response({'error': 'Discord에 연결하지 못했습니다. 잠시 후 다시 시도하세요.'}, status=502)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response
