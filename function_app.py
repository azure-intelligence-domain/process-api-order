"""[Order] Order Process API (openapi.yaml): the brain of the order domain. Commands and queries
come in through the gateways; facts of other domains come in through the platform's event intake
(POST /intake, ADR 0016); the order lives in Salesforce (through its System API); every change
becomes a business fact on evgt-intplat-order (asyncapi.yaml). The rules are in orders.py.
"""

import json
import logging
from functools import wraps

import azure.functions as func
from integration_envelope import EnvelopeError, read

import contract
import orders
from platform_clients import OrderChanged, OrderTopic, SalesforceApi

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Easy Auth: the gateways only

PROBLEMS = "https://github.com/azure-intelligence-domain/process-api-order/blob/main/docs/problems/"
RETRIES = 3  # when the order changed in Salesforce between read and save


# --- Responses ----------------------------------------------------------------------------------


def hal(status: int, body: dict, headers: dict | None = None) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/hal+json", headers=headers)


def problem(status: int, title: str, detail: str, slug: str, instance: str) -> func.HttpResponse:
    body = {"type": PROBLEMS + slug + ".md", "title": title, "status": status, "detail": detail, "instance": instance}
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/problem+json")


def invalid(detail: str, req: func.HttpRequest) -> func.HttpResponse:
    return problem(400, "Invalid Request", detail, "invalid-request", req.url)


def not_found(order_id: str, req: func.HttpRequest) -> func.HttpResponse:
    return problem(404, "Order Not Found", f"No order {order_id} exists.", "order-not-found", req.url)


def resource(order: dict) -> dict:
    """The order as this API shows it: HAL links, without Salesforce's bookkeeping."""
    href = f"/orders/{order['orderId']}"
    links = {"self": {"href": href}, "orderLines": {"href": f"{href}/order-lines"},
             "deliveryAddress": {"href": f"{href}/delivery-address"}, "hold": {"href": f"{href}/hold"}}
    hidden = {"_links", "recordId", "idempotencyKey"}
    return {"_links": links, **{key: value for key, value in order.items() if key not in hidden}}


# --- Requests -----------------------------------------------------------------------------------


class Refused(Exception):
    def __init__(self, response: func.HttpResponse):
        self.response = response


def journey(req: func.HttpRequest) -> tuple[str, str | None]:
    """The Correlation-Id (required) and the W3C trace context to pass on: one trace per journey."""
    correlation_id = req.headers.get("Correlation-Id", "")
    if contract.violations(contract.PARAMETERS["correlationId"], correlation_id):
        raise Refused(invalid("The Correlation-Id header is missing or invalid, for example ord-4711.", req))
    logging.info("Correlation-Id %s: %s %s", correlation_id, req.method, req.url)
    return correlation_id, req.headers.get("traceparent")


def body_of(req: func.HttpRequest, schema: str) -> dict:
    try:
        body = req.get_json()
    except ValueError:
        raise Refused(invalid("The body is not JSON.", req)) from None
    errors = contract.violations(contract.SCHEMAS[schema], body)
    lines = body.get("orderLines") if isinstance(body, dict) else None
    if not errors and lines and len({line["unitPrice"]["currency"] for line in lines}) > 1:
        errors = ["orderLines: all lines must have the same currency"]
    if errors:
        raise Refused(invalid("; ".join(errors), req))
    return body


def refusals(handler):
    """A refused request becomes its Problem Details response."""
    @wraps(handler)
    def handle(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Refused as refused:
            return refused.response
    return handle


# --- Queries ------------------------------------------------------------------------------------


@refusals
def retrieve_order(req: func.HttpRequest, sf: SalesforceApi) -> func.HttpResponse:
    journey(req)
    found = sf.get(req.route_params["orderId"])
    return hal(200, resource(found[0])) if found else not_found(req.route_params["orderId"], req)


@refusals
def retrieve_list_of_orders(req: func.HttpRequest, sf: SalesforceApi) -> func.HttpResponse:
    journey(req)
    query = {"customerId": req.params.get("customerId"), "status": req.params.get("status")}
    try:
        offset, limit = int(req.params.get("offset", 0)), int(req.params.get("limit", 20))
    except ValueError:
        raise Refused(invalid("offset and limit are integers.", req)) from None
    errors = (contract.violations(contract.PARAMETERS["offset"], offset)
              + contract.violations(contract.PARAMETERS["limit"], limit)
              + (contract.violations(contract.PARAMETERS["status"], query["status"]) if query["status"] else []))
    if errors:
        raise Refused(invalid("; ".join(errors), req))

    found = sf.list(**query, offset=offset, limit=limit)
    total = found["totalCount"]
    filters = "".join(f"{name}={value}&" for name, value in query.items() if value)
    page = lambda start: {"href": f"/orders?{filters}offset={start}&limit={limit}"}  # noqa: E731
    links = {"self": page(offset), "first": page(0), "last": page(max(0, (total - 1) // limit * limit))}
    if offset > 0:
        links["prev"] = page(max(0, offset - limit))
    if offset + limit < total:
        links["next"] = page(offset + limit)
    embedded = [resource(order) for order in found["_embedded"]["orders"]]
    return hal(200, {"_links": links, "totalCount": total, "_embedded": {"orders": embedded}})


# --- Commands -----------------------------------------------------------------------------------


@refusals
def place_order(req: func.HttpRequest, sf: SalesforceApi, topic: OrderTopic) -> func.HttpResponse:
    correlation_id, traceparent = journey(req)
    request = body_of(req, "placeOrderRequest")
    key = req.headers.get("Idempotency-Key")
    if key and contract.violations(contract.PARAMETERS["idempotencyKey"], key):
        raise Refused(invalid("The Idempotency-Key header is at most 64 characters.", req))

    if key:  # a retry returns the order placed by the first request, and publishes nothing
        earlier = sf.list(idempotencyKey=key, limit=1)["_embedded"]["orders"]
        if earlier:
            return hal(201, resource(earlier[0]), {"Location": f"/orders/{earlier[0]['orderId']}"})

    placed = sf.create(orders.place_order(request, key))
    decision = orders.order_placed(placed)
    topic.publish(decision.event, placed, orders.event_data(decision, placed), correlation_id, traceparent)
    return hal(201, resource(placed), {"Location": f"/orders/{placed['orderId']}"})


@refusals
def command(req: func.HttpRequest, sf: SalesforceApi, topic: OrderTopic, intent) -> func.HttpResponse:
    """Every command on an existing order: read it, decide, save it, publish the fact.
    `intent(req)` reads the request and returns the decision to take on the order."""
    correlation_id, traceparent = journey(req)
    decide = intent(req)
    order_id = req.route_params["orderId"]
    for _ in range(RETRIES):
        found = sf.get(order_id)
        if found is None:
            return not_found(order_id, req)
        order, etag = found
        try:
            decision = decide(order)
        except orders.StateConflict as conflict:
            return problem(409, "Order State Conflict", str(conflict), "order-state-conflict", req.url)
        changed = orders.apply(order, decision)
        then = orders.follow_up(changed)  # e.g. released, with stock reserved: ready for shipment
        try:
            saved = sf.replace(order_id, orders.apply(changed, then) if then else changed, etag)
        except OrderChanged:
            continue  # someone else changed it meanwhile: decide again on the fresh order
        for fact in [decision] + ([then] if then else []):
            topic.publish(fact.event, saved, orders.event_data(fact, saved), correlation_id, traceparent)
        return hal(200, resource(saved))
    raise RuntimeError(f"Order {order_id} kept changing; gave up after {RETRIES} attempts.")


# --- Policies: facts of other domains, delivered by the platform's event intake -----------------


def receive_event(req: func.HttpRequest, sf: SalesforceApi, topic: OrderTopic) -> func.HttpResponse:
    """One CloudEvent from another domain → on the order's timeline, and at most two order facts (e.g.
    OrderDelivered, OrderCompleted). 200 when handled or deliberately ignored; an error makes Event
    Grid retry, then dead-letter."""
    try:
        event = read(req.get_json())
    except (ValueError, EnvelopeError) as error:
        return invalid(f"Not a platform event: {error}", req)
    policy, order_id = orders.POLICIES.get(event["type"]), event.get("orderid")
    if policy is None or order_id is None:
        return ignored(event, "no policy for this event")
    correlation_id, traceparent = event["correlationid"], event.get("traceparent")
    for _ in range(RETRIES):
        found = sf.get(order_id)
        if found is None:
            return ignored(event, f"no order {order_id}")
        order, etag = found
        try:
            decision = policy(order, event["data"])
        except orders.StateConflict as conflict:
            return ignored(event, str(conflict))
        if decision is None:
            return ignored(event, "the order already reflects it")
        changed = orders.apply(order, decision, *orders.fact_of(event["type"]))
        then = orders.follow_up(changed)
        try:
            saved = sf.replace(order_id, orders.apply(changed, then) if then else changed, etag)
        except OrderChanged:
            continue
        facts = [d for d in [decision] + ([then] if then else []) if d.event]
        for fact in facts:
            topic.publish(fact.event, saved, orders.event_data(fact, saved), correlation_id, traceparent)
        logging.info("%s for order %s: %s", event["type"], order_id, [f.event for f in facts] or "recorded")
        return func.HttpResponse(json.dumps({"applied": [f.event for f in facts]}), status_code=200, mimetype="application/json")
    raise RuntimeError(f"Order {order_id} kept changing; gave up after {RETRIES} attempts.")


def ignored(event: dict, why: str) -> func.HttpResponse:
    logging.warning("Ignored %s %s: %s", event.get("type"), event.get("id"), why)
    return func.HttpResponse(json.dumps({"ignored": why}), status_code=200, mimetype="application/json")


# What each command wants, read from the request: intent(req) -> decide(order) -> Decision.


def cancel(req: func.HttpRequest):
    reason = req.params.get("reason", "")
    if not reason or contract.violations(contract.PARAMETERS["cancellationReason"], reason):
        raise Refused(invalid("Query parameter reason is required, at most 200 characters.", req))
    return lambda order: orders.cancel_order(order, reason)


def amend(req: func.HttpRequest):
    lines = body_of(req, "orderLinesChange")["orderLines"]
    return lambda order: orders.amend_order_lines(order, lines)


def change_address(req: func.HttpRequest):
    address = body_of(req, "deliveryAddress")
    return lambda order: orders.change_delivery_address(order, address)


def reschedule(req: func.HttpRequest):
    date = body_of(req, "requestedDeliveryDateChange")["requestedDeliveryDate"]
    return lambda order: orders.reschedule_delivery(order, date)


def hold(req: func.HttpRequest):
    reason = body_of(req, "holdRequest")["reason"]
    return lambda order: orders.put_order_on_hold(order, reason)


def release(req: func.HttpRequest):
    return orders.release_order


# --- Routes (host.json: no route prefix, so the paths are the contract's) -----------------------


def _salesforce(req: func.HttpRequest) -> SalesforceApi:
    return SalesforceApi.from_environment(req.headers.get("Correlation-Id", ""), req.headers.get("traceparent"))


@app.route(route="orders", methods=["GET"])
def retrieveListOfOrders(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_list_of_orders(req, _salesforce(req))


@app.route(route="orders", methods=["POST"])
def placeOrder(req: func.HttpRequest) -> func.HttpResponse:
    return place_order(req, _salesforce(req), OrderTopic.from_environment())


@app.route(route="intake", methods=["POST"])
def receiveEvent(req: func.HttpRequest) -> func.HttpResponse:
    """The platform's event intake (integration-platform intake.openapi.yaml), not part of openapi.yaml."""
    correlation_id = (req.get_json() or {}).get("correlationid", "") if req.get_body() else ""
    sf = SalesforceApi.from_environment(correlation_id, req.headers.get("traceparent"))
    return receive_event(req, sf, OrderTopic.from_environment())


@app.route(route="orders/{orderId}", methods=["GET"])
def retrieveOrder(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_order(req, _salesforce(req))


COMMANDS = {
    ("orders/{orderId}", "DELETE", "cancelOrder"): cancel,
    ("orders/{orderId}/order-lines", "PUT", "amendOrderLines"): amend,
    ("orders/{orderId}/delivery-address", "PUT", "changeDeliveryAddress"): change_address,
    ("orders/{orderId}/requested-delivery-date", "PUT", "rescheduleDelivery"): reschedule,
    ("orders/{orderId}/hold", "PUT", "putOrderOnHold"): hold,
    ("orders/{orderId}/hold", "DELETE", "releaseOrder"): release,
}


def _route(intent):
    def handle(req: func.HttpRequest) -> func.HttpResponse:
        return command(req, _salesforce(req), OrderTopic.from_environment(), intent)
    return handle


for (route, method, operation_id), intent in COMMANDS.items():
    app.function_name(operation_id)(app.route(route=route, methods=[method])(_route(intent)))
