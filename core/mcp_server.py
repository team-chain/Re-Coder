"""
Local Core — Q4: MCP (Model Context Protocol) stdio 서버 PoC

설계서 §Q4-A (Must):
- MCP stdio transport 구현
- ReCoder 도구 노출: analyze, deploy, incident, policy
- Extension이 MCP 클라이언트로 직접 연결 가능

MCP 프로토콜:
- JSON-RPC 2.0 over stdio
- 메서드: initialize, tools/list, tools/call
- 도구 결과: content array (text/image/resource)

참조: https://spec.modelcontextprotocol.io
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from core.schemas import MCPRequest, MCPResponse, MCPServerConfig, MCPToolDefinition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 도구 정의 (ReCoder MCP 도구 목록)
# ---------------------------------------------------------------------------

RECODER_MCP_TOOLS: list[MCPToolDefinition] = [
    MCPToolDefinition(
        name="recoder_analyze",
        description="코드 파일을 분석하고 개선 제안을 반환합니다. AST 청킹 + PEV 체인 사용.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "분석할 파일 경로"},
                "focus": {
                    "type": "string",
                    "description": "분석 초점 (security/performance/quality/all)",
                    "default": "all",
                },
            },
            "required": ["file_path"],
        },
    ),
    MCPToolDefinition(
        name="recoder_ecs_deploy",
        description="ECS Fargate Rolling Update를 실행합니다. Preflight → 보안스캔 → SBOM → 배포.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "cluster": {"type": "string", "description": "ECS 클러스터 이름"},
                "service": {"type": "string", "description": "ECS 서비스 이름"},
                "region": {"type": "string", "default": "ap-northeast-2"},
                "image": {"type": "string", "description": "배포할 Docker 이미지 (tag 포함)"},
            },
            "required": ["project_id", "cluster", "service", "image"],
        },
    ),
    MCPToolDefinition(
        name="recoder_argocd_sync",
        description="ArgoCD Application을 동기화합니다 (GitOps Q4 배포).",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "app_name": {"type": "string", "description": "ArgoCD Application 이름"},
                "argocd_server": {"type": "string", "description": "ArgoCD 서버 주소"},
                "argocd_token": {"type": "string", "description": "ArgoCD API 토큰"},
                "target_revision": {"type": "string", "default": "HEAD"},
            },
            "required": ["project_id", "app_name", "argocd_server", "argocd_token"],
        },
    ),
    MCPToolDefinition(
        name="recoder_open_incident",
        description="장애를 등록하고 타임라인 및 RCA를 시작합니다.",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "title": {"type": "string", "description": "장애 제목"},
                "severity": {
                    "type": "string",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "description": "장애 심각도",
                },
            },
            "required": ["project_id", "title", "severity"],
        },
    ),
    MCPToolDefinition(
        name="recoder_create_rollback_pr",
        description="지정된 commit을 revert하는 PR을 자동 생성합니다 (ADR-005).",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "repo_owner": {"type": "string"},
                "repo_name": {"type": "string"},
                "target_commit_sha": {"type": "string"},
                "github_token": {"type": "string"},
                "base_branch": {"type": "string", "default": "main"},
            },
            "required": ["project_id", "repo_owner", "repo_name", "target_commit_sha", "github_token"],
        },
    ),
    MCPToolDefinition(
        name="recoder_policy_evaluate",
        description="OPA 정책을 평가합니다. 배포 허용 여부, 승인 필요 여부 반환.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "평가할 액션 (deploy/access/etc)"},
                "context": {"type": "object", "description": "정책 평가 컨텍스트"},
                "security_level": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
            },
            "required": ["action", "context"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP 서버 구현
# ---------------------------------------------------------------------------

class MCPServer:
    """
    MCP stdio 서버.

    JSON-RPC 2.0 over stdin/stdout.
    각 요청을 처리하고 응답을 stdout에 씁니다.
    """

    def __init__(self, config: Optional[MCPServerConfig] = None) -> None:
        self.config = config or MCPServerConfig(tools=RECODER_MCP_TOOLS)
        self._initialized = False

    async def run(self) -> None:
        """stdin에서 JSON-RPC 요청을 읽어 처리하는 메인 루프."""
        logger.info(
            "MCP server starting: name=%s version=%s transport=%s",
            self.config.server_name, self.config.server_version, self.config.transport,
        )

        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.BaseProtocol, sys.stdout
        )

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break

                raw = line.decode("utf-8").strip()
                if not raw:
                    continue

                request_data = json.loads(raw)
                request = MCPRequest(**request_data)
                response = await self._handle_request(request)

                response_json = response.model_dump_json(exclude_none=True) + "\n"
                writer_transport.write(response_json.encode("utf-8"))

            except json.JSONDecodeError as exc:
                error_resp = MCPResponse(
                    error={"code": -32700, "message": f"Parse error: {exc}"}
                )
                sys.stdout.write(error_resp.model_dump_json(exclude_none=True) + "\n")
                sys.stdout.flush()
            except Exception as exc:
                logger.error("MCP server error: %s", exc, exc_info=True)
                break

    async def _handle_request(self, request: MCPRequest) -> MCPResponse:
        """JSON-RPC 메서드 디스패처."""
        handlers = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "ping": self._handle_ping,
        }

        handler = handlers.get(request.method)
        if handler is None:
            return MCPResponse(
                id=request.id,
                error={
                    "code": -32601,
                    "message": f"Method not found: {request.method}",
                },
            )

        try:
            result = await handler(request)
            return MCPResponse(id=request.id, result=result)
        except Exception as exc:
            logger.error("MCP handler error [%s]: %s", request.method, exc)
            return MCPResponse(
                id=request.id,
                error={"code": -32603, "message": str(exc)},
            )

    async def _handle_initialize(self, request: MCPRequest) -> dict:
        self._initialized = True
        client_info = request.params.get("clientInfo", {})
        logger.info("MCP initialized by client: %s", client_info)
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "logging": {},
            },
            "serverInfo": {
                "name": self.config.server_name,
                "version": self.config.server_version,
            },
        }

    async def _handle_tools_list(self, request: MCPRequest) -> dict:
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                for tool in self.config.tools
            ]
        }

    async def _handle_tools_call(self, request: MCPRequest) -> dict:
        """도구 호출 — Local Core HTTP API로 프록시."""
        tool_name = request.params.get("name")
        tool_args = request.params.get("arguments", {})

        if not tool_name:
            raise ValueError("tools/call: 'name' 파라미터 누락")

        # 도구 존재 확인
        tool_def = next((t for t in self.config.tools if t.name == tool_name), None)
        if tool_def is None:
            raise ValueError(f"도구를 찾을 수 없습니다: {tool_name}")

        # Local Core HTTP API로 위임
        result = await self._call_local_core(tool_name, tool_args)

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2),
                }
            ],
            "isError": result.get("error") is not None,
        }

    async def _handle_ping(self, request: MCPRequest) -> dict:
        return {
            "pong": True,
            "server": self.config.server_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _call_local_core(self, tool_name: str, args: dict) -> dict:
        """Local Core REST API (127.0.0.1:17894)로 도구 호출 프록시."""
        import httpx
        from pathlib import Path
        import json as _json

        # runtime.json에서 포트 + 세션 토큰 읽기
        runtime_file = Path.home() / ".recoder" / "runtime.json"
        try:
            runtime = _json.loads(runtime_file.read_text())
            port = runtime.get("port", 17894)
            token = runtime.get("session_token", "")
        except Exception:
            port = 17894
            token = ""

        # 도구 이름 → API 경로 매핑
        route_map = {
            "recoder_analyze":         ("POST", f"http://127.0.0.1:{port}/analyze"),
            "recoder_ecs_deploy":      ("POST", f"http://127.0.0.1:{port}/ecs/deploy"),
            "recoder_argocd_sync":     ("POST", f"http://127.0.0.1:{port}/gitops/sync"),
            "recoder_open_incident":   ("POST", f"http://127.0.0.1:{port}/incident/open"),
            "recoder_create_rollback_pr": ("POST", f"http://127.0.0.1:{port}/gitops/rollback-pr"),
            "recoder_policy_evaluate": ("POST", f"http://127.0.0.1:{port}/policy/evaluate"),
        }

        method, url = route_map.get(tool_name, ("POST", f"http://127.0.0.1:{port}/unknown"))
        headers = {"X-Session-Token": token, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.request(method, url, json=args, headers=headers)
                return resp.json()
        except Exception as exc:
            return {"error": str(exc), "tool": tool_name}


# ---------------------------------------------------------------------------
# Entry point (stdio 서버로 직접 실행 시)
# ---------------------------------------------------------------------------

async def _main() -> None:
    server = MCPServer()
    await server.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_main())


# 모듈 레벨 싱글톤
mcp_server = MCPServer()
