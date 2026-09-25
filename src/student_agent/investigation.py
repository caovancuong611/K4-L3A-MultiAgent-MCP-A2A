"""Pure evidence-reasoning layer for the L3A workflow.

Every function here takes already-fetched MCP `data` payloads (plain dicts,
as returned by `EvidenceGateway.call`) and returns a structured finding. None
of it touches the network, so it is fully unit-testable with literal fixtures
that mirror the real evidence shapes (see tests/test_investigation.py).

Design note (learned by sampling the real gateway before writing this):
`get_policy` returns a fixed rules table keyed by `policy_version`, identical
regardless of `case_id`. So this layer's job is to figure out which
`primary_issue` the evidence actually supports (the hard part) — the
resulting `case_status` / `recommended_action` / `refund_brl` /
`responsible_party.party_type` then come straight from that policy table,
not from re-deriving totals ourselves.

Several evidence domains contain deliberately conflicting/noisy duplicate
rows (same `order_item_id` or `payment_sequential` with different values, or
shipment/payment events with an implausible timestamp). The consistent
pattern observed across samples: genuine rows/events cluster in time close to
`order_approved_at`, and the genuine cluster is the largest one; conflicting/
noisy rows sit alone, far away in time. `pick_canonical_items` and
`group_clusters`/`select_genuine_cluster` encode that rule so a specialist
doesn't have to guess field-by-field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

PrimaryIssue = Literal[
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
    "insufficient_evidence",
]

AMOUNT_TOLERANCE_BRL = 0.01
CLUSTER_GAP = timedelta(hours=6)


def money(value: Any) -> float:
    return round(float(value), 2)


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------- order


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str
    purchase_at: datetime | None
    approved_at: datetime | None
    delivered_carrier_at: datetime | None
    delivered_customer_at: datetime | None
    estimated_delivery_at: datetime | None


def parse_order(data: dict[str, Any]) -> OrderFacts:
    return OrderFacts(
        order_id=data["order_id"],
        status=data["order_status"],
        purchase_at=parse_datetime(data.get("order_purchase_timestamp")),
        approved_at=parse_datetime(data.get("order_approved_at")),
        delivered_carrier_at=parse_datetime(data.get("order_delivered_carrier_date")),
        delivered_customer_at=parse_datetime(data.get("order_delivered_customer_date")),
        estimated_delivery_at=parse_datetime(data.get("order_estimated_delivery_date")),
    )


# ---------------------------------------------------------------------------- items


@dataclass(frozen=True)
class ItemFacts:
    order_item_id: str
    product_id: str
    seller_id: str
    shipping_limit_at: datetime | None
    price: float
    freight_value: float


def parse_items(rows: list[dict[str, Any]]) -> list[ItemFacts]:
    return [
        ItemFacts(
            order_item_id=row["order_item_id"],
            product_id=row["product_id"],
            seller_id=row["seller_id"],
            shipping_limit_at=parse_datetime(row.get("shipping_limit_date")),
            price=money(row["price"]),
            freight_value=money(row["freight_value"]),
        )
        for row in rows
    ]


def pick_canonical_items(
    items: list[ItemFacts], order: OrderFacts
) -> tuple[list[ItemFacts], list[ItemFacts]]:
    """Collapse duplicate `order_item_id` rows to one canonical row each.

    When a group disagrees, the genuine row is the one whose shipping_limit
    sits soon after order approval; a conflicting row placed long after (or
    impossibly before) approval is treated as noise, not a second real item.
    Returns (canonical_items, conflicting_rows_dropped).
    """
    reference = order.approved_at or order.purchase_at
    by_id: dict[str, list[ItemFacts]] = {}
    for item in items:
        by_id.setdefault(item.order_item_id, []).append(item)

    canonical: list[ItemFacts] = []
    conflicts: list[ItemFacts] = []
    for rows in by_id.values():
        if len(rows) == 1:
            canonical.append(rows[0])
            continue
        eligible = [
            row
            for row in rows
            if row.shipping_limit_at is not None
            and reference is not None
            and row.shipping_limit_at >= reference
        ]
        pool = eligible or rows
        chosen = min(pool, key=lambda row: row.shipping_limit_at or datetime.max)
        canonical.append(chosen)
        conflicts.extend(row for row in rows if row is not chosen)
    return canonical, conflicts


# ------------------------------------------------------------------------- payments


@dataclass(frozen=True)
class PaymentEvent:
    event_at: datetime
    event_type: str
    amount_brl: float
    status: str


def parse_payment_events(events: list[dict[str, Any]]) -> list[PaymentEvent]:
    parsed = [
        PaymentEvent(
            event_at=parse_datetime(event["event_at"]) or datetime.min,
            event_type=event["event_type"],
            amount_brl=money(event.get("amount_brl", 0)),
            status=event.get("status", ""),
        )
        for event in events
    ]
    return sorted(parsed, key=lambda event: event.event_at)


def group_clusters(
    events: list[PaymentEvent], max_gap: timedelta = CLUSTER_GAP
) -> list[list[PaymentEvent]]:
    """Split time-sorted events into contiguous runs that stay within
    `max_gap` of their neighbour. Real evidence for one case tends to land
    close together in time; a conflicting/duplicate entry sits in its own,
    separate cluster."""
    if not events:
        return []
    clusters: list[list[PaymentEvent]] = [[events[0]]]
    for previous, current in zip(events, events[1:], strict=False):
        if current.event_at - previous.event_at <= max_gap:
            clusters[-1].append(current)
        else:
            clusters.append([current])
    return clusters


def select_genuine_cluster(
    clusters: list[list[PaymentEvent]], reference_at: datetime | None
) -> list[PaymentEvent]:
    """Pick the cluster that represents genuine evidence: whichever starts
    closest to `reference_at` (the order's approval time) — a real sample
    case had a *larger* decoy cluster (a captured+reconciliation_mismatch
    pair) planted weeks away from the order's real, single genuine capture,
    so cluster size is not a safe signal on its own. Falls back to the
    largest, earliest cluster when no reference time is available."""
    if not clusters:
        return []

    def sort_key(cluster: list[PaymentEvent]) -> tuple[float, int, datetime]:
        start = cluster[0].event_at
        if reference_at is None:
            return (0.0, -len(cluster), start)
        return (abs((start - reference_at).total_seconds()), -len(cluster), start)

    return min(clusters, key=sort_key)


@dataclass(frozen=True)
class PaymentFinding:
    issue: Literal["payment_mismatch", "duplicate_charge", "valid_split_payment"] | None
    genuine_total_brl: float
    due_total_brl: float
    genuine_events: tuple[PaymentEvent, ...]
    conflicting_events: tuple[PaymentEvent, ...]
    note: str


def assess_payment_integrity(
    timeline_events: list[dict[str, Any]],
    due_total_brl: float,
    reference_at: datetime | None = None,
) -> PaymentFinding:
    events = parse_payment_events(timeline_events)
    clusters = group_clusters(events)
    genuine = select_genuine_cluster(clusters, reference_at)
    later = [event for event in events if event not in genuine]

    genuine_captured = [event for event in genuine if event.event_type == "captured"]
    genuine_total = round(sum(event.amount_brl for event in genuine_captured), 2)

    mismatch_flag = any(event.event_type == "reconciliation_mismatch" for event in genuine)
    if mismatch_flag:
        return PaymentFinding(
            issue="payment_mismatch",
            genuine_total_brl=genuine_total,
            due_total_brl=due_total_brl,
            genuine_events=tuple(genuine),
            conflicting_events=tuple(later),
            note="reconciliation_mismatch event present in the genuine payment cluster",
        )

    # A later cluster that recaptures the same amounts as the genuine one is a
    # duplicate charge, not a second legitimate installment.
    genuine_amounts = sorted(event.amount_brl for event in genuine_captured)
    later_captured = [event for event in later if event.event_type == "captured"]
    if later_captured and sorted(event.amount_brl for event in later_captured) == genuine_amounts:
        return PaymentFinding(
            issue="duplicate_charge",
            genuine_total_brl=genuine_total,
            due_total_brl=due_total_brl,
            genuine_events=tuple(genuine),
            conflicting_events=tuple(later),
            note="later cluster recaptures the same amount(s) as the genuine payment",
        )

    # Deliberately NOT flagging payment_mismatch just because genuine_total !=
    # due_total_brl: a real sample case showed a genuine, single, smaller
    # capture with no reconciliation_mismatch event for an order whose true
    # issue was late delivery, not payment — independently re-deriving "the
    # right amount" from item totals produces false positives. Only the
    # platform's own reconciliation_mismatch event is trusted for that call.
    if len(genuine_captured) >= 2:
        return PaymentFinding(
            issue="valid_split_payment",
            genuine_total_brl=genuine_total,
            due_total_brl=due_total_brl,
            genuine_events=tuple(genuine),
            conflicting_events=tuple(later),
            note="multiple genuine captures reconcile exactly with the order total",
        )

    return PaymentFinding(
        issue=None,
        genuine_total_brl=genuine_total,
        due_total_brl=due_total_brl,
        genuine_events=tuple(genuine),
        conflicting_events=tuple(later),
        note="payments reconcile; no integrity issue found",
    )


# ------------------------------------------------------------------------- shipment


@dataclass(frozen=True)
class ShipmentEvent:
    event_at: datetime
    event_type: str
    actor: str
    status: str


def parse_shipment_events(events: list[dict[str, Any]]) -> list[ShipmentEvent]:
    return [
        ShipmentEvent(
            event_at=parse_datetime(event["event_at"]) or datetime.min,
            event_type=event.get("event_type", ""),
            actor=event.get("actor", ""),
            status=event.get("status", ""),
        )
        for event in events
    ]


@dataclass(frozen=True)
class DeliveryFinding:
    issue: Literal["late_delivery_seller", "late_delivery_logistics"] | None
    responsible_seller_id: str | None
    corroborating_event: ShipmentEvent | None
    conflicting_event: ShipmentEvent | None
    note: str


def assess_delivery(
    order: OrderFacts,
    canonical_items: list[ItemFacts],
    shipment_events: list[dict[str, Any]],
) -> DeliveryFinding:
    if order.delivered_customer_at is None or order.estimated_delivery_at is None:
        return DeliveryFinding(None, None, None, None, "order was never delivered to the customer")

    if order.delivered_customer_at <= order.estimated_delivery_at:
        # Still record a conflicting event if the log claims lateness anyway.
        conflicting = next(
            (
                event
                for event in parse_shipment_events(shipment_events)
                if event.event_type == "delivered_late"
            ),
            None,
        )
        note = "delivered on or before the estimated date"
        return DeliveryFinding(None, None, None, conflicting, note)

    seller_missed_handoff = any(
        order.delivered_carrier_at is not None
        and item.shipping_limit_at is not None
        and order.delivered_carrier_at > item.shipping_limit_at
        for item in canonical_items
    )
    issue: Literal["late_delivery_seller", "late_delivery_logistics"] = (
        "late_delivery_seller" if seller_missed_handoff else "late_delivery_logistics"
    )
    expected_actor = "seller" if seller_missed_handoff else "logistics_provider"
    responsible_seller_id = canonical_items[0].seller_id if canonical_items else None

    corroborating = None
    conflicting = None
    for event in parse_shipment_events(shipment_events):
        if event.event_type != "delivered_late":
            continue
        same_day = event.event_at.date() == order.delivered_customer_at.date()
        if same_day and event.actor == expected_actor:
            corroborating = event
        else:
            conflicting = event

    note = (
        f"delivered {order.delivered_customer_at.date()} "
        f"after estimate {order.estimated_delivery_at.date()}"
    )
    return DeliveryFinding(issue, responsible_seller_id, corroborating, conflicting, note)


# --------------------------------------------------------------------------- refund


@dataclass(frozen=True)
class RefundFinding:
    issue: Literal["refund_pending", "refund_failed"] | None
    amount_brl: float | None
    note: str


def assess_refund(
    refund_events: list[dict[str, Any]],
    reference_at: datetime | None = None,
    plausible_amounts: frozenset[float] | None = None,
) -> RefundFinding:
    """`reference_at` (the order's approval time) filters out a refund event
    timestamped before the order even existed. `plausible_amounts` (the
    genuine captured payment amounts, plus their total) filters out a refund
    for an amount that only a *conflicting* payment row had — a real sample
    case had exactly this: a plausibly-timed refund_failed event whose amount
    matched only the decoy duplicate payment, not any genuine one."""
    plausible = [
        event
        for event in refund_events
        if (reference_at is None or parse_datetime(event["event_at"]) >= reference_at)
        and (
            plausible_amounts is None
            or any(
                abs(money(event["amount_brl"]) - amount) <= AMOUNT_TOLERANCE_BRL
                for amount in plausible_amounts
            )
        )
    ]
    if not plausible:
        return RefundFinding(None, None, "no plausible refund history for this order")
    events = sorted(plausible, key=lambda event: event["event_at"])
    latest = events[-1]
    status = latest.get("status")
    amount = money(latest["amount_brl"]) if latest.get("amount_brl") is not None else None
    if status == "pending":
        return RefundFinding("refund_pending", amount, "latest refund request is still pending")
    if status == "failed":
        return RefundFinding("refund_failed", amount, "latest refund request failed")
    return RefundFinding(None, amount, f"latest refund request status is {status!r}")


# ---------------------------------------------------------------------------- order status


def assess_order_status(
    order: OrderFacts, total_paid_brl: float
) -> Literal["canceled_order_paid", "unavailable_order_paid"] | None:
    if total_paid_brl <= AMOUNT_TOLERANCE_BRL:
        return None
    if order.status == "canceled":
        return "canceled_order_paid"
    if order.status == "unavailable":
        return "unavailable_order_paid"
    return None


# ---------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class PolicyRule:
    case_status: Literal["action_required", "no_action", "needs_investigation"]
    recommended_action: str
    refund_brl: float
    responsible_party_type: str


@dataclass(frozen=True)
class PolicyFacts:
    policy_version: str
    rules: dict[str, PolicyRule]


def parse_policy(data: dict[str, Any]) -> PolicyFacts:
    rules = {
        issue_code: PolicyRule(
            case_status=rule["case_status"],
            recommended_action=rule["recommended_action"],
            refund_brl=money(rule["refund_brl"]),
            responsible_party_type=rule["responsible_parties"][0]["party_type"],
        )
        for issue_code, rule in data.get("rules", {}).items()
    }
    return PolicyFacts(policy_version=data["policy_version"], rules=rules)
