from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

VALID_EVIDENCE: dict[str, Any] = {
    "schema_version": "day09-mcp-evidence-v1",
    "evidence_ref": "ev_" + "a" * 24,
    "result_hash": "sha256:" + "0" * 64,
    "domain": "order",
    "data": {"order_status": "delivered"},
}


class FakeSession:
    def __init__(self, tool_names: list[str], response: dict[str, Any] = VALID_EVIDENCE) -> None:
        self._tool_names = tool_names
        self._response = response
        self.call_tool_invocations: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self._tool_names])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.call_tool_invocations.append((tool_name, arguments))
        # Mirrors the installed `mcp` client's real attribute names (snake_case),
        # not the camelCase JSON-wire aliases.
        return SimpleNamespace(is_error=False, content=[], structured_content=self._response)


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_call_rejects_unknown_tool_without_reaching_the_server() -> None:
    session = FakeSession(tool_names=["get_order"])
    gateway = EvidenceGateway(session, contracts())

    async def scenario() -> None:
        with pytest.raises(ValueError, match="unknown MCP tool"):
            await gateway.call("get_orderr", case_id="L3A_CASE_001")

    asyncio.run(scenario())
    assert session.call_tool_invocations == []


def test_call_discovers_tools_automatically_when_not_listed_yet() -> None:
    session = FakeSession(tool_names=["get_order"])
    gateway = EvidenceGateway(session, contracts())

    async def scenario() -> dict[str, Any]:
        return await gateway.call("get_order", case_id="L3A_CASE_001", order_id="abc")

    evidence = asyncio.run(scenario())
    assert evidence == VALID_EVIDENCE
    assert session.call_tool_invocations == [
        ("get_order", {"case_id": "L3A_CASE_001", "order_id": "abc"})
    ]


def test_list_tools_populates_the_discovery_cache() -> None:
    session = FakeSession(tool_names=["get_order", "get_policy"])
    gateway = EvidenceGateway(session, contracts())

    async def scenario() -> list[str]:
        discovered = await gateway.list_tools()
        await gateway.call("get_policy", case_id="L3A_CASE_001", policy_version="EC_POLICY_V1")
        return discovered

    discovered = asyncio.run(scenario())
    assert discovered == ["get_order", "get_policy"]
    assert session.call_tool_invocations == [
        ("get_policy", {"case_id": "L3A_CASE_001", "policy_version": "EC_POLICY_V1"})
    ]
