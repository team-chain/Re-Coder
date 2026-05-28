"""
End-to-End: BridgeHub 가 chunk 들을 받았을 때, 클라이언트(VSCode 확장
역할) 가 도착 순서대로 모두 받아 누락 없이 텍스트를 재구성하는지 검증.

이전 회귀 케이스:
  - 빠른 연속 chunk 가 클라이언트의 await edit() 동안 race 가 나서
    일부 chunk 가 누락되거나 순서가 바뀌어 파일이 깨졌었다.

여기서는 봇 측 broadcast 만 검증한다:
  - 모든 chunk 가 손실 없이 도착하는지
  - start/chunk*/end 순서가 보존되는지
  - 동시 다중 클라이언트에도 전부 전파되는지
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

from recoder_bridge import BridgeHub


@pytest.fixture
def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def hub_running(monkeypatch, free_port):
    """BridgeHub 를 임시 포트에서 띄우고, 종료 시 cleanup."""
    monkeypatch.setattr("recoder_bridge.BRIDGE_BIND", "127.0.0.1")
    monkeypatch.setattr("recoder_bridge.BRIDGE_PORT", free_port)
    monkeypatch.setattr("recoder_bridge.BRIDGE_TOKEN", "")  # 테스트는 인증 비활성

    h = BridgeHub()
    await h.start()
    try:
        yield h, free_port
    finally:
        await h.stop()


async def _consume_until_end(ws, expected_filename: str):
    """ws 에서 end 이벤트가 올 때까지 모든 이벤트를 수집."""
    events = []
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            ev = json.loads(msg.data)
            events.append(ev)
            if ev.get("type") == "end" and ev.get("filename") == expected_filename:
                break
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break
    return events


@pytest.mark.asyncio
async def test_single_client_receives_all_chunks_in_order(hub_running):
    """단일 클라이언트가 100개 chunk 를 전부 도착 순서대로 받는지."""
    hub, port = hub_running

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
            # hello 한 번 받기
            hello = await ws.receive_json()
            assert hello["type"] == "hello"

            consumer = asyncio.create_task(_consume_until_end(ws, "x.html"))
            await asyncio.sleep(0)  # consumer 시작 보장

            await hub.broadcast({"type": "start", "filename": "x.html",
                                 "language": "HTML", "prompt": "p"})

            # 100개 chunk 를 빠르게 broadcast
            sent_texts = []
            for i in range(100):
                t = f"chunk-{i:03d}\n"
                sent_texts.append(t)
                await hub.broadcast({"type": "chunk", "text": t})

            await hub.broadcast({"type": "end", "filename": "x.html"})

            events = await consumer

    # 기대: start + chunk*100 + end
    assert events[0]["type"] == "start"
    chunk_events = [e for e in events if e.get("type") == "chunk"]
    assert len(chunk_events) == 100, f"got {len(chunk_events)} chunks"
    received_texts = [e["text"] for e in chunk_events]
    assert received_texts == sent_texts, "chunk order corrupted"
    assert events[-1]["type"] == "end"


@pytest.mark.asyncio
async def test_multiple_clients_receive_same_stream(hub_running):
    """동시 연결된 여러 클라이언트가 같은 이벤트 시퀀스를 받는지."""
    hub, port = hub_running

    async def connect_and_collect():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
                await ws.receive_json()  # hello
                return await _consume_until_end(ws, "shared.html")

    async with aiohttp.ClientSession() as session:
        ws1 = await session.ws_connect(f"ws://127.0.0.1:{port}/ws")
        ws2 = await session.ws_connect(f"ws://127.0.0.1:{port}/ws")
        try:
            await ws1.receive_json()  # hello
            await ws2.receive_json()

            t1 = asyncio.create_task(_consume_until_end(ws1, "shared.html"))
            t2 = asyncio.create_task(_consume_until_end(ws2, "shared.html"))
            await asyncio.sleep(0)

            await hub.broadcast({"type": "start", "filename": "shared.html"})
            for i in range(20):
                await hub.broadcast({"type": "chunk", "text": f"c{i}\n"})
            await hub.broadcast({"type": "end", "filename": "shared.html"})

            ev1, ev2 = await asyncio.gather(t1, t2)
        finally:
            await ws1.close()
            await ws2.close()

    chunks1 = [e["text"] for e in ev1 if e.get("type") == "chunk"]
    chunks2 = [e["text"] for e in ev2 if e.get("type") == "chunk"]
    assert chunks1 == chunks2
    assert len(chunks1) == 20


@pytest.mark.asyncio
async def test_chunk_after_end_for_next_session(hub_running):
    """이전 세션 종료 후 새 start → chunk → end 사이클이 깨끗하게 분리되는지."""
    hub, port = hub_running

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
            await ws.receive_json()  # hello

            # 첫 세션
            t1 = asyncio.create_task(_consume_until_end(ws, "a.html"))
            await asyncio.sleep(0)
            await hub.broadcast({"type": "start", "filename": "a.html"})
            await hub.broadcast({"type": "chunk", "text": "A1\n"})
            await hub.broadcast({"type": "end", "filename": "a.html"})
            ev_a = await t1

            # 두 번째 세션
            t2 = asyncio.create_task(_consume_until_end(ws, "b.html"))
            await asyncio.sleep(0)
            await hub.broadcast({"type": "start", "filename": "b.html"})
            await hub.broadcast({"type": "chunk", "text": "B1\n"})
            await hub.broadcast({"type": "end", "filename": "b.html"})
            ev_b = await t2

    assert [e["type"] for e in ev_a] == ["start", "chunk", "end"]
    assert [e["type"] for e in ev_b] == ["start", "chunk", "end"]
    assert ev_a[1]["text"] == "A1\n"
    assert ev_b[1]["text"] == "B1\n"


@pytest.mark.asyncio
async def test_unicode_chunks_preserved(hub_running):
    """한글/이모지 등 멀티바이트 텍스트가 손상 없이 도착하는지."""
    hub, port = hub_running

    samples = [
        "한글 텍스트\n",
        "이모지 🎮 게임\n",
        "<style>.btn { color: #ff0; }</style>\n",
        "`백틱` 과 ```펜스``` 가 본문에 있어도 OK\n",
        "특수문자: <>&\"'\n",
    ]

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
            await ws.receive_json()
            consumer = asyncio.create_task(_consume_until_end(ws, "u.html"))
            await asyncio.sleep(0)
            await hub.broadcast({"type": "start", "filename": "u.html"})
            for s in samples:
                await hub.broadcast({"type": "chunk", "text": s})
            await hub.broadcast({"type": "end", "filename": "u.html"})
            events = await consumer

    received = [e["text"] for e in events if e.get("type") == "chunk"]
    assert received == samples
