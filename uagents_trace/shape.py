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

# Passive tree listings (cli.py's `show --view tree`, live.py's diagram
# panel, the web dashboard's tree view) show one line per node for
# potentially dozens of nodes at once -- a raw payload_summary can be a
# multi-thousand-character JSON blob (an assembled document repeated
# across several spans), which makes the tree unreadable. This caps it to
# a couple hundred chars with the omitted size noted; the *full* payload
# stays available wherever a caller shows detail for one explicitly
# selected span (the inspector panel, `_hub_leg_detail`/`_peer_hop_detail`,
# etc.) -- those read `payload_summary` directly, not through this.
TREE_MESSAGE_LIMIT = 160


def truncate_tree_message(text: str | None, limit: int = TREE_MESSAGE_LIMIT) -> str | None:
    """Single line, capped length -- a multi-paragraph reply (e.g. the
    final ChatMessage's assembled starter kit) has real newlines well
    within the char limit, which would otherwise still blow the "a couple
    terminal lines" budget despite being short enough by character count.
    """
    if not text:
        return text
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed if collapsed != text else text
    return f"{collapsed[:limit].rstrip()}… (+{len(text) - limit} chars)"


def _flat_busiest_hub(spans: list[dict[str, Any]]) -> Optional[str]:
    """The single busiest source (by raw send count) with >=2 distinct
    destinations, blind to nesting depth anywhere else in the trace. This
    was `classify_trace_shape`'s only heuristic before causal parentage
    (`parent_span_id`) existed, and is kept as a fallback for spans
    recorded before that column existed -- see `classify_trace_shape` and
    `build_interaction_tree`. It's unsound for depth (a trace where this
    "hub"'s own legs fan out further still reads as a flat hub), which is
    exactly the bug real parentage fixes when it's available.
    """
    dests_by_source: dict[str, set[str]] = {}
    sends_by_source: dict[str, int] = {}
    for s in spans:
        src, dst = s["source_agent"], s["dest_agent"]
        dests_by_source.setdefault(src, set()).add(dst)
        sends_by_source[src] = sends_by_source.get(src, 0) + 1

    if not sends_by_source:
        return None
    busiest_source = max(sends_by_source, key=lambda a: sends_by_source[a])
    if len(dests_by_source.get(busiest_source, ())) >= 2:
        return busiest_source
    return None


def _tree_depth(node: "TreeNode") -> int:
    if not node.children:
        return 0
    return 1 + max(_tree_depth(c) for c in node.children)


def classify_trace_shape(spans: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """Returns (shape, hub_agent); hub_agent is set only when shape == HUB.

    - peer: exactly two agents talk, e.g. a ping/pong round trip.
    - hub: one agent dispatches to >=2 distinct other agents, and none of
      those legs themselves fan out any further -- a genuinely flat,
      2-level fan-out (e.g. an orchestrator calling subagents that don't
      call anyone else).
    - multi_level: anything else (chains, nested fan-out -- including a
      hub whose legs have their own children, which is *not* a flat hub).

    When spans carry causal parentage (`parent_span_id`, see
    `recorder.traced_send`), shape is read directly off the real dispatch
    tree (`build_interaction_tree`) instead of guessed from aggregate send
    counts -- depth is a property of the data, not a property of which
    agent happens to have the most outbound sends. Spans with no parentage
    at all (pre-migration data) fall back to the old blind busiest-source
    heuristic (`_flat_busiest_hub`) so existing databases keep classifying
    the same way they always did.
    """
    if not spans:
        return PEER, None

    agents: set[str] = set()
    for s in spans:
        agents.add(s["source_agent"])
        agents.add(s["dest_agent"])
    if len(agents) == 2:
        return PEER, None

    causal_tree, _ = _build_causal_tree(spans)
    if causal_tree is not None:
        hub_node = causal_tree.children[0] if causal_tree.children else None
        if hub_node is not None and len(hub_node.children) >= 2 and _tree_depth(hub_node) <= 1:
            return HUB, hub_node.agent
        return MULTI_LEVEL, None

    hub = _flat_busiest_hub(spans)
    if hub is not None:
        return HUB, hub
    return MULTI_LEVEL, None


def _span_latency_ms(span: dict[str, Any]) -> int | None:
    ack = span.get("acked_at")
    enq = span.get("enqueued_at")
    if ack is None or enq is None:
        return None
    return max(ack - enq, 0)


def _send_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in spans if s.get("direction") == "send" or s.get("direction") is None]


def _receive_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in spans if s.get("direction") == "receive"]


def _match_sends_to_receives(spans: list[dict[str, Any]]) -> dict[str, Optional[dict[str, Any]]]:
    """send id -> its matched receive span, or None if never received.

    Matches within each (source, dest, payload_type) key by rank order, the
    same greedy "earliest unclaimed receive" approach as before, but ranks
    sends by their *causal parent's* enqueued_at (the receive span that
    caused this send -- see `recorder.traced_send`) when known, instead of
    the send's own enqueued_at. The parent's timestamp reflects when this
    send was truly caused and is unaffected by any recorder-side write
    jitter on the send's own insert; a send with no resolvable parent
    (NULL `parent_span_id`, or a parent outside this span set) falls back
    to its own enqueued_at, unchanged from the pre-parentage behavior.
    """
    spans_by_id = {s["id"]: s for s in spans}
    sends = _send_spans(spans)
    receives = _receive_spans(spans)

    def rank(send: dict[str, Any]) -> tuple[int, int]:
        parent = spans_by_id.get(send.get("parent_span_id"))
        anchor = parent["enqueued_at"] if parent is not None else send["enqueued_at"]
        return (anchor, send["enqueued_at"])

    ordered_sends = sorted(sends, key=rank)
    unclaimed_receives = sorted(range(len(receives)), key=lambda i: receives[i]["enqueued_at"])

    matches: dict[str, Optional[dict[str, Any]]] = {}
    for send in ordered_sends:
        key = (send["source_agent"], send["dest_agent"], send["payload_type"])
        match_idx = next(
            (
                i
                for i in unclaimed_receives
                if (receives[i]["source_agent"], receives[i]["dest_agent"], receives[i]["payload_type"]) == key
            ),
            None,
        )
        if match_idx is not None:
            unclaimed_receives.remove(match_idx)
            matches[send["id"]] = receives[match_idx]
        else:
            matches[send["id"]] = None
    return matches


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


def _legacy_leg_state(parent: str, child: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome for parent -> child dispatch, mirroring build_hub_legs per edge.

    Only used by `_build_legacy_fanout_tree` (pre-parentage databases) --
    see that function's docstring for why this "does the child reply
    directly back to its own caller" model is wrong for a real pipeline and
    was replaced by `_edge_state` for causally-built trees.
    """
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


def _build_legacy_fanout_tree(spans: list[dict[str, Any]], root: str) -> TreeNode:
    """Fan-out tree from chronological send spans, blind to real causality --
    kept only as a fallback for spans with no `parent_span_id` data at all
    (pre-migration databases; see `build_interaction_tree`).

    Every (agent) gets *one* node, no matter how many distinct times it's
    dispatched to -- two separate calls to the same agent (e.g. a retry)
    collapse onto the same tree node, and `_legacy_leg_state` always reports
    the *first* chronological dispatch's payload/timing for that node,
    regardless of which call the tree is actually trying to show. This is
    the crossing bug real parentage fixes; it's an accepted limitation of
    this fallback path, not something worth re-solving for data that can
    never carry the real answer.
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

    _refresh_legacy_tree_states(tree, spans)
    return tree


def _refresh_legacy_tree_states(node: TreeNode, spans: list[dict[str, Any]], parent_agent: str | None = None) -> None:
    if parent_agent is not None:
        _apply_leg_fields(node, _legacy_leg_state(parent_agent, node.agent, spans))
    for child in node.children:
        _refresh_legacy_tree_states(child, spans, node.agent)


def _edge_state(send: dict[str, Any], receive: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Outcome for one causal tree edge (a specific send and the receive it
    was matched to, if any).

    Unlike the legacy `_legacy_leg_state`, "completed" means *this specific
    hop was delivered* -- it never requires the receiving agent to reply
    back to its own caller. A pipeline/relay edge (e.g. `qa_critic` handing
    off to `delivery`, which never replies to `qa_critic`) still reports
    "completed" once `delivery` has received it, instead of hanging at
    "pending" forever (see Task 5 / FINDINGS.md item on spans stuck pending
    that actually delivered).
    """
    base: dict[str, Any] = {
        "message": send.get("payload_summary"),
        "payload_type": send.get("payload_type"),
    }
    dispatch_ms = _span_latency_ms(send)

    if receive is not None and receive.get("state") == "delivered":
        receive_end = receive.get("acked_at") or receive["enqueued_at"]
        return {
            **base,
            "state": "completed",
            "dispatch_ms": dispatch_ms,
            "reply_ms": _span_latency_ms(receive),
            "latency_ms": max(receive_end - send["enqueued_at"], 0),
        }
    if send.get("state") in ("dropped", "timeout"):
        return {
            **base,
            "state": "failed",
            "dispatch_ms": dispatch_ms,
            "reason": send.get("error") or send["state"],
        }
    if receive is not None and receive.get("state") in ("dropped", "timeout"):
        return {
            **base,
            "state": "failed",
            "dispatch_ms": dispatch_ms,
            "reason": receive.get("error") or receive["state"],
        }
    if receive is None and send.get("state") == "delivered":
        # No receive-side span exists for this hop at all -- the
        # destination isn't part of the traced system (e.g. an external
        # chat client whose own handler was never wrapped in @trace, like
        # the final reply to user_proxy). The send's own transport ack is
        # the only delivery signal there is, and it says this arrived, so
        # report it as delivered instead of hanging at "pending" forever
        # waiting for a receive that will never exist (see Task 5 /
        # FINDINGS.md's "spans stuck pending that actually delivered").
        return {**base, "state": "completed", "dispatch_ms": dispatch_ms, "latency_ms": dispatch_ms}
    return {**base, "state": "pending", "dispatch_ms": dispatch_ms}


def _build_causal_tree(spans: list[dict[str, Any]]) -> tuple[Optional[TreeNode], list[TreeNode]]:
    """Build the real dispatch tree from `parent_span_id` (see
    `recorder.traced_send`/`recorder.trace`). Returns `(None, [])` if no
    span in `spans` carries any parentage at all -- callers fall back to
    `_build_legacy_fanout_tree` in that case (see `build_interaction_tree`).

    Each send's *branch* in the tree is determined purely by
    `parent_span_id` (unambiguous: it's the same agent's own receive span,
    captured via the same asyncio task/context, never guessed from
    timing). Each send's *matched receive* -- needed to know its own
    delivery outcome and to find what it caused downstream -- still goes
    through `_match_sends_to_receives`, since no envelope-level link exists
    between a send at one agent and its receive at another (see
    module docstring / FINDINGS.md item 3's root-cause section). Any send
    or receive that can't be reached from the chosen root is returned in
    the second element rather than silently dropped.

    A trace's true entry point often has *no send-side span at all*: an
    external, un-instrumented caller (a scenario client, a real ASI:One
    user) reaches the traced system with a raw `ctx.send`, not
    `traced_send`, so only the receiving agent's `@trace`-recorded receive
    span exists for that first hop. Root candidates are therefore both
    NULL-parent sends (an entry point that *did* use `traced_send`, or a
    detached timer/background job) *and* receives with no matched send at
    all -- whichever is chronologically earliest is the real root.
    """
    sends = _send_spans(spans)
    receives = _receive_spans(spans)
    if not any(s.get("parent_span_id") for s in sends):
        return None, []

    spans_by_id = {s["id"]: s for s in spans}
    matches = _match_sends_to_receives(spans)
    matched_receive_ids = {r["id"] for r in matches.values() if r is not None}

    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for s in sends:
        parent_id = s.get("parent_span_id")
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(s)
    for group in children_by_parent.values():
        group.sort(key=lambda s: s["enqueued_at"])

    visited_sends: set[str] = set()
    visited_receives: set[str] = set()

    def build_send_node(send: dict[str, Any]) -> TreeNode:
        visited_sends.add(send["id"])
        receive = matches.get(send["id"])
        node = TreeNode(agent=send["dest_agent"])
        _apply_leg_fields(node, _edge_state(send, receive))
        if receive is not None:
            visited_receives.add(receive["id"])
            for child_send in children_by_parent.get(receive["id"], []):
                if child_send["id"] in visited_sends:
                    continue
                node.children.append(build_send_node(child_send))
        return node

    def build_receive_root_node(receive: dict[str, Any]) -> TreeNode:
        # An entry point with no send-side record at all -- still has
        # downstream children via parent_span_id, exactly like any node
        # built from a send's matched receive above.
        visited_receives.add(receive["id"])
        node = TreeNode(
            agent=receive["dest_agent"],
            message=receive.get("payload_summary"),
            payload_type=receive.get("payload_type"),
        )
        node.state = _bucket(receive.get("state"))
        if receive.get("acked_at") is not None:
            node.latency_ms = _span_latency_ms(receive)
        for child_send in children_by_parent.get(receive["id"], []):
            if child_send["id"] in visited_sends:
                continue
            node.children.append(build_send_node(child_send))
        return node

    send_roots = [s for s in sends if not s.get("parent_span_id") and s["source_agent"] != s["dest_agent"]]
    receive_roots = [r for r in receives if r["id"] not in matched_receive_ids]

    candidates: list[tuple[int, str, dict[str, Any]]] = [(s["enqueued_at"], "send", s) for s in send_roots]
    candidates += [(r["enqueued_at"], "receive", r) for r in receive_roots]
    if not candidates:
        return None, []
    candidates.sort(key=lambda c: c[0])

    _, root_kind, root_span = candidates[0]
    tree = TreeNode(agent=root_span["source_agent"])
    if root_kind == "send":
        tree.children.append(build_send_node(root_span))
    else:
        tree.children.append(build_receive_root_node(root_span))

    unparented: list[TreeNode] = []
    for _, kind, span in candidates[1:]:
        if kind == "send" and span["id"] not in visited_sends:
            unparented.append(build_send_node(span))
        elif kind == "receive" and span["id"] not in visited_receives:
            unparented.append(build_receive_root_node(span))

    # Any remaining sends never reached at all (parent points at a receive
    # that's itself unreachable, e.g. deep in a chain hanging off some
    # other unparented root) -- surfaced individually rather than dropped.
    for send_id in sorted(
        {s["id"] for s in sends} - visited_sends,
        key=lambda sid: spans_by_id[sid]["enqueued_at"],
    ):
        if send_id not in visited_sends:
            unparented.append(build_send_node(spans_by_id[send_id]))

    return tree, unparented


def build_interaction_tree(spans: list[dict[str, Any]]) -> tuple[Optional[TreeNode], list[TreeNode]]:
    """The one causal interaction tree for a trace: `(root, unparented)`.

    `root` is rooted at the trace's true entry point -- the span(s) with no
    causal parent (see `_build_causal_tree`) -- not a caller-supplied "hub"
    guess, so it nests to whatever real depth the data has (a deep pipeline
    with fan-out at multiple levels renders as one tree, not a flat
    2-level guess). `unparented` holds any material that couldn't be
    reached from that root -- a NULL-parent send that isn't the chosen
    root (e.g. a timer-fired send with no handler context), or a receive
    with no matched send -- so nothing is ever silently dropped.

    Falls back to the old busiest-source-guess tree
    (`_build_legacy_fanout_tree`) when no span in `spans` carries any
    `parent_span_id` at all, so databases recorded before this column
    existed keep rendering (with `unparented` always empty in that case,
    matching the old function's behavior).
    """
    if not spans:
        return None, []

    causal_tree, unparented = _build_causal_tree(spans)
    if causal_tree is not None:
        return causal_tree, unparented

    hub = _flat_busiest_hub(spans)
    if hub is None:
        return None, []
    return _build_legacy_fanout_tree(spans, hub), []


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


@dataclass
class OverviewNode:
    """One box in the whole-trace overview diagram -- one per *agent*, not
    per tree node. `depth` is the column a layered left-to-right layout
    should place it in (see `network_canvas.build_layered_topology`).
    """

    agent: str
    depth: int
    state: LegState
    reason: str | None = None


@dataclass
class OverviewEdge:
    """One box-to-box connection -- one per real dispatch, request and
    response already merged (see `build_overview_graph`'s docstring for
    why a reply is never its own edge). `forward` is False for an edge
    whose destination sits at the same or an earlier depth than its source
    (a retry dispatched back to an agent that already has a box, e.g.
    qa_critic -> brand_lead) -- the layout routes those differently so
    they're visibly distinct from the primary left-to-right flow, never
    hidden.
    """

    source: str
    dest: str
    state: LegState
    latency_ms: int | None
    reason: str | None
    forward: bool


def build_overview_graph(tree: TreeNode) -> tuple[list[OverviewNode], list[OverviewEdge]]:
    """Collapse a causal tree into a deduplicated agent graph: one box per
    agent that participated, one edge per logical call -- for a
    whole-trace overview where every participant needs to fit on screen at
    once, not a chain of nested tree nodes.

    Two separate collapses happen, both because the causal tree (see
    `build_interaction_tree`) represents a *reply* the same way it
    represents any other dispatch -- its own TreeNode, one level deeper
    than the call it's answering:

    1. **Reply collapse (cascading).** A node whose agent equals *its own
       immediate grandparent's* agent (raw parent-of-parent, in the
       original, uncollapsed tree shape) is that grandparent's reply
       coming back (e.g. `orchestrator -> brand_lead -> orchestrator` --
       brand_lead replying). Its state/latency already fully describes
       the round trip via `_edge_state` on the *original* dispatch edge,
       so it contributes no edge/box of its own -- otherwise every
       ordinary request/response pair doubles the apparent depth. This is
       deliberately checked one level at a time, not against the whole
       ancestor chain: matching *any* prior ancestor would wrongly treat
       an agent dispatching to someone who merely appeared earlier in the
       trace for an unrelated reason as "a reply" (e.g. `payment_gate`
       dispatching `RequestPayment` to `gateway` isn't a reply just
       because `gateway` sits higher up the tree). What *does* need to
       cascade is a reply passing back through an intermediate
       dispatcher (worker -> sub-dispatcher -> the sub-dispatcher's own
       caller, e.g. `market_scout_3 -> market_competitor_finder ->
       market_lead`) -- each leg of that chain is its own one-level
       reply, checked against whatever grandparent *that specific agent*
       was first discovered under (`_grandparent_of`), not the raw
       ancestor of the node currently being visited. Whatever a collapsed
       reply dispatches *further* (e.g. brand_lead -> assembler, sent
       while "replying" to the original request) reparents onto the
       matched grandparent's box, since that's the box a further dispatch
       is causally leaving from.

    2. **Node dedup, edges kept distinct.** The same agent can be
       dispatched to more than once (a QA retry, or a multi-message
       protocol bouncing between the same two parties, e.g. Payment
       Protocol's `RequestPayment`/`CompletePayment` both running
       `payment_gate -> gateway`) -- one *box* represents every occurrence
       of that agent, positioned at its first occurrence's depth and
       colored by its most recent one (a retry that eventually succeeded
       should read as succeeded, not flagged failed forever because
       attempt 1 was). But every real (non-reply) dispatch still gets its
       *own* edge, even when another edge already connects the same two
       boxes -- collapsing to one edge per box pair would silently drop
       whichever of two same-direction messages came first (exactly the
       "no hidden children" this whole rewrite exists to fix). Full
       per-attempt detail remains in the tree view regardless; this is
       deliberately the lower-resolution overview.
    """
    nodes: dict[str, OverviewNode] = {tree.agent: OverviewNode(agent=tree.agent, depth=0, state="completed")}
    edges: list[OverviewEdge] = []
    # For each agent, the (agent, depth) it would itself be replying to --
    # i.e. whatever grandparent context was active when *that* agent was
    # first added as a real dispatch. Looked up when collapsing a reply so
    # the reparented children are checked against the right next level up,
    # not the raw ancestor of the node physically being visited.
    grandparent_of: dict[str, tuple[str, int] | None] = {tree.agent: None}

    def walk(
        node: TreeNode,
        parent_agent: str,
        depth: int,
        grandparent_ctx: tuple[str, int] | None,
    ) -> None:
        if grandparent_ctx is not None and node.agent == grandparent_ctx[0]:
            reply_to_agent, reply_to_depth = grandparent_ctx
            next_ctx = grandparent_of.get(reply_to_agent)
            for child in node.children:
                walk(child, reply_to_agent, reply_to_depth + 1, next_ctx)
            return

        state = node.state or "pending"
        if node.agent not in nodes:
            nodes[node.agent] = OverviewNode(agent=node.agent, depth=depth, state=state, reason=node.reason)
        else:
            # Keep the *shallowest* depth across every occurrence -- not
            # just the first one encountered. Depth-first traversal order
            # doesn't guarantee "first encountered" means "shallowest": a
            # retry nested deep under a *different* sibling's subtree (the
            # sibling whose own response happened to be what triggered the
            # retry-causing dispatch -- real causal information, not a
            # traversal artifact) can be reached before that same agent's
            # own ordinary, shallower dispatch is. Picking the minimum
            # keeps the box at its most natural position instead of
            # wherever DFS happened to see it first, and (since routing
            # below assumes a forward edge's dest is exactly one column
            # right of its source) keeps a stray deep occurrence from
            # forcing a same-source edge to visually span many columns.
            nodes[node.agent].depth = min(nodes[node.agent].depth, depth)
            # Most recent state/reason wins (a retry that eventually
            # succeeded should read as succeeded).
            nodes[node.agent].state = state
            nodes[node.agent].reason = node.reason

        # Always the *most recent* occurrence's context, so a reply
        # nested inside a later retry branch collapses against that
        # branch's real grandparent (e.g. qa_critic), not whatever this
        # agent's very first, unrelated dispatch happened to be a reply to.
        grandparent_of[node.agent] = (parent_agent, depth - 1)

        edges.append(
            OverviewEdge(
                source=parent_agent,
                dest=node.agent,
                state=state,
                latency_ms=node.latency_ms,
                reason=node.reason,
                forward=True,  # corrected once every node's final depth is known, below
            )
        )

        for child in node.children:
            walk(child, node.agent, depth + 1, (parent_agent, depth - 1))

    for child in tree.children:
        walk(child, tree.agent, 1, (tree.agent, 0))

    for edge in edges:
        edge.forward = nodes[edge.dest].depth > nodes[edge.source].depth

    return list(nodes.values()), edges


@dataclass
class Hop:
    """One logical message hop, deduplicated from its send/receive twin.

    `traced_send` and `@trace` each write their own span for the same
    logical hop -- the sender's send-side span (ack latency) and the
    receiver's receive-side span (handler processing time). Any renderer
    that lists "messages" (the live feed, `show`'s flat view, the TUI
    waterfall) wants one entry per hop, not one per span, or a single
    ping/pong exchange shows up as four lines instead of two.
    """

    id: str
    source: str
    dest: str
    payload_type: str
    message: str | None
    protocol: str | None
    detail: str | None
    state: str  # delivered | dropped | timeout | pending
    error: str | None
    enqueued_at: int
    acked_at: int | None
    latency_ms: int | None
    source_registered: bool | None = None
    dest_registered: bool | None = None


def _direction_of(span: dict[str, Any]) -> str:
    return span.get("direction") or "send"


def build_hops(spans: list[dict[str, Any]]) -> list[Hop]:
    """Merge send/receive twins into one Hop per logical message, chronological.

    Matching (`_match_sends_to_receives`) pairs a "send" span with the
    earliest not-yet-claimed "receive" span sharing the same (source, dest,
    payload_type), same as before, but ranks same-key sends by their causal
    parent's timestamp when known (`parent_span_id`, see
    `recorder.traced_send`) rather than the send's own possibly-jittered
    insert time -- this is what keeps a retry's two attempts from getting
    their latencies crossed (FINDINGS.md item 3) when parentage is
    available; spans with no parentage fall back to the original
    timing-only behavior. A send with no matching receive (a failed send
    never arrives) becomes a hop on its own, using the send span's own
    timing -- so a dropped/timeout hop's latency is genuinely "time to
    failure", not a round trip.
    """
    sends = _send_spans(spans)
    receives = _receive_spans(spans)
    matches = _match_sends_to_receives(spans)

    hops: list[Hop] = []

    for send in sends:
        receive = matches.get(send["id"])

        if receive is not None and receive.get("state") == "delivered":
            state = receive["state"]
            acked_at = receive.get("acked_at")
            error = receive.get("error")
        else:
            state = send["state"]
            acked_at = send.get("acked_at")
            error = send.get("error")

        latency_ms = (acked_at - send["enqueued_at"]) if acked_at is not None else None

        hops.append(
            Hop(
                id=send["id"],
                source=send["source_agent"],
                dest=send["dest_agent"],
                payload_type=send["payload_type"],
                message=send.get("payload_summary"),
                protocol=send.get("protocol"),
                detail=send.get("detail"),
                state=state,
                error=error,
                enqueued_at=send["enqueued_at"],
                acked_at=acked_at,
                latency_ms=latency_ms,
                source_registered=send.get("source_registered"),
                dest_registered=send.get("dest_registered"),
            )
        )

    # Receive spans with no send counterpart (e.g. a raw ctx.send that
    # wasn't wrapped in traced_send, or older pre-migration data) still get
    # a hop rather than being silently dropped.
    matched_receive_ids = {r["id"] for r in matches.values() if r is not None}
    for receive in receives:
        if receive["id"] in matched_receive_ids:
            continue
        acked_at = receive.get("acked_at")
        hops.append(
            Hop(
                id=receive["id"],
                source=receive["source_agent"],
                dest=receive["dest_agent"],
                payload_type=receive["payload_type"],
                message=receive.get("payload_summary"),
                protocol=receive.get("protocol"),
                detail=receive.get("detail"),
                state=receive["state"],
                error=receive.get("error"),
                enqueued_at=receive["enqueued_at"],
                acked_at=acked_at,
                latency_ms=(acked_at - receive["enqueued_at"]) if acked_at is not None else None,
                source_registered=receive.get("source_registered"),
                dest_registered=receive.get("dest_registered"),
            )
        )

    hops.sort(key=lambda h: h.enqueued_at)
    return hops


def _bucket(state: str | None) -> str:
    if state in ("completed", "delivered"):
        return "completed"
    if state in ("failed", "dropped", "timeout"):
        return "failed"
    return "pending"


def _tree_edge_states(tree: TreeNode) -> list[dict[str, Any]]:
    """One unit per dispatch edge anywhere in the causal tree (every node
    except the synthetic root, which represents the external actor and
    carries no state of its own) -- the full-depth equivalent of a hub
    leg. `TraceState`'s rollup uses this whenever a tree exists so
    completed/failed/pending reflect the trace's *entire* causal chain
    (e.g. `ops_insurance` failing two hops below the hub, or a lead that
    forwards to `assembler` instead of replying to its own caller) instead
    of only the hub's own direct children -- which is what `legs` /
    `build_hub_legs` deliberately stays scoped to, since that's a
    different, intentionally-shallow view (the linear waterfall), not the
    rollup.
    """
    units: list[dict[str, Any]] = []

    def walk(node: TreeNode) -> None:
        for child in node.children:
            units.append({"state": child.state})
            walk(child)

    walk(tree)
    return units


@dataclass
class TraceState:
    """Single computed view of one trace -- the one thing every renderer
    (diagram, table, detail bar, feed, sidebar) should read from, so they
    can't disagree about what happened. Alias-free and DB-free like the
    rest of this module; callers apply display names and do I/O.
    """

    shape: str
    hub: str | None
    hops: list[Hop]
    legs: list[dict[str, Any]]  # only meaningful when shape == HUB
    tree: TreeNode | None  # the causal dispatch tree, whenever one exists (see build_interaction_tree)
    unparented: list[TreeNode]  # material that couldn't be reached from `tree`'s root
    participants: list[str]
    started_at: int
    duration_ms: int
    completed: int
    failed: int
    pending: int
    total: int


def build_trace_state(spans: list[dict[str, Any]], hub_hint: str | None = None) -> TraceState:
    """Build the one TraceState a live view renders from.

    `hub_hint`, if given, unconditionally wins over auto-classification --
    it lets a caller force hub-style rendering for a known orchestrator
    address even before enough spans have arrived for
    `classify_trace_shape` to infer it on its own. Callers decide *when*
    to pass it (e.g. only once the watch setup has 3+ agents and a known
    orchestrator); this function just applies it once given.

    `tree`/`unparented` are built unconditionally (not just for `shape ==
    HUB`) -- the causal tree (`build_interaction_tree`) is a property of
    the trace's real parentage, independent of the peer/hub/multi_level
    classification used for the linear waterfall and hub-legs views.

    `completed`/`failed`/`pending`/`total` come from the causal tree too
    (`_tree_edge_states`), not from `legs` -- `hub_hint` can force `shape`
    to `HUB` for rendering purposes well before the tree's real depth is
    knowable, and even once it is, `legs`' own dispatch/reply model only
    ever looks at the hub's *direct* children, so a trace where a lead
    forwards to `assembler` instead of replying to its own caller, or
    where a failure happens several hops below the hub (`ops_insurance`
    under `ops_lead` under `orchestrator`), would read as an all-pending,
    zero-failure rollup despite completing normally with a real failure
    inside it. The tree has no such blind spot -- every edge's own state
    comes from whether *that specific hop* delivered (`_edge_state`).
    """
    if not spans:
        return TraceState(
            shape=PEER,
            hub=None,
            hops=[],
            legs=[],
            tree=None,
            unparented=[],
            participants=[],
            started_at=0,
            duration_ms=0,
            completed=0,
            failed=0,
            pending=0,
            total=0,
        )

    shape, detected_hub = classify_trace_shape(spans)
    hub = hub_hint or detected_hub
    if hub:
        shape = HUB

    hops = build_hops(spans)
    legs = build_hub_legs(spans, hub) if hub else []
    tree, unparented = build_interaction_tree(spans)

    participants: list[str] = []
    for s in spans:
        for addr in (s["source_agent"], s["dest_agent"]):
            if addr not in participants:
                participants.append(addr)

    started_at = min(s["enqueued_at"] for s in spans)
    ended_at = max((s.get("acked_at") or s["enqueued_at"]) for s in spans)

    # Rollup basis, in priority order: the real causal tree whenever one
    # exists (independent of `shape`/`hub_hint` -- see this function's own
    # docstring on why those can't be trusted to reflect real depth), else
    # `legs` for a hub trace with no parentage at all (pre-migration data,
    # matching the old behavior), else the flat hop list (peer trace).
    if tree is not None:
        units = _tree_edge_states(tree)
    elif shape == HUB:
        units = legs
    else:
        units = [{"state": h.state} for h in hops]
    completed = sum(1 for u in units if _bucket(u["state"]) == "completed")
    failed = sum(1 for u in units if _bucket(u["state"]) == "failed")
    pending = sum(1 for u in units if _bucket(u["state"]) == "pending")

    return TraceState(
        shape=shape,
        hub=hub,
        hops=hops,
        legs=legs,
        tree=tree,
        unparented=unparented,
        participants=participants,
        started_at=started_at,
        duration_ms=max(ended_at - started_at, 0),
        completed=completed,
        failed=failed,
        pending=pending,
        total=len(units),
    )
