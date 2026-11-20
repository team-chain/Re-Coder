"""Discord → 연결된 VS Code 배포·롤백·상태 요청 — 실제 HTTP/WebSocket 경계로 확인한다(Discord 자체만 흉내)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connection_api import PREFIX, ops_intent
from tests.test_connection_api import connected_api, login, chat_message, chat_connection  # noqa: F401 (fixture)


@pytest.mark.parametrize('text,action', [
    ('배포해줘', 'deploy'), ('지금 배포!', 'deploy'), ('배포 해 줘', 'deploy'), ('deploy', 'deploy'),
    ('롤백', 'rollback'), ('되돌려줘', 'rollback'), ('롤백해 주세요', 'rollback'),
    ('상태 알려줘', 'status'), ('배포 상태', 'status'), ('배포는?', 'status'), ('status', 'status'),
    ('배포 버튼 만들어줘', None), ('롤백 기능 추가해줘', None), ('상태 관리 코드 짜줘', None),
    ('hello.py 파일에 인사 페이지 만들어줘', None), ('', None),
])
def test_only_short_commands_become_ops_requests(text, action):
    assert ops_intent(text) == action


def slash(guild, channel, user_id=7):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    response.is_done = lambda: response.send_message.await_count > 0 or response.defer.await_count > 0
    return SimpleNamespace(guild=guild, channel=channel, user=SimpleNamespace(id=user_id, bot=False, display_name='alice'),
                           response=response, followup=SimpleNamespace(send=AsyncMock()))


async def bound(client, development=True):
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    await client.put(PREFIX + '/channel', headers=headers, json={'guild_id': '101', 'channel_id': '201', 'development': development})
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    await ws.receive_json()
    return ws


@pytest.mark.asyncio
async def test_slash_deploy_waits_for_vscode_approval_and_reports_each_step(connected_api):
    client, service, _, guild, channel = connected_api
    ws = await bound(client, development=False)  # 슬래시 배포는 개발 허용과 별개 — 승인은 VS Code 에서
    interaction = slash(guild, channel)
    await service.ops_command(interaction, 'deploy')
    text = interaction.response.send_message.call_args.args[0]
    assert 'VS Code' in text and 'my-project' in text
    assert interaction.response.send_message.call_args.kwargs.get('ephemeral') is not True
    request = await ws.receive_json(timeout=1)
    assert request['type'] == 'ops.request' and request['action'] == 'deploy' and request['requester'] == 'alice'
    assert request['project_id'] == 'a' * 64
    channel.send.reset_mock()

    # 같은 프로젝트에 두 번째 배포 요청은 받지 않는다.
    again = slash(guild, channel)
    await service.ops_command(again, 'deploy')
    assert '이미' in again.response.send_message.call_args.args[0]
    assert again.response.send_message.call_args.kwargs['ephemeral'] is True

    await ws.send_json({'type': 'ops.result', 'request_id': request['request_id'], 'ok': True, 'state': 'accepted',
                        'title': 'VS Code에서 배포를 승인했어요', 'detail': 'S3 정적 사이트로 배포를 시작합니다.'})
    await asyncio.sleep(0.05)
    assert channel.send.await_args.kwargs['embed'].title == 'VS Code에서 배포를 승인했어요'
    assert request['request_id'] in service.ops

    await ws.send_json({'type': 'ops.result', 'request_id': request['request_id'], 'ok': True, 'state': 'done',
                        'title': '배포 완료', 'detail': 'my-bucket', 'url': 'https://example.com/app'})
    await asyncio.sleep(0.05)
    embed = channel.send.await_args.kwargs['embed']
    assert embed.title == '배포 완료' and embed.fields[0].value == 'https://example.com/app'
    assert 'alice' in embed.footer.text
    assert channel.send.await_args.kwargs['allowed_mentions'].everyone is False
    assert request['request_id'] not in service.ops
    await ws.close()


@pytest.mark.asyncio
async def test_results_from_other_sockets_or_unknown_ids_are_ignored_and_bad_urls_dropped(connected_api):
    client, service, _, guild, channel = connected_api
    ws = await bound(client)
    await service.ops_command(slash(guild, channel), 'rollback')
    request = await ws.receive_json(timeout=1)
    assert request['action'] == 'rollback'
    channel.send.reset_mock()
    await ws.send_json({'type': 'ops.result', 'request_id': 'not-a-request', 'ok': True, 'state': 'done', 'title': 'x'})
    await ws.send_json('not json')
    await asyncio.sleep(0.05)
    channel.send.assert_not_awaited()
    await service.ops_result(('8', 'a' * 64), {'request_id': request['request_id'], 'ok': True, 'title': 'spoof'})
    channel.send.assert_not_awaited()
    await ws.send_json({'type': 'ops.result', 'request_id': request['request_id'], 'ok': False, 'state': 'failed',
                        'title': '롤백 결과', 'detail': '실패', 'url': 'javascript:alert(1)'})
    await asyncio.sleep(0.05)
    embed = channel.send.await_args.kwargs['embed']
    assert embed.title == '롤백 결과' and not embed.fields
    await ws.close()


@pytest.mark.asyncio
async def test_status_answers_from_vscode_and_times_out_cleanly(connected_api, monkeypatch):
    client, service, _, guild, channel = connected_api
    ws = await bound(client)
    interaction = slash(guild, channel)
    task = asyncio.create_task(service.ops_command(interaction, 'status'))
    request = await ws.receive_json(timeout=1)
    assert request['action'] == 'status'
    await ws.send_json({'type': 'ops.result', 'request_id': request['request_id'], 'ok': True, 'state': 'status',
                        'title': 'my-project · 배포 완료', 'detail': 'ECS demo/web · 2분 전'})
    await asyncio.wait_for(task, 2)
    assert interaction.followup.send.call_args.kwargs['embed'].title == 'my-project · 배포 완료'
    assert not service.ops

    import connection_api
    monkeypatch.setattr(connection_api, 'OPS_STATUS_SECONDS', 0.1)
    late = slash(guild, channel)
    await service.ops_command(late, 'status')
    await ws.receive_json(timeout=1)
    assert 'VS Code가 응답하지 않았어요' in late.followup.send.call_args.args[0]
    assert not service.ops
    await ws.close()


@pytest.mark.asyncio
async def test_closed_vscode_and_unbound_channels_get_clear_answers(connected_api):
    client, service, _, guild, channel = connected_api
    nobody = slash(guild, channel, user_id=99)
    await service.ops_command(nobody, 'deploy')
    assert 'Discord에서 이 채널을' in nobody.response.send_message.call_args.args[0]
    ws = await bound(client)
    await ws.close()
    await asyncio.sleep(0.05)
    offline = slash(guild, channel)
    await service.ops_command(offline, 'deploy')
    assert 'VS Code가 꺼져 있어요' in offline.response.send_message.call_args.args[0]
    assert not service.ops


@pytest.mark.asyncio
async def test_plain_chat_deploy_goes_to_vscode_but_feature_requests_still_develop(connected_api, monkeypatch):
    client, service, bot, guild, channel = connected_api
    _, ws = await chat_connection(client, bot)
    import api_server
    import make_handler
    from bot import RecoderBot
    monkeypatch.setattr(api_server, 'connections', service)
    message = chat_message(guild, channel, id=9100, content='배포해줘')
    await RecoderBot.on_message(None, message)
    request = await ws.receive_json(timeout=1)
    assert request['type'] == 'ops.request' and request['action'] == 'deploy'
    assert 'VS Code' in message.reply.call_args.args[0]

    seen = []

    async def generate(prompt, filename, language, **kwargs):
        seen.append(prompt)
        await kwargs['emit']({'type': 'chunk', 'text': 'x'})
        return 1, 'end_turn'

    make_handler._SESSIONS.clear()
    monkeypatch.setattr(make_handler, '_stream_bedrock', generate)
    build = chat_message(guild, channel, id=9101, content='hello.py 에 배포 버튼 만들어줘')
    await RecoderBot.on_message(None, build)
    events = [await ws.receive_json(timeout=1) for _ in range(3)]
    assert [e['type'] for e in events] == ['start', 'chunk', 'end']
    assert seen == ['hello.py 에 배포 버튼 만들어줘']
    await ws.close()


@pytest.mark.asyncio
async def test_vscode_restart_does_not_block_new_requests(connected_api):
    client, service, _, guild, channel = connected_api
    token, *_ = await login(client)
    headers = {'Authorization': 'Bearer ' + token}
    await client.put(PREFIX + '/channel', headers=headers, json={'guild_id': '101', 'channel_id': '201', 'development': True})
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)
    await ws.receive_json()
    await service.ops_command(slash(guild, channel), 'deploy')
    first = await ws.receive_json(timeout=1)
    await ws.close()
    ws = await client.ws_connect(PREFIX + '/ws', headers=headers)  # VS Code 다시 시작
    await ws.receive_json()
    again = slash(guild, channel)
    await service.ops_command(again, 'deploy')
    second = await ws.receive_json(timeout=1)
    assert second['request_id'] != first['request_id']
    assert '이미' not in again.response.send_message.call_args.args[0]
    await ws.close()
