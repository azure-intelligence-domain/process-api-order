"""Generate the order flows as BPMN 2.0 (with diagram interchange, so any BPMN tool can show them).

The .bpmn files are the source of truth: edit them in Camunda Modeler or demo.bpmn.io. This
script only produced the first versions with a clean layout; it is kept so the layout can be
regenerated after larger changes.  Usage: python docs/bpmn/generate.py
"""

from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
SIZE = {"task": (130, 80), "event": (36, 36), "gateway": (50, 50)}
LANE_LABEL = 30  # width of the lane name column
X0 = 230  # first column


class Flow:
    def __init__(self, process_id, name, lanes):
        self.process_id, self.name, self.lanes = process_id, name, lanes  # lanes: [(id, label, height)]
        self.nodes, self.edges = {}, []
        y = 0
        self.lane_y = {}
        for lane_id, _, height in lanes:
            self.lane_y[lane_id] = (y, height)
            y += height
        self.height = y

    def node(self, node_id, kind, name, lane, column, **extra):
        self.nodes[node_id] = dict(kind=kind, name=name, lane=lane, column=column, **extra)

    def edge(self, source, target, name=""):
        self.edges.append((source, target, name))

    def bounds(self, node_id):
        n = self.nodes[node_id]
        shape = "task" if n["kind"].endswith("Task") or n["kind"] == "task" else (
            "gateway" if n["kind"].endswith("Gateway") else "event")
        w, h = SIZE[shape]
        lane_top, lane_height = self.lane_y[n["lane"]]
        cx = X0 + n["column"] * 170
        cy = 60 + lane_top + lane_height / 2
        return cx - w / 2, cy - h / 2, w, h

    def xml(self):
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
               'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
               'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" '
               f'id="Definitions_{self.process_id}" targetNamespace="https://github.com/azure-intelligence-domain/process-api-order">',
               f'  <bpmn:collaboration id="Collaboration_{self.process_id}">',
               f'    <bpmn:participant id="Participant_{self.process_id}" name="{escape(self.name)}" processRef="{self.process_id}" />',
               '  </bpmn:collaboration>',
               f'  <bpmn:process id="{self.process_id}" isExecutable="false">',
               f'    <bpmn:laneSet id="LaneSet_{self.process_id}">']
        for lane_id, label, _ in self.lanes:
            out.append(f'      <bpmn:lane id="{lane_id}" name="{escape(label)}">')
            out += [f'        <bpmn:flowNodeRef>{n}</bpmn:flowNodeRef>' for n, v in self.nodes.items() if v["lane"] == lane_id]
            out.append('      </bpmn:lane>')
        out.append('    </bpmn:laneSet>')
        for node_id, n in self.nodes.items():
            incoming = [f"Flow_{i}" for i, (s, t, _) in enumerate(self.edges) if t == node_id]
            outgoing = [f"Flow_{i}" for i, (s, t, _) in enumerate(self.edges) if s == node_id]
            out.append(f'    <bpmn:{n["kind"]} id="{node_id}" name="{escape(n["name"])}">')
            out += [f'      <bpmn:incoming>{f}</bpmn:incoming>' for f in incoming]
            out += [f'      <bpmn:outgoing>{f}</bpmn:outgoing>' for f in outgoing]
            if n.get("message"):
                out.append(f'      <bpmn:messageEventDefinition id="Message_{node_id}" />')
            out.append(f'    </bpmn:{n["kind"]}>')
        for i, (s, t, name) in enumerate(self.edges):
            out.append(f'    <bpmn:sequenceFlow id="Flow_{i}" name="{escape(name)}" sourceRef="{s}" targetRef="{t}" />')
        out.append('  </bpmn:process>')

        width = X0 + max(n["column"] for n in self.nodes.values()) * 170 + 140
        out += [f'  <bpmndi:BPMNDiagram id="Diagram_{self.process_id}">',
                f'    <bpmndi:BPMNPlane id="Plane_{self.process_id}" bpmnElement="Collaboration_{self.process_id}">',
                f'      <bpmndi:BPMNShape id="Participant_{self.process_id}_di" bpmnElement="Participant_{self.process_id}" isHorizontal="true">',
                f'        <dc:Bounds x="100" y="60" width="{width}" height="{self.height}" />',
                '      </bpmndi:BPMNShape>']
        for lane_id, _, height in self.lanes:
            top, _ = self.lane_y[lane_id]
            out += [f'      <bpmndi:BPMNShape id="{lane_id}_di" bpmnElement="{lane_id}" isHorizontal="true">',
                    f'        <dc:Bounds x="{100 + LANE_LABEL}" y="{60 + top}" width="{width - LANE_LABEL}" height="{height}" />',
                    '      </bpmndi:BPMNShape>']
        for node_id, n in self.nodes.items():
            x, y, w, h = self.bounds(node_id)
            out.append(f'      <bpmndi:BPMNShape id="{node_id}_di" bpmnElement="{node_id}"'
                       + (' isMarkerVisible="true"' if n["kind"] == "exclusiveGateway" else "") + '>')
            out.append(f'        <dc:Bounds x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h}" />')
            if not (n["kind"].endswith("Task") or n["kind"] == "task"):
                out += ['        <bpmndi:BPMNLabel>',
                        f'          <dc:Bounds x="{x - 50:.0f}" y="{y + h + 4:.0f}" width="{w + 100}" height="28" />',
                        '        </bpmndi:BPMNLabel>']
            out.append('      </bpmndi:BPMNShape>')
        for i, (s, t, name) in enumerate(self.edges):
            sx, sy, sw, sh = self.bounds(s)
            tx, ty, tw, th = self.bounds(t)
            start = (sx + sw, sy + sh / 2)
            end = (tx, ty + th / 2)
            if abs(start[1] - end[1]) < 1:
                points = [start, end]
            elif tx > sx + sw:  # down or up to another lane, then right
                mid = start[0] + (end[0] - start[0]) / 2
                points = [start, (mid, start[1]), (mid, end[1]), end]
            else:  # target in the same column: leave from the bottom or top
                down = ty > sy
                points = [(sx + sw / 2, sy + (sh if down else 0)), (tx + tw / 2, ty + (0 if down else th))]
            out.append(f'      <bpmndi:BPMNEdge id="Flow_{i}_di" bpmnElement="Flow_{i}">')
            out += [f'        <di:waypoint x="{px:.0f}" y="{py:.0f}" />' for px, py in points]
            if name:
                (ax, ay), (bx, by) = points[-2], points[-1]
                lx, ly = (ax + bx) / 2 - 20, (ay + by) / 2 - 18
                out += ['        <bpmndi:BPMNLabel>', f'          <dc:Bounds x="{lx:.0f}" y="{ly:.0f}" width="60" height="14" />',
                        '        </bpmndi:BPMNLabel>']
            out.append('      </bpmndi:BPMNEdge>')
        out += ['    </bpmndi:BPMNPlane>', '  </bpmndi:BPMNDiagram>', '</bpmn:definitions>', '']
        return "\n".join(out)


def order_to_delivery():
    f = Flow("OrderToDelivery", "Order to delivery (ACME order 4711)", [
        ("Lane_Sales", "Sales · Salesforce", 120), ("Lane_Order", "Order domain", 130),
        ("Lane_Erp", "ERP · SAP", 120), ("Lane_Finance", "Finance", 120),
        ("Lane_Warehouse", "Warehouse · WMS", 120), ("Lane_Logistics", "3PL partner", 120)])
    f.node("Start_ClosedWon", "startEvent", "Opportunity closed-won", "Lane_Sales", 0)
    f.node("Task_PlaceOrder", "userTask", "Place order", "Lane_Sales", 1)
    f.node("Event_OrderPlaced", "intermediateThrowEvent", "OrderPlaced", "Lane_Order", 2, message=True)
    f.node("Task_Confirm", "serviceTask", "Check credit and availability", "Lane_Erp", 3)
    f.node("Gateway_Accepted", "exclusiveGateway", "Accepted?", "Lane_Erp", 4)
    f.node("End_Rejected", "endEvent", "OrderRejected", "Lane_Erp", 5, message=True)
    f.node("Event_OrderConfirmed", "intermediateThrowEvent", "OrderConfirmed", "Lane_Order", 5, message=True)
    f.node("Gateway_Split", "parallelGateway", "", "Lane_Order", 6)
    f.node("Task_Invoice", "serviceTask", "Issue invoice", "Lane_Finance", 7)
    f.node("Event_OrderInvoiced", "intermediateThrowEvent", "OrderInvoiced", "Lane_Finance", 8, message=True)
    f.node("Task_Payment", "receiveTask", "Receive payment", "Lane_Finance", 9)
    f.node("Event_OrderPaid", "intermediateThrowEvent", "OrderPaid", "Lane_Finance", 10, message=True)
    f.node("Task_Reserve", "serviceTask", "Reserve stock", "Lane_Warehouse", 7)
    f.node("Event_Ready", "intermediateThrowEvent", "OrderReadyForShipment", "Lane_Warehouse", 8, message=True)
    f.node("Task_Ship", "serviceTask", "Request and dispatch shipment", "Lane_Logistics", 9)
    f.node("Event_Shipped", "intermediateThrowEvent", "OrderShipped", "Lane_Logistics", 10, message=True)
    f.node("Task_Deliver", "serviceTask", "Deliver to customer", "Lane_Logistics", 11)
    f.node("Event_Delivered", "intermediateThrowEvent", "OrderDelivered", "Lane_Logistics", 12, message=True)
    f.node("Gateway_Join", "parallelGateway", "", "Lane_Order", 13)
    f.node("End_Completed", "endEvent", "OrderCompleted", "Lane_Order", 14, message=True)
    for s, t, n in [("Start_ClosedWon", "Task_PlaceOrder", ""), ("Task_PlaceOrder", "Event_OrderPlaced", ""),
                    ("Event_OrderPlaced", "Task_Confirm", ""), ("Task_Confirm", "Gateway_Accepted", ""),
                    ("Gateway_Accepted", "End_Rejected", "no"), ("Gateway_Accepted", "Event_OrderConfirmed", "yes"),
                    ("Event_OrderConfirmed", "Gateway_Split", ""),
                    ("Gateway_Split", "Task_Invoice", ""), ("Task_Invoice", "Event_OrderInvoiced", ""),
                    ("Event_OrderInvoiced", "Task_Payment", ""), ("Task_Payment", "Event_OrderPaid", ""),
                    ("Event_OrderPaid", "Gateway_Join", ""),
                    ("Gateway_Split", "Task_Reserve", ""), ("Task_Reserve", "Event_Ready", ""),
                    ("Event_Ready", "Task_Ship", "logistics-intake"), ("Task_Ship", "Event_Shipped", ""),
                    ("Event_Shipped", "Task_Deliver", ""), ("Task_Deliver", "Event_Delivered", ""),
                    ("Event_Delivered", "Gateway_Join", ""), ("Gateway_Join", "End_Completed", "")]:
        f.edge(s, t, n)
    return f


LANES = [("Lane_Gateway", "API Management", 120), ("Lane_Process", "Order Process API", 120),
         ("Lane_System", "Salesforce System API", 120), ("Lane_Events", "Event Grid · tap", 120)]


def order_commands():
    f = Flow("OrderCommands", "Order commands: place, amend, change address, reschedule, hold, release, cancel", LANES)
    f.node("Start_Call", "startEvent", "Command with Correlation-Id", "Lane_Gateway", 0, message=True)
    f.node("Gateway_Valid", "exclusiveGateway", "Correlation-Id (and agent token) valid?", "Lane_Gateway", 1)
    f.node("End_Invalid", "endEvent", "400 / 401 Problem", "Lane_Gateway", 2)
    f.node("Task_Tap", "sendTask", "Tap the mutation (gateway mutation or agent action)", "Lane_Events", 2)
    f.node("Task_Load", "serviceTask", "Load order", "Lane_System", 3)
    f.node("Gateway_Allowed", "exclusiveGateway", "Allowed in current status?", "Lane_Process", 4)
    f.node("End_Conflict", "endEvent", "409 Order State Conflict", "Lane_Process", 5)
    f.node("Task_Save", "serviceTask", "Save new status and data", "Lane_System", 5)
    f.node("Event_Business", "intermediateThrowEvent", "Business event, e.g. OrderCancelled", "Lane_Events", 6, message=True)
    f.node("End_Done", "endEvent", "200 / 201 order", "Lane_Process", 7)
    for s_, t, n in [("Start_Call", "Gateway_Valid", ""), ("Gateway_Valid", "End_Invalid", "no"),
                     ("Gateway_Valid", "Task_Tap", "yes"), ("Task_Tap", "Task_Load", ""),
                     ("Task_Load", "Gateway_Allowed", ""), ("Gateway_Allowed", "End_Conflict", "no"),
                     ("Gateway_Allowed", "Task_Save", "yes"), ("Task_Save", "Event_Business", ""),
                     ("Event_Business", "End_Done", "")]:
        f.edge(s_, t, n)
    return f


def order_queries():
    f = Flow("OrderQueries", "Order queries: retrieve order, retrieve list of orders", LANES)
    f.node("Start_Call", "startEvent", "Query with Correlation-Id", "Lane_Gateway", 0, message=True)
    f.node("Gateway_Valid", "exclusiveGateway", "Correlation-Id (and agent token) valid?", "Lane_Gateway", 1)
    f.node("End_Invalid", "endEvent", "400 / 401 Problem", "Lane_Gateway", 2)
    f.node("Task_Tap", "sendTask", "Tap the query with orderid / customerid", "Lane_Events", 2)
    f.node("Task_Read", "serviceTask", "Read order or page of orders", "Lane_System", 3)
    f.node("End_Answer", "endEvent", "200 order or page", "Lane_Process", 4)
    for s_, t, n in [("Start_Call", "Gateway_Valid", ""), ("Gateway_Valid", "End_Invalid", "no"),
                     ("Gateway_Valid", "Task_Tap", "yes"), ("Task_Tap", "Task_Read", ""), ("Task_Read", "End_Answer", "")]:
        f.edge(s_, t, n)
    return f


if __name__ == "__main__":
    for flow, name in [(order_to_delivery(), "order-to-delivery.bpmn"), (order_commands(), "order-commands.bpmn"),
                       (order_queries(), "order-queries.bpmn")]:
        (HERE / name).write_text(flow.xml(), encoding="utf-8")
        print("wrote", HERE / name)
