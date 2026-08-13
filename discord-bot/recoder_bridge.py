"""
discord-bot/recoder_bridge.py — VSCode 확장 ↔ Discord 봇 WebSocket 브리지

지정 채널에 들어온 메시지를 Bedrock으로 보내고, 생성된 코드를
연결된 VSCode 확장(들)에 실시간으로 스트리밍한다.

[A 모드] BIND=127.0.0.1   — 봇과 확장이 같은 PC. 가장 단순.
[B 모드] BIND=0.0.0.0     — 봇이 클라우드(EC2 등), 확장은 노트북.
        TLS 종단은 앞단(Caddy/Nginx/Cloudflare Tunnel)에서 처리하고
        여기서는 평문 WS만 듣는다. 인증 로직은 동일하게 토큰 사용.

환경변수:
  RECODER_BRIDGE_BIND   = "127.0.0.1" (기본) | "0.0.0.0"
  RECODER_BRIDGE_PORT   = 7780 (기본)
  RECODER_BRIDGE_TOKEN  = 공유 토큰. 빈 값이면 인증 비활성(개발용).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from aiohttp import web, WSMsgType, WSCloseCode

log = logging.getLogger(__name__)

BRIDGE_BIND = os.getenv("RECODER_BRIDGE_BIND", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("RECODER_BRIDGE_PORT", "7780"))
BRIDGE_TOKEN = os.getenv("RECODER_BRIDGE_TOKEN", "")
#: 1 이면 student 라우팅에 토큰 소유 증명을 **강제**한다(운영 권장).
REQUIRE_STUDENT_SECRET = os.getenv("RECODER_BRIDGE_REQUIRE_SECRET", "0") == "1"


class BridgeHub:
    """연결된 VSCode 확장 클라이언트들을 관리하고 봇 → 확장 이벤트를 푸시한다."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._student_of: dict[web.WebSocketResponse, str] = {}  # 연결 → student_id (per-user 라우팅)
        self._lock = asyncio.Lock()
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        if not BRIDGE_TOKEN:
            log.warning(
                "RECODER_BRIDGE_TOKEN 미설정 — 인증 비활성화. "
                "A 모드(localhost) 외에는 절대 사용하지 마세요."
            )

        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, BRIDGE_BIND, BRIDGE_PORT)
        await site.start()

        mode = "B(외부 노출)" if BRIDGE_BIND != "127.0.0.1" else "A(로컬)"
        log.info(
            "ReCoder Bridge 시작: ws://%s:%d/ws (mode=%s)",
            BRIDGE_BIND, BRIDGE_PORT, mode,
        )

    async def stop(self) -> None:
        async with self._lock:
            for ws in list(self._clients):
                try:
                    await ws.close(code=WSCloseCode.GOING_AWAY)
                except Exception:
                    pass
            self._clients.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "clients": len(self._clients)})

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # 토큰 검사: Authorization: Bearer <token>  또는  ?token=<token>
        auth_header = request.headers.get("Authorization", "")
        token = (
            auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        ) or request.query.get("token", "")

        if BRIDGE_TOKEN and token != BRIDGE_TOKEN:
            log.warning("브리지 인증 실패 — IP=%s", request.remote)
            return web.Response(status=401, text="unauthorized")

        # per-user 라우팅 식별자. **자칭 student 를 그대로 믿으면 안 된다** —
        # 공유 BRIDGE_TOKEN 만으로는 아무나 ?student=<피해자sid> 로 붙어 그
        # 학생의 코드 스트림을 받는다. 그래서 student 를 선언하려면 그 학생의
        # **전체 토큰**(?secret= 또는 X-Student-Token)을 함께 제시하고, 링크
        # 시 저장된 해시와 일치해야 한다. secret 없이 student 만 오면 거부.
        student = request.query.get("student", "").strip()
        secret = (
            request.headers.get("X-Student-Token", "")
            or request.query.get("secret", "")
        ).strip()

        if student:
            verified = False
            try:
                import guild_store
                verified = guild_store.verify_student_secret(student, secret)
            except Exception as exc:  # noqa: BLE001
                log.warning("student 소유 검증 실패(%s): %s", student, exc)
            if not verified:
                if REQUIRE_STUDENT_SECRET:
                    log.warning("student=%s 소유 증명 실패 — 연결 거부(IP=%s)",
                                student, request.remote)
                    return web.Response(status=403, text="student ownership not proven")
                # 완화 모드(레거시): 검증 실패 시 그 student 로 라우팅하지 않는다.
                log.warning("student=%s 소유 미검증 — 라우팅 대상에서 제외", student)
                student = ""

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        async with self._lock:
            self._clients.add(ws)
            if student:
                self._student_of[ws] = student
        log.info(
            "확장 클라이언트 연결: %s student=%s (현재 연결 수: %d)",
            request.remote, student or "-", len(self._clients),
        )

        try:
            await ws.send_json({"type": "hello", "msg": "ReCoder bridge connected"})

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "ping":
                            await ws.send_json({"type": "pong"})
                    except Exception:
                        pass
                elif msg.type == WSMsgType.ERROR:
                    log.error("확장 WS 오류: %s", ws.exception())
        finally:
            async with self._lock:
                self._clients.discard(ws)
                self._student_of.pop(ws, None)
            log.info(
                "확장 클라이언트 해제 (남은 연결 수: %d)", len(self._clients)
            )

        return ws

    async def broadcast(self, event: dict[str, Any]) -> int:
        """모든 연결 클라이언트에 이벤트 푸시. 보낸 수 반환."""
        async with self._lock:
            clients = list(self._clients)
        sent = 0
        for ws in clients:
            if ws.closed:
                continue
            try:
                await ws.send_json(event)
                sent += 1
            except (ConnectionResetError, RuntimeError) as exc:
                log.debug("브리지 전송 실패(무시): %s", exc)
        return sent

    async def send_to_student(self, student_id: str, event: dict[str, Any]) -> int:
        """특정 student_id 로 등록된 연결(들)에만 이벤트 전송. 보낸 수 반환."""
        if not student_id:
            return 0
        async with self._lock:
            targets = [
                ws for ws, sid in self._student_of.items()
                if sid == student_id and not ws.closed
            ]
        sent = 0
        for ws in targets:
            try:
                await ws.send_json(event)
                sent += 1
            except (ConnectionResetError, RuntimeError) as exc:
                log.debug("send_to_student 전송 실패(무시): %s", exc)
        return sent

    def student_connected(self, student_id: str) -> bool:
        """해당 student_id 의 확장이 현재 연결돼 있는지."""
        return bool(student_id) and student_id in self._student_of.values()

    @property
    def connected_count(self) -> int:
        return len(self._clients)


# 모듈 단일 인스턴스 — make_handler에서 import해 사용
hub = BridgeHub()
