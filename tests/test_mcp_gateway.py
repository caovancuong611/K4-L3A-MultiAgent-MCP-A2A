from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_evidence_gateway_list_tools(contracts: Contracts) -> None:
    async def _test() -> None:
        session = AsyncMock()
        tool_a = MagicMock()
        tool_a.name = "get_order"
        tool_b = MagicMock()
        tool_b.name = "get_policy"
        response = MagicMock()
        response.tools = [tool_b, tool_a]
        session.list_tools.return_value = response

        gateway = EvidenceGateway(session, contracts)
        tools = await gateway.list_tools()
        assert tools == ["get_order", "get_policy"]

    asyncio.run(_test())


def test_evidence_gateway_call_success(contracts: Contracts) -> None:
    async def _test() -> None:
        session = AsyncMock()
        call_result = MagicMock()
        call_result.is_error = False
        call_result.structuredContent = None
        call_result.structured_content = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_012345678901234567890123",
            "result_hash": (
                "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
            "domain": "order",
            "data": {"order_id": "test_order"},
        }
        session.call_tool.return_value = call_result

        gateway = EvidenceGateway(session, contracts)
        result = await gateway.call("get_order", case_id="CASE_001", order_id="test_order")
        assert result["evidence_ref"] == "ev_012345678901234567890123"
        assert result["domain"] == "order"

    asyncio.run(_test())


def test_evidence_gateway_call_error(contracts: Contracts) -> None:
    async def _test() -> None:
        session = AsyncMock()
        call_result = MagicMock()
        call_result.is_error = True
        text_block = MagicMock()
        text_block.text = "order not found"
        call_result.content = [text_block]
        session.call_tool.return_value = call_result

        gateway = EvidenceGateway(session, contracts)
        with pytest.raises(RuntimeError, match="order not found"):
            await gateway.call("get_order", case_id="CASE_001", order_id="missing_order")

    asyncio.run(_test())
