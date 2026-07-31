"""Regression tests for the live TUI's selected-trace state (diagram +
rollup) agreeing with `show --view tree`/`shape.build_interaction_tree` on
the exact same trace_id.

Two real bugs, both caught against a live 33-agent Launchpad run:

  1. `_refresh_display` filtered the *selected* trace's own spans by
     `self.addresses` (the "watch" scope) before building `TraceState`.
     That scope is meant to control which *traces* show up in the sidebar
     and which individual hops appear in the rolling cross-trace feed --
     reasonable there. But once one trace is selected for its own
     diagram, filtering its spans strips out whichever un-watched agents
     sit *between* the trace's true root and the watched ones (e.g.
     gateway/parser ahead of orchestrator) -- every surviving span whose
     `parent_span_id` pointed into that removed material becomes
     unreachable, so the causal tree can't find the real root and falls
     back to guessing one from whatever's left.

  2. `TraceState.completed/failed/pending/total` were computed from
     `legs` (`build_hub_legs`) whenever `hub_hint` forced `shape == HUB`
     -- but `legs` only ever looks at the hub's *direct* children and
     requires a direct reply back to the hub to count as "completed",
     which real Launchpad leads never do (they forward to `assembler`).
     A trace that completed cleanly with one real failure several hops
     down (`ops_insurance`) read as "0/4, 0% success, no failures".
"""

import asyncio
import os
import tempfile
import unittest

from textual.containers import ScrollableContainer

from uagents_trace.live import DiagramCanvas, LiveApp
from uagents_trace.shape import build_interaction_tree
from uagents_trace.store import init_db, insert_span
from uagents_trace.wizard import WatchSetup


def _span(
    *,
    span_id,
    trace_id,
    source,
    dest,
    payload_type,
    direction,
    enqueued_at,
    acked_at,
    state="delivered",
    error=None,
    parent_span_id=None,
):
    return dict(
        id=span_id,
        trace_id=trace_id,
        source_agent=source,
        dest_agent=dest,
        protocol=None,
        payload_type=payload_type,
        payload_size=5,
        enqueued_at=enqueued_at,
        acked_at=acked_at,
        state=state,
        source_registered=True,
        dest_registered=state != "dropped",
        error=error,
        session_id=trace_id,
        kind=None,
        detail=None,
        payload_summary=None,
        direction=direction,
        parent_span_id=parent_span_id,
    )


async def _seed_relay_trace(db_path: str, trace_id: str) -> None:
    """EXTERNAL -> RELAY -> HUB -> {LEAD1 -> WORKER1 (ok), LEAD2 -> WORKER2
    (dropped)}. RELAY stands in for gateway/parser: not itself watched by
    the wizard's usual orchestrator+leads setup, but sitting on the only
    path from the true root (EXTERNAL) to HUB. WORKER2's failure is two
    hops below HUB, mirroring ops_insurance under ops_lead under
    orchestrator.
    """
    await init_db(db_path)
    spans = [
        _span(span_id="s0", trace_id=trace_id, source="EXTERNAL", dest="RELAY", payload_type="Msg", direction="send", enqueued_at=0, acked_at=2),
        _span(span_id="r0", trace_id=trace_id, source="EXTERNAL", dest="RELAY", payload_type="Msg", direction="receive", enqueued_at=2, acked_at=4),
        _span(span_id="s1", trace_id=trace_id, source="RELAY", dest="HUB", payload_type="Fwd", direction="send", enqueued_at=4, acked_at=6, parent_span_id="r0"),
        _span(span_id="r1", trace_id=trace_id, source="RELAY", dest="HUB", payload_type="Fwd", direction="receive", enqueued_at=6, acked_at=8),
        _span(span_id="s2", trace_id=trace_id, source="HUB", dest="LEAD1", payload_type="Task", direction="send", enqueued_at=8, acked_at=10, parent_span_id="r1"),
        _span(span_id="r2", trace_id=trace_id, source="HUB", dest="LEAD1", payload_type="Task", direction="receive", enqueued_at=10, acked_at=12),
        _span(span_id="s3", trace_id=trace_id, source="LEAD1", dest="WORKER1", payload_type="Sub", direction="send", enqueued_at=12, acked_at=14, parent_span_id="r2"),
        _span(span_id="r3", trace_id=trace_id, source="LEAD1", dest="WORKER1", payload_type="Sub", direction="receive", enqueued_at=14, acked_at=16),
        _span(span_id="s5", trace_id=trace_id, source="HUB", dest="LEAD2", payload_type="Task", direction="send", enqueued_at=9, acked_at=11, parent_span_id="r1"),
        _span(span_id="r5", trace_id=trace_id, source="HUB", dest="LEAD2", payload_type="Task", direction="receive", enqueued_at=11, acked_at=13),
        _span(
            span_id="s6", trace_id=trace_id, source="LEAD2", dest="WORKER2", payload_type="Sub2", direction="send",
            enqueued_at=13, acked_at=700, state="dropped", error="Unable to resolve destination endpoint",
            parent_span_id="r5",
        ),
    ]
    for s in spans:
        await insert_span(db_path, s)


class LiveTraceStateMatchesUnfilteredTreeTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_selected_trace_diagram_state_matches_show_view_tree(self):
        trace_id = "trace-relay"

        async def run():
            await _seed_relay_trace(self.db_path, trace_id)

            # The independently-computed ground truth: what `show --view
            # tree` (cli.py) and the web dashboard's /tree endpoint both
            # call directly, on the full, unfiltered spans.
            from uagents_trace.store import get_trace_spans

            all_spans = await get_trace_spans(self.db_path, trace_id)
            expected_tree, expected_unparented = build_interaction_tree(all_spans)
            self.assertEqual(expected_tree.agent, "EXTERNAL")  # sanity: the ground truth itself is right
            self.assertEqual(expected_unparented, [])

            # A wizard setup that watches HUB + LEAD1 only -- deliberately
            # excludes EXTERNAL, RELAY, LEAD2, and both workers, mirroring
            # a real watch config of "orchestrator + a couple leads" that
            # doesn't cover gateway/parser or every lead.
            setup = WatchSetup(
                addresses={"HUB", "LEAD1"},
                names={"HUB": "Hub", "LEAD1": "Lead1"},
                filter_only=True,
                db_path=self.db_path,
                orchestrator="HUB",
            )
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await pilot.pause()
                app._active_trace_id = trace_id
                await app._refresh_display()

                state = app._trace_state
                self.assertIsNotNone(state.tree)
                # Bug 1: filtering the selected trace's own spans must not
                # change its root or drop material relative to the
                # unfiltered ground truth.
                self.assertEqual(state.tree.agent, expected_tree.agent)
                self.assertEqual(state.unparented, [])

                # Bug 2: the rollup must see WORKER2's failure (two hops
                # below HUB, and LEAD2 never replies to HUB) instead of
                # reading a shallow, always-pending direct-leg count. Now
                # that the selected trace isn't filtered (bug 1's fix),
                # the rollup covers the *whole* tree from EXTERNAL down:
                # EXTERNAL->RELAY, RELAY->HUB, HUB->LEAD1, LEAD1->WORKER1,
                # HUB->LEAD2, LEAD2->WORKER2 -- 6 edges, 1 failed.
                self.assertEqual(state.total, 6)
                self.assertEqual(state.completed, 5)
                self.assertEqual(state.failed, 1)
                self.assertEqual(state.pending, 0)

        asyncio.run(run())


async def _seed_deep_chain(db_path: str, trace_id: str, depth: int) -> None:
    """EXTERNAL -> A0 -> A1 -> ... -> A{depth-1}, one hop per level, each
    send parent-linked to the previous hop's receive -- a minimal stand-in
    for a real ~19-level Launchpad tree, tall enough to force
    #diagram-scroll to actually overflow vertically.
    """
    await init_db(db_path)
    prev_receive_id = None
    prev_agent = "EXTERNAL"
    t = 0
    for i in range(depth):
        agent = f"A{i}"
        send_id, recv_id = f"{trace_id}-s{i}", f"{trace_id}-r{i}"
        await insert_span(
            db_path,
            _span(
                span_id=send_id, trace_id=trace_id, source=prev_agent, dest=agent,
                payload_type="Hop", direction="send", enqueued_at=t, acked_at=t + 1,
                parent_span_id=prev_receive_id,
            ),
        )
        await insert_span(
            db_path,
            _span(
                span_id=recv_id, trace_id=trace_id, source=prev_agent, dest=agent,
                payload_type="Hop", direction="receive", enqueued_at=t + 1, acked_at=t + 2,
            ),
        )
        prev_receive_id = recv_id
        prev_agent = agent
        t += 2


class DiagramVerticalScrollTests(unittest.TestCase):
    """A deep tree (the real repro trace runs ~19 levels/~77 rows) must be
    fully reachable by scrolling, never clipped -- #diagram-scroll used to
    be a HorizontalScroll, which Textual documents as not vertically
    scrollable at all; its bottom rows would just clip against whatever's
    below the panel on screen.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_deep_tree_overflows_vertically_and_is_fully_scrollable(self):
        trace_id = "trace-deep"
        depth = 40  # comfortably deeper than a small terminal's row count

        async def run():
            await _seed_deep_chain(self.db_path, trace_id, depth)
            setup = WatchSetup(
                addresses=set(), names={}, filter_only=False, db_path=self.db_path,
                orchestrator=None, view_mode="tree",
            )
            app = LiveApp(setup)
            # Short terminal -- guarantees the diagram panel's viewport is
            # far shorter than a 40-level tree's ~40 rendered rows.
            async with app.run_test(size=(120, 24)) as pilot:
                await pilot.pause()
                app._active_trace_id = trace_id
                await app._refresh_display()
                await pilot.pause()

                scroller = app.query_one("#diagram-scroll", ScrollableContainer)
                self.assertGreater(
                    scroller.virtual_size.height,
                    scroller.size.height,
                    "the tree should be taller than the viewport for this to be a real test",
                )
                self.assertGreater(scroller.max_scroll_y, 0)

                # The deepest node's own text exists in the fully-rendered
                # content -- it's off-screen, not missing.
                content = app.query_one("#diagram-content", DiagramCanvas)
                text = content._Static__content
                plain = text.plain if hasattr(text, "plain") else str(text)
                self.assertIn(f"A{depth - 1}", plain)

                before = scroller.scroll_offset.y
                scroller.scroll_to(y=scroller.max_scroll_y, animate=False)
                await pilot.pause()
                self.assertGreater(scroller.scroll_offset.y, before)
                self.assertEqual(scroller.scroll_offset.y, scroller.max_scroll_y)

        asyncio.run(run())

    def test_switching_trace_resets_scroll_to_top(self):
        trace_id_a, trace_id_b = "trace-a", "trace-b"

        async def run():
            await _seed_deep_chain(self.db_path, trace_id_a, 40)
            await _seed_deep_chain(self.db_path, trace_id_b, 3)
            setup = WatchSetup(
                addresses=set(), names={}, filter_only=False, db_path=self.db_path,
                orchestrator=None, view_mode="tree",
            )
            app = LiveApp(setup)
            async with app.run_test(size=(120, 24)) as pilot:
                await pilot.pause()
                app._active_trace_id = trace_id_a
                await app._refresh_display()
                await pilot.pause()

                scroller = app.query_one("#diagram-scroll", ScrollableContainer)
                scroller.scroll_to(y=scroller.max_scroll_y, animate=False)
                await pilot.pause()
                self.assertGreater(scroller.scroll_offset.y, 0)

                await app._select_trace(trace_id_b)
                await pilot.pause()
                self.assertEqual(scroller.scroll_offset.y, 0)

        asyncio.run(run())


async def _seed_overview_trace(db_path: str, trace_id: str) -> None:
    """EXTERNAL -> GATEWAY -> ORCH -> {LEAD1 -> WORKER1 (ok), LEAD2 ->
    WORKER2 (dropped)} -- a small multi-level trace exercising the same
    shape as the real 33-agent one: a hub (ORCH) whose own legs
    (`state.legs`) only cover LEAD1/LEAD2, with WORKER1/WORKER2 one level
    deeper still needing to be clickable in the overview.
    """
    await init_db(db_path)
    spans = [
        _span(span_id="s0", trace_id=trace_id, source="EXTERNAL", dest="GATEWAY", payload_type="Chat", direction="send", enqueued_at=0, acked_at=2),
        _span(span_id="r0", trace_id=trace_id, source="EXTERNAL", dest="GATEWAY", payload_type="Chat", direction="receive", enqueued_at=2, acked_at=4),
        _span(span_id="s1", trace_id=trace_id, source="GATEWAY", dest="ORCH", payload_type="Task", direction="send", enqueued_at=4, acked_at=6, parent_span_id="r0"),
        _span(span_id="r1", trace_id=trace_id, source="GATEWAY", dest="ORCH", payload_type="Task", direction="receive", enqueued_at=6, acked_at=8),
        _span(span_id="s2", trace_id=trace_id, source="ORCH", dest="LEAD1", payload_type="Task", direction="send", enqueued_at=8, acked_at=10, parent_span_id="r1"),
        _span(span_id="r2", trace_id=trace_id, source="ORCH", dest="LEAD1", payload_type="Task", direction="receive", enqueued_at=10, acked_at=12),
        _span(span_id="s3", trace_id=trace_id, source="LEAD1", dest="WORKER1", payload_type="Sub", direction="send", enqueued_at=12, acked_at=14, parent_span_id="r2"),
        _span(span_id="r3", trace_id=trace_id, source="LEAD1", dest="WORKER1", payload_type="Sub", direction="receive", enqueued_at=14, acked_at=16),
        _span(span_id="s5", trace_id=trace_id, source="ORCH", dest="LEAD2", payload_type="Task", direction="send", enqueued_at=9, acked_at=11, parent_span_id="r1"),
        _span(span_id="r5", trace_id=trace_id, source="ORCH", dest="LEAD2", payload_type="Task", direction="receive", enqueued_at=11, acked_at=13),
        _span(
            span_id="s6", trace_id=trace_id, source="LEAD2", dest="WORKER2", payload_type="Sub2", direction="send",
            enqueued_at=13, acked_at=700, state="dropped", error="Unable to resolve destination endpoint",
            parent_span_id="r5",
        ),
    ]
    for s in spans:
        await insert_span(db_path, s)


class OverviewViewTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _boot(self, pilot):
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await asyncio.sleep(0.1)
        await pilot.pause()

    def test_overview_is_the_default_view_and_covers_every_agent(self):
        trace_id = "trace-overview"

        async def run():
            await _seed_overview_trace(self.db_path, trace_id)
            setup = WatchSetup(
                addresses={"ORCH", "LEAD1", "LEAD2"}, names={}, filter_only=True,
                db_path=self.db_path, orchestrator="ORCH",
            )
            self.assertEqual(setup.view_mode, "overview")  # the new default, unset here
            app = LiveApp(setup)
            async with app.run_test(size=(240, 60)) as pilot:
                await self._boot(pilot)
                app._active_trace_id = trace_id
                await app._refresh_display()
                await pilot.pause()

                content = app.query_one("#diagram-content", DiagramCanvas)
                self.assertEqual(
                    set(content.hit_regions),
                    {"EXTERNAL", "GATEWAY", "ORCH", "LEAD1", "WORKER1", "LEAD2", "WORKER2"},
                )

        asyncio.run(run())

    def test_clicking_a_non_leg_agent_shows_real_detail_not_no_detail(self):
        # WORKER1 is one level below LEAD1 -- not one of ORCH's own direct
        # legs (state.legs only covers LEAD1/LEAD2) -- the inspector must
        # still resolve real detail via the causal tree fallback, not
        # "No detail for this agent in the current trace."
        trace_id = "trace-overview-click"

        async def run():
            await _seed_overview_trace(self.db_path, trace_id)
            setup = WatchSetup(
                addresses={"ORCH", "LEAD1", "LEAD2"}, names={}, filter_only=True,
                db_path=self.db_path, orchestrator="ORCH",
            )
            app = LiveApp(setup)
            async with app.run_test(size=(240, 60)) as pilot:
                await self._boot(pilot)
                app._active_trace_id = trace_id
                await app._refresh_display()
                await pilot.pause()

                content = app.query_one("#diagram-content", DiagramCanvas)
                x0, y0, x1, y1 = content.hit_regions["WORKER1"]
                await pilot.click("#diagram-content", offset=((x0 + x1) // 2, (y0 + y1) // 2))
                await pilot.pause()

                self.assertEqual(app._selected_agent, "WORKER1")
                inspector = app.query_one("#inspector-content")
                inspector_text = inspector._Static__content
                plain = inspector_text.plain if hasattr(inspector_text, "plain") else str(inspector_text)
                self.assertNotIn("No detail for this agent", plain)
                self.assertIn("WORKER1", plain)

        asyncio.run(run())

    def test_failed_worker_reads_failed_in_inspector(self):
        trace_id = "trace-overview-fail"

        async def run():
            await _seed_overview_trace(self.db_path, trace_id)
            setup = WatchSetup(
                addresses={"ORCH", "LEAD1", "LEAD2"}, names={}, filter_only=True,
                db_path=self.db_path, orchestrator="ORCH",
            )
            app = LiveApp(setup)
            async with app.run_test(size=(240, 60)) as pilot:
                await self._boot(pilot)
                app._active_trace_id = trace_id
                await app._refresh_display()
                await pilot.pause()

                content = app.query_one("#diagram-content", DiagramCanvas)
                x0, y0, x1, y1 = content.hit_regions["WORKER2"]
                await pilot.click("#diagram-content", offset=((x0 + x1) // 2, (y0 + y1) // 2))
                await pilot.pause()

                inspector = app.query_one("#inspector-content")
                inspector_text = inspector._Static__content
                plain = inspector_text.plain if hasattr(inspector_text, "plain") else str(inspector_text)
                self.assertIn("resolve", plain)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
