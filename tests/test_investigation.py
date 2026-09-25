from __future__ import annotations

from student_agent.investigation import (
    assess_delivery,
    assess_order_status,
    assess_payment_integrity,
    assess_refund,
    parse_items,
    parse_order,
    parse_policy,
    pick_canonical_items,
)

# Fixtures below mirror the *shape* of real gateway responses sampled while
# building this module (field names, value types, and the noisy-duplicate-row
# pattern), reconstructed as literals for offline testing — not live evidence.


def test_canceled_order_with_payment_is_flagged() -> None:
    order = parse_order(
        {
            "order_id": "order-1",
            "order_status": "canceled",
            "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
            "order_approved_at": "2017-12-20T10:00:00-03:00",
            "order_delivered_carrier_date": "2017-12-22T09:00:00-03:00",
            "order_delivered_customer_date": None,
            "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00",
        }
    )
    assert assess_order_status(order, total_paid_brl=79.00) == "canceled_order_paid"
    assert assess_order_status(order, total_paid_brl=0.0) is None


def test_unavailable_order_with_payment_is_flagged() -> None:
    order = parse_order(
        {
            "order_id": "order-2",
            "order_status": "unavailable",
            "order_purchase_timestamp": "2018-01-21T09:00:00-03:00",
            "order_approved_at": "2018-01-21T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-01-23T09:00:00-03:00",
            "order_delivered_customer_date": None,
            "order_estimated_delivery_date": "2018-01-31T09:00:00-03:00",
        }
    )
    assert assess_order_status(order, total_paid_brl=89.00) == "unavailable_order_paid"


def test_pick_canonical_items_drops_the_far_future_duplicate() -> None:
    order = parse_order(
        {
            "order_id": "order-1",
            "order_status": "canceled",
            "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
            "order_approved_at": "2017-12-20T10:00:00-03:00",
            "order_delivered_carrier_date": "2017-12-22T09:00:00-03:00",
            "order_delivered_customer_date": None,
            "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00",
        }
    )
    items = parse_items(
        [
            {
                "order_id": "order-1",
                "order_item_id": "item-1",
                "product_id": "product-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2017-12-23T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "10.00",
            },
            {
                "order_id": "order-1",
                "order_item_id": "item-1",
                "product_id": "product-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2018-05-14T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "18.00",
            },
        ]
    )
    canonical, conflicts = pick_canonical_items(items, order)
    assert len(canonical) == 1
    assert canonical[0].freight_value == 10.00
    assert len(conflicts) == 1
    assert conflicts[0].freight_value == 18.00


def test_pick_canonical_items_drops_the_before_approval_duplicate() -> None:
    order = parse_order(
        {
            "order_id": "order-4",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-03-23T09:00:00-03:00",
            "order_approved_at": "2018-03-23T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-03-25T09:00:00-03:00",
            "order_delivered_customer_date": "2018-04-07T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-04-02T09:00:00-03:00",
        }
    )
    items = parse_items(
        [
            {
                "order_id": "order-4",
                "order_item_id": "item-4",
                "product_id": "product-4",
                "seller_id": "seller-4",
                "shipping_limit_date": "2018-03-26T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "18.00",
            },
            {
                "order_id": "order-4",
                "order_item_id": "item-4",
                "product_id": "product-4",
                "seller_id": "seller-4",
                "shipping_limit_date": "2018-02-11T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "10.00",
            },
        ]
    )
    canonical, _conflicts = pick_canonical_items(items, order)
    assert len(canonical) == 1
    assert canonical[0].shipping_limit_at.isoformat() == "2018-03-26T09:00:00-03:00"


def test_late_delivery_after_on_time_seller_handoff_is_logistics_fault() -> None:
    order = parse_order(
        {
            "order_id": "order-4",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-03-23T09:00:00-03:00",
            "order_approved_at": "2018-03-23T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-03-25T09:00:00-03:00",
            "order_delivered_customer_date": "2018-04-07T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-04-02T09:00:00-03:00",
        }
    )
    items = parse_items(
        [
            {
                "order_id": "order-4",
                "order_item_id": "item-4",
                "product_id": "product-4",
                "seller_id": "seller-4",
                "shipping_limit_date": "2018-03-26T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "18.00",
            }
        ]
    )
    events = [
        {
            "order_id": "order-4",
            "event_at": "2018-04-07T09:00:00-03:00",
            "event_type": "delivered_late",
            "actor": "logistics_provider",
            "status": "confirmed",
        }
    ]
    finding = assess_delivery(order, items, events)
    assert finding.issue == "late_delivery_logistics"
    assert finding.responsible_seller_id == "seller-4"
    assert finding.corroborating_event is not None


def test_late_delivery_after_missed_seller_handoff_is_seller_fault() -> None:
    order = parse_order(
        {
            "order_id": "order-5",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-03-23T09:00:00-03:00",
            "order_approved_at": "2018-03-23T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-03-30T09:00:00-03:00",
            "order_delivered_customer_date": "2018-04-07T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-04-02T09:00:00-03:00",
        }
    )
    items = parse_items(
        [
            {
                "order_id": "order-5",
                "order_item_id": "item-5",
                "product_id": "product-5",
                "seller_id": "seller-5",
                "shipping_limit_date": "2018-03-26T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "18.00",
            }
        ]
    )
    finding = assess_delivery(order, items, [])
    assert finding.issue == "late_delivery_seller"
    assert finding.responsible_seller_id == "seller-5"


def test_delivery_on_time_ignores_a_stale_delivered_late_event() -> None:
    order = parse_order(
        {
            "order_id": "order-10",
            "order_status": "delivered",
            "order_purchase_timestamp": "2017-12-29T09:00:00-03:00",
            "order_approved_at": "2017-12-29T10:00:00-03:00",
            "order_delivered_carrier_date": "2017-12-31T09:00:00-03:00",
            "order_delivered_customer_date": "2018-01-07T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-01-08T09:00:00-03:00",
        }
    )
    items = parse_items(
        [
            {
                "order_id": "order-10",
                "order_item_id": "item-10",
                "product_id": "product-10",
                "seller_id": "seller-10",
                "shipping_limit_date": "2018-01-01T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "10.00",
            }
        ]
    )
    events = [
        {
            "order_id": "order-10",
            "event_at": "2018-05-17T09:00:00-03:00",
            "event_type": "delivered_late",
            "actor": "logistics_provider",
            "status": "confirmed",
        }
    ]
    finding = assess_delivery(order, items, events)
    assert finding.issue is None
    assert finding.conflicting_event is not None


def test_payment_mismatch_detected_from_reconciliation_event() -> None:
    events = [
        {
            "order_id": "order-6",
            "event_at": "2018-05-25T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "35.00",
            "status": "confirmed",
        },
        {
            "order_id": "order-6",
            "event_at": "2018-05-25T12:00:00-03:00",
            "event_type": "reconciliation_mismatch",
            "amount_brl": "35.00",
            "status": "open",
        },
        {
            "order_id": "order-6",
            "event_at": "2018-09-06T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "89.00",
            "status": "confirmed",
        },
    ]
    finding = assess_payment_integrity(events, due_total_brl=89.00)
    assert finding.issue == "payment_mismatch"
    assert finding.genuine_total_brl == 35.00


def test_duplicate_charge_detected_when_the_same_amounts_recapture_later() -> None:
    events = [
        {
            "order_id": "order-7",
            "event_at": "2018-06-25T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "64.00",
            "status": "confirmed",
        },
        {
            "order_id": "order-7",
            "event_at": "2018-06-25T11:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "64.00",
            "status": "confirmed",
        },
        {
            "order_id": "order-7",
            "event_at": "2018-08-05T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "64.00",
            "status": "confirmed",
        },
        {
            "order_id": "order-7",
            "event_at": "2018-08-05T11:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "64.00",
            "status": "confirmed",
        },
    ]
    finding = assess_payment_integrity(events, due_total_brl=128.00)
    assert finding.issue == "duplicate_charge"
    assert finding.genuine_total_brl == 128.00


def test_valid_split_payment_ignores_the_conflicting_row() -> None:
    events = [
        {
            "order_id": "order-5",
            "event_at": "2018-04-23T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "44.50",
            "status": "confirmed",
        },
        {
            "order_id": "order-5",
            "event_at": "2018-04-23T11:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "44.50",
            "status": "confirmed",
        },
        {
            "order_id": "order-5",
            "event_at": "2018-01-07T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "52.00",
            "status": "confirmed",
        },
    ]
    finding = assess_payment_integrity(events, due_total_brl=89.00)
    assert finding.issue == "valid_split_payment"
    assert finding.genuine_total_brl == 89.00


def test_payment_cluster_selection_prefers_proximity_over_size() -> None:
    from datetime import datetime

    # A real sample case had a *larger* decoy cluster (captured +
    # reconciliation_mismatch, 2 events) sitting weeks away from the order's
    # real, single genuine capture that lands exactly on order_approved_at.
    events = [
        {
            "order_id": "order-8",
            "event_at": "2018-07-04T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "35.00",
            "status": "confirmed",
        },
        {
            "order_id": "order-8",
            "event_at": "2018-07-04T12:00:00-03:00",
            "event_type": "reconciliation_mismatch",
            "amount_brl": "35.00",
            "status": "open",
        },
        {
            "order_id": "order-8",
            "event_at": "2018-07-27T10:00:00-03:00",
            "event_type": "captured",
            "amount_brl": "89.00",
            "status": "confirmed",
        },
    ]
    reference_at = datetime.fromisoformat("2018-07-27T10:00:00-03:00")
    finding = assess_payment_integrity(events, due_total_brl=89.00, reference_at=reference_at)
    assert finding.issue is None
    assert finding.genuine_total_brl == 89.00


def test_refund_event_before_the_order_was_approved_is_ignored() -> None:
    from datetime import datetime

    reference_at = datetime.fromisoformat("2018-04-23T10:00:00-03:00")
    finding = assess_refund(
        [
            {
                "order_id": "order-5",
                "event_at": "2018-01-18T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "52.00",
                "status": "failed",
            }
        ],
        reference_at=reference_at,
    )
    assert finding.issue is None


def test_refund_event_for_an_amount_no_genuine_payment_had_is_ignored() -> None:
    from datetime import datetime

    reference_at = datetime.fromisoformat("2018-06-03T10:00:00-03:00")
    finding = assess_refund(
        [
            {
                "order_id": "order-15",
                "event_at": "2018-09-08T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "52.00",
                "status": "failed",
            }
        ],
        reference_at=reference_at,
        plausible_amounts=frozenset({44.50, 89.00}),
    )
    assert finding.issue is None


def test_refund_pending_and_failed_are_read_from_the_latest_event() -> None:
    pending = assess_refund(
        [
            {
                "order_id": "order-8",
                "event_at": "2018-08-07T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "89.00",
                "status": "pending",
            }
        ]
    )
    assert pending.issue == "refund_pending"

    failed = assess_refund(
        [
            {
                "order_id": "order-9",
                "event_at": "2018-09-08T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "52.00",
                "status": "failed",
            }
        ]
    )
    assert failed.issue == "refund_failed"

    assert assess_refund([]).issue is None


def test_parse_policy_reads_the_flat_rules_table() -> None:
    policy = parse_policy(
        {
            "currency": "BRL",
            "policy_version": "EC_POLICY_V1",
            "rules": {
                "canceled_order_paid": {
                    "case_status": "action_required",
                    "recommended_action": "issue_refund",
                    "refund_brl": 79.0,
                    "responsible_parties": [{"party_id": None, "party_type": "platform"}],
                }
            },
        }
    )
    rule = policy.rules["canceled_order_paid"]
    assert rule.case_status == "action_required"
    assert rule.refund_brl == 79.0
    assert rule.responsible_party_type == "platform"
