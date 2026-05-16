"""
mcp_server.py — MCP 서버화 (설계서 §Q4 Must — local stdio PoC).

설계서 명세:
    Q4 Must : local stdio PoC 만. recoder_analyze 도구 하나만 제공.
    Backlog : Streamable HTTP remote, recoder_deploy, recoder_operate, OAuth 기반 remote 인증.

    Transport / 인증 표:
      | Transport             | 인증                       | 비고 |
      | stdio                | X-Session-Token 내부 검증  | 로컬 Claude Desktop / Cursor 연동 |
      | local HTTP           | X-Session-Token 필수, Origin/Host 검증 | 127.0.0.1 바인딩 |
      | Streamable HTTP remote | Device Token 또는 OAuth, allowlist origin | 기본 비활성화, Backlog |

본 모듈은 다음만 한다.
  1. stdio 기반 JSON-RPC 2.0 루프 — MCP spec 의 핵심 메서드만 직접 처리한다
     (`initialize`, `tools/list`, `tools/call`, `shutdown`).
     mcp Python SDK 가 설치돼 있으면 그것을 사용하고, 없으면 fallback 으로 직접 처리한다.
  2. recoder_analyze 도구 한 개만 노출 (recoder_deploy/recoder_operate 는 주석 처리 — Backlog).
  3. X-Session-Token 은 환경변수 SESSION_TOKEN 으로 받아 내부 검증한다.
  4. raw 파일 내용은 받지 않는다. 클라이언트는 workspace_path 와 error_message 만 보낸다.

CLI 사용:
    python -m core.mcp_server
또는
    python core/mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas (lazy import — module import 자체는 가벼움 유지)
# ---------------------------------------------------------------------------


def _schemas():
    try:
        from schemas import MCPToolDescriptor
    except ImportError:
        from core.schemas import MCPToolDescriptor  # type: ignore
    return MCPToolDescriptor


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------


_RECODER_ANALYZE_SCHEMA = {
    "type": "object",
    "required": ["workspace_path", "error_message"],
    "properties": {
        "workspace_path": {
            "type": "string",
            "description": "Absolute path to the user's workspace (raw source code never leaves the machine).",
        },
        "error_message": {
            "type": "string",
            "description": "User-visible error message or traceback excerpt (will be masked by Context Gate).",
        },
        "active_file_path": {
            "type": "string",
            "description": "Optional — file currently focused in the editor.",
        },
    },
}


def list_tools() -> list[dict[str, Any]]:
    MCPToolDescriptor = _schemas()
    tools = [
        MCPToolDescriptor(
            name="recoder_analyze",
            description=(
                "Run the ReCoder Local Core analyzer on an error context. "
                "Returns a structured PatchProposal (no shell execution)."
            ),
            input_schema=_RECODER_ANALYZE_SCHEMA,
        )
        # Backlog — Q4 이후
        # MCPToolDescriptor(name="recoder_deploy",  ...)
        # MCPToolDescriptor(name="recoder_operate", ...)
    ]
    return [t.model_dump() for t in tools]


# ---------------------------------------------------------------------------
# Tool implementations — 실제 호출은 server.py 의 analyzer pipeline 으로 위임한다
# ---------------------------------------------------------------------------


async def _call_recoder_analyze(arguments: dict[str, Any]) -> dict[str, Any]:
    workspace_path = arguments.get("workspace_path")
    error_message = arguments.get("error_message")
    if not workspace_path or not error_message:
        return _error("workspace_path and error_message are required")

    try:
        # analyzer 모듈을 lazy import
        try:
            from analyzer import analyze
            from schemas import AnalyzeRequest
        except ImportError:
            from core.analyzer import analyze  # type: ignore
            from core.schemas import AnalyzeRequest  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return _error(f"analyzer module unavailable: {exc}")

    req = AnalyzeRequest(
        workspace_path=workspace_path,
        terminal_output=error_message,
        active_file_path=arguments.get("active_file_path"),
    )

    try:
        # analyzer.analyze 가 sync 든 async 든 동작하도록 처리
        result = analyze(req)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        return _error(f"analyze failed: {exc}")

    # MCP content block 으로 직렬화 (JSON 텍스트 1개)
    return _ok_json(_to_jsonable(result))


_TOOL_DISPATCH: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "recoder_analyze": _call_recoder_analyze,
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------


def _ok_json(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "isError": False,
    }


def _error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _to_jsonable(obj: Any) -> Any:
    """Pydantic v2 모델이면 model_dump, 아니면 그대로."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Server loop (stdio, JSON-RPC 2.0)
# ---------------------------------------------------------------------------


_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "recoder-mcp", "version": "0.1.0"}


def _check_token() -> Optional[str]:
    """X-Session-Token 내부 검증. CI 모드 / dev 모드에서는 SKIP."""

    expected = os.environ.get("SESSION_TOKEN")
    if not expected or os.environ.get("DEV_MODE", "0") in ("1", "true", "yes"):
        return None
    provided = os.environ.get("MCP_SESSION_TOKEN") or os.environ.get("X_SESSION_TOKEN")
    if not provided:
        return "SESSION_TOKEN required (set MCP_SESSION_TOKEN env var)"
    if provided != expected:
        return "invalid session token"
    return None


async def _handle_message(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _SERVER_INFO,
            },
        }

    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": msg_id, "result": None}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": list_tools()},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = _TOOL_DISPATCH.get(name or "")
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"tool not found: {name}"},
            }
        result = await handler(arguments)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # 응답을 안 요구하는 notification 인 경우 id 가 없다
    if msg_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


async def _stdio_loop() -> None:
    err = _check_token()
    if err:
        sys.stderr.write(f"[mcp_server] {err}\n")
        sys.exit(2)

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        sys.stdout,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)

    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            msg = json.loads(line.decode("utf-8").strip())
        except json.JSONDecodeError:
            continue
        try:
            response = await _handle_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("mcp handler failed")
            response = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        if response is not None:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[mcp_server] %(message)s")
    try:
        asyncio.run(_stdio_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["list_tools", "main"]
