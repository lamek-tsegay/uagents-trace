import unittest

from uagents_trace.shape import HUB, MULTI_LEVEL, PEER, build_hub_legs, classify_trace_shape


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
):
    return {
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


if __name__ == "__main__":
    unittest.main()
