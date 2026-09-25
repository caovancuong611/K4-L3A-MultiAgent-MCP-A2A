from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.workflow import solve_case

POLICY = {
    "canceled_order_paid": ("action_required", "issue_refund", 79.0, "platform"),
    "unavailable_order_paid": ("action_required", "issue_refund", 89.0, "seller"),
    "late_delivery_seller": ("action_required", "refund_freight", 10.0, "seller"),
    "late_delivery_logistics": (
        "action_required",
        "refund_freight",
        10.0,
        "logistics_provider",
    ),
    "valid_split_payment": ("no_action", "document_no_action", 0.0, "customer"),
    "payment_mismatch": ("action_required", "reconcile_payment", 35.0, "payment_provider"),
    "duplicate_charge": (
        "action_required",
        "refund_duplicate_charge",
        64.0,
        "payment_provider",
    ),
    "refund_pending": ("needs_investigation", "monitor_refund", 0.0, "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", 89.0, "payment_provider"),
    "unsupported_claim": ("no_action", "document_no_action", 0.0, "customer"),
}


class TraceRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    def __init__(self, issue: str) -> None:
        self.issue = issue
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.order_id = "order-1"

    def _ref(self, tool_name: str) -> str:
        digest = hashlib.sha256(tool_name.encode()).hexdigest()[:32]
        return f"ev_{digest}"

    def _order(self) -> dict[str, Any]:
        status = {
            "canceled_order_paid": "canceled",
            "unavailable_order_paid": "unavailable",
        }.get(self.issue, "delivered")
        if self.issue == "unsupported_claim":
            delivered = "2018-01-07T09:00:00-03:00"
        elif self.issue.startswith("late_delivery"):
            delivered = "2018-01-09T09:00:00-03:00"
        else:
            delivered = None
        return {
            "order_id": self.order_id,
            "customer_id": "customer-1",
            "order_status": status,
            "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
            "order_approved_at": "2018-01-01T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-01-06T09:00:00-03:00",
            "order_delivered_customer_date": delivered,
            "order_estimated_delivery_date": "2018-01-08T09:00:00-03:00",
        }

    def _items(self) -> list[dict[str, Any]]:
        total = {
            "canceled_order_paid": 79,
            "unavailable_order_paid": 89,
            "duplicate_charge": 64,
        }.get(self.issue, 90)
        return [
            {
                "order_id": self.order_id,
                "order_item_id": "item-1",
                "product_id": "product-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2018-01-05T09:00:00-03:00",
                "price": str(total - 10),
                "freight_value": "10",
            },
            {
                "order_id": self.order_id,
                "order_item_id": "decoy-item",
                "product_id": "decoy-product",
                "seller_id": "decoy-seller",
                "shipping_limit_date": "2018-02-05T09:00:00-03:00",
                "price": "999",
                "freight_value": "99",
            },
        ]

    def _payment(self) -> dict[str, Any]:
        amounts: list[int]
        if self.issue == "valid_split_payment":
            amounts = [40, 50]
        elif self.issue == "payment_mismatch":
            amounts = [35]
        elif self.issue == "duplicate_charge":
            amounts = [64, 64]
        elif self.issue == "canceled_order_paid":
            amounts = [79]
        elif self.issue == "unavailable_order_paid":
            amounts = [89]
        else:
            amounts = [90]
        payments = [
            {
                "payment_sequential": str(index),
                "payment_type": "credit_card" if index == 1 else "voucher",
                "payment_value": str(amount),
            }
            for index, amount in enumerate(amounts, 1)
        ]
        events = [
            {
                "order_id": self.order_id,
                "event_at": f"2018-01-02T{9 + index:02}:00:00-03:00",
                "event_type": "captured",
                "amount_brl": str(amount),
                "status": "confirmed",
            }
            for index, amount in enumerate(amounts)
        ]
        if self.issue == "payment_mismatch":
            events.append(
                {
                    "order_id": self.order_id,
                    "event_at": "2018-01-02T12:00:00-03:00",
                    "event_type": "reconciliation_mismatch",
                    "amount_brl": "35",
                    "status": "open",
                }
            )
        events.append(
            {
                "order_id": self.order_id,
                "event_at": "2018-02-02T10:00:00-03:00",
                "event_type": "captured",
                "amount_brl": "999",
                "status": "confirmed",
            }
        )
        return {"order_id": self.order_id, "payments": payments, "events": events}

    def _refund(self) -> dict[str, Any]:
        status = "failed" if self.issue == "refund_failed" else "pending"
        return {
            "order_id": self.order_id,
            "events": [
                {
                    "order_id": self.order_id,
                    "event_at": "2018-01-09T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "amount_brl": "89",
                    "status": status,
                }
            ],
        }

    def _shipment(self) -> dict[str, Any]:
        carrier = (
            "2018-01-06T09:00:00-03:00"
            if self.issue == "late_delivery_seller"
            else "2018-01-04T09:00:00-03:00"
        )
        delivered = (
            "2018-01-07T09:00:00-03:00"
            if self.issue == "unsupported_claim"
            else "2018-01-09T09:00:00-03:00"
        )
        event_at = (
            "2018-01-04T09:00:00-03:00"
            if self.issue == "unsupported_claim"
            else "2018-02-09T09:00:00-03:00"
        )
        return {
            "order_id": self.order_id,
            "order_status": "delivered",
            "delivered_carrier_at": carrier,
            "delivered_customer_at": delivered,
            "estimated_delivery_at": "2018-01-08T09:00:00-03:00",
            "shipping_limits": [
                {
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "shipping_limit_at": "2018-01-05T09:00:00-03:00",
                }
            ],
            "events": [
                {
                    "order_id": self.order_id,
                    "event_at": event_at,
                    "event_type": "delivered_late",
                    "actor": "seller",
                    "status": "confirmed",
                }
            ],
        }

    def _policy(self) -> dict[str, Any]:
        case_status, action, refund, party_type = POLICY[self.issue]
        return {
            "currency": "BRL",
            "policy_version": "EC_POLICY_V1",
            "rules": {
                self.issue: {
                    "case_status": case_status,
                    "recommended_action": action,
                    "refund_brl": refund,
                    "responsible_parties": [
                        {
                            "party_type": party_type,
                            "party_id": "wrong-seller" if party_type == "seller" else None,
                        }
                    ],
                }
            },
        }

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        data: Any
        domain: str
        if tool_name == "get_order":
            data, domain = self._order(), "order"
        elif tool_name == "get_order_items":
            data, domain = self._items(), "item"
        elif tool_name == "get_payment_timeline":
            data, domain = self._payment(), "payment"
        elif tool_name == "get_refund_timeline":
            data, domain = self._refund(), "refund"
        elif tool_name == "get_shipment_summary":
            data, domain = self._shipment(), "shipment"
        elif tool_name == "get_sellers":
            data, domain = [{"seller_id": "seller-1"}], "seller"
        elif tool_name == "get_policy":
            data, domain = self._policy(), "policy"
        else:
            raise AssertionError(f"unexpected tool: {tool_name}")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": self._ref(tool_name),
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": data,
        }


def make_case(issue: str) -> dict[str, Any]:
    return {
        "case_id": "CASE_001",
        "opened_at": "2018-01-10T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Please investigate",
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-primary", "topic": issue},
                {"claim_id": "claim-refund", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


@pytest.mark.parametrize("issue", list(POLICY))
def test_workflow_classifies_all_public_issue_types(issue: str) -> None:
    gateway = FakeGateway(issue)
    trace = TraceRecorder()

    output = asyncio.run(solve_case(make_case(issue), gateway, trace))

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == POLICY[issue][0]
    assert output["financial_resolution"]["recommended_refund_brl"] == POLICY[issue][2]
    assert all(call_case_id == "CASE_001" for _, call_case_id, _ in gateway.calls)

    consumed = {
        event["evidence_refs"][0]
        for event in trace.events
        if event["event_type"] == "tool_result_consumed"
    }
    assert consumed == set(output["evidence_refs"])
    assert any(event["event_type"] == "handoff" for event in trace.events)
    assert trace.events[-1]["event_type"] == "verification_completed"


@pytest.mark.parametrize("issue", ["unavailable_order_paid", "late_delivery_seller"])
def test_case_scoped_seller_overrides_policy_seller(issue: str) -> None:
    output = asyncio.run(solve_case(make_case(issue), FakeGateway(issue), TraceRecorder()))

    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-1"}
    ]
    assert any(
        conflict["resolution_code"] == "CASE_SCOPED_ENTITY_WINS"
        for conflict in output["data_conflicts"]
    )


def test_delivery_timestamps_override_contradictory_late_event() -> None:
    issue = "unsupported_claim"
    output = asyncio.run(solve_case(make_case(issue), FakeGateway(issue), TraceRecorder()))

    assert output["assessment"]["primary_issue"] == issue
    assert any(
        conflict["resolution_code"] == "TIMESTAMP_INVARIANT_WINS"
        for conflict in output["data_conflicts"]
    )


def test_full_refund_claim_cites_item_total_when_payment_evidence_is_absent() -> None:
    gateway = FakeGateway("late_delivery_seller")
    output = asyncio.run(
        solve_case(make_case("late_delivery_seller"), gateway, TraceRecorder())
    )

    refund_claim = output["claim_assessments"][1]
    assert refund_claim["verdict"] == "partially_supported"
    assert gateway._ref("get_order_items") in refund_claim["evidence_refs"]
    assert gateway._ref("get_policy") in refund_claim["evidence_refs"]


def test_exact_duplicate_rows_do_not_double_financial_totals() -> None:
    class DuplicateGateway(FakeGateway):
        def _items(self) -> list[dict[str, Any]]:
            actual = super()._items()[0]
            return [actual, dict(actual)]

        def _payment(self) -> dict[str, Any]:
            data = super()._payment()
            actual_payment = data["payments"][0]
            actual_event = data["events"][0]
            return {
                "order_id": self.order_id,
                "payments": [actual_payment, dict(actual_payment)],
                "events": [actual_event, dict(actual_event)],
            }

    output = asyncio.run(
        solve_case(
            make_case("unavailable_order_paid"),
            DuplicateGateway("unavailable_order_paid"),
            TraceRecorder(),
        )
    )

    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert output["affected_entities"]["payment_references"] == ["1"]
    assert any(
        conflict["resolution_code"] == "EXCLUDE_OUTSIDE_CASE_WINDOW"
        for conflict in output["data_conflicts"]
    )


def test_same_payment_reference_uses_earliest_confirmed_capture() -> None:
    class ConflictingPaymentGateway(FakeGateway):
        def _payment(self) -> dict[str, Any]:
            return {
                "order_id": self.order_id,
                "payments": [
                    {"payment_sequential": "1", "payment_value": "79"},
                    {"payment_sequential": "1", "payment_value": "18"},
                ],
                "events": [
                    {
                        "order_id": self.order_id,
                        "event_at": "2018-01-02T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "79",
                        "status": "confirmed",
                    },
                    {
                        "order_id": self.order_id,
                        "event_at": "2018-01-03T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "18",
                        "status": "confirmed",
                    },
                ],
            }

    output = asyncio.run(
        solve_case(
            make_case("canceled_order_paid"),
            ConflictingPaymentGateway("canceled_order_paid"),
            TraceRecorder(),
        )
    )

    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert output["affected_entities"]["payment_references"] == ["1"]
    assert any(
        conflict["resolution_code"] == "EARLIEST_CONFIRMED_CAPTURE_WINS"
        for conflict in output["data_conflicts"]
    )


def test_refund_amount_correlates_payment_amid_in_window_decoys() -> None:
    class RefundCorrelationGateway(FakeGateway):
        def _payment(self) -> dict[str, Any]:
            return {
                "order_id": self.order_id,
                "payments": [
                    {"payment_sequential": "1", "payment_value": "89"},
                    {"payment_sequential": "1", "payment_value": "44.5"},
                    {"payment_sequential": "2", "payment_value": "44.5"},
                ],
                "events": [
                    {
                        "order_id": self.order_id,
                        "event_at": "2018-01-02T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "89",
                        "status": "confirmed",
                    },
                    {
                        "order_id": self.order_id,
                        "event_at": "2018-01-02T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.5",
                        "status": "confirmed",
                    },
                    {
                        "order_id": self.order_id,
                        "event_at": "2018-01-02T11:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.5",
                        "status": "confirmed",
                    },
                ],
            }

    output = asyncio.run(
        solve_case(
            make_case("refund_failed"),
            RefundCorrelationGateway("refund_failed"),
            TraceRecorder(),
        )
    )

    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert output["affected_entities"]["payment_references"] == ["1"]
    assert any(
        conflict["resolution_code"] == "REFUND_AMOUNT_CORRELATION"
        for conflict in output["data_conflicts"]
    )
