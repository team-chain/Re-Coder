"""Exercise login through the real HTTP/WebSocket boundary; Discord itself is mocked."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import connection_store as store
from connection_api import ConnectionService, connection_errors, PREFIX


@pytest_asyncio.fixture
async def connected_api(temp_db, monkeypatch):
    monkeypatch.setenv('DISCORD_CLIENT_ID', '123456789')
    monkeypatch.setenv('DISCORD_CLIENT_SECRET', 'operator-only-secret')
    monkeypatch.setenv('DISCORD_PUBLIC_URL', 'http://127.0.0.1:8765')
    rights = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.name = 201, 'development'
    channel.send = AsyncMock()
    channel.permissions_for.return_value = rights
    guild = SimpleNamespace(id=101, name='team', me=object(), text_channels=[channel],
                            fetch_member=AsyncMock(return_value=object()), get_channel=lambda id: channel if id == 201 else None)
    channel.guild = guild
    bot = SimpleNamespace(is_ready=lambda: True, get_guild=lambda id: guild if id == 101 else None)
    service = ConnectionService(lambda: bot)
    service.discord_identity = AsyncMock(return_value=({'id': '7', 'username': 'alice'}, [{'id': '101', 'name': 'team'}]))
    app = web.Application(middlewares=[connection_errors])
    service.install(app)
    client = TestClient(TestServer(app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield client, service, bot, guild, channel
    await client.close()


async def login(client, project='a' * 64):
    verifier = 'v' * 64
    response = await client.post(PREFIX + '/start', json={'challenge': store.digest(verifier), 'project_id': project, 'project_name': 'my-project'})
    assert response.status == 200
    data = await response.json()
    key = data['attempt']
    response = await client.get(PREFIX + '/authorize/' + key, allow_redirects=False)
    assert response.status == 302
    assert 'scope=identify+guilds' in response.headers['Location']
    response = await client.get(PREFIX + '/callback', params={'state': key, 'code': 'discord-code'})
    assert response.status == 200
    assert data['confirmation_code'] in await response.text()
    response = await client.post(PREFIX + '/confirm', data={'state': key, 'decision': 'approve'})
    assert response.status == 200
    response = await client.post(PREFIX + '/poll', json={'attempt': key, 'verifier': verifier})
    assert response.status == 200
    return (await response.json())['token'], key, verifier


@pytest.mark.asyncio
async def test_login_is_confirmed_one_time_and_token_hashed(connected_api):
    client, service, *_ = connected_api
    token, key, verifier = await login(client)
    assert store.session(token)['user_id'] == '7'
    assert token not in str(store.session(token))
    assert (await client.post(PREFIX + '/poll', json={'attempt': key, 'verifier': verifier})).status == 410
    assert (await client.get(PREFIX + '/status')).status == 401
    status = await (await client.get(PREFIX + '/status', headers={'Authorization': 'Bearer ' + token})).json()
    assert status['username'] == 'alice'
    assert 'guilds' not in status and 'token_hash' not in status


@pytest.mark.asyncio
async def test_state_cookie_verifier_and_expiry_are_required(connected_api):
    client, service, *_ = connected_api
    data = await (await client.post(PREFIX + '/start', json={'challenge': store.digest('v' * 64), 'project_id': 'b' * 64, 'project_name': '<unsafe>'})).json()
    key = data['attempt']
    assert (await client.get(PREFIX + '/callback', params={'state': key, 'code': 'code'})).status == 403
    assert (await client.post(PREFIX + '/poll', json={'attempt': key, 'verifier': 'w' * 64})).status == 403
    await client.get(PREFIX + '/authorize/' + key, allow_redirects=False)
    response = await client.get(PREFIX + '/callback', params={'state': key, 'code': 'code'})
    assert '&lt;unsafe&gt;' in await response.text()
    # OAuth acceptance alone must not connect an arbitrary project.
    status = await (await client.post(PREFIX + '/poll', json={'attempt': key, 'verifier': 'v' * 64})).json()
    assert status['status'] == 'confirm'
    service.pending[key]['expires'] = 0
    assert (await client.post(PREFIX + '/confirm', data={'state': key, 'decision': 'approve'})).status == 410


@pytest.mark.asyncio
async def test_cancel_and_missing_operator_configuration(connected_api, monkeypatch):
    client, service, bot, *_ = connected_api
    monkeypatch.setenv('DISCORD_CLIENT_SECRET', '')
    assert (await client.get(PREFIX + '/info')).status == 200
    assert (await client.post(PREFIX + '/start', json={})).status == 503
    monkeypatch.setenv('DISCORD_CLIENT_SECRET', 'secret')
    bot.is_ready = lambda: False
    assert (await client.post(PREFIX + '/start', json={})).status == 503


@pytest.mark.asyncio
async def test_project_binding_notifications_dedup_and_disconnect(connected_api):
    client, service, _, _, channel = connected_api
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    event = {'event_id': 'finished', 'title': 'Deploy complete', 'detail': 'done'}
    assert (await client.post(PREFIX + '/events', json=event, headers=headers)).status == 409
    response = await client.put(PREFIX + '/channel', headers=headers, json={'guild_id': '101', 'channel_id': '201', 'development': True})
    assert response.status == 200
    assert (await response.json())['development'] is True
    for _ in range(2):
        assert (await client.post(PREFIX + '/events', json=event, headers=headers)).status == 200
    channel.send.assert_awaited_once()
    assert channel.send.call_args.kwargs['allowed_mentions'].everyone is False
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    assert (await ws.receive_json())['project_id'] == 'a' * 64
    assert service.project_connected(store.project('7', 'a' * 64))
    assert (await client.delete(PREFIX + '/session', headers=headers)).status == 200
    await ws.receive(timeout=1)
    assert store.session(token) is None
    assert store.project('7', 'a' * 64) is None
    assert (await client.post(PREFIX + '/events', json=event, headers=headers)).status == 401
    await ws.close()


@pytest.mark.asyncio
async def test_other_project_cannot_rebind_same_channel_and_permissions_checked(connected_api):
    client, service, _, guild, channel = connected_api
    first, *_ = await login(client)
    h1 = {'Authorization': 'Bearer ' + first}
    binding = {'guild_id': '101', 'channel_id': '201', 'development': True}
    assert (await client.put(PREFIX + '/channel', headers=h1, json=binding)).status == 200
    second, *_ = await login(client, 'b' * 64)
    h2 = {'Authorization': 'Bearer ' + second}
    assert (await client.put(PREFIX + '/channel', headers=h2, json=binding)).status == 409
    assert (await (await client.get(PREFIX + '/status', headers=h2)).json())['active_channel_id'] == ''
    assert store.project('7', 'a' * 64)['channel_id'] == '201'
    channel.permissions_for.return_value = SimpleNamespace(view_channel=False, send_messages=False, embed_links=False)
    assert (await client.put(PREFIX + '/channel', headers=h1, json=binding)).status == 403
    assert (await client.post(PREFIX + '/events', headers=h1, json={'event_id':'new','title':'hello','detail':''})).status == 403
    assert (await (await client.get(PREFIX + '/guilds/101/channels', headers=h1)).json())['channels'] == []


@pytest.mark.asyncio
async def test_develop_only_reaches_owner_project_and_disabled_is_rejected(connected_api, monkeypatch):
    client, service, _, guild, channel = connected_api
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    binding = {'guild_id': '101', 'channel_id': '201', 'development': True}
    await client.put(PREFIX + '/channel', headers=headers, json=binding)
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    await ws.receive_json()
    second, *_ = await login(client, 'b' * 64)
    other = await client.ws_connect(PREFIX + '/ws', headers={'Authorization': 'Bearer ' + second})
    await other.receive_json()
    import make_handler
    async def generate(bot, message, *, route):
        assert route.target == 'a' * 64
        await route.send({'type': 'start', 'filename': 'test.html'})
        await route.send({'type': 'end', 'filename': 'test.html'})
    mocked = AsyncMock(side_effect=generate)
    monkeypatch.setattr(make_handler, 'handle_make_message', mocked)
    interaction = SimpleNamespace(guild=guild, channel=channel, user=SimpleNamespace(id=7),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    await service.develop(interaction, 'make a page')
    assert (await ws.receive_json())['type'] == 'start'
    assert (await ws.receive_json())['type'] == 'end'
    with pytest.raises(asyncio.TimeoutError):
        await other.receive_json(timeout=.05)
    mocked.assert_awaited_once()
    interaction.user.id = 99
    await service.develop(interaction, 'steal project')
    mocked.assert_awaited_once()
    interaction.user.id = 7
    await client.put(PREFIX + '/channel', headers=headers, json={**binding, 'development': False})
    await service.develop(interaction, 'not enabled')
    mocked.assert_awaited_once()
    await ws.close()
    await other.close()

@pytest.mark.asyncio
async def test_real_generation_handler_streams_ordered_fixture_into_project_socket(connected_api, monkeypatch):
    client, service, _, guild, channel = connected_api
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    await client.put(PREFIX + '/channel', headers=headers, json={'guild_id':'101','channel_id':'201','development':True})
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    await ws.receive_json()
    import make_handler
    async def generate(prompt, filename, language, **kwargs):
        for value in ['print(', '"hello"', ')\n']:
            await kwargs['emit']({'type':'chunk','text':value})
        return 3, 'end_turn'
    monkeypatch.setattr(make_handler, '_stream_bedrock', generate)
    status_message = SimpleNamespace(edit=AsyncMock())
    interaction = SimpleNamespace(guild=guild, channel=channel, user=SimpleNamespace(id=7,bot=False),
        response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=status_message)))
    await service.develop(interaction,'hello.py 파일 만들어줘')
    events = [await ws.receive_json(timeout=1) for _ in range(5)]
    assert [e['type'] for e in events] == ['start','chunk','chunk','chunk','end']
    assert all(e['project_id']=='a'*64 for e in events)
    assert ''.join(e.get('text','') for e in events) == 'print("hello")\n'
    assert store.channel_project('7','101','201')['project_id'] == 'a'*64
    status_message.edit.assert_awaited_once()
    await ws.close()


def chat_message(base_guild, base_channel, **changes):
    value = dict(id=9001, guild=base_guild, channel=base_channel, content='hello.py 파일에 인사 페이지 만들어줘',
                 author=SimpleNamespace(id=7, bot=False), webhook_id=None,
                 type=discord.MessageType.default,
                 reply=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
    value.update(changes)
    return SimpleNamespace(**value)


async def chat_connection(client, bot):
    bot.intents = SimpleNamespace(message_content=True)
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    await client.put(PREFIX + '/channel', headers=headers,
                     json={'guild_id': '101', 'channel_id': '201', 'development': True})
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    await ws.receive_json()
    return headers, ws


@pytest.mark.asyncio
async def test_plain_chat_on_message_streams_without_prefix_to_own_project(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    headers, ws = await chat_connection(client, bot)
    import api_server
    import make_handler
    from bot import RecoderBot
    monkeypatch.setattr(api_server, 'connections', service)
    make_handler._SESSIONS.clear()
    seen = []

    async def generate(prompt, filename, language, **kwargs):
        seen.append(prompt)
        for value in ['print(', '"hello")\n']:
            await kwargs['emit']({'type': 'chunk', 'text': value})
        return 2, 'end_turn'

    monkeypatch.setattr(make_handler, '_stream_bedrock', generate)
    message = chat_message(guild, channel)
    await RecoderBot.on_message(None, message)
    events = [await ws.receive_json(timeout=1) for _ in range(4)]
    assert [event['type'] for event in events] == ['start', 'chunk', 'chunk', 'end']
    assert all(event['project_id'] == 'a' * 64 for event in events)
    assert seen == [message.content]
    message.reply.assert_awaited_once()
    assert message.reply.call_args.kwargs['allowed_mentions'].everyone is False
    assert message.reply.call_args.kwargs['mention_author'] is False
    # A re-delivered Gateway event must not execute a second time.
    await RecoderBot.on_message(None, message)
    assert len(seen) == 1
    assert (await (await client.get(PREFIX + '/info')).json())['plain_chat_ready'] is True
    assert (await (await client.get(PREFIX + '/status', headers=headers)).json())['plain_chat_ready'] is True
    await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'author': SimpleNamespace(id=7, bot=True)}, {'webhook_id': 123}, {'guild': None},
    {'type': discord.MessageType.pins_add}, {'content': '   '},
    {'content': '/recoder develop prompt:do not duplicate'}, {'content': ''},
])
async def test_plain_chat_ignores_non_requests(connected_api, monkeypatch, changes):
    client, service, bot, guild, channel = connected_api
    _, ws = await chat_connection(client, bot)
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate', generate)
    message = chat_message(guild, channel, **changes)
    assert await service.develop_message(message) is True
    generate.assert_not_awaited()
    message.reply.assert_not_awaited()
    await ws.close()


@pytest.mark.asyncio
async def test_plain_chat_requires_intent_owner_channel_and_development_opt_in(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    headers, ws = await chat_connection(client, bot)
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate', generate)
    for changes in [{'author': SimpleNamespace(id=99, bot=False)}, {'channel': SimpleNamespace(id=999)}]:
        message = chat_message(guild, channel, **changes)
        assert await service.develop_message(message) is False
        message.reply.assert_not_awaited()
    bot.intents.message_content = False
    assert await service.develop_message(chat_message(guild, channel)) is True
    assert (await (await client.get(PREFIX + '/info')).json())['plain_chat_ready'] is False
    bot.intents.message_content = True
    await client.put(PREFIX + '/channel', headers=headers,
                     json={'guild_id': '101', 'channel_id': '201', 'development': False})
    # Disabled OAuth bindings are consumed, so they never reach the legacy handler.
    assert await service.develop_message(chat_message(guild, channel)) is True
    generate.assert_not_awaited()
    await ws.close()


@pytest.mark.asyncio
async def test_plain_chat_rechecks_permissions_and_reports_disconnected_project(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    headers, ws = await chat_connection(client, bot)
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate', generate)
    rights = channel.permissions_for.return_value
    channel.permissions_for.return_value = SimpleNamespace(view_channel=False, send_messages=False, embed_links=False)
    denied = chat_message(guild, channel)
    assert await service.develop_message(denied) is True
    assert '권한' in denied.reply.call_args.args[0]
    channel.permissions_for.return_value = rights
    await service.close_project(('7', 'a' * 64))
    offline = chat_message(guild, channel, id=9002)
    assert await service.develop_message(offline) is True
    assert 'VS Code' in offline.reply.call_args.args[0]
    generate.assert_not_awaited()
    assert not service.jobs
    await ws.close()


@pytest.mark.asyncio
async def test_plain_chat_and_slash_share_busy_lock(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    _, ws = await chat_connection(client, bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(p, message):
        started.set()
        await release.wait()

    generate_mock = AsyncMock(side_effect=generate)
    monkeypatch.setattr(service, 'generate', generate_mock)
    task = asyncio.create_task(service.develop_message(chat_message(guild, channel)))
    await asyncio.wait_for(started.wait(), 1)
    try:
        second = chat_message(guild, channel, id=9002)
        await service.develop_message(second)
        assert '진행 중' in second.reply.call_args.args[0]
        interaction = SimpleNamespace(guild=guild, channel=channel, user=SimpleNamespace(id=7),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await service.develop(interaction, 'another request')
        assert '진행 중' in interaction.response.send_message.call_args.args[0]
        generate_mock.assert_awaited_once()
    finally:
        release.set()
        await task
    assert not service.jobs
    await ws.close()


@pytest.mark.asyncio
async def test_plain_chat_stops_stream_when_development_is_disabled(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    headers, ws = await chat_connection(client, bot)
    import make_handler

    async def generate(bot, message, *, route):
        await client.put(PREFIX + '/channel', headers=headers,
                         json={'guild_id': '101', 'channel_id': '201', 'development': False})
        await route.send({'type': 'start', 'filename': 'must-not-be-written.py'})

    monkeypatch.setattr(make_handler, 'handle_make_message', generate)
    message = chat_message(guild, channel)
    assert await service.develop_message(message) is True
    assert '실패' in message.reply.call_args.args[0]
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_json(timeout=.05)
    assert not service.jobs
    await ws.close()


@pytest.mark.asyncio
async def test_server_shutdown_keeps_session_and_allows_bridge_reconnect(connected_api):
    client, service, bot, *_ = connected_api
    headers, ws = await chat_connection(client, bot)
    await service.cleanup(None)
    event = await ws.receive(timeout=1)
    assert event.type == aiohttp.WSMsgType.CLOSE
    assert event.data == 1001
    assert store.session(headers['Authorization'][7:]) is not None
    await ws.close()
