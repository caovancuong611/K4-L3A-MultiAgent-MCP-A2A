from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent import OUTPUT_SCHEMA_VERSION
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

POLICY_DATA: dict[str, Any] = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 79.0,
            "responsible_parties": [{"party_id": None, "party_type": "platform"}],
        },
        "unavailable_order_paid": {
            "case_status": "action_required",
            "recommended_action": "issue_refund",
            "refund_brl": 89.0,
            "responsible_parties": [{"party_id": None, "party_type": "seller"}],
        },
        "late_delivery_logistics": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_id": None, "party_type": "logistics_provider"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": None, "party_type": "seller"}],
        },
        "payment_mismatch": {
            "case_status": "action_required",
            "recommended_action": "reconcile_payment",
            "refund_brl": 35.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "duplicate_charge": {
            "case_status": "action_required",
            "recommended_action": "refund_duplicate_charge",
            "refund_brl": 64.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "valid_split_payment": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
        "refund_pending": {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "refund_failed": {
            "case_status": "action_required",
            "recommended_action": "retry_refund",
            "refund_brl": 52.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "unsupported_claim": {
            "case_status": "no_action",
            "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


class FakeGateway:
    """Serves one fixed fixture per tool for a single case; raises RuntimeError
    (mirrors a real gateway 404) when a tool is intentionally left out of the
    scenario's fixture, e.g. an order with no refund history."""

    def __init__(self, fixtures: dict[str, tuple[str, Any]]) -> None:
        self._fixtures = fixtures
        self._counter = 0

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name not in self._fixtures:
            raise RuntimeError(f"MCP tool {tool_name} failed: not found")
        self._counter += 1
        domain, data = self._fixtures[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{self._counter:024d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": data,
        }


def make_case(case_id: str, order_id: str, topics: list[str]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": "2018-04-04T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": order_id,
            "claims": [
                {"claim_id": f"{case_id}-a", "topic": topics[0]},
                {"claim_id": f"{case_id}-b", "topic": topics[1]},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def run_case(
    case: dict[str, Any], fixtures: dict[str, tuple[str, Any]], tmp_path: Path
) -> dict[str, Any]:
    gateway = FakeGateway(fixtures)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts().validate_output(output, "test output")
    return output


def order_data(**overrides: Any) -> dict[str, Any]:
    base = {
        "order_id": "order-x",
        "customer_id": "customer-row-x",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-03-23T09:00:00-03:00",
        "order_approved_at": "2018-03-23T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-03-25T09:00:00-03:00",
        "order_delivered_customer_date": "2018-03-30T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-04-02T09:00:00-03:00",
    }
    base.update(overrides)
    return base


def items_data(**overrides: Any) -> list[dict[str, Any]]:
    row = {
        "order_id": "order-x",
        "order_item_id": "item-x",
        "product_id": "product-x",
        "seller_id": "seller-x",
        "shipping_limit_date": "2018-03-26T09:00:00-03:00",
        "price": "79.00",
        "freight_value": "10.00",
    }
    row.update(overrides)
    return [row]


def payments_data(*values: float) -> list[dict[str, Any]]:
    return [
        {
            "order_id": "order-x",
            "payment_sequential": str(i + 1),
            "payment_type": "credit_card",
            "payment_installments": "1",
            "payment_value": f"{value:.2f}",
        }
        for i, value in enumerate(values)
    ]


def test_canceled_order_paid(tmp_path: Path) -> None:
    case = make_case(
        "L3A_CASE_TEST_001", "order-x", ["canceled_order_paid", "requested_full_refund"]
    )
    fixtures = {
        "get_order": (
            "order",
            order_data(order_status="canceled", order_delivered_customer_date=None),
        ),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(79.00)),
        "get_policy": ("policy", POLICY_DATA),
    }
    output = run_case(case, fixtures, tmp_path)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["claim_assessments"][1]["verdict"] == "supported"


def test_late_delivery_logistics(tmp_path: Path) -> None:
    case = make_case(
        "L3A_CASE_TEST_002", "order-x", ["late_delivery_logistics", "requested_full_refund"]
    )
    fixtures = {
        "get_order": ("order", order_data()),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(89.00)),
        "get_shipment_summary": (
            "shipment",
            {
                "order_id": "order-x",
                "order_status": "delivered",
                "delivered_carrier_at": "2018-03-25T09:00:00-03:00",
                "delivered_customer_at": "2018-03-30T09:00:00-03:00",
                "estimated_delivery_at": "2018-04-02T09:00:00-03:00",
                "shipping_limits": [],
                "events": [],
            },
        ),
        "get_policy": ("policy", POLICY_DATA),
    }
    # force lateness: override delivered_customer_at beyond estimate
    fixtures["get_order"] = (
        "order",
        order_data(order_delivered_customer_date="2018-04-07T09:00:00-03:00"),
    )
    output = run_case(case, fixtures, tmp_path)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert (
        output["root_cause_analysis"]["responsible_parties"][0]["party_type"]
        == "logistics_provider"
    )


def test_payment_mismatch(tmp_path: Path) -> None:
    case = make_case("L3A_CASE_TEST_003", "order-x", ["payment_mismatch", "requested_full_refund"])
    fixtures = {
        "get_order": ("order", order_data()),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(35.00)),
        "get_payment_timeline": (
            "payment",
            {
                "order_id": "order-x",
                "payments": payments_data(35.00),
                "events": [
                    {
                        "order_id": "order-x",
                        "event_at": "2018-03-23T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "35.00",
                        "status": "confirmed",
                    },
                    {
                        "order_id": "order-x",
                        "event_at": "2018-03-23T12:00:00-03:00",
                        "event_type": "reconciliation_mismatch",
                        "amount_brl": "35.00",
                        "status": "open",
                    },
                ],
            },
        ),
        "get_policy": ("policy", POLICY_DATA),
    }
    output = run_case(case, fixtures, tmp_path)
    assert output["assessment"]["primary_issue"] == "payment_mismatch"


def test_refund_pending(tmp_path: Path) -> None:
    case = make_case("L3A_CASE_TEST_004", "order-x", ["refund_pending", "requested_full_refund"])
    fixtures = {
        "get_order": ("order", order_data()),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(89.00)),
        "get_refund_timeline": (
            "refund",
            {
                "order_id": "order-x",
                "events": [
                    {
                        "order_id": "order-x",
                        "event_at": "2018-04-01T09:00:00-03:00",
                        "event_type": "refund_requested",
                        "amount_brl": "89.00",
                        "status": "pending",
                    }
                ],
            },
        ),
        "get_policy": ("policy", POLICY_DATA),
    }
    output = run_case(case, fixtures, tmp_path)
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_unsupported_claim_when_everything_reconciles(tmp_path: Path) -> None:
    case = make_case(
        "L3A_CASE_TEST_005", "order-x", ["late_delivery_seller", "requested_full_refund"]
    )
    fixtures = {
        "get_order": (
            "order",
            order_data(order_delivered_customer_date="2018-03-30T09:00:00-03:00"),
        ),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(89.00)),
        "get_policy": ("policy", POLICY_DATA),
    }
    output = run_case(case, fixtures, tmp_path)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_missing_order_falls_back_to_insufficient_evidence(tmp_path: Path) -> None:
    case = make_case(
        "L3A_CASE_TEST_006", "missing-order", ["canceled_order_paid", "requested_full_refund"]
    )
    output = run_case(case, {}, tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert all(c["verdict"] == "insufficient_evidence" for c in output["claim_assessments"])


def test_trace_has_all_required_lifecycle_events(tmp_path: Path) -> None:
    case = make_case(
        "L3A_CASE_TEST_007", "order-x", ["canceled_order_paid", "requested_full_refund"]
    )
    fixtures = {
        "get_order": (
            "order",
            order_data(order_status="canceled", order_delivered_customer_date=None),
        ),
        "get_order_items": ("item", items_data()),
        "get_order_payments": ("payment", payments_data(79.00)),
        "get_policy": ("policy", POLICY_DATA),
    }
    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(fixtures)
    trace = TraceWriter(trace_path, contracts())
    output = asyncio.run(solve_case(case, gateway, trace))
    assert output["schema_version"] == OUTPUT_SCHEMA_VERSION

    import json

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_types = {event["event_type"] for event in events}
    assert {
        "task_assigned",
        "handoff",
        "verification_completed",
        "tool_result_consumed",
    } <= event_types
