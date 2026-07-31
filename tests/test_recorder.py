"""Tests for `recorder`'s causal parentage capture (`parent_span_id`).

Uses a minimal duck-typed `FakeContext` instead of a real uAgents `Context`
-- `trace`/`traced_send` only ever touch `ctx.agent.address`, `ctx.session`,
and `ctx.send(...)`, so a real agent/Bureau isn't needed to exercise the
`contextvars` plumbing itself.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from uagents import Model

from uagents_trace.recorder import trace, traced_send
from uagents_trace.store import get_trace_spans, init_db


class Ping(Model):
    text: str


class FakeAgent:
    def __init__(self, address: str) -> None:
        self.address = address


class FakeContext:
    """Enough of `uagents.Context` for `trace`/`traced_send` to run."""

    def __init__(self, address: str, session: str) -> None:
        self.agent = FakeAgent(address)
        self.session = session

    async def send(self, destination, message, timeout=None):
        return SimpleNamespace(status=SimpleNamespace(value="delivered"), detail=None)


class RecorderParentageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["UAGENTS_TRACE_DB"] = self.db_path
        asyncio.run(init_db(self.db_path))

    def tearDown(self):
        os.environ.pop("UAGENTS_TRACE_DB", None)
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _spans(self, session: str):
        return asyncio.run(get_trace_spans(self.db_path, session))

    def test_send_inside_handler_captures_receive_as_parent(self):
        session = "session-1"

        @trace
        async def handler(ctx, sender, msg):
            await traced_send(ctx, "dest-agent", Ping(text="hi"), db_path=self.db_path)

        ctx = FakeContext("agent-a", session)
        asyncio.run(handler(ctx, "sender-agent", Ping(text="incoming")))

        spans = self._spans(session)
        receive_span = next(s for s in spans if s["direction"] == "receive")
        send_span = next(s for s in spans if s["direction"] == "send")
        self.assertEqual(send_span["parent_span_id"], receive_span["id"])

    def test_send_outside_any_handler_has_null_parent(self):
        # No `trace`-wrapped handler ever ran in this asyncio task -- e.g.
        # a scenario client's opening message, or the trace's true entry
        # point -- so there's nothing to attribute the send to.
        session = "session-2"
        ctx = FakeContext("agent-a", session)
        asyncio.run(traced_send(ctx, "dest-agent", Ping(text="hi"), db_path=self.db_path))

        spans = self._spans(session)
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0]["parent_span_id"])

    def test_nested_dispatch_chain_links_each_hop_to_its_own_receive(self):
        # agent-a receives, dispatches to agent-b; agent-b receives (a
        # separate handler invocation, its own asyncio task) and dispatches
        # to agent-c. Each send's parent must be *that agent's own* receive
        # span, not some earlier or later one -- the exact structure
        # shape.build_interaction_tree walks to build the causal tree.
        session = "session-3"

        @trace
        async def handler_a(ctx, sender, msg):
            await traced_send(ctx, "agent-b", Ping(text="to-b"), db_path=self.db_path)

        @trace
        async def handler_b(ctx, sender, msg):
            await traced_send(ctx, "agent-c", Ping(text="to-c"), db_path=self.db_path)

        asyncio.run(handler_a(FakeContext("agent-a", session), "external", Ping(text="start")))
        asyncio.run(handler_b(FakeContext("agent-b", session), "agent-a", Ping(text="to-b")))

        spans = self._spans(session)
        receive_a = next(s for s in spans if s["dest_agent"] == "agent-a" and s["direction"] == "receive")
        send_a_to_b = next(s for s in spans if s["source_agent"] == "agent-a" and s["direction"] == "send")
        receive_b = next(s for s in spans if s["dest_agent"] == "agent-b" and s["direction"] == "receive")
        send_b_to_c = next(s for s in spans if s["source_agent"] == "agent-b" and s["direction"] == "send")

        self.assertEqual(send_a_to_b["parent_span_id"], receive_a["id"])
        self.assertEqual(send_b_to_c["parent_span_id"], receive_b["id"])
        # No envelope changes (project non-goal) means agent-b's own
        # *receive* span can't automatically know it was caused by
        # send_a_to_b -- that cross-process link stays unresolved by
        # parent_span_id, matched by shape._match_sends_to_receives
        # instead. Confirmed here so this limitation stays intentional.
        self.assertIsNone(receive_b.get("parent_span_id"))

    def test_asyncio_create_task_inside_handler_still_carries_parent(self):
        # contextvars.copy_context() is what asyncio.create_task uses to
        # snapshot context at task-creation time, per the Python docs --
        # verified here for `trace`/`traced_send`'s actual usage rather
        # than assumed.
        session = "session-4"
        done = asyncio.Event()

        @trace
        async def handler(ctx, sender, msg):
            async def background():
                await traced_send(ctx, "dest-agent", Ping(text="from-task"), db_path=self.db_path)
                done.set()

            asyncio.create_task(background())

        async def body():
            ctx = FakeContext("agent-a", session)
            await handler(ctx, "sender-agent", Ping(text="incoming"))
            await asyncio.wait_for(done.wait(), timeout=2)

        asyncio.run(body())

        spans = self._spans(session)
        receive_span = next(s for s in spans if s["direction"] == "receive")
        send_span = next(s for s in spans if s["direction"] == "send")
        self.assertEqual(send_span["parent_span_id"], receive_span["id"])

    def test_detached_task_started_outside_any_handler_has_null_parent(self):
        # A timer-style background job that never runs inside a
        # `trace`-wrapped handler -- honestly NULL, not guessed at.
        session = "session-5"

        async def detached_job(ctx):
            await traced_send(ctx, "dest-agent", Ping(text="tick"), db_path=self.db_path)

        async def body():
            ctx = FakeContext("agent-a", session)
            await asyncio.create_task(detached_job(ctx))

        asyncio.run(body())
        spans = self._spans(session)
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0]["parent_span_id"])

    def test_current_span_reset_after_handler_does_not_leak_to_later_sends(self):
        # Once a handler returns, `_current_span` must be back to None (or
        # whatever it was before) so a later, unrelated send in the same
        # asyncio task doesn't get mis-attributed to a handler that already
        # finished.
        session = "session-6"

        @trace
        async def handler(ctx, sender, msg):
            return None

        async def body():
            ctx = FakeContext("agent-a", session)
            await handler(ctx, "sender-agent", Ping(text="incoming"))
            await traced_send(ctx, "dest-agent", Ping(text="unrelated"), db_path=self.db_path)

        asyncio.run(body())
        spans = self._spans(session)
        send_span = next(s for s in spans if s["direction"] == "send")
        self.assertIsNone(send_span["parent_span_id"])


if __name__ == "__main__":
    unittest.main()
