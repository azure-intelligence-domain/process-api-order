# Order State Conflict

`409 Conflict`, `application/problem+json`

The order's current status does not allow this command. The order lifecycle is a state machine:

| Command | Allowed while |
|---|---|
| Cancel Order (`DELETE /orders/{orderId}`) | placed, confirmed, onHold |
| Amend Order Lines | placed, confirmed |
| Change Delivery Address | placed, confirmed, onHold |
| Reschedule Delivery | placed, confirmed, onHold, readyForShipment |
| Put Order on Hold | confirmed |
| Release Order | onHold |

For example, an order that has shipped can no longer be cancelled.
