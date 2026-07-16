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
    build_hub_tree_diagram,
    format_event_line,
    message_label,
    render_agent_box,
    sidebar_label,
)
from uagents_trace.network_canvas import ERROR, SUCCESS, WARN, build_hub_topology, build_peer_topology, format_ms
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

    def test_peer_topology_shows_both_agents(self):
        # The diagram panel is topology-only now (no leg/route table beside
        # it) -- this exercises build_peer_topology directly, since it's
        # what the live view actually renders (build_peer_network_diagram,
        # which used to wrap it with a table, is gone).
        diagram = build_peer_topology("Alice", "Bob", state="completed")
        text = diagram.plain
        self.assertIn("Alice", text)
        self.assertIn("Bob", text)
        self.assertIn("┌", text)
        self.assertIn("▶", text)

    def test_hub_topology_shows_orchestrator_and_subagents(self):
        legs = [
            {"subagent": "sub1", "state": "completed"},
            {"subagent": "sub2", "state": "completed"},
        ]
        diagram = build_hub_topology(legs, "Orchestrator", ["SubAgent1", "SubAgent2"])
        text = diagram.plain
        self.assertIn("Orchestrator", text)
        self.assertIn("SubAgent1", text)
        self.assertIn("SubAgent2", text)
        self.assertIn("┌", text)
        self.assertIn("orchestrator", text)

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
        text, style = sidebar_label("1ffa482a-dead-beef", state, {"orch": "Orchestrator"})
        plain = text.plain
        # Two lines now: identity (id + header) on line 1, status
        # (fraction/bar/duration) indented on line 2.
        self.assertEqual(plain.count("\n"), 1)
        self.assertIn("Orchestrator→4", plain)
        self.assertIn("3/4 ✓", plain)
        self.assertNotIn("FAILURE", plain)
        # 3/4 succeeded -- this must NOT read as a total failure (red).
        self.assertEqual(style, WARN)

    def test_peer_trace_label(self):
        spans = [
            span("a", "b", payload_type="Hello", enqueued_at=0, acked_at=10),
            span("b", "a", payload_type="Reply", enqueued_at=10, acked_at=20),
        ]
        state = build_trace_state(spans)
        text, style = sidebar_label("abc12345", state, {"a": "Alice", "b": "Bob"})
        plain = text.plain
        self.assertIn("Alice↔Bob", plain)
        self.assertIn("2/2 ✓", plain)
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
        _text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        self.assertEqual(style, ERROR)

    def test_pending_trace_is_amber_not_red(self):
        # Nothing has failed yet, but it's not fully resolved either --
        # amber ("in progress"), not red and not green.
        spans = [span("orch", "sub1", payload_type="Task", state="pending", enqueued_at=0, acked_at=None)]
        state = build_trace_state(spans, hub_hint="orch")
        _text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        self.assertEqual(style, WARN)

    def test_sidebar_label_dims_trace_id_separately_from_status(self):
        spans = [
            span("orch", "sub1", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub1", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub2", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub2", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub3", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub3", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span(
                "orch", "sub4", payload_type="Task", state="dropped",
                error="Unable to resolve destination endpoint", enqueued_at=0, acked_at=700,
            ),
        ]
        state = build_trace_state(spans, hub_hint="orch")
        text, style = sidebar_label("1ffa482a-dead-beef", state, {"orch": "Orchestrator"})
        plain = text.plain

        def style_at(index: int) -> str:
            return next(run.style for run in text.spans if run.start <= index < run.end)

        # The id segment (line 1, column 0) is always neutral/dim...
        self.assertEqual(style_at(0), MUTED)
        # ...while the header (line 1) and all of line 2 carry the
        # semantic color, not a single flat style applied to the whole
        # block.
        header_idx = plain.index("Orchestrator")
        status_idx = plain.index("3/4")
        self.assertEqual(style_at(header_idx), style)
        self.assertEqual(style_at(status_idx), style)


class SidebarMarkerMappingTests(unittest.TestCase):
    """Rollup state -> sidebar marker: a partially-failed trace must carry
    a distinct marker from a plain in-progress one even though both read
    WARN, and a fully-failed trace's marker must differ from both.
    """

    def _hub_state(self, *, completed, failed, pending):
        spans = []
        for i in range(completed):
            spans.append(span("orch", f"ok{i}", payload_type="Task", enqueued_at=0, acked_at=10))
            spans.append(span(f"ok{i}", "orch", payload_type="Result", enqueued_at=10, acked_at=20))
        for i in range(failed):
            spans.append(
                span(
                    "orch", f"bad{i}", payload_type="Task", state="dropped",
                    error="Unable to resolve destination endpoint", enqueued_at=0, acked_at=700,
                )
            )
        for i in range(pending):
            spans.append(span("orch", f"pending{i}", payload_type="Task", state="pending", enqueued_at=0, acked_at=None))
        return build_trace_state(spans, hub_hint="orch")

    def test_fully_failed_gets_failure_marker_not_degraded(self):
        state = self._hub_state(completed=0, failed=3, pending=0)
        text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        plain = text.plain
        self.assertEqual(style, ERROR)
        self.assertIn(FAILURE_MARKER.strip(), plain)
        self.assertNotIn(DEGRADED_MARKER.strip(), plain)

    def test_partial_failure_gets_degraded_marker_not_failure(self):
        # Some legs ok, one failed -- WARN (not red), but must still be
        # visually distinct from a trace that's merely still in progress.
        state = self._hub_state(completed=3, failed=1, pending=0)
        text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        plain = text.plain
        self.assertEqual(style, WARN)
        self.assertIn(DEGRADED_MARKER.strip(), plain)
        self.assertNotIn(FAILURE_MARKER.strip(), plain)

    def test_plain_pending_gets_no_marker(self):
        # Nothing has failed -- just still running. Must not carry either
        # failure marker, or it'd be indistinguishable from a degraded trace.
        state = self._hub_state(completed=1, failed=0, pending=1)
        text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        plain = text.plain
        self.assertEqual(style, WARN)
        self.assertNotIn(DEGRADED_MARKER.strip(), plain)
        self.assertNotIn(FAILURE_MARKER.strip(), plain)

    def test_fully_delivered_gets_no_marker(self):
        state = self._hub_state(completed=2, failed=0, pending=0)
        text, style = sidebar_label("deadbeef", state, {"orch": "Orchestrator"})
        plain = text.plain
        self.assertEqual(style, SUCCESS)
        self.assertNotIn(DEGRADED_MARKER.strip(), plain)
        self.assertNotIn(FAILURE_MARKER.strip(), plain)

    def test_marker_column_is_reserved_even_when_absent(self):
        # A marker (⚑/⚠) must not shift line 1's header sideways depending
        # on whether it's present -- the header should land in the same
        # column whether or not a trace carries a marker.
        failed_state = self._hub_state(completed=0, failed=3, pending=0)
        clean_state = self._hub_state(completed=2, failed=0, pending=0)
        failed_text, _ = sidebar_label("deadbeef", failed_state, {"orch": "Orchestrator"})
        clean_text, _ = sidebar_label("deadbeef", clean_state, {"orch": "Orchestrator"})
        failed_header_col = failed_text.plain.split("\n")[0].index("Orchestrator")
        clean_header_col = clean_text.plain.split("\n")[0].index("Orchestrator")
        self.assertEqual(failed_header_col, clean_header_col)


class LatencyBarTests(unittest.TestCase):
    def test_zero_duration_is_all_empty(self):
        bar = _latency_bar(0)
        self.assertEqual(bar, LATENCY_BAR_EMPTY * LATENCY_BAR_WIDTH)

    def test_tiny_duration_still_shows_one_tick(self):
        # A trace that took *some* time shouldn't look identical to zero.
        bar = _latency_bar(1)
        self.assertEqual(bar.count(LATENCY_BAR_FILLED), 1)

    def test_duration_at_scale_ceiling_is_full(self):
        bar = _latency_bar(LATENCY_BAR_SCALE_MS)
        self.assertEqual(bar, LATENCY_BAR_FILLED * LATENCY_BAR_WIDTH)

    def test_duration_beyond_ceiling_clamps_full(self):
        bar = _latency_bar(LATENCY_BAR_SCALE_MS * 10)
        self.assertEqual(bar, LATENCY_BAR_FILLED * LATENCY_BAR_WIDTH)

    def test_bar_always_fixed_width(self):
        for ms in (0, 1, 250, 999, 2000, 50_000):
            self.assertEqual(len(_latency_bar(ms)), LATENCY_BAR_WIDTH)

    def test_sidebar_label_includes_bar_alongside_duration_text(self):
        spans = [
            span("a", "b", payload_type="Hello", enqueued_at=0, acked_at=10),
            span("b", "a", payload_type="Reply", enqueued_at=10, acked_at=1200),
        ]
        state = build_trace_state(spans)
        text, _ = sidebar_label("abc12345", state, {"a": "Alice", "b": "Bob"})
        # The bar replaces the *bare* number -- but the exact duration text
        # must still be present right next to it, not lost.
        plain = text.plain
        self.assertIn(_latency_bar(state.duration_ms), plain)
        self.assertIn(format_ms(state.duration_ms), plain)


class ColorEconomyTests(unittest.TestCase):
    """Bright/bold is reserved for failures -- a fully-delivered trace or
    leg must never carry the same visual weight as a failed one, even
    though both are still colored (green vs red) for quick scanning.
    """

    def test_sidebar_label_bolds_only_the_failed_style(self):
        def status_style(text):
            # First char of line 2 (the status line) -- its style is what
            # this test cares about, whatever line 2's exact wording is.
            status_idx = text.plain.index("\n") + 1
            return next(run.style for run in text.spans if run.start <= status_idx < run.end)

        failed_spans = [
            span(
                "orch", f"sub{i}", payload_type="Task", state="dropped",
                error="Unable to resolve destination endpoint", enqueued_at=0, acked_at=700,
            )
            for i in range(1, 5)
        ]
        failed_state = build_trace_state(failed_spans, hub_hint="orch")
        failed_text, failed_style = sidebar_label("1ffa48", failed_state, {"orch": "Orchestrator"})
        self.assertEqual(failed_style, ERROR)
        self.assertIn("bold", status_style(failed_text))

        success_spans = [
            span("a", "b", payload_type="Hello", enqueued_at=0, acked_at=10),
            span("b", "a", payload_type="Reply", enqueued_at=10, acked_at=20),
        ]
        success_state = build_trace_state(success_spans)
        success_text, success_style = sidebar_label("abc123", success_state, {"a": "Alice", "b": "Bob"})
        self.assertEqual(success_style, SUCCESS)
        self.assertNotIn("bold", status_style(success_text))

        warn_spans = [
            span("orch", "sub1", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub1", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub2", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub2", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span("orch", "sub3", payload_type="Task", enqueued_at=0, acked_at=10),
            span("sub3", "orch", payload_type="Result", enqueued_at=10, acked_at=20),
            span(
                "orch", "sub4", payload_type="Task", state="dropped",
                error="Unable to resolve destination endpoint", enqueued_at=0, acked_at=700,
            ),
        ]
        warn_state = build_trace_state(warn_spans, hub_hint="orch")
        warn_text, warn_style = sidebar_label("cafeba", warn_state, {"orch": "Orchestrator"})
        self.assertEqual(warn_style, WARN)
        self.assertNotIn("bold", status_style(warn_text))

    def test_hub_topology_bolds_only_the_failed_agent_box(self):
        # The leg table this used to check is gone -- the same bold-only-
        # for-failure invariant now lives in the topology's own box styling
        # (network_canvas._box_style), so this exercises that directly.
        # Styles are looked up by overlapping index range, not by searching
        # for the label inside a single span's text: Canvas.to_text() can
        # split one same-styled run across several adjacent spans, so a
        # 4-char label like "Sub1" isn't guaranteed to sit wholly inside
        # any one span.
        legs = [
            {"subagent": "sub1", "state": "completed"},
            {"subagent": "sub2", "state": "failed"},
        ]
        diagram = build_hub_topology(legs, "Orchestrator", ["Sub1", "Sub2"])
        plain = diagram.plain

        def styles_at(label: str) -> list[str]:
            start = plain.index(label)
            end = start + len(label)
            return [run.style for run in diagram.spans if run.start < end and run.end > start]

        self.assertTrue(all("bold" not in (style or "") for style in styles_at("Sub1")))
        self.assertIn(f"bold {ERROR}", styles_at("Sub2"))

    def test_success_is_visibly_dimmer_than_error_and_accent(self):
        # Regression guard for the color-economy fix itself: SUCCESS must
        # no longer be the same saturated green as before -- if a future
        # edit quietly reverts it, this should catch it rather than only
        # showing up as a vibes-based UI regression.
        self.assertNotEqual(SUCCESS, "#4ade80")


if __name__ == "__main__":
    unittest.main()
