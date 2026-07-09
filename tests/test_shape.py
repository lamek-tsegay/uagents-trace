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
    build_trace_state,
    classify_trace_shape,
    tree_node_to_dict,
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
        tree = build_interaction_tree(spans, "ORCH")
        self.assertEqual(tree.agent, "ORCH")
        self.assertEqual(len(tree.children), 3)
        by_agent = {c.agent: c for c in tree.children}
        self.assertEqual(by_agent["SUB1"].state, "completed")
        self.assertEqual(by_agent["SUB1"].latency_ms, 15)
        self.assertEqual(by_agent["SUB2"].state, "failed")
        self.assertIn("resolve", by_agent["SUB2"].reason or "")
        self.assertEqual(by_agent["SUB3"].state, "pending")

    def test_nested_fan_out(self):
        spans = [
            span("ORCH", "SUB1", payload_type="Task", enqueued_at=0, acked_at=5, state="delivered"),
            span("SUB1", "SUB1A", payload_type="Task", enqueued_at=10, acked_at=15, state="delivered"),
            span("SUB1A", "SUB1", payload_type="Result", enqueued_at=15, acked_at=25, state="delivered"),
            span("SUB1", "ORCH", payload_type="Result", enqueued_at=30, acked_at=40, state="delivered"),
        ]
        tree = build_interaction_tree(spans, "ORCH")
        self.assertEqual(len(tree.children), 1)
        sub1 = tree.children[0]
        self.assertEqual(sub1.agent, "SUB1")
        self.assertEqual(sub1.state, "completed")
        self.assertEqual(len(sub1.children), 1)
        self.assertEqual(sub1.children[0].agent, "SUB1A")
        self.assertEqual(sub1.children[0].state, "completed")

    def test_tree_node_to_dict(self):
        tree = TreeNode(agent="ORCH", children=[TreeNode(agent="SUB1", state="pending")])
        data = tree_node_to_dict(tree)
        self.assertEqual(data["agent"], "ORCH")
        self.assertEqual(data["children"][0]["agent"], "SUB1")
        self.assertEqual(data["children"][0]["state"], "pending")


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
