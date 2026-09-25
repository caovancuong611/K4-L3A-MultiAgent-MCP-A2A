from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import httpx2

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")

from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.trace import TraceWriter


async def ensure_active_run(settings: Settings) -> None:
    headers = {
        "Authorization": f"Bearer {settings.team_api_key}",
        "Content-Type": "application/json",
    }
    # Direct competition host endpoint is preferred to avoid 302 timeout issues
    api_url = "https://day09-competition.34-142-201-239.sslip.io/api/v2/runs"
    try:
        async with httpx2.AsyncClient(timeout=15.0) as client:
            await client.post(api_url, headers=headers, json={"variant_id": "l3a"})
    except Exception:
        pass


async def main() -> None:
    root = Path(__file__).resolve().parent
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")

    await ensure_active_run(settings)

    case_path = root / "inputs" / "L3A_CASE_001.json"
    case_data = json.loads(case_path.read_text(encoding="utf-8"))
    case_id = case_data["case_id"]
    claimed_order_id = case_data["customer_request"]["claimed_order_id"]

    trace_path = root / "traces" / "manual-test.jsonl"
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        evidence = await gateway.call("get_order", case_id=case_id, order_id=claimed_order_id)
        evidence_ref = evidence["evidence_ref"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-agent",
            tool_name="get_order",
            evidence_refs=[evidence_ref],
        )

        print(f"case_id: {case_id}")
        print(f"evidence_ref: {evidence_ref}")
        print("Trace được ghi tại:")
        print(trace_path.relative_to(root).as_posix())


if __name__ == "__main__":
    asyncio.run(main())
