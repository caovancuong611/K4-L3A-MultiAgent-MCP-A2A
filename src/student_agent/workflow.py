from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}

PAYMENT_TOPICS = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}
SHIPMENT_TOPICS = {
    "late_delivery_seller",
    "late_delivery_logistics",
    "unsupported_claim",
}
SELLER_TOPICS = {"unavailable_order_paid", "late_delivery_seller"}
REFUND_TOPICS = {"refund_pending", "refund_failed"}

CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_LATE",
    "late_delivery_logistics": "LOGISTICS_DELIVERY_LATE",
    "valid_split_payment": "SPLIT_PAYMENT_RECONCILED",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_CAPTURE",
    "refund_pending": "REFUND_PROCESSING_PENDING",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED",
}


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, str]


@dataclass(frozen=True)
class Assignment:
    actor: str
    tools: tuple[ToolRequest, ...]


@dataclass(frozen=True)
class Evidence:
    actor: str
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any


@dataclass(frozen=True)
class CaseFacts:
    opened_at: datetime
    purchased_at: datetime
    order: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    payment_data: dict[str, Any]
    payment_events: tuple[dict[str, Any], ...]
    refund_events: tuple[dict[str, Any], ...]
    shipment_data: dict[str, Any]
    shipment_events: tuple[dict[str, Any], ...]

    @property
    def captured_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            event
            for event in self.payment_events
            if str(event.get("event_type", "")).lower() == "captured"
            and str(event.get("status", "")).lower() == "confirmed"
        )

    @property
    def captured_total(self) -> Decimal:
        return sum((_money(event.get("amount_brl")) for event in self.captured_events), Decimal())

    @property
    def reconciled_payments(self) -> tuple[tuple[str, Decimal], ...]:
        """Return one in-window capture for each authoritative payment reference.

        The gateway can expose contradictory rows for the same payment sequence.  A
        sequence is one payment entity, so prefer the amount whose confirmed capture
        happened first in the case window.  Exact duplicate payment rows are ignored.
        """
        payments = self.payment_data.get("payments")
        if not isinstance(payments, list):
            return ()

        first_capture: dict[Decimal, datetime] = {}
        for event in self.captured_events:
            amount = _money(event.get("amount_brl"))
            moment = _optional_datetime(event.get("event_at"))
            if moment is not None and (
                amount not in first_capture or moment < first_capture[amount]
            ):
                first_capture[amount] = moment

        selected: dict[str, tuple[datetime, int, Decimal]] = {}
        seen_rows: set[str] = set()
        for index, payment in enumerate(payments):
            if not isinstance(payment, dict):
                continue
            fingerprint = json.dumps(
                payment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if fingerprint in seen_rows:
                continue
            seen_rows.add(fingerprint)
            reference = payment.get("payment_reference", payment.get("payment_sequential"))
            amount = _money(payment.get("payment_value"))
            if reference is None or amount not in first_capture:
                continue
            key = str(reference)
            candidate = (first_capture[amount], index, amount)
            if key not in selected or candidate[:2] < selected[key][:2]:
                selected[key] = candidate
        return tuple((key, selected[key][2]) for key in sorted(selected))

    @property
    def reconciled_capture_total(self) -> Decimal:
        payments = self.reconciled_payments
        if payments:
            return sum((amount for _, amount in payments), Decimal())
        return self.captured_total

    @property
    def latest_refund_amount(self) -> Decimal:
        if not self.refund_events:
            return Decimal()
        return _money(self.refund_events[-1].get("amount_brl"))

    @property
    def expected_total(self) -> Decimal | None:
        if not self.items:
            return None
        return sum(
            (_money(item.get("price")) + _money(item.get("freight_value")) for item in self.items),
            Decimal(),
        )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _parse_datetime(value: Any, label: str) -> datetime:
    text = _required_string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        return Decimal()
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid BRL amount: {value!r}") from exc


def _money_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return sorted({value for value in values if isinstance(value, str) and value})


def _in_case_window(value: Any, purchased_at: datetime, opened_at: datetime) -> bool:
    moment = _optional_datetime(value)
    return moment is not None and purchased_at <= moment <= opened_at


def _primary_claim_topic(case: dict[str, Any]) -> str:
    request = case.get("customer_request")
    if not isinstance(request, dict):
        raise ValueError("customer_request must be an object")
    claims = request.get("claims")
    if not isinstance(claims, list):
        raise ValueError("customer_request.claims must be an array")
    for claim in claims:
        if isinstance(claim, dict) and claim.get("topic") in PRIMARY_ISSUES:
            return str(claim["topic"])
    return "unsupported_claim"


def _assignments(case: dict[str, Any], topic: str) -> tuple[Assignment, ...]:
    request = case["customer_request"]
    order_id = _required_string(request.get("claimed_order_id"), "claimed_order_id")
    policy_version = _required_string(case.get("policy_version"), "policy_version")

    order_tools = [
        ToolRequest("get_order", {"order_id": order_id}),
        ToolRequest("get_order_items", {"order_id": order_id}),
    ]
    if topic in SELLER_TOPICS:
        order_tools.append(ToolRequest("get_sellers", {"order_id": order_id}))

    result = [Assignment("order-agent", tuple(order_tools))]
    if topic in PAYMENT_TOPICS:
        payment_tools = [ToolRequest("get_payment_timeline", {"order_id": order_id})]
        if topic in REFUND_TOPICS:
            payment_tools.append(ToolRequest("get_refund_timeline", {"order_id": order_id}))
        result.append(Assignment("payment-agent", tuple(payment_tools)))
    if topic in SHIPMENT_TOPICS:
        result.append(
            Assignment(
                "shipment-agent",
                (ToolRequest("get_shipment_summary", {"order_id": order_id}),),
            )
        )
    result.append(
        Assignment(
            "policy-agent",
            (ToolRequest("get_policy", {"policy_version": policy_version}),),
        )
    )
    return tuple(result)


async def _run_assignment(
    *,
    case_id: str,
    assignment: Assignment,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> tuple[Evidence, ...]:
    collected: list[Evidence] = []
    for request in assignment.tools:
        response = await gateway.call(request.name, case_id=case_id, **request.arguments)
        evidence = Evidence(
            actor=assignment.actor,
            tool_name=request.name,
            evidence_ref=_required_string(response.get("evidence_ref"), "evidence_ref"),
            domain=_required_string(response.get("domain"), "evidence domain"),
            data=response.get("data"),
        )
        collected.append(evidence)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=assignment.actor,
            tool_name=request.name,
            evidence_refs=[evidence.evidence_ref],
        )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=assignment.actor,
        target="verifier",
        decision_code="EVIDENCE_READY",
        evidence_refs=[item.evidence_ref for item in collected],
        attributes={"evidence_count": len(collected)},
    )
    return tuple(collected)


def _evidence_by_tool(evidence: tuple[Evidence, ...]) -> dict[str, Evidence]:
    result: dict[str, Evidence] = {}
    for item in evidence:
        if item.tool_name in result:
            raise ValueError(f"duplicate evidence for tool {item.tool_name}")
        result[item.tool_name] = item
    return result


def _valid_items(
    raw: Any, order_id: str, purchased_at: datetime, opened_at: datetime
) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        raise ValueError("get_order_items data must be an array")
    matching = [item for item in raw if isinstance(item, dict) and item.get("order_id") == order_id]
    timed = [item for item in matching if _optional_datetime(item.get("shipping_limit_date"))]
    scoped = (
        [
            item
            for item in timed
            if _in_case_window(item.get("shipping_limit_date"), purchased_at, opened_at)
        ]
        if timed
        else matching
    )
    deduplicated = _dedupe_records(scoped)

    selected: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for item in deduplicated:
        item_id = item.get("order_item_id")
        if not isinstance(item_id, str) or not item_id or item_id not in positions:
            if isinstance(item_id, str) and item_id:
                positions[item_id] = len(selected)
            selected.append(item)
            continue
        position = positions[item_id]
        current = selected[position]
        current_limit = _optional_datetime(current.get("shipping_limit_date"))
        candidate_limit = _optional_datetime(item.get("shipping_limit_date"))
        if candidate_limit is not None and (
            current_limit is None or candidate_limit < current_limit
        ):
            selected[position] = item
    return tuple(selected)


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        fingerprint = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if fingerprint not in seen:
            seen.add(fingerprint)
            result.append(record)
    return result


def _valid_events(
    raw: Any, *, purchased_at: datetime, opened_at: datetime
) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        return ()
    events = [
        event
        for event in raw
        if isinstance(event, dict)
        and _in_case_window(event.get("event_at"), purchased_at, opened_at)
    ]
    return tuple(
        sorted(_dedupe_records(events), key=lambda event: str(event.get("event_at", "")))
    )


def _facts(
    case: dict[str, Any], by_tool: dict[str, Evidence], order_id: str
) -> CaseFacts:
    order_data = by_tool["get_order"].data
    if not isinstance(order_data, dict) or order_data.get("order_id") != order_id:
        raise ValueError("get_order returned a mismatched order")
    opened_at = _parse_datetime(case.get("opened_at"), "opened_at")
    purchased_at = _parse_datetime(
        order_data.get("order_purchase_timestamp"), "order_purchase_timestamp"
    )
    if purchased_at > opened_at:
        raise ValueError("order purchase is after the case opened")

    raw_items = by_tool["get_order_items"].data
    items = _valid_items(raw_items, order_id, purchased_at, opened_at)

    payment_data: dict[str, Any] = {}
    payment_events: tuple[dict[str, Any], ...] = ()
    payment = by_tool.get("get_payment_timeline")
    if payment is not None:
        if not isinstance(payment.data, dict) or payment.data.get("order_id") != order_id:
            raise ValueError("get_payment_timeline returned a mismatched order")
        payment_data = payment.data
        payment_events = _valid_events(
            payment.data.get("events"), purchased_at=purchased_at, opened_at=opened_at
        )

    refund_events: tuple[dict[str, Any], ...] = ()
    refund = by_tool.get("get_refund_timeline")
    if refund is not None:
        if not isinstance(refund.data, dict) or refund.data.get("order_id") != order_id:
            raise ValueError("get_refund_timeline returned a mismatched order")
        refund_events = _valid_events(
            refund.data.get("events"), purchased_at=purchased_at, opened_at=opened_at
        )

    shipment_data: dict[str, Any] = {}
    shipment_events: tuple[dict[str, Any], ...] = ()
    shipment = by_tool.get("get_shipment_summary")
    if shipment is not None:
        if not isinstance(shipment.data, dict) or shipment.data.get("order_id") != order_id:
            raise ValueError("get_shipment_summary returned a mismatched order")
        shipment_data = shipment.data
        shipment_events = _valid_events(
            shipment.data.get("events"), purchased_at=purchased_at, opened_at=opened_at
        )

    return CaseFacts(
        opened_at=opened_at,
        purchased_at=purchased_at,
        order=order_data,
        items=items,
        payment_data=payment_data,
        payment_events=payment_events,
        refund_events=refund_events,
        shipment_data=shipment_data,
        shipment_events=shipment_events,
    )


def _late_issue(facts: CaseFacts) -> str | None:
    if not facts.shipment_data:
        return None
    estimated = _optional_datetime(facts.shipment_data.get("estimated_delivery_at"))
    delivered = _optional_datetime(facts.shipment_data.get("delivered_customer_at"))
    if estimated is None or estimated >= facts.opened_at:
        return None
    if delivered is not None and delivered <= estimated:
        return None

    for event in facts.shipment_events:
        if str(event.get("event_type", "")).lower() != "delivered_late":
            continue
        event_at = _optional_datetime(event.get("event_at"))
        if event_at is None or event_at < estimated:
            continue
        actor = str(event.get("actor", "")).lower()
        if actor == "seller":
            return "late_delivery_seller"
        if actor == "logistics_provider":
            return "late_delivery_logistics"

    carrier = _optional_datetime(facts.shipment_data.get("delivered_carrier_at"))
    limits = [
        moment
        for row in facts.shipment_data.get("shipping_limits", [])
        if isinstance(row, dict)
        and (moment := _optional_datetime(row.get("shipping_limit_at"))) is not None
        and facts.purchased_at <= moment <= facts.opened_at
    ]
    if carrier is None or any(carrier > limit for limit in limits):
        return "late_delivery_seller"
    return "late_delivery_logistics"


def _detect_issue(facts: CaseFacts) -> str:
    if facts.refund_events:
        latest = facts.refund_events[-1]
        status = str(latest.get("status", "")).lower()
        event_type = str(latest.get("event_type", "")).lower()
        if status == "failed" or "failed" in event_type:
            return "refund_failed"
        if status == "pending" or event_type == "refund_requested":
            return "refund_pending"

    order_status = str(facts.order.get("order_status", "")).lower()
    if facts.captured_total > 0 and order_status == "canceled":
        return "canceled_order_paid"
    if facts.captured_total > 0 and order_status == "unavailable":
        return "unavailable_order_paid"

    if facts.payment_data:
        mismatch = any(
            str(event.get("event_type", "")).lower() == "reconciliation_mismatch"
            and str(event.get("status", "")).lower() in {"open", "failed"}
            for event in facts.payment_events
        )
        if mismatch:
            return "payment_mismatch"

        captured_amounts = [_money(event.get("amount_brl")) for event in facts.captured_events]
        amount_counts = Counter(captured_amounts)
        expected_total = facts.expected_total
        if (
            expected_total is not None
            and facts.captured_total > expected_total
            and any(count > 1 for count in amount_counts.values())
        ):
            return "duplicate_charge"
        if (
            expected_total is not None
            and len(captured_amounts) > 1
            and facts.captured_total == expected_total
        ):
            return "valid_split_payment"
        if (
            expected_total is not None
            and captured_amounts
            and facts.captured_total != expected_total
        ):
            return "payment_mismatch"

    late_issue = _late_issue(facts)
    return late_issue or "unsupported_claim"


def _policy_rule(by_tool: dict[str, Evidence], issue: str) -> dict[str, Any]:
    policy = by_tool["get_policy"].data
    if not isinstance(policy, dict) or not isinstance(policy.get("rules"), dict):
        raise ValueError("get_policy data must contain a rules object")
    rule = policy["rules"].get(issue)
    if not isinstance(rule, dict):
        raise ValueError(f"policy does not define {issue}")
    return rule


def _late_seller_ids(facts: CaseFacts) -> list[str]:
    carrier = _optional_datetime(facts.shipment_data.get("delivered_carrier_at"))
    result: set[str] = set()
    for row in facts.shipment_data.get("shipping_limits", []):
        if not isinstance(row, dict):
            continue
        limit = _optional_datetime(row.get("shipping_limit_at"))
        seller_id = row.get("seller_id")
        if (
            isinstance(seller_id, str)
            and seller_id
            and limit is not None
            and facts.purchased_at <= limit <= facts.opened_at
            and (carrier is None or carrier > limit)
        ):
            result.add(seller_id)
    return sorted(result)


def _responsible_parties(
    issue: str, rule: dict[str, Any], facts: CaseFacts
) -> list[dict[str, str | None]]:
    raw = rule.get("responsible_parties")
    if not isinstance(raw, list):
        raise ValueError(f"policy rule {issue} has no responsible_parties")
    item_sellers = _unique_strings([item.get("seller_id") for item in facts.items])
    seller_ids = _late_seller_ids(facts) if issue == "late_delivery_seller" else item_sellers

    result: list[dict[str, str | None]] = []
    for party in raw:
        if not isinstance(party, dict):
            continue
        party_type = party.get("party_type")
        if not isinstance(party_type, str):
            continue
        if party_type == "seller" and seller_ids:
            result.extend({"party_type": "seller", "party_id": value} for value in seller_ids)
        else:
            party_id = party.get("party_id")
            result.append(
                {
                    "party_type": party_type,
                    "party_id": party_id if isinstance(party_id, str) else None,
                }
            )
    unique: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for party in result:
        key = (party["party_type"], party["party_id"])
        if key not in seen:
            seen.add(key)
            unique.append(party)
    return unique[:5]


def _data_conflicts(
    *,
    by_tool: dict[str, Evidence],
    facts: CaseFacts,
    rule: dict[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    raw_items = by_tool["get_order_items"].data
    if isinstance(raw_items, list) and len(raw_items) != len(facts.items):
        conflicts.append(
            {
                "field": "order_items",
                "sources": ["get_order_items", "get_order"],
                "selected_source": "get_order",
                "resolution_code": "EXCLUDE_OUTSIDE_CASE_WINDOW",
            }
        )

    payment = by_tool.get("get_payment_timeline")
    if payment is not None and isinstance(payment.data, dict):
        raw_events = payment.data.get("events")
        if isinstance(raw_events, list) and len(raw_events) != len(facts.payment_events):
            conflicts.append(
                {
                    "field": "payment_timeline.events",
                    "sources": ["get_payment_timeline", "get_order"],
                    "selected_source": "get_order",
                    "resolution_code": "EXCLUDE_OUTSIDE_CASE_WINDOW",
                }
            )
        valid_amounts = {
            _money(event.get("amount_brl")) for event in facts.captured_events
        }
        amounts_by_reference: dict[str, set[Decimal]] = {}
        for row in payment.data.get("payments", []):
            if not isinstance(row, dict):
                continue
            reference = row.get("payment_reference", row.get("payment_sequential"))
            amount = _money(row.get("payment_value"))
            if reference is not None and amount in valid_amounts:
                amounts_by_reference.setdefault(str(reference), set()).add(amount)
        if any(len(amounts) > 1 for amounts in amounts_by_reference.values()):
            conflicts.append(
                {
                    "field": "payment_timeline.payment_reference",
                    "sources": [
                        "payment_timeline:earliest_capture",
                        "payment_timeline:conflicting_row",
                    ],
                    "selected_source": "payment_timeline:earliest_capture",
                    "resolution_code": "EARLIEST_CONFIRMED_CAPTURE_WINS",
                }
            )

    if facts.refund_events and facts.captured_events:
        refund_amount = facts.latest_refund_amount
        unrelated_captures = {
            _money(event.get("amount_brl"))
            for event in facts.captured_events
            if _money(event.get("amount_brl")) != refund_amount
        }
        if refund_amount > 0 and unrelated_captures:
            conflicts.append(
                {
                    "field": "payment_timeline.refund_amount",
                    "sources": ["get_payment_timeline", "get_refund_timeline"],
                    "selected_source": "get_refund_timeline",
                    "resolution_code": "REFUND_AMOUNT_CORRELATION",
                }
            )

    shipment = by_tool.get("get_shipment_summary")
    if shipment is not None and isinstance(shipment.data, dict):
        raw_events = shipment.data.get("events")
        if isinstance(raw_events, list) and len(raw_events) != len(facts.shipment_events):
            conflicts.append(
                {
                    "field": "shipment_summary.events",
                    "sources": ["get_shipment_summary", "get_order"],
                    "selected_source": "get_order",
                    "resolution_code": "EXCLUDE_OUTSIDE_CASE_WINDOW",
                }
            )
        estimated = _optional_datetime(shipment.data.get("estimated_delivery_at"))
        delivered = _optional_datetime(shipment.data.get("delivered_customer_at"))
        claims_late = any(
            str(event.get("event_type", "")).lower() == "delivered_late"
            for event in facts.shipment_events
        )
        if (
            claims_late
            and estimated is not None
            and delivered is not None
            and delivered <= estimated
        ):
            conflicts.append(
                {
                    "field": "shipment_summary.delivery_status",
                    "sources": ["shipment_summary.events", "shipment_summary.timestamps"],
                    "selected_source": "shipment_summary.timestamps",
                    "resolution_code": "TIMESTAMP_INVARIANT_WINS",
                }
            )

    policy_seller_ids = {
        party.get("party_id")
        for party in rule.get("responsible_parties", [])
        if isinstance(party, dict)
        and party.get("party_type") == "seller"
        and isinstance(party.get("party_id"), str)
    }
    case_seller_ids = {item.get("seller_id") for item in facts.items if item.get("seller_id")}
    if policy_seller_ids and case_seller_ids and policy_seller_ids != case_seller_ids:
        conflicts.append(
            {
                "field": "responsible_party.party_id",
                "sources": ["get_policy", "get_order_items"],
                "selected_source": "get_order_items",
                "resolution_code": "CASE_SCOPED_ENTITY_WINS",
            }
        )
    return conflicts[:5]


def _payment_references(facts: CaseFacts, issue: str) -> list[str]:
    payments = facts.reconciled_payments
    if issue in REFUND_TOPICS and facts.latest_refund_amount > 0:
        matching = [
            reference
            for reference, amount in payments
            if amount == facts.latest_refund_amount
        ]
        if matching:
            return matching
    return [reference for reference, _ in payments]


def _refs_for_tools(by_tool: dict[str, Evidence], names: set[str]) -> list[str]:
    return [item.evidence_ref for name, item in by_tool.items() if name in names]


def _claim_assessments(
    *,
    case: dict[str, Any],
    issue: str,
    refund: Decimal,
    facts: CaseFacts,
    by_tool: dict[str, Evidence],
) -> list[dict[str, Any]]:
    claims = case["customer_request"].get("claims", [])
    issue_refs = [item.evidence_ref for item in by_tool.values()]
    refund_tools = {"get_payment_timeline", "get_refund_timeline", "get_policy"}
    refund_basis = facts.latest_refund_amount or facts.reconciled_capture_total
    if refund_basis <= 0:
        refund_basis = facts.expected_total or Decimal()
        refund_tools.add("get_order_items")
    refund_refs = _refs_for_tools(by_tool, refund_tools)
    result: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if topic == issue:
            verdict, confidence, refs = "supported", 0.98, issue_refs
        elif topic == "requested_full_refund":
            if refund <= 0:
                verdict, confidence, refs = "unsupported", 0.98, refund_refs
            elif refund_basis > 0 and refund == refund_basis:
                verdict, confidence, refs = "supported", 0.97, refund_refs
            elif refund_basis > 0:
                verdict, confidence, refs = "partially_supported", 0.96, refund_refs
            else:
                verdict, confidence, refs = "insufficient_evidence", 0.45, refund_refs
        else:
            verdict, confidence, refs = "unsupported", 0.95, issue_refs
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    return result


def _build_output(
    *,
    case: dict[str, Any],
    hypothesis: str,
    evidence: tuple[Evidence, ...],
) -> dict[str, Any]:
    case_id = _required_string(case.get("case_id"), "case_id")
    order_id = _required_string(
        case["customer_request"].get("claimed_order_id"), "claimed_order_id"
    )
    by_tool = _evidence_by_tool(evidence)
    facts = _facts(case, by_tool, order_id)
    issue = _detect_issue(facts)
    rule = _policy_rule(by_tool, issue)
    case_status = _required_string(rule.get("case_status"), "policy case_status")
    action = _required_string(rule.get("recommended_action"), "policy recommended_action")
    refund = _money(rule.get("refund_brl"))
    if refund < 0:
        raise ValueError("policy refund_brl cannot be negative")

    item_ids = _unique_strings([item.get("order_item_id") for item in facts.items])
    seller_ids = _unique_strings([item.get("seller_id") for item in facts.items])
    confidence = {
        "canceled_order_paid": 0.99,
        "unavailable_order_paid": 0.99,
        "refund_pending": 0.99,
        "refund_failed": 0.99,
        "payment_mismatch": 0.97,
        "duplicate_charge": 0.97,
        "valid_split_payment": 0.97,
        "late_delivery_seller": 0.96,
        "late_delivery_logistics": 0.96,
        "unsupported_claim": 0.95,
    }[issue]
    if hypothesis != issue:
        confidence = min(confidence, 0.82)

    refs = [item.evidence_ref for item in evidence]
    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": CAUSE_CODES[issue],
                "amount_brl": _money_number(refund),
                "entity_id": order_id,
            }
        )

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": _payment_references(facts, issue),
            "shipment_ids": [],
        },
        "claim_assessments": _claim_assessments(
            case=case,
            issue=issue,
            refund=refund,
            facts=facts,
            by_tool=by_tool,
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": CAUSE_CODES[issue], "rank": 1}],
            "responsible_parties": _responsible_parties(issue, rule, facts),
        },
        "evidence_refs": refs,
        "data_conflicts": _data_conflicts(
            by_tool=by_tool,
            facts=facts,
            rule=rule,
        ),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money_number(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the evidence-grounded L3A coordinator/specialist/verifier workflow."""
    case_id = _required_string(case.get("case_id"), "case_id")
    hypothesis = _primary_claim_topic(case)
    assignments = _assignments(case, hypothesis)

    for assignment in assignments:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=assignment.actor,
            decision_code=f"ROUTE_{hypothesis.upper()}",
            attributes={"tool_count": len(assignment.tools)},
        )

    groups = await asyncio.gather(
        *(
            _run_assignment(
                case_id=case_id,
                assignment=assignment,
                gateway=gateway,
                trace=trace,
            )
            for assignment in assignments
        )
    )
    evidence = tuple(item for group in groups for item in group)
    output = _build_output(case=case, hypothesis=hypothesis, evidence=evidence)

    by_tool = _evidence_by_tool(evidence)
    policy_ref = by_tool["get_policy"].evidence_ref
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=[policy_ref],
        attributes={"case_status": output["assessment"]["case_status"]},
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code=f"VERIFIED_{output['assessment']['primary_issue'].upper()}",
        evidence_refs=output["evidence_refs"],
        attributes={
            "confidence": output["assessment"]["confidence"],
            "evidence_count": len(output["evidence_refs"]),
        },
    )
    return output
