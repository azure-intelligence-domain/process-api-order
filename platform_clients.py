"""The hands the brain needs from the platform: the Salesforce System API (through the gateway, with
an Entra token of this API's runtime identity) and the order topic (Event Grid, same identity).
No keys anywhere (ADR 0014)."""

import json
import os
import re
import urllib.error
import urllib.request
from functools import cache

from azure.identity import DefaultAzureCredential
from integration_envelope import BusinessKeys, new_event

VERSION = "1.2.0"
NEW_ORDER_FIELDS = ("customerId", "channel", "status", "paymentStatus", "orderLines", "deliveryAddress",
                    "requestedDeliveryDate", "holdReason", "cancellationReason", "idempotencyKey", "references", "timeline")


class OrderChanged(Exception):
    """Salesforce: the order changed since it was read (412)."""


@cache
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))


def _call(method: str, url: str, scope: str, headers: dict, body=None) -> tuple[int, dict, dict]:
    token = credential().get_token(scope).token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), json.loads(error.read() or b"{}")


class SalesforceApi:
    """[Sales] Salesforce System API, through the gateway's system-apis product."""

    def __init__(self, base_url: str, audience: str, correlation_id: str, traceparent: str | None = None):
        self.base_url, self.scope = base_url.rstrip("/"), f"{audience}/.default"
        self.headers = {"Correlation-Id": correlation_id, **({"traceparent": traceparent} if traceparent else {})}

    @classmethod
    def from_environment(cls, correlation_id: str, traceparent: str | None = None) -> "SalesforceApi":
        return cls(os.environ["SALESFORCE_API_URL"], os.environ["PLATFORM_APIS_AUDIENCE"], correlation_id, traceparent)

    def _expect(self, status: int, body: dict, *ok: int) -> None:
        if status not in ok:
            raise RuntimeError(f"Salesforce System API answered {status}: {body.get('title')} {body.get('detail')}")

    def create(self, order: dict) -> dict:
        status, _, body = _call("POST", f"{self.base_url}/orders", self.scope, self.headers, as_new_order(order))
        self._expect(status, body, 201)
        return body

    def get(self, order_id: str) -> tuple[dict, str] | None:
        status, headers, body = _call("GET", f"{self.base_url}/orders/{order_id}", self.scope, self.headers)
        if status == 404:
            return None
        self._expect(status, body, 200)
        return body, headers.get("ETag") or headers.get("Etag", "")

    def replace(self, order_id: str, order: dict, etag: str) -> dict:
        status, _, body = _call("PUT", f"{self.base_url}/orders/{order_id}", self.scope,
                                {**self.headers, "If-Match": etag}, as_new_order(order))
        if status == 412:
            raise OrderChanged
        self._expect(status, body, 200)
        return body

    def list(self, **query) -> dict:
        filters = "&".join(f"{name}={urllib.request.quote(str(value))}" for name, value in query.items() if value is not None)
        status, _, body = _call("GET", f"{self.base_url}/orders?{filters}", self.scope, self.headers)
        self._expect(status, body, 200)
        return body


def as_new_order(order: dict) -> dict:
    """What the System API stores: the order without what Salesforce computes or links."""
    return {key: order[key] for key in NEW_ORDER_FIELDS if order.get(key) is not None}


def event_type(fact: str) -> str:
    """OrderDeliveryAddressChanged → com.acme.order.deliveryAddressChanged (asyncapi.yaml)."""
    rest = re.sub(r"^Order", "", fact)
    return f"com.acme.order.{rest[0].lower()}{rest[1:]}"


class OrderTopic:
    """evgt-intplat-order: every order fact, in the platform's envelope."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    @classmethod
    def from_environment(cls) -> "OrderTopic":
        return cls(os.environ["ORDER_TOPIC_ENDPOINT"])

    def publish(self, fact: str, order: dict, data: dict, correlation_id: str, traceparent: str | None = None) -> dict:
        event = new_event(
            type=event_type(fact),
            source=f"/process-api-order/{VERSION}",
            correlation_id=correlation_id,
            keys=BusinessKeys(order_id=order["orderId"], customer_id=order["customerId"]),
            data=data,
            traceparent=traceparent,
        )
        status, _, body = _call("POST", self.endpoint, "https://eventgrid.azure.net/.default",
                                {"Content-Type": "application/cloudevents+json; charset=utf-8"}, event)
        if status != 200:
            raise RuntimeError(f"Event Grid refused {fact} for order {order['orderId']}: {status} {body}")
        return event
