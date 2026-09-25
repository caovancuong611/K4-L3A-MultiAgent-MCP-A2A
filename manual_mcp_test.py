"""Manual smoke test for the MCP evidence layer.

Makes exactly one real, audited MCP call (get_order for L3A_CASE_001) and
prints the case_id / evidence_ref it received, then writes the observable
trace event to traces/manual-test.jsonl. This is a manual check only, not
part of the graded run (`day09 run` writes to traces/trace.jsonl instead).

Run:
    python manual_mcp_test.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from student_agent.cases import load_case_set
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.evidence import CaseEvidenceCollector
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parent
CASE_ID = "L3A_CASE_001"


async def main() -> None:
    settings = Settings.load(ROOT)
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case_set = load_case_set(ROOT)
    case = case_set.cases[CASE_ID]
    order_id = case["customer_request"]["claimed_order_id"]

    trace_path = ROOT / "traces" / "manual-test.jsonl"
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        await gateway.list_tools()
        collector = CaseEvidenceCollector(gateway, trace, case_id=CASE_ID)
        record = await collector.fetch("get_order", actor="order-agent", order_id=order_id)

    print(f"case_id: {CASE_ID}")
    print(f"evidence_ref: {record.evidence_ref}")
    print(f"domain: {record.domain}")
    print("data:")
    print(json.dumps(record.data, ensure_ascii=False, indent=2))
    print(f"Trace written to: {trace_path}")


if __name__ == "__main__":
    asyncio.run(main())
