"""API-level tests for the web dashboard's backend.

These seed a scratch SQLite file with the same shape orchestrator_fanout.py
produces (an orchestrator dispatching to 4 sub-agents, 3 complete, 1 fails
because its destination can't be resolved), then hit the FastAPI app
directly to make sure the JSON handed to ui/index.html is already merged
into logical hops server-side -- not raw send/receive span pairs the JS
would have to dedup itself.
"""

import asyncio
import os
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from uagents_trace.store import init_db, insert_span

ORCH = "agent1qorchestrator"
SUBS = ["agent1qsub1", "agent1qsub2", "agent1qsub3", "agent1qsub4unreachable"]


def _span(
    *,
    trace_id,
    source,
    dest,
    payload_type,
    direction,
    enqueued_at,
    acked_at=None,
    state="delivered",
    error=None,
    payload_summary=None,
):
    return {
        "id": str(uuid.uuid4()),
        "trace_id": trace_id,
        "source_agent": source,
        "dest_agent": dest,
        "protocol": None,
        "payload_type": payload_type,
        "payload_size": 0,
        "enqueued_at": enqueued_at,
        "acked_at": acked_at,
        "state": state,
        "source_registered": True,
        "dest_registered": None if state == "dropped" else True,
        "error": error,
        "session_id": trace_id,
        "kind": None,
        "detail": None,
        "payload_summary": payload_summary,
        "direction": direction,
    }


async def _seed_orchestrator_fanout_trace(db_path: str) -> str:
    """3 subagents complete (dispatch + reply, each with a send/receive
    twin), 1 subagent (SUBS[3]) fails to resolve -- the exact shape
    orchestrator_fanout.py produces.
    """
    await init_db(db_path)
    trace_id = str(uuid.uuid4())
    t = 0

    for sub in SUBS[:3]:
        dispatch_send = _span(
            trace_id=trace_id, source=ORCH, dest=sub, payload_type="Task",
            direction="send", enqueued_at=t, acked_at=t + 5, payload_summary='{"job":"go"}',
        )
        dispatch_recv = _span(
            trace_id=trace_id, source=ORCH, dest=sub, payload_type="Task",
            direction="receive", enqueued_at=t + 5, acked_at=t + 60, payload_summary='{"job":"go"}',
        )
        reply_send = _span(
            trace_id=trace_id, source=sub, dest=ORCH, payload_type="Result",
            direction="send", enqueued_at=t + 60, acked_at=t + 65, payload_summary='{"job":"done"}',
        )
        reply_recv = _span(
            trace_id=trace_id, source=sub, dest=ORCH, payload_type="Result",
            direction="receive", enqueued_at=t + 65, acked_at=t + 75, payload_summary='{"job":"done"}',
        )
        for span in (dispatch_send, dispatch_recv, reply_send, reply_recv):
            await insert_span(db_path, span)
        t += 100

    failed_dispatch = _span(
        trace_id=trace_id, source=ORCH, dest=SUBS[3], payload_type="Task",
        direction="send", enqueued_at=t, acked_at=t + 723, state="dropped",
        error="Unable to resolve destination endpoint", payload_summary='{"job":"go"}',
    )
    await insert_span(db_path, failed_dispatch)

    return trace_id


class ServerHopsApiTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["UAGENTS_TRACE_DB"] = self.db_path
        self.trace_id = asyncio.run(_seed_orchestrator_fanout_trace(self.db_path))

        # Import after UAGENTS_TRACE_DB is set so default_db_path() (read at
        # call time, not import time) resolves to the scratch file.
        from uagents_trace.server import app

        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("UAGENTS_TRACE_DB", None)
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_raw_spans_endpoint_still_has_send_receive_twins(self):
        # Sanity check on the seed data / existing endpoint: the twin-pair
        # duplication this whole feature exists to hide is really there at
        # the raw span level.
        resp = self.client.get(f"/api/traces/{self.trace_id}")
        self.assertEqual(resp.status_code, 200)
        spans = resp.json()
        self.assertEqual(len(spans), 13)  # 3 legs x 4 spans (dispatch+reply, send+receive) + 1 failed dispatch

    def test_hops_endpoint_returns_merged_hops_not_raw_span_pairs(self):
        resp = self.client.get(f"/api/traces/{self.trace_id}/hops")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # 3 legs x 2 logical messages (dispatch, reply) + 1 failed dispatch = 7,
        # not the 13 raw spans -- twins are merged, not just relisted.
        self.assertEqual(len(data["hops"]), 7)

        by_dest = {h["dest"]: h for h in data["hops"] if h["source"] == ORCH}
        self.assertEqual(by_dest[SUBS[3]]["state"], "dropped")
        self.assertIn("resolve", by_dest[SUBS[3]]["error"])
        # Time-to-failure is preserved on the hop (it's the API's job to
        # expose it; the *client* decides not to label it as outbound ack
        # latency -- that's a rendering choice, not a data omission).
        self.assertEqual(by_dest[SUBS[3]]["latency_ms"], 723)

    def test_hops_endpoint_rollup_matches_shape_semantics(self):
        resp = self.client.get(f"/api/traces/{self.trace_id}/hops")
        data = resp.json()
        self.assertEqual(data["shape"], "hub")
        self.assertEqual(data["hub"], ORCH)
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["completed"], 3)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["pending"], 0)
        self.assertEqual(len(data["legs"]), 4)

    def test_hops_endpoint_404s_for_unknown_trace(self):
        resp = self.client.get("/api/traces/does-not-exist/hops")
        self.assertEqual(resp.status_code, 404)

    def test_trace_list_is_enriched_with_fractional_rollup(self):
        resp = self.client.get("/api/traces")
        self.assertEqual(resp.status_code, 200)
        traces = resp.json()
        self.assertEqual(len(traces), 1)
        t = traces[0]
        # Old binary field stays for back-compat...
        self.assertTrue(t["has_failure"])
        # ...but the dashboard now has enough to show "3/4 ✓" instead of
        # a blanket FAILURE badge.
        self.assertEqual(t["completed"], 3)
        self.assertEqual(t["failed"], 1)
        self.assertEqual(t["total"], 4)
        self.assertEqual(t["shape"], "hub")


if __name__ == "__main__":
    unittest.main()
