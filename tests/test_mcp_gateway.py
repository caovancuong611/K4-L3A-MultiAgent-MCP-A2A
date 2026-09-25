from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from student_agent.mcp_gateway import EvidenceGateway


class RecordingContracts:
    def __init__(self) -> None:
        self.validated: list[tuple[object, str]] = []

    def validate_evidence(self, value: object, label: str) -> None:
        self.validated.append((value, label))


class FakeSession:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call_tool(self, name: str, *, arguments: dict[str, str]) -> object:
        self.calls.append((name, arguments))
        return self.result


def test_call_supports_snake_case_mcp_result_fields() -> None:
    evidence = {"schema_version": "day09-mcp-evidence-v1", "data": {}}
    result = SimpleNamespace(
        is_error=False,
        structured_content=evidence,
        content=[],
    )
    session = FakeSession(result)
    contracts = RecordingContracts()

    actual = asyncio.run(
        EvidenceGateway(session, contracts).call(
            "get_order", case_id="CASE_001", order_id="order-1"
        )
    )

    assert actual is evidence
    assert session.calls == [
        ("get_order", {"case_id": "CASE_001", "order_id": "order-1"})
    ]
    assert contracts.validated == [(evidence, "MCP tool get_order")]


def test_call_raises_for_snake_case_error_result() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(text="forbidden")],
    )

    with pytest.raises(RuntimeError, match="forbidden"):
        asyncio.run(
            EvidenceGateway(FakeSession(result), RecordingContracts()).call(
                "get_order", case_id="CASE_001", order_id="order-1"
            )
        )


def test_call_falls_back_to_one_json_text_block() -> None:
    evidence = {"schema_version": "day09-mcp-evidence-v1", "data": {}}
    result = SimpleNamespace(
        is_error=False,
        structured_content=None,
        content=[SimpleNamespace(text=json.dumps(evidence))],
    )

    actual = asyncio.run(
        EvidenceGateway(FakeSession(result), RecordingContracts()).call(
            "get_order", case_id="CASE_001", order_id="order-1"
        )
    )

    assert actual == evidence
