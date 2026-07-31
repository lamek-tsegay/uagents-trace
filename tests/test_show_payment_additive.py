"""Regression test for `show`/`watch` dropping every non-payment span the
moment a trace contains any Payment Protocol message.

`protocols.py` is a classifier: it attaches a label to spans that already
exist, and must never change which ones get rendered (see `cli.py`'s
`print_trace_detail`). Before the fix, `is_payment_trace(spans)` being True
made `print_trace_detail` print the payment ladder and `return` immediately
-- `classify_trace_shape`/`build_hops` (and therefore every non-payment
span) were never reached at all.

Seeds a two-agent trace mixing a Payment Protocol exchange (RequestPayment
-> CommitPayment) with an unrelated Task -> Result exchange between the
same two agents, so the shape classifies as `peer` and every hop -- payment
and non-payment alike -- goes through the one flat-waterfall renderer with
no hub-legs-only complication to muddy what "every span rendered" means.
"""

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
import uuid

from uagents_trace.cli import print_trace_detail
from uagents_trace.store import init_db, insert_span

BUYER = "agent1qbuyer"
SELLER = "agent1qseller"


def _span(
    *,
    trace_id,
    source,
    dest,
    payload_type,
    direction,
    enqueued_at,
    acked_at,
    protocol=None,
    detail=None,
    payload_summary=None,
):
    return {
        "id": str(uuid.uuid4()),
        "trace_id": trace_id,
        "source_agent": source,
        "dest_agent": dest,
        "protocol": protocol,
        "payload_type": payload_type,
        "payload_size": 0,
        "enqueued_at": enqueued_at,
        "acked_at": acked_at,
        "state": "delivered",
        "source_registered": True,
        "dest_registered": True,
        "error": None,
        "session_id": trace_id,
        "detail": detail,
        "payload_summary": payload_summary,
        "direction": direction,
    }


async def _seed_mixed_trace(db_path: str) -> str:
    await init_db(db_path)
    trace_id = str(uuid.uuid4())

    hops = [
        # (source, dest, payload_type, protocol, detail)
        (BUYER, SELLER, "RequestPayment", "Payment Protocol", "5.0 FET"),
        (SELLER, BUYER, "CommitPayment", "Payment Protocol", "5.0 FET"),
        (BUYER, SELLER, "Task", None, None),
        (SELLER, BUYER, "Result", None, None),
    ]

    t = 0
    for source, dest, payload_type, protocol, detail in hops:
        await insert_span(
            db_path,
            _span(
                trace_id=trace_id, source=source, dest=dest, payload_type=payload_type,
                direction="send", enqueued_at=t, acked_at=t + 5, protocol=protocol, detail=detail,
            ),
        )
        await insert_span(
            db_path,
            _span(
                trace_id=trace_id, source=source, dest=dest, payload_type=payload_type,
                direction="receive", enqueued_at=t + 5, acked_at=t + 10, protocol=protocol, detail=detail,
            ),
        )
        t += 100

    return trace_id


class ShowPaymentAdditiveTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.trace_id = asyncio.run(_seed_mixed_trace(self.db_path))

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _render(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(print_trace_detail(self.db_path, self.trace_id, color=False))
        return buf.getvalue()

    def test_payment_ladder_and_non_payment_hops_both_appear(self):
        out = self._render()

        # The ladder is still there...
        self.assertIn("PAYMENT", out)
        self.assertIn("RequestPayment", out)
        self.assertIn("CommitPayment", out)

        # ...and so is the rest of the trace. This is the assertion that
        # fails against the pre-fix code: `print_trace_detail` returned
        # right after the payment ladder, so "Task" and "Result" (this
        # trace's only non-payment spans) never made it into the output at
        # all.
        self.assertIn("Task", out)
        self.assertIn("Result", out)

    def test_every_hop_appears_in_the_waterfall(self):
        # All 4 logical hops (RequestPayment, CommitPayment, Task, Result)
        # go through the one flat-waterfall renderer here (peer shape --
        # `print_flat_spans(build_hops(spans))` unconditionally renders
        # every `Hop`, labelled with its payload_type). The payment pair
        # additionally appears in the ladder above it, so they're expected
        # twice; Task/Result have no ladder entry, so once.
        out = self._render()
        for payload_type, expected_count in (
            ("RequestPayment", 2),  # ladder + waterfall
            ("CommitPayment", 2),  # ladder + waterfall
            ("Task", 1),  # waterfall only -- this is what pre-fix code drops entirely
            ("Result", 1),  # waterfall only -- this is what pre-fix code drops entirely
        ):
            self.assertEqual(
                out.count(payload_type),
                expected_count,
                f"expected {payload_type} to appear {expected_count}x -- got {out.count(payload_type)} in:\n{out}",
            )


if __name__ == "__main__":
    unittest.main()
