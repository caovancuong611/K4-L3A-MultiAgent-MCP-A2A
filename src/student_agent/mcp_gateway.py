from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import Contracts

MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.5


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._known_tools: list[str] | None = None

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        self._known_tools = sorted(tool.name for tool in response.tools)
        return self._known_tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if self._known_tools is None:
            await self.list_tools()
        if tool_name not in self._known_tools:
            raise ValueError(
                f"unknown MCP tool {tool_name!r}: call list_tools() for discovery instead "
                "of guessing a tool name"
            )
        payload = {"case_id": case_id, **arguments}
        result = await self._call_tool_with_retry(tool_name, payload)
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        # The installed `mcp` client exposes this as the snake_case `structured_content`
        # attribute (the "structuredContent" name is only the JSON-wire alias), but we
        # keep both lookups in case a different SDK build restores the camelCase name.
        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence

    async def _call_tool_with_retry(self, tool_name: str, payload: dict[str, Any]) -> Any:
        """One bounded, idempotent retry for a transient network/server blip —
        a real run against the gateway hit both dropped connections
        (`httpx2.TransportError`) and a mid-session 502 surfaced by the mcp
        client as `MCPError`. A tool error the server itself returns cleanly
        (`result.is_error`) is not retried here; it is surfaced as-is."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._session.call_tool(tool_name, arguments=payload)
            except* (httpx2.TransportError, TimeoutError, MCPError):
                if attempt == MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
