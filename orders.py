"""The brain of the order domain: which command is allowed in which status, what it changes, and
which business fact it becomes. Pure functions, no I/O: easy to test, easy to show on a slide.

    command(order, ...) -> Decision(changes, event type, event data)
    or raises StateConflict (409) when the order's status does not allow it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Statuses in which each command is allowed (README: the state machine).
ALLOWED = {
    "cancelOrder": {"placed", "confirmed", "onHold"},
    "amendOrderLines": {"placed", "confirmed"},
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
    event: str             # the business fact, e.g. OrderCancelled
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


def place_order(request: dict, idempotency_key: str | None) -> dict:
    """A new order, as it is handed to Salesforce. OrderPlaced follows once Salesforce numbered it."""
    order = {
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
    _allow("cancelOrder", order)
    return Decision({"status": "cancelled", "cancellationReason": reason, "holdReason": None},
                    "OrderCancelled", {"reason": reason, "cancelledAt": now()})


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


def apply(order: dict, decision: Decision) -> dict:
    """The order after the decision, as it is handed back to Salesforce (None removes a field)."""
    changed = {**order, **decision.changes}
    return {key: value for key, value in changed.items() if value is not None}


def event_data(decision: Decision, saved: dict) -> dict:
    """The event's data: the decision's facts, plus what Salesforce computed (the total of new lines)."""
    data = {"orderId": saved["orderId"], **decision.data}
    if decision.event == "OrderAmended":
        data = {"orderId": saved["orderId"], "orderLines": saved["orderLines"],
                "totalAmount": saved["totalAmount"], **decision.data}
    return data
