import unittest

from uagents import Model

from uagents_trace.live import (
    DEGRADED_MARKER,
    FAILURE_MARKER,
    LATENCY_BAR_EMPTY,
    LATENCY_BAR_FILLED,
    LATENCY_BAR_SCALE_MS,
    LATENCY_BAR_WIDTH,
    MUTED,
    _latency_bar,
    _sidebar_markup,
    build_hub_detail_summary,
    build_hub_leg_table,
    build_hub_network_diagram,
    build_hub_tree_diagram,
    build_peer_network_diagram,
    format_event_line,
    message_label,
    render_agent_box,
    sidebar_label,
)
from uagents_trace.network_canvas import ERROR, SUCCESS, WARN, format_ms
from uagents_trace.recorder import payload_summary
from uagents_trace.shape import build_hops, build_interaction_tree, build_trace_state


class Hello(Model):
    text: str
    count: int


def span(
    source,
    dest,
    payload_type="Hello",
    payload_summary=None,
    state="delivered",
    direction="send",
    enqueued_at=0,
    acked_at=None,
    error=None,
    span_id=None,
):
    return {
        "id": span_id or f"{source}-{dest}-{payload_type}-{direction}-{enqueued_at}",
        "source_agent": source,
        "dest_agent": dest,
        "payload_type": payload_type,
        "payload_summary": payload_summary,
        "protocol": None,
        "detail": None,
        "state": state,
        "direction": direction,
        "enqueued_at": enqueued_at,
        "acked_at": acked_at,
        "error": error,
        "source_registered": None,
        "dest_registered": None,
    }


class PayloadSummaryTests(unittest.TestCase):
    def test_text_field(self):
        self.assertEqual(payload_summary(Hello(text="Hi Bob!", count=1)), "Hi Bob!")


class MessageLabelTests(unittest.TestCase):
    def test_hello_is_message(self):
        self.assertEqual(message_label("Hello"), "Message")

    def test_reply_is_reply(self):
        self.assertEqual(message_label("Reply"), "Reply")

    def test_pong_is_reply(self):
        self.assertEqual(message_label("Pong"), "Reply")


class FormatMsTests(unittest.TestCase):
    def test_format_latency_ms(self):
        self.assertEqual(format_ms(45), "45ms")

    def test_format_latency_seconds(self):
        self.assertEqual(format_ms(1500), "1.50s")

    def test_format_latency_none(self):
        self.assertEqual(format_ms(None), "…")


class LiveFormatTests(unittest.TestCase):
    def test_agent_box_with_message(self):
        lines = render_agent_box('Alice: "Hi Bob!"')
        self.assertIn('Alice: "Hi Bob!"', lines[1])

    def test_event_line_uses_message_label(self):
        spans = [
            span("a", "b", payload_type="Hello", payload_summary="Hi Bob!", enqueued_at=0, acked_at=50),
        ]
        hop = build_hops(spans)[0]
        line = format_event_line(hop, {"a": "Alice", "b": "Bob"})
        self.assertIn("Message:", line.plain)
        self.assertIn("Hi Bob!", line.plain)
        self.assertIn("→", line.plain)
        self.assertNotIn("◀", line.plain)

    def test_event_line_reply_label(self):
        spans = [
            span("b", "a", payload_type="Reply", payload_summary="Hi Alice!", enqueued_at=0, acked_at=50),
        ]
        hop = build_hops(spans)[0]
        line = format_event_line(hop, {"a": "Alice", "b": "Bob"})
        self.assertIn("Reply:", line.plain)

    def test_event_line_dedupes_send_receive_twins(self):
        # traced_send's send-side span and @trace's receive-side span
        # describe the same logical hop -- build_hops must collapse them
        # into one entry, not one line per span.
        spans = [
            span("a", "b", payload_type="Hello", payload_summary="Hi Bob!", direction="send", enqueued_at=0, acked_at=10),
            span("a", "b", payload_type="Hello", payload_summary="Hi Bob!", direction="receive", enqueued_at=10, acked_at=75),
        ]
        hops = build_hops(spans)
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0].latency_ms, 75)  # full hop: send enqueue -> receive ack

    def test_peer_network_diagram(self):
        spans = [
            span("a", "b", payload_type="Hello", payload_summary="Hi Bob!", enqueued_at=0, acked_at=50),
            span("b", "a", payload_type="Reply", payload_summary="Hi Alice!", enqueued_at=60, acked_at=80),
        ]
        hops = build_hops(spans)
        diagram = build_peer_network_diagram(hops, {"a": "Alice", "b": "Bob"})
        text = diagram.plain
        self.assertIn("Alice", text)
        self.assertIn("Bob", text)
        self.assertIn("┌", text)
        self.assertIn("Route", text)
        self.assertIn("out", text)
        self.assertIn("in", text)
        self.assertIn("50ms", text)
        self.assertNotIn("Request (", text)

    def test_hub_network_diagram_fanout(self):
        spans = [
            span("orch", "sub1", payload_type="Hello", payload_summary="Hi Bob!", enqueued_at=0, acked_at=3),
            span("sub1", "orch", payload_type="Reply", payload_summary="done", enqueued_at=10, acked_at=20),
            span("orch", "sub2", payload_type="Hello", payload_summary="Hi John!", enqueued_at=0, acked_at=4),
            span("sub2", "orch", payload_type="Reply", payload_summary="done", enqueued_at=12, acked_at=22),
        ]
        aliases = {"orch": "Orchestrator", "sub1": "SubAgent1", "sub2": "SubAgent2"}
        state = build_trace_state(spans, hub_hint="orch")
        diagram = build_hub_network_diagram(state, aliases)
        text = diagram.plain
        self.assertIn("Orchestrator", text)
        self.assertIn("SubAgent1", text)
        self.assertIn("SubAgent2", text)
        self.assertIn("┌", text)
        self.assertIn("orchestrator", text)
        self.assertIn("Agent", text)
        self.assertIn("Out", text)
        self.assertIn("In", text)
        self.assertIn("Total", text)
        self.assertIn("3ms", text)
        self.assertNotIn("Request (", text)

    def test_hub_network_diagram_failed_leg_hides_misleading_out(self):
        # A failed leg's dispatch_ms is time-to-failure, not outbound
        # latency -- it must not appear under Out, and the failure
        # duration should be visible next to the status instead.
        spans = [
            span("orch", "sub1", payload_type="Hello", enqueued_at=0, acked_at=3),
            span("sub1", "orch", payload_type="Reply", enqueued_at=10, acked_at=20),
            span(
                "orch",
                "sub2",
                payload_type="Hello",
                state="dropped",
                error="Unable to resolve destination endpoint",
                enqueued_at=0,
                acked_at=698,
            ),
        ]
        aliases = {"orch": "Orchestrator", "sub1": "SubAgent1", "sub2": "SubAgent2"}
        state = build_trace_state(spans, hub_hint="orch")
        diagram = build_hub_network_diagram(state, aliases)
        text = diagram.plain
        self.assertIn("698ms", text)  # failure duration surfaces near the status...
        table_and_after = text.split("Agent", 1)[1]
        # ...but never under the Out column: SubAgent2's row shows "—", not 698ms.
        sub2_row_start = table_and_after.index("SubAgent2")
        sub2_row = table_and_after[sub2_row_start : sub2_row_start + 60]
        self.assertNotIn("698ms", sub2_row.split("✗")[0])

    def test_hub_leg_table_columns(self):
        legs = [
            {
                "subagent": "sub1",
                "dispatch_ms": 30,
                "reply_ms": 15,
                "latency_ms": 45,
                "state": "completed",
            },
            {
                "subagent": "sub2",
                "dispatch_ms": 25,
                "state": "pending",
            },
        ]
        table = build_hub_leg_table(legs, ["Bob", "John"])
        text = table.plain
        self.assertIn("Bob", text)
        self.assertIn("John", text)
        self.assertIn("30ms", text)
        self.assertIn("15ms", text)
        self.assertIn("45ms", text)
        self.assertIn("⋯ waiting", text)

    def test_hub_leg_table_failed_leg_omits_out_shows_failure_time(self):
        legs = [
            {"subagent": "sub4", "dispatch_ms": 723, "state": "failed", "reason": "Unable to resolve destination endpoint"},
        ]
        table = build_hub_leg_table(legs, ["SubAgent4"])
        text = table.plain
        self.assertIn("✗ failed 723ms", text)
        # The Out column itself must read as unknown/failed, not "723ms".
        row_line = next(line for line in text.splitlines() if "SubAgent4" in line)
        out_field = row_line[len("SubAgent4"):].strip().split()[0]
        self.assertEqual(out_field, "—")

    def test_hub_detail_summary(self):
        legs = [
            {"subagent": "sub1", "state": "completed", "latency_ms": 45},
            {"subagent": "sub2", "state": "pending"},
        ]
        summary = build_hub_detail_summary("Alice", legs, ["Bob", "John"], "abc12345-dead")
        self.assertIn("Alice dispatched to Bob, John", summary)
        self.assertIn("1/2 complete", summary)
        self.assertIn("45ms max", summary)

    def test_hub_tree_diagram_fan_out(self):
        spans = [
            span("orch", "sub1", payload_type="Hello", payload_summary="Hi Bob!", enqueued_at=0, acked_at=3),
            span("sub1", "orch", payload_type="Reply", payload_summary="done", enqueued_at=10, acked_at=20),
            span("orch", "sub2", payload_type="Hello", payload_summary="Hi John!", enqueued_at=0, acked_at=4),
        ]
        aliases = {"orch": "Orchestrator", "sub1": "SubAgent1", "sub2": "SubAgent2"}
        tree = build_interaction_tree(spans, "orch")
        diagram = build_hub_tree_diagram(tree, aliases)
        text = diagram.plain
        self.assertIn("Orchestrator", text)
        self.assertIn("SubAgent1", text)
        self.assertIn("SubAgent2", text)
        self.assertIn("├──", text)


class SidebarLabelTests(unittest.TestCase):
    def test_hub_trace_shows_fractional_rollup_not_binary_failure(self):
        # The orchestrator_fanout demo shape: 3 legs complete, 1 fails --
        # the sidebar must say "3/4", not flip to an all-or-nothing FAILURE.
        spans = [
            span("orch", "sub1", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub1", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub2", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub2", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub3", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub3", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span(
                "orch",
                "sub4",
                payload_type="Task",
                state="dropped",
                error="Unable to resolve destination endpoint",
                enqueued_at=0,
                acked_at=700,
            ),
        ]
        state = build_trace_state(spans, hub_hint="orch")
        label, style = sidebar_label("1ffa482a-dead-beef", state, {"orch": "Orchestrator"})
        self.assertIn("Orchestrator→4", label)
        self.assertIn("3/4 ✓", label)
        self.assertNotIn("FAILURE", label)
        # 3/4 succeeded -- this must NOT read as a total failure (red).
        self.assertEqual(style, WARN)

    def test_peer_trace_label(self):
        spans = [
            span("a", "b", payload_type="Hello", enqueued_at=0, acked_at=10),
            span("b", "a", payload_type="Reply", enqueued_at=10, acked_at=20),
        ]
        state = build_trace_state(spans)
        label, style = sidebar_label("abc12345", state, {"a": "Alice", "b": "Bob"})
        self.assertIn("Alice↔Bob", label)
        self.assertIn("2/2 ✓", label)
        self.assertEqual(style, SUCCESS)

    def test_fully_failed_hub_trace_is_red(self):
        # Every leg failed -- this is the only case red should apply to.
        spans = [
            span(
                "orch", f"sub{i}", payload_type="Task", state="dropped",
                error="Unable to resolve destination endpoint", enqueued_at=0, acked_at=700,
            )
            for i in range(1, 5)
        ]
        state = build_trace_state(spans, hub_hint="orch")
        label, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        self.assertEqual(style, ERROR)

    def test_pending_trace_is_amber_not_red(self):
        # Nothing has failed yet, but it's not fully resolved either --
        # amber ("in progress"), not red and not green.
        spans = [span("orch", "sub1", payload_type="Task", state="pending", enqueued_at=0, acked_at=None)]
        state = build_trace_state(spans, hub_hint="orch")
        label, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        self.assertEqual(style, WARN)

    def test_sidebar_markup_dims_trace_id_separately_from_status(self):
        markup = _sidebar_markup("1ffa48 · Orchestrator→4 · 3/4 ✓ · 1.12s", WARN)
        # The id segment is always neutral/dim...
        self.assertIn(f"[{MUTED}]1ffa48[/]", markup)
        # ...while the status-bearing remainder carries the semantic color,
        # not a single flat style applied to the whole line.
        self.assertIn(f"[{WARN}]Orchestrator→4 · 3/4 ✓ · 1.12s[/]", markup)


if __name__ == "__main__":
    unittest.main()
