import unittest

from uagents import Model

from uagents_trace.live import (
    build_hub_diagram,
    build_peer_diagram,
    format_event_line,
    format_latency,
    message_label,
    render_agent_box,
)
from uagents_trace.recorder import payload_summary


class Hello(Model):
    text: str
    count: int


class PayloadSummaryTests(unittest.TestCase):
    def test_text_field(self):
        self.assertEqual(payload_summary(Hello(text="Hi Bob!", count=1)), "Hi Bob!")


class MessageLabelTests(unittest.TestCase):
    def test_hello_is_message(self):
        self.assertEqual(message_label({"payload_type": "Hello"}), "Message")

    def test_reply_is_reply(self):
        self.assertEqual(message_label({"payload_type": "Reply"}), "Reply")

    def test_pong_is_reply(self):
        self.assertEqual(message_label({"payload_type": "Pong"}), "Reply")


class LiveFormatTests(unittest.TestCase):
    def test_format_latency_ms(self):
        span = {"enqueued_at": 1000, "acked_at": 1045}
        self.assertEqual(format_latency(span), "45ms")

    def test_format_latency_seconds(self):
        span = {"enqueued_at": 0, "acked_at": 1500}
        self.assertEqual(format_latency(span), "1.50s")

    def test_agent_box_with_message(self):
        lines = render_agent_box('Alice: "Hi Bob!"')
        self.assertIn('Alice: "Hi Bob!"', lines[1])

    def test_event_line_uses_message_label(self):
        span = {
            "source_agent": "a",
            "dest_agent": "b",
            "payload_summary": "Hi Bob!",
            "payload_type": "Hello",
            "state": "delivered",
            "direction": "send",
            "enqueued_at": 0,
            "acked_at": 50,
        }
        line = format_event_line(span, {"a": "Alice", "b": "Bob"})
        self.assertIn("Message:", line.plain)
        self.assertIn("Hi Bob!", line.plain)
        self.assertIn("→", line.plain)
        self.assertNotIn("◀", line.plain)

    def test_event_line_reply_label(self):
        span = {
            "source_agent": "b",
            "dest_agent": "a",
            "payload_summary": "Hi Alice!",
            "payload_type": "Reply",
            "state": "delivered",
            "direction": "send",
            "enqueued_at": 0,
            "acked_at": 50,
        }
        line = format_event_line(span, {"a": "Alice", "b": "Bob"})
        self.assertIn("Reply:", line.plain)

    def test_peer_diagram_horizontal(self):
        spans = [
            {
                "source_agent": "a",
                "dest_agent": "b",
                "payload_summary": "Hi Bob!",
                "payload_type": "Hello",
                "state": "delivered",
                "direction": "send",
                "enqueued_at": 0,
                "acked_at": 50,
            }
        ]
        diagram = build_peer_diagram(spans, {"a": "Alice", "b": "Bob"})
        self.assertIn('Alice: "Hi Bob!"', diagram.plain)
        self.assertIn("50ms", diagram.plain)
        self.assertIn("─", diagram.plain)

    def test_hub_diagram_orchestrator_fanout(self):
        spans = [
            {
                "source_agent": "orch",
                "dest_agent": "sub1",
                "payload_type": "Hello",
                "payload_summary": "Hi Bob!",
                "state": "delivered",
                "direction": "send",
                "enqueued_at": 0,
                "acked_at": 3,
            },
            {
                "source_agent": "sub1",
                "dest_agent": "orch",
                "payload_type": "Reply",
                "payload_summary": "done",
                "state": "delivered",
                "direction": "send",
                "enqueued_at": 10,
                "acked_at": 20,
            },
            {
                "source_agent": "orch",
                "dest_agent": "sub2",
                "payload_type": "Hello",
                "payload_summary": "Hi John!",
                "state": "delivered",
                "direction": "send",
                "enqueued_at": 0,
                "acked_at": 4,
            },
            {
                "source_agent": "sub2",
                "dest_agent": "orch",
                "payload_type": "Reply",
                "payload_summary": "done",
                "state": "delivered",
                "direction": "send",
                "enqueued_at": 12,
                "acked_at": 22,
            },
        ]
        aliases = {"orch": "Orchestrator", "sub1": "SubAgent1", "sub2": "SubAgent2"}
        diagram = build_hub_diagram(spans, "orch", aliases)
        text = diagram.plain
        self.assertIn("Orchestrator", text)
        self.assertIn("SubAgent1", text)
        self.assertIn("SubAgent2", text)
        self.assertIn("success", text)
        self.assertNotIn("◀", text)


if __name__ == "__main__":
    unittest.main()
