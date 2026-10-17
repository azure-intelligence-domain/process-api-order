"""The brain of the order domain: which command is allowed in which status, what it changes, and
which business fact it becomes. Pure functions, no I/O: easy to test, easy to show on a slide.

    command(order, ...) -> Decision(changes, event type, event data)
    or raises StateConflict (409) when the order's status does not allow it.

Every fact about an order lands on its timeline, and the order records where it lives in the other
domains' systems (references: the SAP sales order, the WMS reservation, the 3PL shipment), so a
cancellation can tell each domain what to undo (ADR 0018).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Statuses in which each command is allowed (README: the state machine).
ALLOWED = {
    "cancelOrder": {"placed", "confirmed", "onHold", "readyForShipment"},  # until the 3PL picks it up
    "amendOrderLines": {"placed"},  # once SAP and the WMS have it: cancel and order again
    "changeDeliveryAddress": {"placed", "confirmed", "onHold"},
    "rescheduleDelivery": {"placed", "confirmed", "onHold", "readyForShipment"},
    "putOrderOnHold": {"confirmed"},
    "releaseOrder": {"onHold"},
}


class StateConflict(Exception):
    """The order's status does not allow this command (409 Order State Conflict)."""


@dataclass(frozen=True)
class Decision:
    changes: dict          # fields of the order that change
    event: str | None      # the business fact, e.g. OrderCancelled; None: only recorded on the order
    data: dict = field(default_factory=dict)  # the event's data, without orderId


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _allow(command: str, order: dict) -> None:
    if order["status"] not in ALLOWED[command]:
        allowed = ", ".join(sorted(ALLOWED[command]))
        raise StateConflict(f"Order {order['orderId']} is {order['status']}; {command} is allowed while {allowed}.")


def numbered(lines: list[dict]) -> list[dict]:
    """Line numbers are assigned by the order domain, in the order given."""
    return [{**line, "lineNumber": number} for number, line in enumerate(lines, start=1)]


def milestone(fact: str, domain: str = "order") -> dict:
    return {"fact": fact, "domain": domain, "occurredAt": now()}


def place_order(request: dict, idempotency_key: str | None) -> dict:
    """A new order, as it is handed to Salesforce. OrderPlaced follows once Salesforce numbered it."""
    order = {
        "timeline": [milestone("OrderPlaced")],
        "customerId": request["customerId"],
        "channel": request["channel"],
        "status": "placed",
        "paymentStatus": "unpaid",
        "orderLines": numbered(request["orderLines"]),
        "deliveryAddress": request["deliveryAddress"],
        "requestedDeliveryDate": request.get("requestedDeliveryDate"),
        "idempotencyKey": idempotency_key,
    }
    return {key: value for key, value in order.items() if value is not None}


def order_placed(order: dict) -> Decision:
    data = {key: order[key] for key in ("customerId", "channel", "orderLines", "totalAmount", "deliveryAddress")}
    if "requestedDeliveryDate" in order:
        data["requestedDeliveryDate"] = order["requestedDeliveryDate"]
    return Decision({}, "OrderPlaced", {**data, "placedAt": order["createdAt"]})


def cancel_order(order: dict, reason: str) -> Decision:
    """The other domains undo their part: the event names what each of them has for this order."""
    _allow("cancelOrder", order)
    known = {key: value for key, value in order.get("references", {}).items()
             if key in ("erpOrderNumber", "reservationId", "shipmentId")}
    return Decision({"status": "cancelled", "cancellationReason": reason, "holdReason": None},
                    "OrderCancelled", {"reason": reason, "cancelledAt": now(), **known})


def amend_order_lines(order: dict, lines: list[dict]) -> Decision:
    _allow("amendOrderLines", order)
    return Decision({"orderLines": numbered(lines)}, "OrderAmended", {"amendedAt": now()})


def change_delivery_address(order: dict, address: dict) -> Decision:
    _allow("changeDeliveryAddress", order)
    return Decision({"deliveryAddress": address}, "OrderDeliveryAddressChanged",
                    {"deliveryAddress": address, "changedAt": now()})


def reschedule_delivery(order: dict, date: str) -> Decision:
    _allow("rescheduleDelivery", order)
    data = {"requestedDeliveryDate": date, "rescheduledAt": now()}
    if order.get("requestedDeliveryDate"):
        data["previousRequestedDeliveryDate"] = order["requestedDeliveryDate"]
    return Decision({"requestedDeliveryDate": date}, "OrderDeliveryRescheduled", data)


def put_order_on_hold(order: dict, reason: str) -> Decision:
    _allow("putOrderOnHold", order)
    return Decision({"status": "onHold", "holdReason": reason}, "OrderPutOnHold", {"reason": reason, "heldAt": now()})


def release_order(order: dict) -> Decision:
    _allow("releaseOrder", order)
    return Decision({"status": "confirmed", "holdReason": None}, "OrderReleased", {"releasedAt": now()})


def apply(order: dict, decision: Decision, fact: str | None = None, domain: str = "order") -> dict:
    """The order after the decision, as it is handed back to Salesforce (None removes a field). On
    the timeline: the other domain's fact that led to it (if any), then the order's own fact."""
    changed = {**order, **decision.changes}
    timeline = order.get("timeline", []) + ([milestone(fact, domain)] if fact else [])
    changed["timeline"] = timeline + ([milestone(decision.event)] if decision.event else [])
    return {key: value for key, value in changed.items() if value is not None}


def recorded(order: dict, **references) -> dict:
    """The order's references with new ones added, as a change."""
    return {"references": {**order.get("references", {}), **{k: v for k, v in references.items() if v is not None}}}


def seen(order: dict, fact: str) -> bool:
    return any(m["fact"] == fact for m in order.get("timeline", []))


def event_data(decision: Decision, saved: dict) -> dict:
    """The event's data: the decision's facts, plus what Salesforce computed (the total of new lines)."""
    data = {"orderId": saved["orderId"], **decision.data}
    if decision.event in ("OrderConfirmed", "OrderReadyForShipment"):
        data.update(_carried(saved))
    if decision.event == "OrderAmended":
        data = {"orderId": saved["orderId"], "orderLines": saved["orderLines"],
                "totalAmount": saved["totalAmount"], **decision.data}
    return data


# --- Policies: facts of other domains become facts of the order --------------------------------
# policy(order, data) -> Decision, or None when the order already reflects the fact (Event Grid
# delivers at least once), or StateConflict when the fact does not fit the order's status.


def _carried(order: dict) -> dict:
    """What downstream domains need from the order, carried in the event (event-carried state)."""
    keys = ("customerId", "orderLines", "totalAmount", "deliveryAddress", "requestedDeliveryDate")
    return {key: order[key] for key in keys if key in order}


def _require(order: dict, fact: str, *statuses: str) -> None:
    if order["status"] not in statuses:
        raise StateConflict(f"Order {order['orderId']} is {order['status']}; {fact} applies while {', '.join(statuses)}.")


def sales_order_accepted(order: dict, data: dict) -> Decision | None:
    refs = recorded(order, erpOrderNumber=data["erpOrderNumber"])
    if order["status"] == "cancelled" and not seen(order, "SalesOrderAccepted"):
        return Decision(refs, None)  # cancelled meanwhile: remember the sales order, nothing more
    if order["status"] in ("confirmed", "onHold", "readyForShipment", "shipped", "delivered", "completed", "cancelled"):
        return None
    _require(order, "SalesOrderAccepted", "placed")
    return Decision({"status": "confirmed", **refs}, "OrderConfirmed",
                    {"erpOrderNumber": data["erpOrderNumber"], "confirmedAt": now(), **_carried(order)})


def sales_order_refused(order: dict, data: dict) -> Decision | None:
    if order["status"] == "rejected":
        return None
    _require(order, "SalesOrderRefused", "placed")
    return Decision({"status": "rejected", **recorded(order, erpOrderNumber=data["erpOrderNumber"])}, "OrderRejected",
                    {"reason": data["reason"], "rejectedAt": now()})


def invoice_issued(order: dict, data: dict) -> Decision | None:
    if order["paymentStatus"] != "unpaid":
        return None
    return Decision({"paymentStatus": "invoiced", **recorded(order, invoiceNumber=data["invoiceNumber"])}, "OrderInvoiced",
                    {"invoiceNumber": data["invoiceNumber"], "invoicedAt": now()})


def payment_received(order: dict, data: dict) -> Decision | None:
    if not data["invoicePaidInFull"] or order["paymentStatus"] == "paid":
        return None  # a part payment is finance's business; the order is paid when the invoice is
    return Decision({"paymentStatus": "paid"}, "OrderPaid", {"paidAmount": order["totalAmount"], "paidAt": data["receivedAt"]})


def stock_reserved(order: dict, data: dict) -> Decision | None:
    if seen(order, "StockReserved"):
        return None
    refs = recorded(order, reservationId=data["reservationId"], warehouseId=data["warehouseId"])
    if order["status"] in ("onHold", "cancelled"):
        return Decision(refs, None)  # ready once released (ready_for_shipment); undone by the cancellation
    _require(order, "StockReserved", "confirmed")
    return Decision({"status": "readyForShipment", **refs}, "OrderReadyForShipment",
                    {"warehouseId": data["warehouseId"], "reservationId": data["reservationId"], "readyAt": now(), **_carried(order)})


def shipment_booked(order: dict, data: dict) -> Decision | None:
    if seen(order, "ShipmentBooked"):
        return None
    return Decision(recorded(order, shipmentId=data["shipmentId"], trackingNumber=data["trackingNumber"], partnerId=data["partnerId"]), None)


def shipment_dispatched(order: dict, data: dict) -> Decision | None:
    if order["status"] in ("shipped", "delivered", "completed"):
        return None
    _require(order, "ShipmentDispatched", "readyForShipment")
    refs = recorded(order, shipmentId=data["shipmentId"], trackingNumber=data["trackingNumber"], partnerId=data["partnerId"])
    return Decision({"status": "shipped", **refs}, "OrderShipped", {"shipmentId": data["shipmentId"], "partnerId": data["partnerId"],
                                                                  "trackingNumber": data["trackingNumber"], "shippedAt": data["dispatchedAt"]})


def sales_order_cancelled(order: dict, data: dict) -> Decision | None:
    """Finance undid its part of a cancelled order: recorded, no new order fact."""
    if seen(order, "SalesOrderCancelled"):
        return None
    return Decision(recorded(order, creditNoteNumber=data.get("creditNoteNumber")), None)


def stock_released(order: dict, data: dict) -> Decision | None:
    return None if seen(order, "StockReleased") else Decision({}, None)


def shipment_cancelled(order: dict, data: dict) -> Decision | None:
    return None if seen(order, "ShipmentCancelled") else Decision({}, None)


def shipment_delivered(order: dict, data: dict) -> Decision | None:
    if order["status"] in ("delivered", "completed"):
        return None
    _require(order, "ShipmentDelivered", "shipped")
    return Decision({"status": "delivered"}, "OrderDelivered", {"deliveredAt": data["deliveredAt"]})


def completion(order: dict) -> Decision | None:
    """OrderCompleted: the second of OrderDelivered and OrderPaid, in any order."""
    if order["status"] == "delivered" and order["paymentStatus"] == "paid":
        return Decision({"status": "completed"}, "OrderCompleted", {"completedAt": now()})
    return None


def ready_for_shipment(order: dict) -> Decision | None:
    """A confirmed order whose stock was reserved while it was on hold: ready once released."""
    refs = order.get("references", {})
    if order["status"] == "confirmed" and refs.get("reservationId"):
        return Decision({"status": "readyForShipment"}, "OrderReadyForShipment",
                        {"warehouseId": refs["warehouseId"], "reservationId": refs["reservationId"], "readyAt": now(), **_carried(order)})
    return None


FOLLOW_UPS = (completion, ready_for_shipment)  # what an order's change can lead to by itself


def follow_up(order: dict) -> Decision | None:
    return next((decision for decision in (rule(order) for rule in FOLLOW_UPS) if decision), None)


POLICIES = {
    "com.acme.finance.salesOrderAccepted": sales_order_accepted,
    "com.acme.finance.salesOrderRefused": sales_order_refused,
    "com.acme.finance.invoiceIssued": invoice_issued,
    "com.acme.finance.paymentReceived": payment_received,
    "com.acme.finance.salesOrderCancelled": sales_order_cancelled,
    "com.acme.supply.stockReserved": stock_reserved,
    "com.acme.supply.stockReleased": stock_released,
    "com.acme.logistics.shipmentBooked": shipment_booked,
    "com.acme.logistics.shipmentDispatched": shipment_dispatched,
    "com.acme.logistics.shipmentDelivered": shipment_delivered,
    "com.acme.logistics.shipmentCancelled": shipment_cancelled,
}


def fact_of(event_type: str) -> tuple[str, str]:
    """com.acme.finance.salesOrderAccepted → (SalesOrderAccepted, finance)."""
    _, _, domain, name = event_type.split(".", 3)
    return name[0].upper() + name[1:], domain
