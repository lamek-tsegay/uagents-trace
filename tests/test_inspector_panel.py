"""Tests for the live TUI's right-hand inspector panel.

Covers: the panel scrolls when its content is taller than the viewport
(mouse wheel and keyboard), per-agent click-to-reveal detail, and the
empty-state hint shown before anything is clicked.
"""

import os
import tempfile
import unittest

from textual import events
from textual.containers import HorizontalScroll, VerticalScroll

from uagents_trace.live import DiagramCanvas, INSPECTOR_EMPTY_HINT, LiveApp
from uagents_trace.store import init_db, insert_span, set_alias
from uagents_trace.wizard import WatchSetup


def _hub_span(
    *,
    span_id,
    trace_id,
    source,
    dest,
    payload_type,
    enqueued_at,
    acked_at,
    state="delivered",
    error=None,
    payload_summary=None,
    protocol="P1",
):
    return dict(
        id=span_id,
        trace_id=trace_id,
        source_agent=source,
        dest_agent=dest,
        protocol=protocol,
        payload_type=payload_type,
        payload_size=5,
        enqueued_at=enqueued_at,
        acked_at=acked_at,
        state=state,
        source_registered=True,
        dest_registered=state != "dropped",
        error=error,
        session_id=trace_id,
        kind="send",
        detail=None,
        payload_summary=payload_summary,
        direction="send",
    )


async def _seed_hub_trace(
    db_path: str,
    *,
    trace_id="trace-hub",
    n_subagents=4,
    long_payload_for=None,
    fail_last=False,
) -> tuple[set, dict]:
    """An orchestrator with `n_subagents` sub-agents. `long_payload_for`
    (a 1-based index) gets a very long dispatch payload -- enough on its
    own, once wrapped to the inspector panel's width, to overflow the
    viewport and require scrolling. `fail_last` drops the last subagent's
    dispatch, for testing the per-agent error-detail path.
    """
    await init_db(db_path)
    names = {"orch": "Orchestrator"}
    addrs = {"orch"}
    for i in range(1, n_subagents + 1):
        sub = f"sub{i}"
        names[sub] = f"SubAgent{i}"
        addrs.add(sub)

        is_failure = fail_last and i == n_subagents
        payload_summary = "done deal" if not is_failure else None
        if long_payload_for == i:
            payload_summary = "x" * 3000

        await insert_span(
            db_path,
            _hub_span(
                span_id=f"{trace_id}-d{i}",
                trace_id=trace_id,
                source="orch",
                dest=sub,
                payload_type="Task",
                enqueued_at=0,
                acked_at=3 + i,
                state="dropped" if is_failure else "delivered",
                error="Unable to resolve destination endpoint" if is_failure else None,
                payload_summary=payload_summary,
            ),
        )
        if not is_failure:
            await insert_span(
                db_path,
                _hub_span(
                    span_id=f"{trace_id}-r{i}",
                    trace_id=trace_id,
                    source=sub,
                    dest="orch",
                    payload_type="Result",
                    enqueued_at=10,
                    acked_at=20 + i,
                    payload_summary="ack",
                ),
            )
    for addr, name in names.items():
        await set_alias(db_path, name, addr)
    return addrs, names


async def _boot(pilot):
    """Dismiss the splash and let the first bootstrap/render settle."""
    import asyncio

    await pilot.pause()
    await pilot.press("x")
    await pilot.pause()
    await asyncio.sleep(0.1)
    await pilot.pause()


async def _click_agent(pilot, app, address: str) -> None:
    """Simulate clicking the agent's box in the diagram -- computes the
    click offset from the same hit_regions the widget itself hit-tests
    against, rather than hardcoding coordinates. No left_pad term: Textual
    translates a click into the widget's own local content-coordinate
    space (absorbing both CSS alignment and any horizontal scroll offset)
    before `on_click` sees it, and `pilot.click`'s `offset=` is relative
    to that same widget-region math -- so a raw hit_region coordinate
    resolves to the right on-screen position either way.
    """
    content = app.query_one("#diagram-content", DiagramCanvas)
    x0, y0, x1, y1 = content.hit_regions[address]
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    await pilot.click("#diagram-content", offset=(cx, cy))
    await pilot.pause()


class InspectorScrollTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_clicked_agent_with_long_payload_overflows_viewport(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, long_payload_for=1)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub1")

                scroll = app.query_one("#inspector-scroll", VerticalScroll)
                self.assertGreater(
                    scroll.virtual_size.height,
                    scroll.size.height,
                    "a 3000-char wrapped payload should overflow the panel -- otherwise this "
                    "test isn't actually exercising the overflow/scroll case",
                )

        asyncio.run(run())

    def test_mouse_wheel_scrolls_inspector(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, long_payload_for=1)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub1")

                scroll = app.query_one("#inspector-scroll", VerticalScroll)
                before = scroll.scroll_offset.y
                event = events.MouseScrollDown(
                    scroll, x=5, y=5, delta_x=0, delta_y=3, button=0, shift=False, meta=False, ctrl=False
                )
                scroll._on_mouse_scroll_down(event)
                await pilot.pause()
                self.assertGreater(scroll.scroll_offset.y, before)

        asyncio.run(run())

    def test_keyboard_scrolls_inspector_when_focused(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, long_payload_for=1)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub1")

                scroll = app.query_one("#inspector-scroll", VerticalScroll)
                scroll.focus()
                await pilot.pause()
                self.assertIs(app.focused, scroll)

                await pilot.press("down")
                await pilot.pause()
                after_down = scroll.scroll_offset.y
                self.assertGreater(after_down, 0)

                await pilot.press("end")
                await pilot.pause()
                max_scroll = scroll.virtual_size.height - scroll.size.height
                self.assertGreaterEqual(scroll.scroll_offset.y, max_scroll - 1)

        asyncio.run(run())


class InspectorClickToRevealTests(unittest.TestCase):
    """Fix 3: the inspector shows nothing until an agent box is clicked,
    and then shows *only* that agent's detail -- not the whole trace.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _inspector_plain(self, app) -> str:
        widget = app.query_one("#inspector-content")
        content = widget._Static__content
        return content.plain if hasattr(content, "plain") else str(content)

    def test_empty_state_before_any_click(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)

                plain = self._inspector_plain(app)
                # The empty state is now the brand logo with the hint
                # underneath, not the bare hint string on its own.
                self.assertIn(INSPECTOR_EMPTY_HINT, plain)
                self.assertNotEqual(plain, INSPECTOR_EMPTY_HINT)
                # No agent, hop, or leg detail (payloads, protocol, timing)
                # should have leaked into the default view.
                self.assertNotIn("protocol", plain)
                self.assertNotIn("dispatch", plain)

                scroll = app.query_one("#inspector-scroll", VerticalScroll)
                self.assertIn("inspector-empty", scroll.classes)

        asyncio.run(run())

    def test_click_shows_only_that_agents_detail(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=4)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub2")

                plain = self._inspector_plain(app)
                self.assertIn("SubAgent2", plain)
                self.assertIn("protocol: P1", plain)
                self.assertIn("dispatch", plain)
                self.assertIn("total", plain)
                # Only the clicked agent -- the other three must not appear.
                self.assertNotIn("SubAgent1", plain)
                self.assertNotIn("SubAgent3", plain)
                self.assertNotIn("SubAgent4", plain)
                # The trace-level summary moved out of the inspector.
                self.assertNotIn("dispatched to", plain)

                scroll = app.query_one("#inspector-scroll", VerticalScroll)
                self.assertNotIn("inspector-empty", scroll.classes)

        asyncio.run(run())

    def test_clicking_different_agent_swaps_the_panel(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=4)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)

                await _click_agent(pilot, app, "sub1")
                self.assertIn("SubAgent1", self._inspector_plain(app))
                self.assertNotIn("SubAgent3", self._inspector_plain(app))

                await _click_agent(pilot, app, "sub3")
                plain = self._inspector_plain(app)
                self.assertIn("SubAgent3", plain)
                self.assertNotIn("SubAgent1", plain)

        asyncio.run(run())

    def test_click_shows_raw_error_for_failed_agent(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=3, fail_last=True)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub3")

                plain = self._inspector_plain(app)
                self.assertIn("SubAgent3", plain)
                self.assertIn("Unable to resolve destination endpoint", plain)

        asyncio.run(run())

    def test_switching_trace_resets_selection_to_empty_state(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, trace_id="trace-a", n_subagents=3)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)
                await _click_agent(pilot, app, "sub1")
                self.assertIn("SubAgent1", self._inspector_plain(app))

                # A second, separate trace for the same agents.
                await _seed_hub_trace(self.db_path, trace_id="trace-b", n_subagents=3)
                await app._refresh_trace_list()
                await app._select_trace("trace-b", follow=False)
                await app._refresh_display()

                plain = self._inspector_plain(app)
                self.assertIn(INSPECTOR_EMPTY_HINT, plain)
                self.assertNotIn("SubAgent1", plain)

        asyncio.run(run())


class DiagramScrollClickTests(unittest.TestCase):
    """Slice 2 of the scrollable-diagram work: `left_pad`'s manual
    centering-offset correction is gone from `DiagramCanvas.on_click`,
    replaced by trusting Textual's own click-coordinate translation (which
    already absorbs both CSS alignment and horizontal scroll offset before
    `on_click` ever sees the event -- confirmed empirically in the recon
    for this work, not just from documentation). These tests prove that
    trust is warranted inside the real running app, not just in isolated
    experiments -- especially the click-after-scroll case, which is the
    one old `left_pad` scheme could never have gotten right.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _inspector_plain(self, app) -> str:
        widget = app.query_one("#inspector-content")
        content = widget._Static__content
        return content.plain if hasattr(content, "plain") else str(content)

    def _diagram_plain(self, app) -> str:
        content = app.query_one("#diagram-content", DiagramCanvas)
        text = content._Static__content
        return text.plain if hasattr(text, "plain") else str(text)

    def _has_double_border_box(self, app, address: str) -> bool:
        """Whether the rendered diagram shows a double-line border (╔/╚)
        anywhere within the given agent's own hit_region rows -- the
        visual marker `build_hub_topology` draws only for the currently
        `selected` agent.
        """
        content = app.query_one("#diagram-content", DiagramCanvas)
        x0, y0, x1, y1 = content.hit_regions[address]
        lines = self._diagram_plain(app).split("\n")
        for y in range(y0, y1):
            if y >= len(lines):
                continue
            row = lines[y][x0:x1]
            if "╔" in row or "╚" in row or "║" in row:
                return True
        return False

    def test_click_after_scroll_selects_the_visible_agent(self):
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=5)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            # Narrow enough that a 5-agent diagram (natural floor ~125
            # cols) can't fit -- forces #diagram-scroll to actually scroll.
            async with app.run_test(size=(150, 45)) as pilot:
                await _boot(pilot)

                content = app.query_one("#diagram-content", DiagramCanvas)
                scroller = app.query_one("#diagram-scroll", HorizontalScroll)
                self.assertGreater(
                    scroller.max_scroll_x, 0, "diagram should overflow its viewport for this to be a real test"
                )

                # sub5 is off-screen at the default hub-centered scroll
                # position -- confirm the test's own premise before relying
                # on it.
                panel_width = app._diagram_panel_width()
                x0, _y0, x1, _y1 = content.hit_regions["sub5"]
                self.assertGreater(
                    x1,
                    scroller.scroll_offset.x + panel_width,
                    "test assumption violated: sub5 should start off-screen",
                )

                # Scroll it into view, then click it -- no left_pad term
                # anywhere in this path (see _click_agent).
                scroller.scroll_to(x=scroller.max_scroll_x, animate=False)
                await pilot.pause()
                await _click_agent(pilot, app, "sub5")

                self.assertEqual(app._selected_agent, "sub5")
                self.assertIn("SubAgent5", self._inspector_plain(app))
                # Selection indicator (item 5): the double-border box shows
                # up on sub5 specifically, in the scrolled case.
                self.assertTrue(self._has_double_border_box(app, "sub5"))

        asyncio.run(run())

    def test_click_fits_without_scroll_still_selects_correctly(self):
        """Regression guard for the common case: with left_pad gone, a
        diagram that fits (no scrolling involved at all) must still center
        via CSS alone and still click correctly.
        """
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=2)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(240, 45)) as pilot:
                await _boot(pilot)

                scroller = app.query_one("#diagram-scroll", HorizontalScroll)
                self.assertEqual(scroller.max_scroll_x, 0, "diagram should fit without scrolling at this width")

                await _click_agent(pilot, app, "sub2")

                self.assertEqual(app._selected_agent, "sub2")
                self.assertIn("SubAgent2", self._inspector_plain(app))
                # Selection indicator (item 5): also present in the
                # unscrolled/CSS-centered-only case.
                self.assertTrue(self._has_double_border_box(app, "sub2"))

        asyncio.run(run())

    def test_click_at_stale_prescroll_position_does_not_select_moved_agent(self):
        """After scrolling, a click at the screen position an agent USED
        to occupy must not select that agent -- it should match whatever
        (if anything) is actually visible there now, not a stale
        hit_region-to-screen mapping from before the scroll.
        """
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, n_subagents=5)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(150, 45)) as pilot:
                await _boot(pilot)

                content = app.query_one("#diagram-content", DiagramCanvas)
                scroller = app.query_one("#diagram-scroll", HorizontalScroll)
                self.assertGreater(scroller.max_scroll_x, 0)

                # sub2's absolute on-screen position at the default
                # (hub-centered) scroll -- confirmed via the hit_region
                # math itself rather than an actual click, so this test
                # doesn't trigger an extra _refresh_display() (which would
                # re-arm the hub-centering scroll and confuse the timing).
                x0, y0, x1, y1 = content.hit_regions["sub2"]
                screen_x = content.region.x + (x0 + x1) // 2
                screen_y = content.region.y + (y0 + y1) // 2
                local_x = screen_x - content.region.x
                self.assertTrue(x0 <= local_x < x1, "test assumption: this screen position starts inside sub2's box")

                # Scroll further right -- sub2's box moves off that screen
                # position (shifts left on screen as scroll increases).
                scroller.scroll_to(x=scroller.max_scroll_x, animate=False)
                await pilot.pause()

                # Click the exact same absolute screen coordinates as
                # before scrolling.
                await pilot.click(offset=(screen_x, screen_y))
                await pilot.pause()

                self.assertNotEqual(app._selected_agent, "sub2")

        asyncio.run(run())


class InspectorVisibilityThresholdTests(unittest.TestCase):
    """MIN_WIDTH_FOR_INSPECTOR's hide/show boundary. Lowered from 228 to
    180 as the final slice of the scrollable-diagram work (see
    DiagramScrollClickTests above): the diagram no longer needs
    worst-case width reserved for it, since it scrolls instead of
    clipping when it doesn't fit -- so the threshold only needs to
    reserve a comfortable minimum. No test previously pinned the old 228
    value (confirmed via grep before this change), so this is new
    coverage, not an edit to an existing assertion.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _measure(self, width: int) -> tuple[bool, int]:
        """(inspector_visible, diagram_col_width) after boot at the given
        terminal width. Each call seeds its own trace_id, since a test
        method may call this more than once against the same db_path --
        reusing "trace-hub" (the seeder's default) would collide on span
        ids the second time.
        """
        import asyncio

        async def run():
            addrs, names = await _seed_hub_trace(self.db_path, trace_id=f"trace-{width}", n_subagents=2)
            setup = WatchSetup(addresses=addrs, names=names, filter_only=False, db_path=self.db_path, orchestrator="orch")
            app = LiveApp(setup)
            async with app.run_test(size=(width, 45)) as pilot:
                await _boot(pilot)
                visible = app.query_one("#inspector-col").display
                diagram_col_width = app.query_one("#diagram-col").size.width
            return visible, diagram_col_width

        return asyncio.run(run())

    def test_inspector_hidden_just_below_threshold(self):
        visible, _ = self._measure(179)
        self.assertFalse(visible)

    def test_inspector_shown_at_threshold(self):
        visible, _ = self._measure(180)
        self.assertTrue(visible)

    def test_diagram_col_reclaims_width_when_inspector_hidden(self):
        # No dead gap: #diagram-col (width: 1fr) should absorb roughly the
        # ~76-col inspector column (plus the space that would've separated
        # them) the moment the inspector hides, not leave it empty.
        _, width_hidden = self._measure(179)
        _, width_shown = self._measure(180)
        self.assertGreater(width_hidden, width_shown + 50)


if __name__ == "__main__":
    unittest.main()
