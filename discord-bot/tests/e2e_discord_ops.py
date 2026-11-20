"""Discord 운영 요청 E2E — 실제 봇 연결 서버(aiohttp) ↔ 실제 확장 DiscordController·BridgeClient(node).

Discord API 만 흉내 낸다. 실행: python discord-bot/tests/e2e_discord_ops.py (먼저 extension 에서 npm run compile)
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'discord-bot'))
os.environ.setdefault('DISCORD_PUBLIC_URL', 'http://127.0.0.1:8765')

import discord  # noqa: E402
from aiohttp import web  # noqa: E402

import guild_store  # noqa: E402
import connection_store as store  # noqa: E402
from connection_api import ConnectionService, connection_errors, PREFIX  # noqa: E402


async def main():
    guild_store.DB_PATH = os.path.join(tempfile.mkdtemp(), 'e2e.db')
    store.init_db()
    rights = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.name = 201, 'dev'
    channel.send = AsyncMock()
    channel.permissions_for.return_value = rights
    guild = SimpleNamespace(id=101, name='team', me=object(), text_channels=[channel],
                            fetch_member=AsyncMock(return_value=object()), get_channel=lambda i: channel if i == 201 else None)
    channel.guild = guild
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda i: guild if i == 101 else None,
                          intents=SimpleNamespace(message_content=True))
    service = ConnectionService(lambda: bot)
    app = web.Application(middlewares=[connection_errors])
    service.install(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    agent = await asyncio.create_subprocess_exec('node', str(ROOT / 'extension/harness/discord-ops-agent.js'),
                                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    lines = []

    async def read():
        while True:
            raw = await agent.stdout.readline()
            if not raw:
                return
            lines.append(json.loads(raw))
    reader = asyncio.create_task(read())

    async def wait(pred, what, timeout=5):
        for _ in range(int(timeout / 0.02)):
            if pred():
                return
            await asyncio.sleep(0.02)
        raise AssertionError('timeout: ' + what)

    await wait(lambda: any(l['event'] == 'project' for l in lines), 'project id')
    project_id = next(l['id'] for l in lines if l['event'] == 'project')
    token = store.issue({'id': '7', 'username': 'alice'}, project_id, 'shop', [{'id': '101', 'name': 'team'}])
    store.bind(store.session(token), 101, 201, True)
    agent.stdin.write((json.dumps({'url': f'ws://127.0.0.1:{port}{PREFIX}/ws', 'token': token}) + '\n').encode())
    await agent.stdin.drain()
    await wait(lambda: service.project_connected(store.project('7', project_id)), 'VS Code socket')
    user = SimpleNamespace(id=7, bot=False, display_name='alice')

    def interaction():
        response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
        response.is_done = lambda: response.send_message.await_count > 0 or response.defer.await_count > 0
        return SimpleNamespace(guild=guild, channel=channel, user=user, response=response, followup=SimpleNamespace(send=AsyncMock()))

    results = []
    # 1) /recoder status — VS Code 가 배포 기록으로 바로 답한다.
    status = interaction()
    await service.ops_command(status, 'status')
    embed = status.followup.send.call_args.kwargs['embed']
    assert embed.title == 'shop · 마지막 배포 완료', embed.title
    assert 'ECS demo / web' in embed.description and embed.fields[0].value == 'https://shop.example'
    results.append(f'status: {embed.title} | {embed.description} | {embed.fields[0].value}')

    # 2) 채팅 "배포해줘" → VS Code 요청 → 승인 → 완료가 채널에 온다.
    message = SimpleNamespace(id=1, guild=guild, channel=channel, content='배포해줘', author=user, webhook_id=None,
                              type=discord.MessageType.default, reply=AsyncMock())
    await service.develop_message(message)
    results.append('chat reply: ' + message.reply.call_args.args[0])
    await wait(lambda: channel.send.await_count >= 2, 'accepted + done in channel')
    titles = [c.kwargs['embed'].title for c in channel.send.await_args_list]
    assert titles == ['VS Code에서 배포를 승인했어요', 'S3 배포 완료'], titles
    final = channel.send.await_args_list[-1].kwargs['embed']
    assert final.fields[0].value == 'https://shop.example' and 'alice' in final.footer.text
    results.append('channel: ' + ' → '.join(titles) + f' | {final.fields[0].value} | {final.footer.text}')
    assert any(l.get('event') == 'vscode-request' and l['action'] == 'deploy' for l in lines)

    # 3) /recoder rollback → VS Code 에서 거절 → 채널에 거절이 온다.
    rb = interaction()
    await service.ops_command(rb, 'rollback')
    await wait(lambda: channel.send.await_count >= 3, 'rollback answer')
    results.append('rollback: ' + channel.send.await_args_list[-1].kwargs['embed'].title)
    assert not service.ops, service.ops

    agent.kill()
    await agent.wait()
    reader.cancel()
    await asyncio.sleep(0.2)
    # 4) VS Code 가 꺼지면 바로 알려 준다.
    off = interaction()
    await service.ops_command(off, 'deploy')
    results.append('offline: ' + off.response.send_message.call_args.args[0])
    assert 'VS Code가 꺼져 있어요' in off.response.send_message.call_args.args[0]
    await runner.cleanup()
    print('\n'.join(results))
    print('E2E PASS')


asyncio.run(main())
