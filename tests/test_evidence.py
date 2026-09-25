from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.evidence import CaseEvidenceCollector
from student_agent.trace import TraceWriter


class FakeGateway:
    """Stands in for EvidenceGateway.call without touching the network."""

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._counter = 0

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.invocations.append((tool_name, {"case_id": case_id, **arguments}))
        self._counter += 1
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{self._counter:024d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"seen": self._counter},
        }


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def read_trace_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_fetch_scopes_every_call_to_the_collector_case_id(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    collector = CaseEvidenceCollector(gateway, trace, case_id="L3A_CASE_004")

    async def scenario() -> None:
        await collector.fetch("get_order", actor="order-agent", order_id="abc")

    asyncio.run(scenario())

    assert gateway.invocations == [
        ("get_order", {"case_id": "L3A_CASE_004", "order_id": "abc"})
    ]


def test_fetch_emits_tool_result_consumed_with_the_real_evidence_ref(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts())
    collector = CaseEvidenceCollector(gateway, trace, case_id="L3A_CASE_004")

    async def scenario() -> None:
        await collector.fetch("get_order", actor="order-agent", order_id="abc")

    asyncio.run(scenario())

    events = read_trace_events(trace_path)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "tool_result_consumed"
    assert event["actor"] == "order-agent"
    assert event["tool_name"] == "get_order"
    assert event["evidence_refs"] == ["ev_" + "0" * 23 + "1"]


def test_repeat_fetch_with_same_arguments_is_served_from_cache(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    collector = CaseEvidenceCollector(gateway, trace, case_id="L3A_CASE_004")

    async def scenario() -> None:
        first = await collector.fetch("get_order", actor="order-agent", order_id="abc")
        second = await collector.fetch("get_order", actor="order-agent", order_id="abc")
        assert first is second

    asyncio.run(scenario())

    assert len(gateway.invocations) == 1
    assert len(collector.records) == 1


def test_fetch_with_different_arguments_calls_the_gateway_again(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    collector = CaseEvidenceCollector(gateway, trace, case_id="L3A_CASE_004")

    async def scenario() -> None:
        await collector.fetch("get_order", actor="order-agent", order_id="abc")
        await collector.fetch("get_order", actor="order-agent", order_id="xyz")

    asyncio.run(scenario())

    assert len(gateway.invocations) == 2
    assert len(collector.records) == 2
