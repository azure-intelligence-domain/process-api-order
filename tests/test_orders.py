"""The Order Process API against its contracts, with a fake Salesforce System API and topic."""

import json

import azure.functions as func
import pytest
from integration_envelope import validate

import function_app as api
import orders
from platform_clients import OrderChanged, as_new_order, event_type

PLACE = {
    "customerId": "ACME", "channel": "salesforce",
    "orderLines": [{"productId": "SKU-1042", "quantity": 12, "unitPrice": {"value": 7000.0, "currency": "EUR"}}],
    "deliveryAddress": {"street": "Rue de la Loi 16", "postalCode": "1000", "city": "Brussels", "countryCode": "BE"},
    "requestedDeliveryDate": "2026-10-09",
}


class FakeSalesforce:
    """Behaves like the Salesforce System API: numbers orders from 4711, computes totals, ETags."""

    def __init__(self):
        self.orders, self.etags, self.next, self.conflicts = {}, {}, 4711, 0

    def _store(self, order_id, new, created):
        lines = new["orderLines"]
        total = sum(line["quantity"] * line["unitPrice"]["value"] for line in lines)
        self.orders[order_id] = {"orderId": order_id, "recordId": f"801Ad{int(order_id):010d}AAA", **new,
                                 "totalAmount": {"value": total, "currency": lines[0]["unitPrice"]["currency"]},
                                 "createdAt": created, "updatedAt": "2026-10-16T10:05:00Z"}
        self.etags[order_id] = self.etags.get(order_id, 0) + 1
        return self.orders[order_id]

    def create(self, order):
        order_id, self.next = str(self.next), self.next + 1
        return self._store(order_id, as_new_order(order), "2026-10-16T10:00:00Z")

    def get(self, order_id):
        return (dict(self.orders[order_id]), f'"{self.etags[order_id]}"') if order_id in self.orders else None

    def replace(self, order_id, order, etag):
        if self.conflicts:  # someone else saves first
            self.conflicts -= 1
            self.etags[order_id] += 1
            raise OrderChanged
        assert etag == f'"{self.etags[order_id]}"'
        return self._store(order_id, as_new_order(order), self.orders[order_id]["createdAt"])

    def list(self, customerId=None, status=None, idempotencyKey=None, offset=0, limit=20):
        found = [o for o in self.orders.values()
                 if (customerId is None or o["customerId"] == customerId)
                 and (status is None or o["status"] == status)
                 and (idempotencyKey is None or o.get("idempotencyKey") == idempotencyKey)]
        return {"totalCount": len(found), "_embedded": {"orders": found[offset:offset + limit]}}


class FakeTopic:
    def __init__(self):
        self.events = []

    def publish(self, fact, order, data, correlation_id, traceparent=None):
        self.events.append((fact, order["orderId"], data, correlation_id))


def request(method, url, body=None, order_id=None, params=None, headers=None):
    headers = {"Correlation-Id": "ord-4711", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else b""
    route = {"orderId": order_id} if order_id else {}
    return func.HttpRequest(method, url, headers=headers, params=params or {}, route_params=route, body=data)


@pytest.fixture
def sf():
    return FakeSalesforce()


@pytest.fixture
def topic():
    return FakeTopic()


def placed(sf, topic, **headers):
    response = api.place_order(request("POST", "/orders", PLACE, headers=headers), sf, topic)
    return response, json.loads(response.get_body())


def do(intent, sf, topic, method="PUT", body=None, params=None, order_id="4711"):
    response = api.command(request(method, f"/orders/{order_id}", body, order_id, params), sf, topic, intent)
    return response, json.loads(response.get_body())


def test_place_order_publishes_order_placed(sf, topic):
    response, order = placed(sf, topic)
    assert response.status_code == 201 and response.headers["Location"] == "/orders/4711"
    assert order["status"] == "placed" and order["paymentStatus"] == "unpaid"
    assert order["orderLines"][0]["lineNumber"] == 1
    assert order["_links"]["hold"]["href"] == "/orders/4711/hold"
    assert "recordId" not in order
    fact, order_id, data, correlation = topic.events[0]
    assert (fact, order_id, correlation) == ("OrderPlaced", "4711", "ord-4711")
    assert data["totalAmount"] == {"value": 84000.0, "currency": "EUR"} and data["placedAt"]


def test_a_retry_with_the_same_idempotency_key_places_nothing(sf, topic):
    placed(sf, topic, **{"Idempotency-Key": "acme-0001"})
    response, order = placed(sf, topic, **{"Idempotency-Key": "acme-0001"})
    assert response.status_code == 201 and order["orderId"] == "4711"
    assert len(sf.orders) == 1 and len(topic.events) == 1


def test_place_order_follows_the_contract(sf, topic):
    response = api.place_order(request("POST", "/orders", {**PLACE, "channel": "fax"}), sf, topic)
    assert response.status_code == 400 and "channel" in json.loads(response.get_body())["detail"]
    assert not topic.events


def test_correlation_id_is_required(sf, topic):
    response = api.retrieve_order(request("GET", "/orders/4711", order_id="4711", headers={"Correlation-Id": ""}), sf)
    assert response.status_code == 400


def test_cancel_is_a_soft_delete(sf, topic):
    placed(sf, topic)
    response, order = do(api.cancel, sf, topic, "DELETE", params={"reason": "Customer withdrew the order."})
    assert response.status_code == 200
    assert order["status"] == "cancelled" and order["cancellationReason"] == "Customer withdrew the order."
    assert api.retrieve_order(request("GET", "/orders/4711", order_id="4711"), sf).status_code == 200  # still there
    assert topic.events[-1][0] == "OrderCancelled"


def test_cancel_needs_a_reason(sf, topic):
    placed(sf, topic)
    assert do(api.cancel, sf, topic, "DELETE")[0].status_code == 400


def test_an_invalid_transition_is_409(sf, topic):
    placed(sf, topic)
    do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."})
    response, problem = do(api.hold, sf, topic, body={"reason": "Credit check."})
    assert response.status_code == 409 and problem["title"] == "Order State Conflict"
    assert "cancelled" in problem["detail"]
    assert [e[0] for e in topic.events] == ["OrderPlaced", "OrderCancelled"]  # nothing for the refused command


def test_hold_and_release(sf, topic):
    placed(sf, topic)
    sf.orders["4711"]["status"] = "confirmed"  # the ERP confirmed it (a policy, later)
    _, held = do(api.hold, sf, topic, body={"reason": "Credit limit exceeded, awaiting approval."})
    assert held["status"] == "onHold" and held["holdReason"]
    _, released = do(api.release, sf, topic, "DELETE")
    assert released["status"] == "confirmed" and "holdReason" not in released
    assert [e[0] for e in topic.events][-2:] == ["OrderPutOnHold", "OrderReleased"]


def test_amend_reschedule_and_change_address(sf, topic):
    placed(sf, topic)
    lines = [{"productId": "SKU-1042", "quantity": 10, "unitPrice": {"value": 7000.0, "currency": "EUR"}}]
    _, amended = do(api.amend, sf, topic, body={"orderLines": lines})
    assert amended["totalAmount"]["value"] == 70000.0
    assert topic.events[-1][2]["totalAmount"]["value"] == 70000.0
    _, rescheduled = do(api.reschedule, sf, topic, body={"requestedDeliveryDate": "2026-10-14"})
    assert topic.events[-1][2]["previousRequestedDeliveryDate"] == "2026-10-09"
    address = {"street": "Avenue Louise 54", "postalCode": "1050", "city": "Brussels", "countryCode": "BE"}
    _, moved = do(api.change_address, sf, topic, body=address)
    assert moved["deliveryAddress"] == address and rescheduled["requestedDeliveryDate"] == "2026-10-14"
    assert [e[0] for e in topic.events][1:] == ["OrderAmended", "OrderDeliveryRescheduled", "OrderDeliveryAddressChanged"]


def test_a_concurrent_change_is_retried(sf, topic):
    placed(sf, topic)
    sf.conflicts = 1
    response, _ = do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."})
    assert response.status_code == 200 and len(topic.events) == 2


def test_unknown_order_is_404(sf, topic):
    assert do(api.cancel, sf, topic, "DELETE", params={"reason": "x"}, order_id="9999")[0].status_code == 404


def test_list_filters_by_status(sf, topic):
    placed(sf, topic)
    placed(sf, topic)
    do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."})
    response = api.retrieve_list_of_orders(request("GET", "/orders", params={"status": "cancelled"}), sf)
    page = json.loads(response.get_body())
    assert page["totalCount"] == 1 and page["_embedded"]["orders"][0]["orderId"] == "4711"
    assert api.retrieve_list_of_orders(request("GET", "/orders", params={"status": "lost"}), sf).status_code == 400


@pytest.mark.parametrize("fact, expected", [
    ("OrderPlaced", "com.acme.order.placed"),
    ("OrderDeliveryAddressChanged", "com.acme.order.deliveryAddressChanged"),
    ("OrderPutOnHold", "com.acme.order.putOnHold"),
])
def test_event_types_follow_asyncapi(fact, expected):
    assert event_type(fact) == expected


def test_every_command_fact_is_in_asyncapi():
    import yaml
    spec = yaml.safe_load(open("asyncapi.yaml"))
    types = {c["x-configurations"]["demo"]["eventGrid"]["eventType"] for c in spec["channels"].values()}
    facts = ["OrderPlaced", "OrderCancelled", "OrderAmended", "OrderDeliveryAddressChanged",
             "OrderDeliveryRescheduled", "OrderPutOnHold", "OrderReleased"]
    assert {event_type(f) for f in facts} <= types


def test_events_are_valid_envelopes(monkeypatch):
    from platform_clients import OrderTopic
    sent = []
    monkeypatch.setattr("platform_clients._call", lambda *args: (sent.append(args[4]) or (200, {}, {})))
    order = {"orderId": "4711", "customerId": "ACME"}
    OrderTopic("https://example").publish("OrderCancelled", order, {"orderId": "4711", "reason": "x",
                                          "cancelledAt": orders.now()}, "ord-4711")
    validate(sent[0])
    assert sent[0]["type"] == "com.acme.order.cancelled" and sent[0]["orderid"] == "4711"


def test_every_operation_is_a_function():
    names = {f.get_function_name() for f in api.app.get_functions()}
    import yaml
    spec = yaml.safe_load(open("openapi.yaml"))
    operations = {op["operationId"] for path in spec["paths"].values() for op in path.values() if isinstance(op, dict) and "operationId" in op}
    assert operations == names - {"receiveEvent"}  # the event intake: the platform's contract


# --- Policies -----------------------------------------------------------------------------------


def event(type_, data, order_id="4711"):
    return {"specversion": "1.0", "id": f"{type_}-1", "source": "/test", "type": type_, "time": "2026-10-16T10:00:00.000Z",
            "datacontenttype": "application/json", "correlationid": "ord-4711", "orderid": order_id, "customerid": "ACME", "data": data}


def deliver(sf, topic, type_, data, order_id="4711"):
    response = api.receive_event(request("POST", "/intake", event(type_, data, order_id)), sf, topic)
    return response.status_code, json.loads(response.get_body())


ACCEPTED = ("com.acme.finance.salesOrderAccepted", {"orderId": "4711", "erpOrderNumber": "8000043217",
            "netValue": {"value": 84000.0, "currency": "EUR"}, "acceptedAt": "2026-10-16T10:00:00Z"})
INVOICED = ("com.acme.finance.invoiceIssued", {"orderId": "4711", "invoiceNumber": "90004711", "erpOrderNumber": "8000043217",
            "amount": {"value": 84000.0, "currency": "EUR"}, "dueDate": "2026-11-15", "issuedAt": "2026-10-16T10:00:00Z"})
RESERVED = ("com.acme.supply.stockReserved", {"orderId": "4711", "reservationId": "RSV-4711", "warehouseId": "BRU-1",
            "orderLines": [{"productId": "SKU-1042", "quantity": 12}], "reservedAt": "2026-10-16T10:01:00Z"})
DISPATCHED = ("com.acme.logistics.shipmentDispatched", {"orderId": "4711", "shipmentId": "SPD-004711", "partnerId": "speedy-3pl",
              "trackingNumber": "SPD4711BE", "dispatchedAt": "2026-10-16T10:02:00Z"})
DELIVERED = ("com.acme.logistics.shipmentDelivered", {"orderId": "4711", "shipmentId": "SPD-004711", "partnerId": "speedy-3pl",
             "deliveredAt": "2026-10-17T10:00:00Z"})
PAID = ("com.acme.finance.paymentReceived", {"orderId": "4711", "invoiceNumber": "90004711", "paymentId": "1400000001",
        "amount": {"value": 84000.0, "currency": "EUR"}, "invoicePaidInFull": True, "receivedAt": "2026-10-18T10:00:00Z"})


def test_order_4711_from_placed_to_completed(sf, topic):
    placed(sf, topic)
    for fact in (ACCEPTED, INVOICED, RESERVED, DISPATCHED, DELIVERED, PAID):
        status, body = deliver(sf, topic, *fact)
        assert status == 200 and "applied" in body, (fact[0], body)
    assert [e[0] for e in topic.events] == ["OrderPlaced", "OrderConfirmed", "OrderInvoiced", "OrderReadyForShipment",
                                           "OrderShipped", "OrderDelivered", "OrderPaid", "OrderCompleted"]
    assert sf.orders["4711"]["status"] == "completed" and sf.orders["4711"]["paymentStatus"] == "paid"


def test_confirmed_and_ready_carry_what_downstream_needs(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, *ACCEPTED)
    deliver(sf, topic, *RESERVED)
    confirmed, ready = topic.events[1][2], topic.events[2][2]
    for data in (confirmed, ready):
        assert data["orderLines"][0]["productId"] == "SKU-1042" and data["deliveryAddress"]["city"] == "Brussels"
    assert confirmed["erpOrderNumber"] == "8000043217" and ready["warehouseId"] == "BRU-1"


def test_paid_before_delivered_completes_on_delivery(sf, topic):
    placed(sf, topic)
    for fact in (ACCEPTED, INVOICED, PAID, RESERVED, DISPATCHED, DELIVERED):
        deliver(sf, topic, *fact)
    assert [e[0] for e in topic.events][-2:] == ["OrderDelivered", "OrderCompleted"]


def test_a_redelivered_fact_changes_nothing(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, *ACCEPTED)
    status, body = deliver(sf, topic, *ACCEPTED)
    assert status == 200 and "ignored" in body and len(topic.events) == 2


def test_refused_by_sap(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, "com.acme.finance.salesOrderRefused", {"orderId": "4711", "erpOrderNumber": "8000043218",
            "reason": "Credit limit exceeded.", "refusedAt": "2026-10-16T10:00:00Z"})
    assert sf.orders["4711"]["status"] == "rejected" and topic.events[-1][0] == "OrderRejected"


def test_a_fact_that_does_not_fit_is_ignored_not_retried(sf, topic):
    placed(sf, topic)
    do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."})
    status, body = deliver(sf, topic, *DISPATCHED)
    assert status == 200 and "cancelled" in body["ignored"]


BOOKED = ("com.acme.logistics.shipmentBooked", {"orderId": "4711", "shipmentId": "SPD-004711", "partnerId": "speedy-3pl",
          "trackingNumber": "SPD4711BE", "bookedAt": "2026-10-16T10:01:30Z"})


def test_the_timeline_and_references(sf, topic):
    placed(sf, topic)
    for fact in (ACCEPTED, INVOICED, RESERVED, BOOKED):
        deliver(sf, topic, *fact)
    order = sf.orders["4711"]
    assert [(m["fact"], m["domain"]) for m in order["timeline"]] == [
        ("OrderPlaced", "order"), ("SalesOrderAccepted", "finance"), ("OrderConfirmed", "order"), ("InvoiceIssued", "finance"),
        ("OrderInvoiced", "order"), ("StockReserved", "supply"), ("OrderReadyForShipment", "order"), ("ShipmentBooked", "logistics")]
    assert order["references"] == {"erpOrderNumber": "8000043217", "invoiceNumber": "90004711", "reservationId": "RSV-4711",
                                   "warehouseId": "BRU-1", "shipmentId": "SPD-004711", "trackingNumber": "SPD4711BE", "partnerId": "speedy-3pl"}
    assert order["status"] == "readyForShipment"  # booked is not shipped: the partner reports the pick-up
    assert "ignored" in deliver(sf, topic, *BOOKED)[1]  # a redelivered fact changes nothing


def test_cancelling_while_ready_tells_every_domain_what_to_undo(sf, topic):
    placed(sf, topic)
    for fact in (ACCEPTED, INVOICED, RESERVED, BOOKED):
        deliver(sf, topic, *fact)
    response, cancelled = do(api.cancel, sf, topic, "DELETE", params={"reason": "Customer changed their mind."})
    assert response.status_code == 200 and cancelled["status"] == "cancelled"
    data = topic.events[-1][2]
    assert topic.events[-1][0] == "OrderCancelled"
    assert (data["erpOrderNumber"], data["reservationId"], data["shipmentId"]) == ("8000043217", "RSV-4711", "SPD-004711")
    for fact in (("com.acme.finance.salesOrderCancelled", {"orderId": "4711", "erpOrderNumber": "8000043217", "reason": "x",
                  "invoiceNumber": "90004711", "creditNoteNumber": "70004711", "cancelledAt": "2026-10-16T10:05:00Z"}),
                 ("com.acme.supply.stockReleased", {"orderId": "4711", "reservationId": "RSV-4711", "warehouseId": "BRU-1",
                  "orderLines": [], "releasedAt": "2026-10-16T10:05:00Z"}),
                 ("com.acme.logistics.shipmentCancelled", {"orderId": "4711", "shipmentId": "SPD-004711", "partnerId": "speedy-3pl",
                  "cancelledAt": "2026-10-16T10:05:00Z"})):
        status, body = deliver(sf, topic, *fact)
        assert status == 200 and body == {"applied": []}  # recorded, no new order fact
    order = sf.orders["4711"]
    assert order["references"]["creditNoteNumber"] == "70004711"
    assert [m["fact"] for m in order["timeline"]][-4:] == ["OrderCancelled", "SalesOrderCancelled", "StockReleased", "ShipmentCancelled"]


def test_a_late_fact_on_a_cancelled_order_is_recorded(sf, topic):
    """Cancelled while the WMS was reserving: the reservation is remembered (ADR 0018: the known race)."""
    placed(sf, topic)
    deliver(sf, topic, *ACCEPTED)
    do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."})
    status, body = deliver(sf, topic, *RESERVED)
    assert status == 200 and body == {"applied": []} and sf.orders["4711"]["references"]["reservationId"] == "RSV-4711"
    assert sf.orders["4711"]["status"] == "cancelled"


def test_released_with_stock_reserved_is_ready_for_shipment(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, *ACCEPTED)
    do(api.hold, sf, topic, body={"reason": "Customer asked to wait."})
    deliver(sf, topic, *RESERVED)  # the WMS reserved meanwhile: recorded, the order stays on hold
    assert sf.orders["4711"]["status"] == "onHold"
    _, released = do(api.release, sf, topic, "DELETE")
    assert released["status"] == "readyForShipment"
    assert [e[0] for e in topic.events][-2:] == ["OrderReleased", "OrderReadyForShipment"]
    assert topic.events[-1][2]["reservationId"] == "RSV-4711" and topic.events[-1][2]["deliveryAddress"]["city"] == "Brussels"


def test_lines_are_amended_only_while_placed(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, *ACCEPTED)
    lines = [{"productId": "SKU-1042", "quantity": 10, "unitPrice": {"value": 7000.0, "currency": "EUR"}}]
    response, problem = do(api.amend, sf, topic, body={"orderLines": lines})
    assert response.status_code == 409 and "placed" in problem["detail"]


def test_part_payment_is_not_paid(sf, topic):
    placed(sf, topic)
    deliver(sf, topic, *INVOICED)
    status, body = deliver(sf, topic, PAID[0], {**PAID[1], "invoicePaidInFull": False})
    assert "ignored" in body and sf.orders["4711"]["paymentStatus"] == "invoiced"


def test_unknown_events_and_orders_are_ignored(sf, topic):
    assert "ignored" in deliver(sf, topic, "com.acme.sales.opportunityWon", {})[1]
    assert "ignored" in deliver(sf, topic, *ACCEPTED, order_id="9999")[1]


def test_not_an_event_is_400(sf, topic):
    assert api.receive_event(request("POST", "/intake", {"hello": "world"}), sf, topic).status_code == 400


def test_order_events_match_asyncapi(sf, topic):
    """Every fact the order domain publishes, policies included, matches its own asyncapi.yaml."""
    import yaml
    from jsonschema import Draft7Validator
    spec = yaml.safe_load(open("asyncapi.yaml"))
    by_type = {c["x-configurations"]["demo"]["eventGrid"]["eventType"]: c["publish"]["message"]["$ref"].rsplit("/", 1)[-1]
               for c in spec["channels"].values() if "publish" in c}
    placed(sf, topic)
    for fact in (ACCEPTED, INVOICED, RESERVED, DISPATCHED, DELIVERED, PAID):
        deliver(sf, topic, *fact)
    do(api.cancel, sf, topic, "DELETE", params={"reason": "Withdrawn."}, order_id=placed(sf, topic)[1]["orderId"])
    for fact, _, data, _ in topic.events:
        schema = spec["components"]["schemas"][by_type[event_type(fact)] + "Data"]
        errors = list(Draft7Validator({**schema, "components": spec["components"]}).iter_errors(data))
        assert not errors, (fact, [e.message for e in errors])
