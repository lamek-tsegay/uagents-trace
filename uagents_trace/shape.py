"""Classify a trace's shape for rendering, purely as a function of its spans
-- no DB access. `show`, the TUI's expand, and the web dashboard all use this
to choose between the peer, hub, and flat-fallback renderers.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

LegState = Literal["pending", "completed", "failed"]

PEER = "peer"
HUB = "hub"
MULTI_LEVEL = "multi_level"


def classify_trace_shape(spans: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """Returns (shape, hub_agent); hub_agent is set only when shape == HUB.

    - peer: exactly two agents talk, e.g. a ping/pong round trip.
    - hub: one agent (the "busiest source", by send count) dispatches to
      >=2 distinct other agents -- e.g. an orchestrator fanning out to
      subagents.
    - multi_level: anything else (chains, nested fan-out). Callers should
      fall back to a flat span list rather than attempt nested rendering.
    """
    if not spans:
        return PEER, None

    agents: set[str] = set()
    dests_by_source: dict[str, set[str]] = {}
    sends_by_source: dict[str, int] = {}
    for s in spans:
        src, dst = s["source_agent"], s["dest_agent"]
        agents.add(src)
        agents.add(dst)
        dests_by_source.setdefault(src, set()).add(dst)
        sends_by_source[src] = sends_by_source.get(src, 0) + 1

    if len(agents) == 2:
        return PEER, None

    busiest_source = max(sends_by_source, key=lambda a: sends_by_source[a])
    if len(dests_by_source.get(busiest_source, ())) >= 2:
        return HUB, busiest_source

    return MULTI_LEVEL, None


def _span_latency_ms(span: dict[str, Any]) -> int | None:
    ack = span.get("acked_at")
    enq = span.get("enqueued_at")
    if ack is None or enq is None:
        return None
    return max(ack - enq, 0)


def _send_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in spans if s.get("direction") == "send" or s.get("direction") is None]


def build_hub_legs(spans: list[dict[str, Any]], hub: str) -> list[dict[str, Any]]:
    """One entry per subagent the hub dispatched to:

        {"subagent": addr, "dispatch_payload": str, "reply_payload": str,
         "dispatch_ms": int, "reply_ms": int | None,
         "dispatch_message": str | None, "reply_message": str | None,
         "state": "completed" | "failed" | "pending",
         "latency_ms": int, "reason": str}

    dispatch_ms / reply_ms come from send-side spans only (`traced_send`).
    """
    legs_by_subagent: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        if s["source_agent"] == hub:
            other = s["dest_agent"]
        elif s["dest_agent"] == hub:
            other = s["source_agent"]
        else:
            continue
        legs_by_subagent.setdefault(other, []).append(s)

    legs: list[dict[str, Any]] = []
    for subagent, leg_spans in legs_by_subagent.items():
        dispatch_spans = _send_spans([s for s in leg_spans if s["source_agent"] == hub])
        reply_spans = _send_spans(
            [s for s in leg_spans if s["dest_agent"] == hub and s["source_agent"] == subagent]
        )
        if not dispatch_spans:
            continue

        dispatch = dispatch_spans[0]
        dispatch_start = dispatch["enqueued_at"]
        dispatch_payload = dispatch["payload_type"]
        dispatch_ms = _span_latency_ms(dispatch)
        failed_dispatch = next((s for s in dispatch_spans if s["state"] in ("dropped", "timeout")), None)

        base = {
            "subagent": subagent,
            "dispatch_payload": dispatch_payload,
            "dispatch_ms": dispatch_ms,
            "dispatch_message": dispatch.get("payload_summary"),
        }

        if reply_spans and reply_spans[0].get("state") == "delivered":
            reply = reply_spans[0]
            reply_end = reply.get("acked_at") or reply["enqueued_at"]
            legs.append(
                {
                    **base,
                    "reply_payload": reply["payload_type"],
                    "reply_ms": _span_latency_ms(reply),
                    "reply_message": reply.get("payload_summary"),
                    "state": "completed",
                    "latency_ms": max(reply_end - dispatch_start, 0),
                }
            )
        elif failed_dispatch:
            legs.append(
                {
                    **base,
                    "state": "failed",
                    "reason": failed_dispatch.get("error") or failed_dispatch["state"],
                }
            )
        else:
            legs.append({**base, "state": "pending"})

    return legs


@dataclass
class TreeNode:
    """One agent in an interaction tree; children are outbound dispatches."""

    agent: str
    message: str | None = None
    payload_type: str | None = None
    state: LegState | None = None
    latency_ms: int | None = None
    dispatch_ms: int | None = None
    reply_ms: int | None = None
    reason: str | None = None
    children: list["TreeNode"] = field(default_factory=list)


def _find_node(node: TreeNode, agent: str) -> TreeNode | None:
    if node.agent == agent:
        return node
    for child in node.children:
        found = _find_node(child, agent)
        if found is not None:
            return found
    return None


def _find_parent(root: TreeNode, agent: str) -> TreeNode | None:
    for child in root.children:
        if child.agent == agent:
            return root
        found = _find_parent(child, agent)
        if found is not None:
            return found
    return None


def _leg_state(parent: str, child: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome for parent -> child dispatch, mirroring build_hub_legs per edge."""
    leg_spans = [
        s
        for s in spans
        if (s["source_agent"] == parent and s["dest_agent"] == child)
        or (s["source_agent"] == child and s["dest_agent"] == parent)
    ]
    dispatch_spans = _send_spans([s for s in leg_spans if s["source_agent"] == parent])
    reply_spans = _send_spans(
        [s for s in leg_spans if s["source_agent"] == child and s["dest_agent"] == parent]
    )
    if not dispatch_spans:
        return {"state": "pending"}

    dispatch = dispatch_spans[0]
    dispatch_start = dispatch["enqueued_at"]
    failed_dispatch = next((s for s in dispatch_spans if s["state"] in ("dropped", "timeout")), None)

    base: dict[str, Any] = {
        "message": dispatch.get("payload_summary"),
        "payload_type": dispatch["payload_type"],
        "dispatch_ms": _span_latency_ms(dispatch),
    }

    if reply_spans and reply_spans[0].get("state") == "delivered":
        reply = reply_spans[0]
        reply_end = reply.get("acked_at") or reply["enqueued_at"]
        return {
            **base,
            "state": "completed",
            "reply_ms": _span_latency_ms(reply),
            "latency_ms": max(reply_end - dispatch_start, 0),
        }
    if failed_dispatch:
        return {
            **base,
            "state": "failed",
            "reason": failed_dispatch.get("error") or failed_dispatch["state"],
        }
    return {**base, "state": "pending"}


def _apply_leg_fields(node: TreeNode, fields: dict[str, Any]) -> None:
    node.message = fields.get("message")
    node.payload_type = fields.get("payload_type")
    node.state = fields.get("state")
    node.latency_ms = fields.get("latency_ms")
    node.dispatch_ms = fields.get("dispatch_ms")
    node.reply_ms = fields.get("reply_ms")
    node.reason = fields.get("reason")


def build_interaction_tree(spans: list[dict[str, Any]], root: str) -> TreeNode:
    """Fan-out tree from chronological send spans.

    Root is the orchestrator (or hub). Each outbound send creates or updates a
    child branch; a reply from child back to parent marks that branch completed.
    Nested fan-out (sub-agent dispatching further) adds deeper children.
    """
    tree = TreeNode(agent=root)
    sends = sorted(_send_spans(spans), key=lambda s: s["enqueued_at"])

    for s in sends:
        src, dst = s["source_agent"], s["dest_agent"]
        if src == dst:
            continue

        parent_node = _find_parent(tree, src)
        if parent_node is not None and parent_node.agent == dst:
            continue

        src_node = _find_node(tree, src)
        if src_node is None:
            if src == root:
                src_node = tree
            else:
                continue

        existing = next((c for c in src_node.children if c.agent == dst), None)
        if existing is None:
            existing = TreeNode(agent=dst)
            src_node.children.append(existing)

    _refresh_tree_states(tree, spans)
    return tree


def _refresh_tree_states(node: TreeNode, spans: list[dict[str, Any]], parent_agent: str | None = None) -> None:
    if parent_agent is not None:
        _apply_leg_fields(node, _leg_state(parent_agent, node.agent, spans))
    for child in node.children:
        _refresh_tree_states(child, spans, node.agent)


def tree_node_to_dict(node: TreeNode) -> dict[str, Any]:
    return {
        "agent": node.agent,
        "message": node.message,
        "payload_type": node.payload_type,
        "state": node.state,
        "latency_ms": node.latency_ms,
        "dispatch_ms": node.dispatch_ms,
        "reply_ms": node.reply_ms,
        "reason": node.reason,
        "children": [tree_node_to_dict(c) for c in node.children],
    }
