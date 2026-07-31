import itertools
import unittest

from uagents_trace.shape import (
    HUB,
    MULTI_LEVEL,
    PEER,
    TreeNode,
    build_hops,
    build_hub_legs,
    build_interaction_tree,
    build_overview_graph,
    build_trace_state,
    classify_trace_shape,
    tree_node_to_dict,
    truncate_tree_message,
)

_span_ids = itertools.count()


def span(
    source,
    dest,
    state="delivered",
    payload_type="Msg",
    enqueued_at=0,
    acked_at=None,
    error=None,
    direction="send",
    payload_summary=None,
    span_id=None,
    parent_span_id=None,
):
    return {
        "id": span_id or f"span-{next(_span_ids)}",
        "source_agent": source,
        "dest_agent": dest,
        "state": state,
        "payload_type": payload_type,
        "enqueued_at": enqueued_at,
        "acked_at": acked_at,
        "error": error,
        "direction": direction,
        "payload_summary": payload_summary,
        "parent_span_id": parent_span_id,
    }


class ClassifyTraceShapeTests(unittest.TestCase):
    def test_no_spans_is_peer_by_default(self):
        self.assertEqual(classify_trace_shape([]), (PEER, None))

    def test_ping_pong_is_peer(self):
        spans = [
            span("A", "B", payload_type="Ping", enqueued_at=0, acked_at=10),
            span("B", "A", payload_type="Pong", enqueued_at=10, acked_at=20),
        ]
        shape, hub = classify_trace_shape(spans)
        self.assertEqual(shape, PEER)
        self.assertIsNone(hub)

    def test_one_source_to_four_dests_is_hub(self):
        spans = [span("ORCH", f"SUB{i}", enqueued_at=0, acked_at=10) for i in range(4)]
        shape, hub = classify_trace_shape(spans)
        self.assertEqual(shape, HUB)
        self.assertEqual(hub, "ORCH")

    def test_chain_is_multi_level(self):
        # A -> B -> C: each agent only ever talks to one distinct dest, so
        # this isn't a clean hub -- and there are 3 agents, so it isn't peer.
        spans = [
            span("A", "B", enqueued_at=0, acked_at=10),
            span("B", "C", enqueued_at=10, acked_at=20),
        ]
        shape, hub = classify_trace_shape(spans)
        self.assertEqual(shape, MULTI_LEVEL)
        self.assertIsNone(hub)


class BuildHubLegsTests(unittest.TestCase):
    def test_completed_and_failed_legs(self):
        spans = [
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=5, state="delivered"),
            span("SUB1", "ORCH", payload_type="Result", enqueued_at=5, acked_at=15, state="delivered"),
            span(
                "ORCH",
                "SUB2",
                payload_type="Task",
                enqueued_at=0,
                acked_at=200,
                state="dropped",
                error="Could not resolve destination endpoint.",
            ),
        ]
        legs = build_hub_legs(spans, "ORCH")
        by_subagent = {leg["subagent"]: leg for leg in legs}

        self.assertEqual(by_subagent["SUB1"]["state"], "completed")
        self.assertEqual(by_subagent["SUB1"]["latency_ms"], 15)
        self.assertEqual(by_subagent["SUB1"]["dispatch_ms"], 5)
        self.assertEqual(by_subagent["SUB1"]["reply_ms"], 10)
        self.assertEqual(by_subagent["SUB2"]["state"], "failed")
        self.assertEqual(by_subagent["SUB2"]["dispatch_ms"], 200)
        self.assertIn("resolve", by_subagent["SUB2"]["reason"])

    def test_pending_leg_with_no_reply_and_no_failure(self):
        spans = [span("ORCH", "SUB1", payload_type="Task", state="pending", enqueued_at=0, acked_at=None)]
        legs = build_hub_legs(spans, "ORCH")
        self.assertEqual(legs[0]["state"], "pending")


class BuildInteractionTreeTests(unittest.TestCase):
    def test_fan_out_completed_and_failed(self):
        spans = [
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=5, state="delivered"),
            span("SUB1", "ORCH", payload_type="Result", enqueued_at=5, acked_at=15, state="delivered"),
            span(
                "ORCH",
                "SUB2",
                payload_type="Task",
                enqueued_at=0,
                acked_at=200,
                state="dropped",
                error="Could not resolve destination endpoint.",
            ),
            span("ORCH", "SUB3", payload_type="Task", enqueued_at=0, acked_at=5, state="delivered"),
        ]
        tree, unparented = build_interaction_tree(spans)
        self.assertEqual(unparented, [])
        self.assertEqual(tree.agent, "ORCH")
        self.assertEqual(len(tree.children), 3)
        by_agent = {c.agent: c for c in tree.children}
        self.assertEqual(by_agent["SUB1"].state, "completed")
        self.assertEqual(by_agent["SUB1"].latency_ms, 15)
        self.assertEqual(by_agent["SUB2"].state, "failed")
        self.assertIn("resolve", by_agent["SUB2"].reason or "")
        self.assertEqual(by_agent["SUB3"].state, "pending")

    def test_nested_fan_out_without_parentage_uses_legacy_busiest_source(self):
        # No parent_span_id anywhere -- pre-migration data. Falls back to
        # the old blind busiest-source heuristic (SUB1 sends twice, more
        # than ORCH's one send), not a caller-supplied root -- see
        # test_nested_fan_out_with_parentage_roots_at_true_entry_point
        # below for the real, causally-correct equivalent.
        spans = [
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=5, state="delivered"),
            span("SUB1", "SUB1A", payload_type="Task", enqueued_at=10, acked_at=15, state="delivered"),
            span("SUB1A", "SUB1", payload_type="Result", enqueued_at=15, acked_at=25, state="delivered"),
            span("SUB1", "ORCH", payload_type="Result", enqueued_at=30, acked_at=40, state="delivered"),
        ]
        tree, unparented = build_interaction_tree(spans)
        self.assertEqual(unparented, [])
        self.assertEqual(tree.agent, "SUB1")
        by_agent = {c.agent: c for c in tree.children}
        self.assertIn("SUB1A", by_agent)
        self.assertEqual(by_agent["SUB1A"].state, "completed")

    def test_nested_fan_out_with_parentage_roots_at_true_entry_point(self):
        # CLIENT -> ORCH -> SUB1 -> SUB1A, each hop's send carrying the
        # causing receive as its parent (see recorder.traced_send). The
        # tree must root at CLIENT (the true entry point, no parent at
        # all) and nest to the real depth -- this is the shape the repro
        # trace needs (gateway -> parser -> ... -> market_scout_N).
        r_orch = span("CLIENT", "ORCH", payload_type="Task", enqueued_at=2, acked_at=5, direction="receive", span_id="r-orch")
        r_sub1 = span("ORCH", "SUB1", payload_type="Task", enqueued_at=8, acked_at=10, direction="receive", span_id="r-sub1")
        r_sub1a = span("SUB1", "SUB1A", payload_type="Task", enqueued_at=12, acked_at=20, direction="receive", span_id="r-sub1a")
        spans = [
            span("CLIENT", "ORCH", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-client"),
            r_orch,
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=5, acked_at=8, span_id="s-orch-sub1", parent_span_id="r-orch"),
            r_sub1,
            span("SUB1", "SUB1A", payload_type="Task", enqueued_at=10, acked_at=12, span_id="s-sub1-sub1a", parent_span_id="r-sub1"),
            r_sub1a,
        ]
        tree, unparented = build_interaction_tree(spans)
        self.assertEqual(unparented, [])
        self.assertEqual(tree.agent, "CLIENT")
        self.assertEqual(len(tree.children), 1)
        orch = tree.children[0]
        self.assertEqual(orch.agent, "ORCH")
        self.assertEqual(orch.state, "completed")
        self.assertEqual(len(orch.children), 1)
        sub1 = orch.children[0]
        self.assertEqual(sub1.agent, "SUB1")
        self.assertEqual(sub1.state, "completed")
        self.assertEqual(len(sub1.children), 1)
        sub1a = sub1.children[0]
        self.assertEqual(sub1a.agent, "SUB1A")
        self.assertEqual(sub1a.state, "completed")
        self.assertEqual(sub1a.latency_ms, 20 - 10)

    def test_roots_at_an_unmatched_receive_when_the_entry_point_has_no_send(self):
        # A real, common case: the external caller (a scenario client, a
        # real ASI:One user) reaches the traced system via a raw ctx.send,
        # not traced_send -- so the entry hop has *no send-side span at
        # all*, only the receiving agent's @trace-recorded receive. The
        # root must still be found (as this receive, since nothing claims
        # it as a match), not fall through to the legacy busiest-source
        # guess just because there's no NULL-parent send to anchor on.
        r_gateway = span("CLIENT", "GATEWAY", payload_type="Chat", enqueued_at=0, acked_at=3, direction="receive", span_id="r-gateway")
        r_worker = span("GATEWAY", "WORKER", payload_type="Task", enqueued_at=5, acked_at=9, direction="receive", span_id="r-worker")
        spans = [
            r_gateway,
            span("GATEWAY", "WORKER", payload_type="Task", enqueued_at=3, acked_at=5, span_id="s-gw-worker", parent_span_id="r-gateway"),
            r_worker,
        ]
        tree, unparented = build_interaction_tree(spans)
        self.assertEqual(unparented, [])
        self.assertEqual(tree.agent, "CLIENT")
        self.assertEqual(len(tree.children), 1)
        gateway_node = tree.children[0]
        self.assertEqual(gateway_node.agent, "GATEWAY")
        self.assertEqual(gateway_node.state, "completed")
        self.assertEqual(len(gateway_node.children), 1)
        self.assertEqual(gateway_node.children[0].agent, "WORKER")
        self.assertEqual(gateway_node.children[0].state, "completed")

    def test_unparented_material_is_surfaced_not_dropped(self):
        # A NULL-parent send that isn't the chosen root (e.g. a detached
        # timer job) and a receive with no matched send at all (e.g. a raw
        # ctx.send that bypassed traced_send) both must show up in
        # `unparented`, never silently vanish.
        spans = [
            span("CLIENT", "ORCH", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-client"),
            span("CLIENT", "ORCH", payload_type="Task", enqueued_at=2, acked_at=4, direction="receive", span_id="r-orch"),
            # At least one real parent link, so this trace is recognized as
            # having causal data at all (an all-NULL-parent trace falls
            # back to the legacy path instead -- see the "without
            # parentage" tests above).
            span("ORCH", "DOWNSTREAM", payload_type="Task", enqueued_at=5, acked_at=6, parent_span_id="r-orch"),
            span("TIMER", "OTHER", payload_type="Tick", enqueued_at=50, acked_at=55, span_id="s-timer"),
            span("RAW", "ORCH", payload_type="Untracked", enqueued_at=60, acked_at=None, state="pending", direction="receive", span_id="r-raw"),
        ]
        tree, unparented = build_interaction_tree(spans)
        self.assertEqual(tree.agent, "CLIENT")
        unparented_agents = {n.agent for n in unparented}
        self.assertEqual(unparented_agents, {"OTHER", "ORCH"})  # TIMER's send, and the raw receive
        self.assertEqual(len(unparented), 2)

    def test_pipeline_edge_completes_without_a_reply_to_its_own_caller(self):
        # qa_critic -> delivery never replies back to qa_critic (it forwards
        # onward to user_proxy instead) -- the edge must still read
        # "completed" once delivery has received it, not hang at "pending"
        # forever waiting for a reply that was never going to come back to
        # THIS caller (see Task 5 / FINDINGS.md).
        r_a = span("EXT", "A", payload_type="Task", enqueued_at=0, acked_at=2, direction="receive", span_id="r-a")
        r_b = span("A", "B", payload_type="Forward", enqueued_at=5, acked_at=8, direction="receive", span_id="r-b")
        r_c = span("B", "C", payload_type="Forward", enqueued_at=10, acked_at=15, direction="receive", span_id="r-c")
        spans = [
            span("EXT", "A", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-ext"),
            r_a,
            span("A", "B", payload_type="Forward", enqueued_at=2, acked_at=5, span_id="s-a-b", parent_span_id="r-a"),
            r_b,
            span("B", "C", payload_type="Forward", enqueued_at=8, acked_at=10, span_id="s-b-c", parent_span_id="r-b"),
            r_c,
        ]
        tree, _ = build_interaction_tree(spans)
        a_node = tree.children[0]
        b_node = a_node.children[0]
        c_node = b_node.children[0]
        self.assertEqual(a_node.state, "completed")
        self.assertEqual(b_node.state, "completed")
        self.assertEqual(c_node.state, "completed")

    def test_send_with_no_receive_at_all_completes_on_its_own_ack(self):
        # delivery -> user_proxy: user_proxy is an external chat client
        # whose handler is never wrapped in @trace, so no receive-side
        # span for this hop will ever exist. The send's own successful ack
        # is the only signal available and it says delivered -- must not
        # hang at "pending" forever (Task 5 / FINDINGS.md).
        r_a = span("EXT", "A", payload_type="Task", enqueued_at=0, acked_at=2, direction="receive", span_id="r-a")
        spans = [
            span("EXT", "A", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-ext"),
            r_a,
            span(
                "A", "UNTRACED_CLIENT", payload_type="Reply", enqueued_at=5, acked_at=9,
                state="delivered", parent_span_id="r-a",
            ),
        ]
        tree, _ = build_interaction_tree(spans)
        a_node = tree.children[0]
        reply_node = a_node.children[0]
        self.assertEqual(reply_node.agent, "UNTRACED_CLIENT")
        self.assertEqual(reply_node.state, "completed")
        self.assertEqual(reply_node.latency_ms, 4)

    def test_tree_node_to_dict(self):
        tree = TreeNode(agent="ORCH", children=[TreeNode(agent="SUB1", state="pending")])
        data = tree_node_to_dict(tree)
        self.assertEqual(data["agent"], "ORCH")
        self.assertEqual(data["children"][0]["agent"], "SUB1")
        self.assertEqual(data["children"][0]["state"], "pending")


class BuildOverviewGraphTests(unittest.TestCase):
    def _nodes_edges(self, tree):
        nodes, edges = build_overview_graph(tree)
        return {n.agent: n for n in nodes}, edges

    def test_simple_reply_collapses_to_one_edge_no_extra_box(self):
        # ORCH -> WORKER -> ORCH(reply) -> nothing further. The reply must
        # not become its own box or edge -- a plain request/response pair
        # is one logical call, one edge.
        reply = TreeNode(agent="ORCH", state="completed", latency_ms=10)
        worker = TreeNode(agent="WORKER", state="completed", latency_ms=50, children=[reply])
        tree = TreeNode(agent="ORCH", children=[worker])

        nodes, edges = self._nodes_edges(tree)
        self.assertEqual(set(nodes), {"ORCH", "WORKER"})
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0].source, edges[0].dest), ("ORCH", "WORKER"))

    def test_cascading_reply_through_a_sub_dispatcher_collapses_fully(self):
        # LEAD -> FINDER -> SCOUT -> FINDER(reply) -> LEAD(reply) -> ASSEMBLER.
        # Mirrors market_lead -> market_competitor_finder -> market_scout_3
        # -> [reply] -> [reply] -> assembler. Both replies must collapse
        # (FINDER's own reply to LEAD is itself a reply-of-a-reply), and
        # ASSEMBLER must end up dispatched directly from LEAD, not FINDER.
        assembler = TreeNode(agent="ASSEMBLER", state="completed", latency_ms=3)
        lead_reply = TreeNode(agent="LEAD", state="completed", latency_ms=20, children=[assembler])
        finder_reply = TreeNode(agent="FINDER", state="completed", latency_ms=8, children=[lead_reply])
        scout = TreeNode(agent="SCOUT", state="completed", latency_ms=100, children=[finder_reply])
        finder = TreeNode(agent="FINDER", state="completed", latency_ms=150, children=[scout])
        lead = TreeNode(agent="LEAD", state="completed", latency_ms=200, children=[finder])
        tree = TreeNode(agent="ORCH", children=[lead])

        nodes, edges = self._nodes_edges(tree)
        self.assertEqual(set(nodes), {"ORCH", "LEAD", "FINDER", "SCOUT", "ASSEMBLER"})
        pairs = {(e.source, e.dest) for e in edges}
        self.assertEqual(pairs, {("ORCH", "LEAD"), ("LEAD", "FINDER"), ("FINDER", "SCOUT"), ("LEAD", "ASSEMBLER")})
        # Neither reply leg (FINDER->LEAD, SCOUT->FINDER) survives as an edge.
        self.assertNotIn(("FINDER", "LEAD"), pairs)
        self.assertNotIn(("SCOUT", "FINDER"), pairs)

    def test_non_reply_dispatch_to_an_earlier_agent_is_not_collapsed(self):
        # A -> B -> A(genuine new dispatch, NOT a reply -- B is calling A
        # about something new, not answering A's original call). Mirrors
        # payment_gate dispatching RequestPayment to gateway well after
        # gateway received the original chat message -- gateway sitting
        # earlier in the tree must not make this look like a reply.
        # Distinguished from a real reply by NOT being B's direct child of
        # A's own dispatch (there's an intervening real node, C, so B's
        # grandparent is C, not A).
        real_new_call = TreeNode(agent="A", state="completed", latency_ms=7)
        c = TreeNode(agent="C", state="completed", latency_ms=40, children=[real_new_call])
        b = TreeNode(agent="B", state="completed", latency_ms=90, children=[c])
        a = TreeNode(agent="A", state="completed", latency_ms=120, children=[b])
        tree = TreeNode(agent="ROOT", children=[a])

        nodes, edges = self._nodes_edges(tree)
        pairs = [(e.source, e.dest) for e in edges]
        # A->B, B->C are real; C->A is a GENUINE new dispatch (C's
        # grandparent is B, not A) and must survive as its own edge, not
        # be silently treated as A's reply just because A appears earlier.
        self.assertIn(("ROOT", "A"), pairs)
        self.assertIn(("A", "B"), pairs)
        self.assertIn(("B", "C"), pairs)
        self.assertIn(("C", "A"), pairs)
        self.assertEqual(len(edges), 4)

    def test_same_pair_multiple_messages_all_kept_not_collapsed_to_one(self):
        # Payment Protocol shape: ROOT -> X -> W -> Y (RequestPayment: Y
        # dispatches to X -- real, since Y's grandparent is W, not X) -> X
        # -> Y (CommitPayment: X's reply to Y -- collapses, since its
        # grandparent IS Y) -> {X (CompletePayment: Y's genuine further
        # dispatch back to X, sharing RequestPayment's (Y, X) direction --
        # must survive as its own edge, not overwrite the first), Z (a
        # genuinely new forward dispatch)}. Mirrors
        # payment_gate<->gateway's real 3-message exchange, reached via
        # parser as the real intermediate hop (W here).
        complete = TreeNode(agent="X", state="completed", latency_ms=6, payload_type="Complete")
        forward = TreeNode(agent="Z", state="completed", latency_ms=9, payload_type="Forward")
        commit = TreeNode(agent="Y", state="completed", latency_ms=19, payload_type="Commit", children=[complete, forward])
        request = TreeNode(agent="X", state="completed", latency_ms=16, payload_type="Request", children=[commit])
        y = TreeNode(agent="Y", state="completed", latency_ms=5, payload_type="Mid", children=[request])
        w = TreeNode(agent="W", state="completed", latency_ms=4, payload_type="Intermediate", children=[y])
        x = TreeNode(agent="X", state="completed", latency_ms=3, payload_type="Entry", children=[w])
        root = TreeNode(agent="ROOT", children=[x])

        nodes, edges = self._nodes_edges(root)
        pairs = [(e.source, e.dest, e.state) for e in edges]
        self.assertIn(("Y", "X", "completed"), pairs)  # RequestPayment-equivalent survives
        # CompletePayment-equivalent (Y -> X again) must ALSO survive, not
        # overwrite the first Y->X edge.
        self.assertEqual(sum(1 for s, d, _ in pairs if (s, d) == ("Y", "X")), 2)
        self.assertIn(("Y", "Z", "completed"), pairs)  # the genuinely-new forward dispatch
        # CommitPayment itself (X -> Y, the reply) never becomes its own edge.
        self.assertNotIn(("X", "Y", "completed"), pairs)

    def test_retry_dedupes_box_but_keeps_both_edges(self):
        # WORKER dispatched to twice (a QA-style retry) -- one box, but
        # both dispatches remain visible as separate edges; the box's
        # state reflects the most recent attempt.
        attempt2 = TreeNode(agent="WORKER", state="failed", reason="still bad", latency_ms=12)
        attempt1 = TreeNode(agent="WORKER", state="completed", latency_ms=8)
        tree = TreeNode(agent="ORCH", children=[attempt1, attempt2])

        nodes, edges = self._nodes_edges(tree)
        self.assertEqual(len(nodes), 2)  # ORCH, WORKER -- one box each
        self.assertEqual(nodes["WORKER"].state, "failed")  # most recent attempt wins
        self.assertEqual(len(edges), 2)  # both attempts kept as distinct edges

    def test_every_participant_gets_a_box_real_trace_shape(self):
        # No participant should ever be missing from the overview -- this
        # mirrors the "no hidden children" requirement at the graph level
        # (network_canvas's layout is a separate, later concern).
        spans = [
            span("EXT", "GW", payload_type="Chat", enqueued_at=0, acked_at=2, direction="receive", span_id="r-gw"),
            span("GW", "ORCH", payload_type="Task", enqueued_at=2, acked_at=4, span_id="s-orch", parent_span_id="r-gw"),
        ]
        r_orch = span("GW", "ORCH", payload_type="Task", enqueued_at=4, acked_at=6, direction="receive", span_id="r-orch")
        spans.append(r_orch)
        for i in range(3):
            spans.append(span("ORCH", f"SUB{i}", payload_type="Task", enqueued_at=6 + i, acked_at=8 + i, parent_span_id="r-orch"))
        tree, _ = build_interaction_tree(spans)
        nodes, edges = build_overview_graph(tree)
        self.assertEqual({n.agent for n in nodes}, {"EXT", "GW", "ORCH", "SUB0", "SUB1", "SUB2"})


class TruncateTreeMessageTests(unittest.TestCase):
    def test_short_message_passed_through(self):
        self.assertEqual(truncate_tree_message("hi bob"), "hi bob")

    def test_long_message_truncated_with_size_note(self):
        text = "x" * 500
        result = truncate_tree_message(text)
        self.assertLessEqual(len(result), 200)
        self.assertIn("+340 chars", result)

    def test_embedded_newlines_collapsed_to_one_line_even_under_the_limit(self):
        # A multi-paragraph reply (e.g. the final assembled ChatMessage)
        # can be well within the char limit yet still span many terminal
        # lines via real newlines -- must collapse to one line regardless.
        text = "Here's the starter kit:\n\n## Brand\n- brand.name: Foo\n\n## Market\n- market.seo: Bar"
        result = truncate_tree_message(text)
        self.assertNotIn("\n", result)


class BuildHopsTests(unittest.TestCase):
    def test_send_receive_twins_collapse_to_one_hop(self):
        # traced_send's send-side span and @trace's receive-side span
        # both describe the same logical message -- the "fast twin / slow
        # twin" pattern -- and must merge into a single hop, not render as
        # two separate messages.
        spans = [
            span("A", "B", payload_type="Task", direction="send", enqueued_at=100, acked_at=110, span_id="send-1"),
            span("A", "B", payload_type="Task", direction="receive", enqueued_at=110, acked_at=185, span_id="recv-1"),
        ]
        hops = build_hops(spans)
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0].latency_ms, 85)  # send enqueue (100) -> receive ack (185)
        self.assertEqual(hops[0].state, "delivered")

    def test_failed_send_with_no_receive_is_its_own_hop(self):
        spans = [
            span(
                "A",
                "B",
                payload_type="Task",
                direction="send",
                state="dropped",
                error="Unable to resolve destination endpoint",
                enqueued_at=0,
                acked_at=723,
            ),
        ]
        hops = build_hops(spans)
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0].state, "dropped")
        self.assertEqual(hops[0].latency_ms, 723)  # time-to-failure, not a round trip
        self.assertIn("resolve", hops[0].error)

    def test_hops_sorted_chronologically(self):
        spans = [
            span("A", "B", payload_type="Second", direction="send", enqueued_at=50, acked_at=60),
            span("A", "B", payload_type="First", direction="send", enqueued_at=0, acked_at=10),
        ]
        hops = build_hops(spans)
        self.assertEqual([h.payload_type for h in hops], ["First", "Second"])


class RetryHopMatchingTests(unittest.TestCase):
    """FINDINGS.md item 3: a retry's two attempts sharing the same
    (source, dest, payload_type) can get their send/receive pairing
    crossed if a later-caused attempt's write to the DB happens to land
    before an earlier-caused attempt's -- e.g. ordinary asyncio scheduling
    jitter on the recorder's own insert, not a business-logic race. Real
    Launchpad traffic never overlaps in *outcome* order (FINDINGS confirms
    build_hops never mis-pairs the three real retries it captured), but
    the underlying matching algorithm was never actually safe against it --
    this is that same class of bug, reproduced deterministically instead of
    relying on real timing to happen to go the right way.
    """

    def _jittered_retry_spans(self, *, with_parentage: bool) -> list[dict]:
        # P1 causes S1 (attempt 1); P2 causes S2 (attempt 2); P1 happens
        # before P2 (attempt 2 is a genuine retry of attempt 1). But S2's
        # own recorder-side insert (enqueued_at=10) lands *before* S1's
        # (enqueued_at=12) -- exactly the kind of write-order jitter that
        # breaks an algorithm relying on the send's own timestamp alone.
        p1 = span("HUB", "WORKER", payload_type="Retry", direction="receive", enqueued_at=0, span_id="p1")
        p2 = span("HUB", "WORKER", payload_type="Retry", direction="receive", enqueued_at=5, span_id="p2")
        s1 = span(
            "WORKER", "TARGET", payload_type="Request", direction="send",
            enqueued_at=12, acked_at=13, span_id="s1",
            parent_span_id="p1" if with_parentage else None,
        )
        s2 = span(
            "WORKER", "TARGET", payload_type="Request", direction="send",
            enqueued_at=10, acked_at=11, span_id="s2",
            parent_span_id="p2" if with_parentage else None,
        )
        # attempt 1's reply arrives first (r_early); attempt 2's arrives
        # later (r_late) -- the *correct* pairing is s1<->r_early,
        # s2<->r_late, matching which attempt actually caused which.
        r_early = span("WORKER", "TARGET", payload_type="Request", direction="receive", enqueued_at=15, acked_at=16, span_id="r_early")
        r_late = span("WORKER", "TARGET", payload_type="Request", direction="receive", enqueued_at=30, acked_at=31, span_id="r_late")
        return [p1, p2, s1, s2, r_early, r_late]

    def test_parent_link_keeps_jittered_retry_attempts_from_crossing(self):
        hops = build_hops(self._jittered_retry_spans(with_parentage=True))
        by_id = {h.id: h for h in hops}
        self.assertEqual(by_id["s1"].acked_at, 16)  # paired with r_early, not r_late
        self.assertEqual(by_id["s2"].acked_at, 31)  # paired with r_late, not r_early

    def test_without_parentage_the_same_jitter_can_still_cross(self):
        # Documents the accepted fallback: with no parent_span_id at all
        # (pre-migration data), matching is exactly the old timing-only
        # heuristic and remains exposed to this class of bug -- "fall back
        # to the current timing heuristic only where parent_span_id is
        # NULL" per the fix's own scope, not a claim that timing alone was
        # made safe.
        hops = build_hops(self._jittered_retry_spans(with_parentage=False))
        by_id = {h.id: h for h in hops}
        self.assertEqual(by_id["s1"].acked_at, 31)  # crossed: got the later reply
        self.assertEqual(by_id["s2"].acked_at, 16)  # crossed: got the earlier reply


class ClassifyTraceShapeWithParentageTests(unittest.TestCase):
    def test_genuine_flat_hub_still_classifies_hub_with_real_parentage(self):
        r_hub = span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, direction="receive", span_id="r-hub")
        spans = [
            span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-ext"),
            r_hub,
        ]
        for i in range(4):
            spans.append(
                span("HUB", f"SUB{i}", payload_type="Task", enqueued_at=5 + i, acked_at=8 + i, parent_span_id="r-hub")
            )
        shape, hub = classify_trace_shape(spans)
        self.assertEqual(shape, HUB)
        self.assertEqual(hub, "HUB")

    def test_hub_whose_legs_fan_out_further_is_multi_level_not_hub(self):
        # This is FINDINGS.md item 2's bug: a hub with >=2 legs where one
        # leg itself dispatches further must not read as a flat 2-level
        # hub just because it's the busiest source -- depth is a property
        # of the data now that real parentage exists.
        r_hub = span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, direction="receive", span_id="r-hub")
        r_sub0 = span("HUB", "SUB0", payload_type="Task", enqueued_at=5, acked_at=8, direction="receive", span_id="r-sub0")
        spans = [
            span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-ext"),
            r_hub,
            span("HUB", "SUB0", payload_type="Task", enqueued_at=5, acked_at=8, parent_span_id="r-hub"),
            r_sub0,
            span("HUB", "SUB1", payload_type="Task", enqueued_at=6, acked_at=9, parent_span_id="r-hub"),
            # SUB0 dispatches further -- this leg has its own child.
            span("SUB0", "SUB0A", payload_type="Task", enqueued_at=9, acked_at=12, parent_span_id="r-sub0"),
        ]
        shape, hub = classify_trace_shape(spans)
        self.assertEqual(shape, MULTI_LEVEL)
        self.assertIsNone(hub)


class BuildTraceStateTests(unittest.TestCase):
    def test_hub_rollup_is_fractional_not_binary(self):
        # 3 legs complete, 1 fails -- the rollup should read 3/4, not flip
        # to an all-or-nothing failure just because one leg is broken.
        spans = [
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=10),
            span("SUB1", "ORCH", payload_type="Result", enqueued_at=10, acked_at=20),
            span("ORCH", "SUB2", payload_type="Task", enqueued_at=0, acked_at=10),
            span("SUB2", "ORCH", payload_type="Result", enqueued_at=10, acked_at=20),
            span("ORCH", "SUB3", payload_type="Task", enqueued_at=0, acked_at=10),
            span("SUB3", "ORCH", payload_type="Result", enqueued_at=10, acked_at=20),
            span(
                "ORCH",
                "SUB4",
                payload_type="Task",
                state="dropped",
                error="Unable to resolve destination endpoint",
                enqueued_at=0,
                acked_at=700,
            ),
        ]
        state = build_trace_state(spans, hub_hint="ORCH")
        self.assertEqual(state.shape, HUB)
        self.assertEqual(state.hub, "ORCH")
        self.assertEqual(state.total, 4)
        self.assertEqual(state.completed, 3)
        self.assertEqual(state.failed, 1)
        self.assertEqual(state.pending, 0)

    def test_rollup_reflects_a_failure_the_hub_hint_forced_legs_view_cant_see(self):
        # A real Launchpad shape: EXT -> HUB -> LEAD -> WORKER, where WORKER
        # fails two hops below HUB, and LEAD never replies directly back to
        # HUB (it forwards elsewhere in a real system -- here it just
        # doesn't reply at all, same blind spot). hub_hint forces shape to
        # HUB even though classify_trace_shape alone would call this
        # multi_level; the rollup must still see the real failure via the
        # causal tree, not read "0/1, no failures" off HUB's own single
        # direct (permanently pending, by this model) leg.
        r_ext = span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, direction="receive", span_id="r-ext")
        r_lead = span("HUB", "LEAD", payload_type="Task", enqueued_at=3, acked_at=5, direction="receive", span_id="r-lead")
        spans = [
            span("EXT", "HUB", payload_type="Task", enqueued_at=0, acked_at=2, span_id="s-ext"),
            r_ext,
            span("HUB", "LEAD", payload_type="Task", enqueued_at=2, acked_at=3, span_id="s-hub-lead", parent_span_id="r-ext"),
            r_lead,
            span(
                "LEAD", "WORKER", payload_type="Task", enqueued_at=5, acked_at=700,
                state="dropped", error="Unable to resolve destination endpoint",
                span_id="s-lead-worker", parent_span_id="r-lead",
            ),
        ]
        state = build_trace_state(spans, hub_hint="HUB")
        self.assertEqual(state.shape, HUB)  # forced by the hint
        self.assertEqual(state.tree.agent, "EXT")  # but the tree itself is untouched by it
        self.assertEqual(state.total, 3)  # EXT->HUB, HUB->LEAD, LEAD->WORKER -- not just HUB's 1 direct leg
        self.assertEqual(state.completed, 2)
        self.assertEqual(state.failed, 1)
        self.assertEqual(state.pending, 0)

    def test_hub_hint_forces_hub_shape_before_enough_spans_arrive(self):
        # Only one dispatch has happened so far -- auto-classification
        # alone would call this "peer" (2 agents), but the wizard already
        # knows ORCH is the orchestrator.
        spans = [span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=None, state="pending")]
        state = build_trace_state(spans, hub_hint="ORCH")
        self.assertEqual(state.shape, HUB)

    def test_peer_rollup_counts_hops(self):
        spans = [
            span("A", "B", payload_type="Ping", enqueued_at=0, acked_at=10),
            span("B", "A", payload_type="Pong", enqueued_at=10, acked_at=20),
        ]
        state = build_trace_state(spans)
        self.assertEqual(state.shape, PEER)
        self.assertEqual(state.total, 2)
        self.assertEqual(state.completed, 2)
        self.assertEqual(state.failed, 0)

    def test_empty_spans(self):
        state = build_trace_state([])
        self.assertEqual(state.total, 0)
        self.assertEqual(state.hops, [])


if __name__ == "__main__":
    unittest.main()
