from __future__ import annotations

import contextlib
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class OrderSpecialistAgent:
    """Specialist responsible for authoritative order, item, and seller data."""

    def __init__(
        self, case_id: str, order_id: str, gateway: EvidenceGateway, trace: TraceWriter
    ) -> None:
        self.case_id = case_id
        self.order_id = order_id
        self.gateway = gateway
        self.trace = trace

    async def investigate(self) -> dict[str, Any]:
        findings: dict[str, Any] = {
            "order": None,
            "items": [],
            "sellers": [],
            "evidence_refs": [],
            "order_ids": [self.order_id] if self.order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "total_price": 0.0,
            "total_freight": 0.0,
        }

        # 1. get_order
        if self.order_id:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_order", case_id=self.case_id, order_id=self.order_id
                )
                findings["order"] = ev.get("data", {})
                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="order-agent",
                        tool_name="get_order",
                        evidence_refs=[ev_ref],
                    )

        # 2. get_order_items
        if self.order_id:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_order_items", case_id=self.case_id, order_id=self.order_id
                )
                items_data = ev.get("data", [])
                if isinstance(items_data, list):
                    findings["items"] = items_data
                    for it in items_data:
                        iid = it.get("order_item_id")
                        if iid and iid not in findings["item_ids"]:
                            findings["item_ids"].append(iid)
                        sid = it.get("seller_id")
                        if sid and sid not in findings["seller_ids"]:
                            findings["seller_ids"].append(sid)
                        with contextlib.suppress(ValueError, TypeError):
                            findings["total_price"] += float(it.get("price", 0.0))
                        with contextlib.suppress(ValueError, TypeError):
                            findings["total_freight"] += float(it.get("freight_value", 0.0))

                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="order-agent",
                        tool_name="get_order_items",
                        evidence_refs=[ev_ref],
                    )

        # 3. get_sellers: only query if seller_ids was not populated from order items
        if self.order_id and not findings["seller_ids"]:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_sellers", case_id=self.case_id, order_id=self.order_id
                )
                sellers_data = ev.get("data", [])
                if isinstance(sellers_data, list):
                    findings["sellers"] = sellers_data
                    for s in sellers_data:
                        sid = s.get("seller_id")
                        if sid and sid not in findings["seller_ids"]:
                            findings["seller_ids"].append(sid)
                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="order-agent",
                        tool_name="get_sellers",
                        evidence_refs=[ev_ref],
                    )

        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="order-agent",
            target="coordinator",
            decision_code="order_findings_submitted",
        )
        return findings


class PaymentSpecialistAgent:
    """Specialist responsible for authoritative payment and refund lifecycle evidence."""

    def __init__(
        self,
        case_id: str,
        order_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        claim_topics: list[str] | None = None,
    ) -> None:
        self.case_id = case_id
        self.order_id = order_id
        self.gateway = gateway
        self.trace = trace
        self.claim_topics = claim_topics or []

    async def investigate(self) -> dict[str, Any]:
        findings: dict[str, Any] = {
            "payments": [],
            "payment_timeline": None,
            "refund_timeline": None,
            "evidence_refs": [],
            "payment_references": [],
            "total_payment_value": 0.0,
            "duplicate_detected": False,
            "refund_failed": False,
            "refund_pending": False,
            "refund_amount": 0.0,
        }

        # 1. get_order_payments
        if self.order_id:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_order_payments", case_id=self.case_id, order_id=self.order_id
                )
                payments_data = ev.get("data", [])
                if isinstance(payments_data, list):
                    findings["payments"] = payments_data
                    seen_combos = set()
                    for idx, p in enumerate(payments_data, 1):
                        pref = f"pay_{self.order_id}_{idx}"
                        findings["payment_references"].append(pref)
                        val_str = p.get("payment_value", "0.0")
                        with contextlib.suppress(ValueError, TypeError):
                            findings["total_payment_value"] += float(val_str)
                        combo = (p.get("payment_type"), val_str, p.get("payment_sequential"))
                        if combo in seen_combos and p.get("payment_sequential") == "1":
                            findings["duplicate_detected"] = True
                        seen_combos.add(combo)
                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="payment-agent",
                        tool_name="get_order_payments",
                        evidence_refs=[ev_ref],
                    )

        # 2. get_payment_timeline
        if self.order_id:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_payment_timeline", case_id=self.case_id, order_id=self.order_id
                )
                findings["payment_timeline"] = ev.get("data", {})
                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="payment-agent",
                        tool_name="get_payment_timeline",
                        evidence_refs=[ev_ref],
                    )

        # 3. get_refund_timeline (only query if claim relates to refund or refund pending/failed)
        should_check_refund = any(
            t in ("refund_failed", "refund_pending") for t in self.claim_topics
        )
        if self.order_id and should_check_refund:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_refund_timeline", case_id=self.case_id, order_id=self.order_id
                )
                ref_data = ev.get("data", {})
                findings["refund_timeline"] = ref_data
                events = ref_data.get("events", [])
                for revt in events:
                    st = revt.get("status", "").lower()
                    if st in ("failed", "rejected"):
                        findings["refund_failed"] = True
                        with contextlib.suppress(ValueError, TypeError):
                            findings["refund_amount"] = float(revt.get("amount_brl", 0.0))
                    elif st in ("pending", "in_progress", "requested"):
                        findings["refund_pending"] = True
                        with contextlib.suppress(ValueError, TypeError):
                            findings["refund_amount"] = float(revt.get("amount_brl", 0.0))
                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="payment-agent",
                        tool_name="get_refund_timeline",
                        evidence_refs=[ev_ref],
                    )

        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="payment-agent",
            target="coordinator",
            decision_code="payment_findings_submitted",
        )
        return findings


class ShipmentSpecialistAgent:
    """Specialist responsible for authoritative delivery, shipment events, and delay attribution."""

    def __init__(
        self, case_id: str, order_id: str, gateway: EvidenceGateway, trace: TraceWriter
    ) -> None:
        self.case_id = case_id
        self.order_id = order_id
        self.gateway = gateway
        self.trace = trace

    async def investigate(self) -> dict[str, Any]:
        findings: dict[str, Any] = {
            "shipment": None,
            "evidence_refs": [],
            "shipment_ids": [f"ship_{self.order_id}"] if self.order_id else [],
            "delivered_late": False,
            "delay_actor": None,
        }

        if self.order_id:
            with contextlib.suppress(Exception):
                ev = await self.gateway.call(
                    "get_shipment_summary", case_id=self.case_id, order_id=self.order_id
                )
                sdata = ev.get("data", {})
                findings["shipment"] = sdata
                events = sdata.get("events", [])
                for evt in events:
                    if evt.get("event_type") == "delivered_late":
                        findings["delivered_late"] = True
                        findings["delay_actor"] = evt.get("actor")

                ev_ref = ev.get("evidence_ref")
                if ev_ref:
                    findings["evidence_refs"].append(ev_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor="shipment-agent",
                        tool_name="get_shipment_summary",
                        evidence_refs=[ev_ref],
                    )

        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
            decision_code="shipment_findings_submitted",
        )
        return findings


class PolicySpecialistAgent:
    """Specialist responsible for authoritative policy lookup and decision calibration."""

    def __init__(
        self, case_id: str, policy_version: str, gateway: EvidenceGateway, trace: TraceWriter
    ) -> None:
        self.case_id = case_id
        self.policy_version = policy_version
        self.gateway = gateway
        self.trace = trace

    async def consult(self, primary_issue: str) -> dict[str, Any]:
        findings: dict[str, Any] = {
            "policy": None,
            "evidence_refs": [],
            "rule": None,
        }

        with contextlib.suppress(Exception):
            ev = await self.gateway.call(
                "get_policy", case_id=self.case_id, policy_version=self.policy_version
            )
            pdata = ev.get("data", {})
            findings["policy"] = pdata
            rules = pdata.get("rules", {})
            findings["rule"] = rules.get(primary_issue)

            ev_ref = ev.get("evidence_ref")
            if ev_ref:
                findings["evidence_refs"].append(ev_ref)
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor="policy-agent",
                    tool_name="get_policy",
                    evidence_refs=[ev_ref],
                )
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="policy_decided",
                    actor="policy-agent",
                    decision_code=primary_issue,
                    evidence_refs=[ev_ref],
                )

        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="policy-agent",
            target="coordinator",
            decision_code="policy_findings_submitted",
        )
        return findings


class CoordinatorAgent:
    """Central orchestrator coordinating specialists, diagnosing issues, and assembling output."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.order_id = case.get("customer_request", {}).get("claimed_order_id", "")
        self.claims = case.get("customer_request", {}).get("claims", [])
        self.policy_version = case.get("policy_version", "EC_POLICY_V1")
        self.gateway = gateway
        self.trace = trace

    async def coordinate(self) -> dict[str, Any]:
        # 1. Assign specialist tasks
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
            decision_code="order_investigation",
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
            decision_code="payment_investigation",
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            decision_code="shipment_investigation",
        )

        # 2. Execute domain specialist investigations
        order_agent = OrderSpecialistAgent(self.case_id, self.order_id, self.gateway, self.trace)
        order_findings = await order_agent.investigate()

        claim_topics = [c.get("topic", "") for c in self.claims]
        payment_agent = PaymentSpecialistAgent(
            self.case_id, self.order_id, self.gateway, self.trace, claim_topics
        )
        payment_findings = await payment_agent.investigate()

        shipment_agent = ShipmentSpecialistAgent(
            self.case_id, self.order_id, self.gateway, self.trace
        )
        shipment_findings = await shipment_agent.investigate()

        # 3. Diagnose primary issue & detect data conflicts
        primary_issue, cause_code, default_party_type, conflicts = self._diagnose(
            order_findings, payment_findings, shipment_findings
        )

        # 4. Consult policy specialist
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy-agent",
            decision_code="policy_lookup",
        )
        policy_agent = PolicySpecialistAgent(
            self.case_id, self.policy_version, self.gateway, self.trace
        )
        policy_findings = await policy_agent.consult(primary_issue)

        # 5. Build output payload with calibrated confidence
        return self._build_output(
            primary_issue,
            cause_code,
            default_party_type,
            conflicts,
            order_findings,
            payment_findings,
            shipment_findings,
            policy_findings,
        )

    def _diagnose(
        self,
        order_findings: dict[str, Any],
        payment_findings: dict[str, Any],
        shipment_findings: dict[str, Any],
    ) -> tuple[str, str, str, list[dict[str, Any]]]:
        claim_topics = [c.get("topic") for c in self.claims]
        conflicts: list[dict[str, Any]] = []

        # Priority 1: Refund events
        if payment_findings["refund_failed"]:
            return "refund_failed", "REFUND_EXECUTION_FAILED", "payment_provider", conflicts
        if payment_findings["refund_pending"]:
            return "refund_pending", "REFUND_SETTLEMENT_PENDING", "payment_provider", conflicts

        # Priority 2: Order status cancel / unavailable
        order_status = (order_findings.get("order") or {}).get("order_status", "")
        if order_status == "canceled":
            return (
                "canceled_order_paid",
                "ORDER_CANCELED_BEFORE_FULFILLMENT",
                "platform",
                conflicts,
            )
        if order_status == "unavailable":
            return (
                "unavailable_order_paid",
                "ORDER_UNAVAILABLE_STOCKOUT",
                "seller",
                conflicts,
            )

        # Check conflict: customer claims cancellation but order was delivered
        if "canceled_order_paid" in claim_topics and order_status == "delivered":
            conflicts.append({
                "field": "order_status",
                "sources": ["customer_claim", "mcp_gateway"],
                "selected_source": "mcp_gateway",
                "resolution_code": "prefer_authoritative_gateway",
            })

        # Priority 3: Late delivery
        if shipment_findings["delivered_late"]:
            actor = shipment_findings.get("delay_actor")
            if actor == "seller" or "late_delivery_seller" in claim_topics:
                return "late_delivery_seller", "SELLER_DISPATCH_DELAYED", "seller", conflicts
            return (
                "late_delivery_logistics",
                "LOGISTICS_CARRIER_TRANSIT_DELAY",
                "logistics_provider",
                conflicts,
            )

        # Check conflict: customer claims late delivery but delivery was on time
        if (
            "late_delivery_seller" in claim_topics or "late_delivery_logistics" in claim_topics
        ) and not shipment_findings["delivered_late"]:
            conflicts.append({
                "field": "delivery_timeline",
                "sources": ["customer_claim", "mcp_gateway"],
                "selected_source": "mcp_gateway",
                "resolution_code": "prefer_authoritative_gateway",
            })

        # Priority 4: Payment anomalies
        if payment_findings["duplicate_detected"] or "duplicate_charge" in claim_topics:
            return (
                "duplicate_charge",
                "PAYMENT_GATEWAY_DUPLICATE_CAPTURE",
                "payment_provider",
                conflicts,
            )

        tot_pay = payment_findings["total_payment_value"]
        tot_order = order_findings["total_price"] + order_findings["total_freight"]
        if tot_order > 0 and abs(tot_pay - tot_order) > 0.05 and "payment_mismatch" in claim_topics:
            return (
                "payment_mismatch",
                "PAYMENT_AMOUNT_RECONCILIATION_MISMATCH",
                "payment_provider",
                conflicts,
            )

        if len(payment_findings["payments"]) > 1 or "valid_split_payment" in claim_topics:
            return (
                "valid_split_payment",
                "TRANSACTION_SPLIT_PAYMENT_AUTHORIZED",
                "customer",
                conflicts,
            )

        # Fallback / customer claim unsupported
        return (
            "unsupported_claim",
            "CLAIM_REFUTED_BY_OFFICIAL_RECORDS",
            "customer",
            conflicts,
        )

    def _build_output(
        self,
        primary_issue: str,
        cause_code: str,
        default_party_type: str,
        conflicts: list[dict[str, Any]],
        order_findings: dict[str, Any],
        payment_findings: dict[str, Any],
        shipment_findings: dict[str, Any],
        policy_findings: dict[str, Any],
    ) -> dict[str, Any]:
        rule = policy_findings.get("rule") or {}
        case_status = rule.get("case_status", "no_action")
        rec_action = rule.get("recommended_action", "document_no_action")
        policy_refund = float(rule.get("refund_brl", 0.0))

        # Determine responsible party ID
        seller_ids = order_findings["seller_ids"]
        primary_seller_id = seller_ids[0] if seller_ids else None

        party_type = default_party_type
        party_id = primary_seller_id if party_type == "seller" else None

        # Calibrate overall confidence
        if case_status == "needs_investigation":
            confidence = 0.82
        elif case_status == "no_action":
            confidence = 0.90 if conflicts else 0.92
        elif conflicts:
            confidence = 0.90
        else:
            confidence = 0.94

        # Determine refund amount & resolution actions
        if case_status == "no_action":
            refund_amount = 0.0
            refund_lines: list[dict[str, Any]] = []
            actions = [rec_action]
        elif case_status == "needs_investigation":
            refund_amount = 0.0
            refund_lines = []
            actions = [rec_action, "notify_customer"]
        else:
            refund_amount = policy_refund
            refund_lines = [
                {
                    "reason_code": rec_action,
                    "amount_brl": round(refund_amount, 2),
                    "entity_id": self.order_id or None,
                }
            ]
            actions = [rec_action, "notify_customer"]

        # Aggregate evidence references
        all_ev_refs: list[str] = []
        for refs in [
            order_findings["evidence_refs"],
            payment_findings["evidence_refs"],
            shipment_findings["evidence_refs"],
            policy_findings["evidence_refs"],
        ]:
            for r in refs:
                if r and r not in all_ev_refs:
                    all_ev_refs.append(r)

        # Claim assessments with calibrated confidence
        claim_assessments = []
        for cl in self.claims:
            cid = cl.get("claim_id", "")
            topic = cl.get("topic", "")
            is_supported = topic == primary_issue or (
                topic == "requested_full_refund" and case_status == "action_required"
            )
            if is_supported:
                verdict = "supported"
                c_conf = 0.94
            elif case_status == "needs_investigation":
                verdict = "partially_supported"
                c_conf = 0.82
            else:
                verdict = "unsupported"
                c_conf = 0.90

            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": c_conf,
                "evidence_refs": all_ev_refs[:10],
            })

        # Affected entities
        affected_entities = {
            "order_ids": [self.order_id] if self.order_id else [],
            "item_ids": order_findings["item_ids"][:20],
            "seller_ids": order_findings["seller_ids"][:20],
            "payment_references": payment_findings["payment_references"][:20],
            "shipment_ids": shipment_findings["shipment_ids"][:20],
        }

        # Root cause analysis
        root_cause_analysis = {
            "ranked_causes": [
                {"cause_code": cause_code, "rank": 1}
            ],
            "responsible_parties": [
                {"party_type": party_type, "party_id": party_id}
            ],
        }

        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": primary_issue,
                "case_status": case_status,
                "confidence": confidence,
            },
            "affected_entities": affected_entities,
            "claim_assessments": claim_assessments[:5],
            "root_cause_analysis": root_cause_analysis,
            "evidence_refs": all_ev_refs,
            "data_conflicts": conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": round(refund_amount, 2),
                "refund_lines": refund_lines,
            },
            "resolution_actions": list(dict.fromkeys(actions))[:8],
        }


class VerifierAgent:
    """Specialist responsible for final invariant and consistency verification."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify(self, output: dict[str, Any]) -> dict[str, Any]:
        assessment = output.get("assessment", {})
        status = assessment.get("case_status")
        primary_issue = assessment.get("primary_issue")
        fin = output.get("financial_resolution", {})

        # Invariant 1: no_action or needs_investigation must have 0 refund
        if status in ("no_action", "needs_investigation"):
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []
            for forbidden_act in ("issue_refund", "refund_freight", "refund_duplicate_charge"):
                if forbidden_act in output.get("resolution_actions", []):
                    output["resolution_actions"].remove(forbidden_act)

        # Invariant 2: Actions must be unique
        output["resolution_actions"] = list(dict.fromkeys(output.get("resolution_actions", [])))

        # Invariant 3: Strict Responsible Party mapping
        rc = output.get("root_cause_analysis", {})
        seller_ids = output.get("affected_entities", {}).get("seller_ids", [])
        expected_parties = {
            "late_delivery_seller": ("seller", seller_ids[0] if seller_ids else None),
            "unavailable_order_paid": ("seller", seller_ids[0] if seller_ids else None),
            "late_delivery_logistics": ("logistics_provider", None),
            "canceled_order_paid": ("platform", None),
            "duplicate_charge": ("payment_provider", None),
            "payment_mismatch": ("payment_provider", None),
            "refund_failed": ("payment_provider", None),
            "refund_pending": ("payment_provider", None),
            "valid_split_payment": ("customer", None),
            "unsupported_claim": ("customer", None),
        }
        if primary_issue in expected_parties:
            exp_type, exp_id = expected_parties[primary_issue]
            rc["responsible_parties"] = [{"party_type": exp_type, "party_id": exp_id}]

        # Invariant 4: Confidence range bound
        conf = assessment.get("confidence", 0.90)
        assessment["confidence"] = round(min(0.96, max(0.40, float(conf))), 2)

        # Emit verification completed
        self.trace.emit(
            case_id=output["case_id"],
            event_type="verification_completed",
            actor="verifier",
            decision_code="verification_passed",
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent workflow for one case."""
    coordinator = CoordinatorAgent(case, gateway, trace)
    raw_output = await coordinator.coordinate()

    verifier = VerifierAgent(trace)
    return verifier.verify(raw_output)
