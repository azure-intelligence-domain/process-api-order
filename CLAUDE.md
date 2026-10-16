# CLAUDE.md

The Order Process API of the order domain: the order lifecycle (commands and queries) and the
order's business events. Ground rules and decisions live in the
[ai-intelligence-domain](https://github.com/azure-intelligence-domain/ai-intelligence-domain)
repository (ADR 0013 for this domain).

- Event names are business facts in the past tense (Event Storming), never CRUD: OrderPlaced,
  OrderCancelled, OrderPaid. No generic OrderUpdated: every change has its own intent. When a
  name is unclear, ask the user.
- Cancelling is a soft delete (`DELETE /orders/{orderId}` → status `cancelled` → OrderCancelled).
- Statuses follow the state machine in README.md; an invalid transition is `409 Order State Conflict`.
- Contract first: `openapi.yaml` and `asyncapi.yaml` change before the code, with a higher
  `info.version`; the contract check enforces adidas rules and no breaking changes.
- Every read operation declares its business keys in `x-business-keys`, so the gateway can tap
  queries with orderid / customerid.
- The BPMN files in `docs/bpmn/` are the source of the flows (edit in Camunda Modeler or
  demo.bpmn.io); keep the Mermaid versions in README.md in step.
