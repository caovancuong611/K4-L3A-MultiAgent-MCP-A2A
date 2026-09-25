from __future__ import annotations

from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

from . import OUTPUT_SCHEMA_VERSION
from .evidence import CaseEvidenceCollector, EvidenceRecord
from .investigation import (
    PaymentFinding,
    assess_delivery,
    assess_order_status,
    assess_payment_integrity,
    assess_refund,
    money,
    parse_items,
    parse_order,
    parse_policy,
    pick_canonical_items,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Confidence is deliberately calibrated by how direct the supporting signal
# was, not set to a constant — L3A scores calibration against correctness.
CONFIDENCE_BY_ISSUE: dict[str, float] = {
    "canceled_order_paid": 0.95,
    "unavailable_order_paid": 0.95,
    "payment_mismatch": 0.9,
    "duplicate_charge": 0.85,
    "refund_pending": 0.9,
    "refund_failed": 0.9,
    "valid_split_payment": 0.8,
    "unsupported_claim": 0.6,
    "insufficient_evidence": 0.2,
}
DELIVERY_CONFIDENCE_WITH_EVENT = 0.9
DELIVERY_CONFIDENCE_DATES_ONLY = 0.75

# A real run against the gateway hit all three of these: business-level "not
# found" (RuntimeError from EvidenceGateway.call), malformed evidence
# (ValueError from schema validation), a dropped connection
# (httpx2.TransportError / TimeoutError even after the gateway's own retry),
# and a mid-session 502 surfaced as MCPError. Any of them means "this one
# lookup didn't come through" — never invented data — so the case degrades
# gracefully instead of crashing the whole run.
EVIDENCE_LOOKUP_ERRORS = (
    RuntimeError,
    ValueError,
    KeyError,
    httpx2.TransportError,
    TimeoutError,
    MCPError,
)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3A coordinator: dispatch order/payment/shipment/policy specialists over
    the MCP gateway, then verify their findings into the case output.

    Every conclusion traces back to real evidence pulled through
    `CaseEvidenceCollector` (mục 4) — nothing here invents data or an
    evidence_ref. See ARCHITECTURE.md for the full design write-up.
    """
    case_id = case["case_id"]
    order_id = case["customer_request"]["claimed_order_id"]
    claims = case["customer_request"]["claims"]
    policy_version = case["policy_version"]

    evidence = CaseEvidenceCollector(gateway, trace, case_id=case_id)
    cited: list[EvidenceRecord] = []

    # --- order-agent -----------------------------------------------------
    trace.emit(case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=ORDER_AGENT)
    try:
        order_record = await evidence.fetch("get_order", actor=ORDER_AGENT, order_id=order_id)
        items_record = await evidence.fetch(
            "get_order_items", actor=ORDER_AGENT, order_id=order_id
        )
    except EVIDENCE_LOOKUP_ERRORS as exc:
        return _insufficient_evidence_output(
            case_id, claims, cited, trace, note=f"order lookup failed: {exc}"
        )
    order = parse_order(order_record.data)
    raw_items = parse_items(items_record.data)
    canonical_items, conflicting_items = pick_canonical_items(raw_items, order)
    cited += [order_record, items_record]

    # --- payment-agent -----------------------------------------------------
    trace.emit(case_id=case_id, event_type="handoff", actor=ORDER_AGENT, target=PAYMENT_AGENT)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=PAYMENT_AGENT
    )
    try:
        payments_record = await evidence.fetch(
            "get_order_payments", actor=PAYMENT_AGENT, order_id=order_id
        )
    except EVIDENCE_LOOKUP_ERRORS as exc:
        return _insufficient_evidence_output(
            case_id, claims, cited, trace, note=f"payment lookup failed: {exc}"
        )
    cited.append(payments_record)
    total_paid_brl = round(sum(money(row["payment_value"]) for row in payments_record.data), 2)

    payment_finding: PaymentFinding | None = None
    try:
        timeline_record = await evidence.fetch(
            "get_payment_timeline", actor=PAYMENT_AGENT, order_id=order_id
        )
    except EVIDENCE_LOOKUP_ERRORS:
        timeline_record = None
    else:
        cited.append(timeline_record)
        due_total_brl = round(
            sum(item.price + item.freight_value for item in canonical_items), 2
        )
        payment_finding = assess_payment_integrity(
            timeline_record.data.get("events", []), due_total_brl, reference_at=order.approved_at
        )

    plausible_refund_amounts: frozenset[float] | None = None
    if payment_finding is not None:
        genuine_captured = [
            event for event in payment_finding.genuine_events if event.event_type == "captured"
        ]
        amounts = {event.amount_brl for event in genuine_captured}
        if genuine_captured:
            amounts.add(payment_finding.genuine_total_brl)
        plausible_refund_amounts = frozenset(amounts)

    refund_issue: str | None = None
    refund_record: EvidenceRecord | None = None
    try:
        refund_record = await evidence.fetch(
            "get_refund_timeline", actor=PAYMENT_AGENT, order_id=order_id
        )
    except EVIDENCE_LOOKUP_ERRORS:
        refund_record = None
    else:
        cited.append(refund_record)
        refund_finding = assess_refund(
            refund_record.data.get("events", []),
            reference_at=order.approved_at,
            plausible_amounts=plausible_refund_amounts,
        )
        refund_issue = refund_finding.issue

    # --- shipment-agent ------------------------------------------------
    trace.emit(case_id=case_id, event_type="handoff", actor=PAYMENT_AGENT, target=SHIPMENT_AGENT)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=SHIPMENT_AGENT
    )
    try:
        shipment_record = await evidence.fetch(
            "get_shipment_summary", actor=SHIPMENT_AGENT, order_id=order_id
        )
    except EVIDENCE_LOOKUP_ERRORS:
        shipment_record = None
        delivery_finding = assess_delivery(order, canonical_items, [])
    else:
        cited.append(shipment_record)
        delivery_finding = assess_delivery(
            order, canonical_items, shipment_record.data.get("events", [])
        )

    # --- policy-agent --------------------------------------------------
    trace.emit(case_id=case_id, event_type="handoff", actor=SHIPMENT_AGENT, target=POLICY_AGENT)
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor=COORDINATOR, target=POLICY_AGENT
    )
    try:
        policy_record = await evidence.fetch(
            "get_policy", actor=POLICY_AGENT, policy_version=policy_version
        )
    except EVIDENCE_LOOKUP_ERRORS as exc:
        return _insufficient_evidence_output(
            case_id, claims, cited, trace, note=f"policy lookup failed: {exc}"
        )
    cited.append(policy_record)
    policy = parse_policy(policy_record.data)

    # --- verifier --------------------------------------------------------
    trace.emit(case_id=case_id, event_type="handoff", actor=POLICY_AGENT, target=VERIFIER)

    order_status_issue = assess_order_status(order, total_paid_brl)
    if order_status_issue:
        primary_issue = order_status_issue
        confidence = CONFIDENCE_BY_ISSUE[primary_issue]
        supporting = [order_record, payments_record]
    elif payment_finding is not None and payment_finding.issue in (
        "payment_mismatch",
        "duplicate_charge",
    ):
        primary_issue = payment_finding.issue
        confidence = CONFIDENCE_BY_ISSUE[primary_issue]
        supporting = [payments_record] + ([timeline_record] if timeline_record else [])
    elif refund_issue:
        primary_issue = refund_issue
        confidence = CONFIDENCE_BY_ISSUE[primary_issue]
        supporting = [refund_record] if refund_record else []
    elif delivery_finding.issue:
        primary_issue = delivery_finding.issue
        confidence = (
            DELIVERY_CONFIDENCE_WITH_EVENT
            if delivery_finding.corroborating_event
            else DELIVERY_CONFIDENCE_DATES_ONLY
        )
        supporting = [order_record, items_record] + (
            [shipment_record] if shipment_record else []
        )
    elif payment_finding is not None and payment_finding.issue == "valid_split_payment":
        primary_issue = "valid_split_payment"
        confidence = CONFIDENCE_BY_ISSUE[primary_issue]
        supporting = [payments_record] + ([timeline_record] if timeline_record else [])
    else:
        primary_issue = "unsupported_claim"
        confidence = CONFIDENCE_BY_ISSUE[primary_issue]
        supporting = [order_record, payments_record]

    policy_rule = policy.rules.get(primary_issue)
    case_status = policy_rule.case_status if policy_rule else "needs_investigation"
    refund_brl = policy_rule.refund_brl if policy_rule else 0.0
    recommended_action = (
        policy_rule.recommended_action if policy_rule else "escalate_for_manual_review"
    )
    responsible_party_type = policy_rule.responsible_party_type if policy_rule else "unknown"

    is_late_delivery_issue = primary_issue.startswith("late_delivery")
    responsible_seller_id = (
        delivery_finding.responsible_seller_id if is_late_delivery_issue else None
    )
    party_id = responsible_seller_id if responsible_party_type == "seller" else None

    supporting += [policy_record]
    supporting_refs = _dedup_refs(supporting)

    data_conflicts = _build_data_conflicts(conflicting_items, delivery_finding)

    claim_assessments = _build_claim_assessments(
        claims, primary_issue, case_status, supporting_refs
    )

    refund_lines = []
    if refund_brl > 0:
        refund_lines.append(
            {
                "reason_code": primary_issue,
                "amount_brl": refund_brl,
                "entity_id": party_id or order_id,
            }
        )

    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": sorted({item.order_item_id for item in canonical_items}),
            "seller_ids": sorted({item.seller_id for item in canonical_items}),
            "payment_references": [order_id],
            "shipment_ids": [order_id],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": [
                {"party_type": responsible_party_type, "party_id": party_id}
            ],
        },
        "evidence_refs": supporting_refs,
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [recommended_action],
    }

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code=primary_issue,
        evidence_refs=supporting_refs,
    )
    return output


def _dedup_refs(records: list[EvidenceRecord]) -> list[str]:
    seen: dict[str, None] = {}
    for record in records:
        seen.setdefault(record.evidence_ref, None)
    return list(seen)


def _build_claim_assessments(
    claims: list[dict[str, Any]],
    primary_issue: str,
    case_status: str,
    supporting_refs: list[str],
) -> list[dict[str, Any]]:
    assessments = []
    for claim in claims:
        topic = claim["topic"]
        if topic == "requested_full_refund":
            if case_status == "needs_investigation":
                verdict = "insufficient_evidence"
                confidence = 0.3
            elif case_status == "action_required":
                verdict = "supported"
                confidence = CONFIDENCE_BY_ISSUE.get(primary_issue, 0.5)
            else:
                verdict = "unsupported"
                confidence = CONFIDENCE_BY_ISSUE.get(primary_issue, 0.5)
        elif topic == primary_issue:
            verdict = "supported"
            confidence = CONFIDENCE_BY_ISSUE.get(primary_issue, 0.5)
        elif primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            confidence = 0.2
        else:
            verdict = "unsupported"
            confidence = CONFIDENCE_BY_ISSUE.get(primary_issue, 0.5)
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": supporting_refs,
            }
        )
    return assessments


def _build_data_conflicts(
    conflicting_items: list[Any], delivery_finding: Any
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if conflicting_items:
        conflicts.append(
            {
                "field": "order_items.shipping_limit_date",
                "sources": ["get_order_items:canonical_row", "get_order_items:conflicting_row"],
                "selected_source": "get_order_items:canonical_row",
                "resolution_code": "picked_row_closest_to_order_approval",
            }
        )
    if delivery_finding.conflicting_event is not None:
        conflicts.append(
            {
                "field": "shipment_summary.events.delivered_late",
                "sources": ["order_delivered_customer_date", "get_shipment_summary:events"],
                "selected_source": "order_delivered_customer_date",
                "resolution_code": "event_inconsistent_with_authoritative_timestamps",
            }
        )
    return conflicts


def _insufficient_evidence_output(
    case_id: str,
    claims: list[dict[str, Any]],
    cited: list[EvidenceRecord],
    trace: TraceWriter,
    *,
    note: str,
) -> dict[str, Any]:
    refs = _dedup_refs(cited)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ORDER_AGENT,
        target=VERIFIER,
        attributes={"reason": note[:160]},
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="insufficient_evidence",
        evidence_refs=refs,
    )
    claim_assessments = [
        {
            "claim_id": claim["claim_id"],
            "verdict": "insufficient_evidence",
            "confidence": 0.2,
            "evidence_refs": refs,
        }
        for claim in claims
    ]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": CONFIDENCE_BY_ISSUE["insufficient_evidence"],
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_for_manual_review"],
    }
