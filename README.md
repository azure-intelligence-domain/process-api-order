# process-api-order

The **Order Process API** of the order domain: the lifecycle of an order across systems. Owned by
the **order domain team**. Decision:
[ADR 0013](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/adr/0013-order-domain.md).

| File | What |
|---|---|
| [`openapi.yaml`](openapi.yaml) | Commands and queries: `[Order] Order Process API` |
| [`asyncapi.yaml`](asyncapi.yaml) | The 15 business events of the order domain, on `evgt-intplat-order`, and the 11 facts of other domains it reacts to |
| [`orders.py`](orders.py) | **The brain**: which command is allowed in which status, and which fact it becomes. No I/O |
| [`function_app.py`](function_app.py) | The routes: read the order, decide, save it in Salesforce, publish the fact |
| [`platform_clients.py`](platform_clients.py) | Salesforce System API (through the gateway) and the order topic, with the runtime identity |
| [`infra/`](infra/) | Function (Flex Consumption), storage, Easy Auth (both gateways only) |
| [`docs/bpmn/`](docs/bpmn/) | The flows as BPMN 2.0: order to delivery, commands, queries |
| [`docs/problems/`](docs/problems/) | Problem types: invalid request, order not found, order state conflict |

Both contracts follow the adidas API guidelines and pass the platform's contract check.

## How it works: the brain and the hands

The **Order Process API** (Azure Function, Python) is the brain: it owns the order, its status
and its facts. **Logic Apps** are the hands: integration flows that move data between systems
(mapping, calls). They never call each other: they react to events, and they reach systems only
through the gateway ([ADR 0014](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/adr/0014-process-layer-split.md)).
There is one per **receiving** domain, owned by that domain's team and speaking only to its own
System APIs ([ADR 0015](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/adr/0015-one-intake-per-domain.md)).

```mermaid
flowchart LR
    subgraph callers["Callers"]
        people["People / websites"]
        agents["AI agents"]
    end
    subgraph gw["API Management"]
        apimH{{"apim-intplat-demo"}}
        apimA{{"apim-intplat-agents"}}
    end
    subgraph order["Order domain · the brain"]
        papi["Order Process API<br/>Function (Python)<br/>commands · queries · state machine<br/>idempotency · 409 · envelope"]
        qOrder[("sbq-process-api-order<br/>facts from other domains")]
    end
    subgraph topics["Event Grid"]
        tOrder[["evgt-intplat-order<br/>OrderPlaced · OrderConfirmed · ..."]]
        tFin[["evgt-intplat-finance<br/>SalesOrderAccepted · InvoiceIssued · PaymentReceived"]]
        tSup[["evgt-intplat-supply<br/>StockReserved"]]
        tLog[["evgt-intplat-logistics<br/>ShipmentDispatched · ShipmentDelivered"]]
    end
    subgraph flows["Integration flows · the hands · one Logic App per receiving domain"]
        la1["finance-intake<br/>on OrderPlaced, OrderAmended,<br/>OrderCancelled"]
        la2["supply-intake<br/>on OrderConfirmed, OrderCancelled"]
        la3["logistics-intake<br/>on StockReserved"]
    end
    subgraph sys["System APIs · Functions"]
        sSf["Salesforce System API"]
        sSap["SAP System API"]
        sWms["WMS System API"]
        s3pl["3PL System API"]
    end
    subgraph src["Source systems"]
        dbSf[("Salesforce<br/>system of record: orders")]
        dbSap[("SAP")]
        dbWms[("WMS")]
        p3pl["3PL partner API"]
    end

    people --> apimH --> papi
    agents --> apimA --> papi
    papi -- "via gateway" --> sSf --> dbSf
    papi -- "publish" --> tOrder
    tOrder --> la1 & la2
    la1 -- "via gateway" --> sSap --> dbSap
    la2 -- "via gateway" --> sWms --> dbWms
    sSap -- "publish" --> tFin
    sWms -- "publish" --> tSup
    tSup --> la3
    la3 -- "via gateway" --> s3pl --> p3pl
    s3pl -- "publish" --> tLog
    tFin & tSup & tLog --> qOrder --> papi
```

| | Order Process API (Function) | Logic Apps |
|---|---|---|
| Role | Decides: owns the order, its status and its facts | Moves data between systems |
| Triggered by | Commands via the gateways; facts from other domains via its own queue | Order events via their own queue |
| Publishes | The 15 order events | Nothing of its own: the System API behind it publishes the fact |
| Fails how | Command refused (409 / 400) | Message in the flow's own dead-letter queue |

### ACME's order 4711 through it

```mermaid
sequenceDiagram
    autonumber
    participant SR as Sales rep
    participant P as Order Process API (Function)
    participant EO as evgt-intplat-order
    participant L1 as finance-intake (Logic App)
    participant SAP as SAP System API
    participant EF as evgt-intplat-finance
    participant L2 as supply-intake (Logic App)
    participant WMS as WMS System API
    participant ES as evgt-intplat-supply
    participant L3 as logistics-intake (Logic App)
    participant TPL as 3PL System API

    SR->>P: Place Order (via gateway)
    P->>P: validate, idempotency, status = placed
    P->>EO: OrderPlaced
    EO->>L1: OrderPlaced (finance queue)
    L1->>SAP: Create sales order (mapping)
    SAP->>EF: SalesOrderAccepted
    EF->>P: policy: ERP accepted
    P->>EO: OrderConfirmed
    EO->>L2: OrderConfirmed (supply queue)
    L2->>WMS: Reserve stock (mapping)
    WMS->>ES: StockReserved
    ES->>P: policy: stock reserved
    P->>EO: OrderReadyForShipment
    ES->>L3: StockReserved (logistics queue)
    L3->>TPL: Shipment request (mapping v2.4)
    TPL--xL3: 400 schema mismatch
    L3->>L3: retries exhausted → dead-letter queue
    Note over P,TPL: The order stays readyForShipment: no OrderShipped ever comes. The tap saw every hop.
```

## Implementation (1.0.0)

A Python Function on Flex Consumption, running as `id-intplat-process-api-order-runtime`. No keys
anywhere: Entra tokens for the gateway (System API) and Event Grid (topic), granted by the
platform's routing (`internal-callers.yaml`, `producers.yaml`).

```mermaid
sequenceDiagram
    autonumber
    participant G as Gateway (people or agents)
    participant F as Order Process API
    participant B as orders.py (the brain)
    participant S as Salesforce System API (via gateway)
    participant E as evgt-intplat-order
    G->>F: DELETE /orders/4711?reason=... (gateway identity, Easy Auth)
    F->>S: GET /salesforce/orders/4711 (runtime identity token)
    S-->>F: order + ETag
    F->>B: cancel_order(order, reason)
    alt status does not allow it
        B-->>F: StateConflict
        F-->>G: 409 Order State Conflict
    else allowed
        B-->>F: Decision(status cancelled, OrderCancelled)
        F->>S: PUT /salesforce/orders/4711 (If-Match: ETag)
        Note over F,S: 412: someone changed it meanwhile → read and decide again
        F->>E: OrderCancelled (envelope: orderid, customerid, correlationid)
        F-->>G: 200 order
    end
```

| Concern | How |
|---|---|
| Invalid request | Validated against `openapi.yaml` itself → `400` |
| Idempotency | `Idempotency-Key` stored on the order; a retry returns the first order and publishes nothing |
| Concurrency | ETag / `If-Match` on Salesforce; on `412` the command is decided again on the fresh order (3 times) |
| Envelope | `integration-envelope` from `contracts` v1.2.0 builds and validates every event |
| Trace | `Correlation-Id` and `traceparent` passed on to the System API and into every event |

Facts that follow a command are published since 1.0.0; facts that follow other domains' facts
since 1.1.0 (policies, below).

## Policies

Other domains' facts reach the Order Process API through the platform's **event intake**
([ADR 0016](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/adr/0016-event-delivery-through-the-gateway.md)):
Event Grid → `apim-intplat-demo/intake/process-api-order` → `POST /intake` on this Function. Each
policy is a pure function in [`orders.py`](orders.py), like the commands.

| When (other domain) | Then (order domain) | Order |
|---|---|---|
| SalesOrderAccepted (finance) | **OrderConfirmed**, carrying lines and address for the warehouse | placed → confirmed |
| SalesOrderRefused (finance) | **OrderRejected** | placed → rejected |
| InvoiceIssued (finance) | **OrderInvoiced** | payment: unpaid → invoiced |
| PaymentReceived, invoice paid in full (finance) | **OrderPaid** | payment: → paid |
| StockReserved (supply) | **OrderReadyForShipment**, carrying lines and address for the 3PL | confirmed → readyForShipment; on hold: recorded, ready once released |
| ShipmentBooked (logistics) | recorded: shipment and tracking number | stays readyForShipment until the pick-up |
| ShipmentDispatched (logistics) | **OrderShipped** | readyForShipment → shipped |
| ShipmentDelivered (logistics) | **OrderDelivered** | shipped → delivered |
| the second of OrderDelivered and OrderPaid | **OrderCompleted** | delivered → completed |
| OrderReleased, with stock reserved meanwhile | **OrderReadyForShipment** | confirmed → readyForShipment |
| SalesOrderCancelled · StockReleased · ShipmentCancelled | recorded: the credit note, on the timeline | stays cancelled |

Since 1.2.0 every fact about an order lands on its **timeline** (the other domain's fact, then the
order fact it led to), and the order keeps its **references** in the other domains' systems: SAP
sales order, invoice and credit note, WMS reservation, 3PL shipment. That is what a cancellation
needs: **OrderCancelled** names what each domain has, and each one's intake undoes its own part
([ADR 0018](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/adr/0018-cancellation-reaches-every-domain.md)).

```mermaid
flowchart LR
    cancel["Cancel Order<br/>(until the 3PL picks it up)"] --> oc[["OrderCancelled<br/>erpOrderNumber · reservationId · shipmentId"]]
    oc --> fi["finance-intake"] --> sap["SAP: Cancel Sales Order<br/>credit note"] --> soc[["SalesOrderCancelled"]]
    oc --> si["supply-intake"] --> wms["WMS: Release Stock"] --> sr[["StockReleased"]]
    oc --> li["logistics-intake"] --> tpl["3PL: Cancel Shipment Booking"] --> sc[["ShipmentCancelled"]]
    soc & sr & sc --> timeline["the order's timeline"]
```

```mermaid
sequenceDiagram
    autonumber
    participant EG as Event Grid
    participant G as apim-intplat-demo /intake
    participant F as Order Process API
    participant B as orders.py (policy)
    participant S as Salesforce System API
    EG->>G: StockReserved (Entra token of the topic)
    G->>F: POST /intake (gateway identity)
    F->>S: GET order 4711 + ETag
    F->>B: stock_reserved(order, data)
    B-->>F: Decision(readyForShipment, OrderReadyForShipment)
    F->>S: PUT order (If-Match)
    F->>F: publish OrderReadyForShipment
    F-->>G: 200 applied
    Note over EG,F: a redelivered fact: 200 ignored (at-least-once). An error: Event Grid retries, then dead-letters.
```

A fact that does not fit the order's status (a pick-up for a cancelled order) is logged and
answered `200 ignored`: retrying would not make it fit. A sales order or reservation that arrives
just after a cancellation is recorded on the order, so customer service sees it (the known race in
ADR 0018). Only real failures (Salesforce down) make
Event Grid retry and, in the end, dead-letter into the tap.

## Language: business facts, not CRUD

Events are named after what happened in the business (Event Storming), in the past tense. An order
is never *updated* or *deleted*; it is amended, rescheduled, put on hold, cancelled.

| Command | API | Event |
|---|---|---|
| Place Order | `POST /orders` (with `Idempotency-Key`) | **OrderPlaced** |
| Retrieve Order | `GET /orders/{orderId}` | *query, tapped with `orderid`* |
| Retrieve List of Orders | `GET /orders?customerId=&status=&offset=&limit=` | *query, tapped with `customerid`* |
| Amend Order Lines | `PUT /orders/{orderId}/order-lines` | **OrderAmended** |
| Change Delivery Address | `PUT /orders/{orderId}/delivery-address` | **OrderDeliveryAddressChanged** |
| Reschedule Delivery | `PUT /orders/{orderId}/requested-delivery-date` | **OrderDeliveryRescheduled** |
| Put Order on Hold / Release Order | `PUT` / `DELETE /orders/{orderId}/hold` | **OrderPutOnHold** / **OrderReleased** |
| Cancel Order (soft delete) | `DELETE /orders/{orderId}?reason=` | **OrderCancelled** |

Facts from other domains become order facts through policies:

| When (other domain) | Then (order domain) |
|---|---|
| The ERP confirms or refuses | **OrderConfirmed** / **OrderRejected** |
| Finance: InvoiceIssued | **OrderInvoiced** |
| Finance: PaymentReceived (invoice covered) | **OrderPaid** |
| Supply: StockReserved | **OrderReadyForShipment** |
| Logistics: ShipmentDispatched / ShipmentDelivered | **OrderShipped** / **OrderDelivered** |
| OrderDelivered and OrderPaid both happened | **OrderCompleted** |

## Order status

A state machine; payment status (`unpaid`, `invoiced`, `paid`) is separate, so an order can be
shipped and still unpaid. Order lines are amended only while `placed`: once SAP and the warehouse
have the order, the customer cancels and orders again (since 1.2.0). An invalid transition is
[`409 Order State Conflict`](docs/problems/order-state-conflict.md).

```mermaid
stateDiagram-v2
    [*] --> placed: OrderPlaced
    placed --> confirmed: OrderConfirmed
    placed --> rejected: OrderRejected
    confirmed --> onHold: OrderPutOnHold
    onHold --> confirmed: OrderReleased
    confirmed --> readyForShipment: OrderReadyForShipment
    readyForShipment --> shipped: OrderShipped
    shipped --> delivered: OrderDelivered
    delivered --> completed: OrderCompleted
    placed --> cancelled: OrderCancelled
    confirmed --> cancelled: OrderCancelled
    onHold --> cancelled: OrderCancelled
    onHold --> readyForShipment: OrderReleased + OrderReadyForShipment (stock reserved meanwhile)
    readyForShipment --> cancelled: OrderCancelled (until the pick-up)
    rejected --> [*]
    cancelled --> [*]
    completed --> [*]
```

## Data model

One aggregate, the **Order**, with its lines and two value objects. Salesforce keeps it (system of
record); `Account` and `Product` belong to the sales domain. Defined once in [`ai-intelligence-domain/docs/architecture/data_models.py`](https://github.com/azure-intelligence-domain/ai-intelligence-domain/blob/main/docs/architecture/data_models.py), which also writes the entities of EventCatalog.

```mermaid
erDiagram
    Order {
        string orderId PK
        string customerId FK
        string channel
        string status
        string paymentStatus
        list orderLines
        Money totalAmount
        DeliveryAddress deliveryAddress
        date requestedDeliveryDate
        string holdReason
        string cancellationReason
        OrderReferences references
        list timeline
        datetime createdAt
        datetime updatedAt
    }
    OrderReferences {
        string erpOrderNumber FK
        string invoiceNumber FK
        string creditNoteNumber FK
        string reservationId FK
        string warehouseId
        string shipmentId FK
        string trackingNumber
        string partnerId
    }
    OrderMilestone {
        string fact
        string domain
        datetime occurredAt
    }
    OrderLine {
        integer lineNumber
        string productId FK
        integer quantity
        Money unitPrice
    }
    DeliveryAddress {
        string street
        string postalCode
        string city
        string countryCode
    }
    Money {
        number value
        string currency
    }
    Account {
        string see_sales_domain
    }
    CreditNote {
        string see_finance_domain
    }
    Invoice {
        string see_finance_domain
    }
    Product {
        string see_sales_domain
    }
    SalesOrder {
        string see_finance_domain
    }
    Shipment {
        string see_logistics_domain
    }
    StockReservation {
        string see_supply_domain
    }
    Order }o--|| Account : "customerId"
    Order ||--|{ OrderLine : "orderLines"
    Order ||--|| Money : "totalAmount"
    Order ||--|| DeliveryAddress : "deliveryAddress"
    Order }o--o| OrderReferences : "references"
    Order ||--|{ OrderMilestone : "timeline"
    OrderReferences }o--o| SalesOrder : "erpOrderNumber"
    OrderReferences }o--o| Invoice : "invoiceNumber"
    OrderReferences }o--o| CreditNote : "creditNoteNumber"
    OrderReferences }o--o| StockReservation : "reservationId"
    OrderReferences }o--o| Shipment : "shipmentId"
    OrderLine }o--|| Product : "productId"
    OrderLine ||--|| Money : "unitPrice"
```

## Flows (BPMN)

The `.bpmn` files are the source: open them in Camunda Modeler or
[demo.bpmn.io](https://demo.bpmn.io), or view them in
[EventCatalog](https://azure-intelligence-domain.github.io/eventcatalog/docs/domains/order/1.0.0).
GitHub cannot render BPMN, so here is the Mermaid version of the same flows.

### Order to delivery ([`order-to-delivery.bpmn`](docs/bpmn/order-to-delivery.bpmn))

```mermaid
flowchart LR
    subgraph sales["Sales · Salesforce"]
        won(("Opportunity<br/>closed-won")) --> place["Place order"]
    end
    subgraph order["Order domain"]
        placed(["OrderPlaced"])
        confirmed(["OrderConfirmed"])
        split{{"+"}}
        join{{"+"}}
        completed((("OrderCompleted")))
    end
    subgraph erp["ERP · SAP"]
        check["Check credit and availability"] --> accepted{"Accepted?"}
        rejected((("OrderRejected")))
    end
    subgraph finance["Finance"]
        invoice["Issue invoice"] --> invoiced(["OrderInvoiced"]) --> payment["Receive payment"] --> paid(["OrderPaid"])
    end
    subgraph warehouse["Warehouse · WMS"]
        reserve["Reserve stock"] --> ready(["OrderReadyForShipment"])
    end
    subgraph tpl["3PL partner"]
        ship["Request and dispatch shipment"] --> shipped(["OrderShipped"]) --> deliver["Deliver to customer"] --> delivered(["OrderDelivered"])
    end
    place --> placed --> check
    accepted -- no --> rejected
    accepted -- yes --> confirmed --> split
    split --> invoice
    split --> reserve
    ready -- "logistics-intake" --> ship
    paid --> join
    delivered --> join
    join --> completed
```

### Commands ([`order-commands.bpmn`](docs/bpmn/order-commands.bpmn))

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller (person or agent)
    participant G as API Management
    participant T as Tap
    participant P as Order Process API
    participant S as Salesforce System API
    participant E as evgt-intplat-order
    C->>G: Command + Correlation-Id (+ agent token)
    alt Correlation-Id or token invalid
        G-->>C: 400 / 401 Problem
    else valid
        G->>T: Gateway mutation / agent action
        G->>P: Command
        P->>S: Load order
        alt not allowed in current status
            P-->>C: 409 Order State Conflict
        else allowed
            P->>S: Save new status and data
            P->>E: Business event (e.g. OrderCancelled)
            E->>T: Tap subscription
            P-->>C: 200 / 201 order
        end
    end
```

### Queries ([`order-queries.bpmn`](docs/bpmn/order-queries.bpmn))

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant G as API Management
    participant T as Tap
    participant P as Order Process API
    participant S as Salesforce System API
    C->>G: GET order or list + Correlation-Id
    G->>T: Query event with orderid / customerid (x-business-keys)
    G->>P: Query
    P->>S: Read order(s)
    P-->>C: 200 order or page
```

Every retrieval is tapped, so the Intelligence domain can see how often orders are looked at, and
by whom: "ACME's order 4711 was retrieved 5 times by customer service today".
