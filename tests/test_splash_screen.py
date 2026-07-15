"""Regression tests for `SplashScreen`'s fade-out.

Three fixes down this path so far, in order:

1. Animating `Screen`/`Widget.opacity` directly crashes: it's a read-only,
   ancestor-derived property, not the settable CSS value (`widget.styles
   .opacity`). Fixed by animating `.styles.opacity` instead.
2. The splash used to be a Container wrapping a separate content Static,
   with the *Container's* opacity animated -- two independently-composited
   widgets, which in practice faded unevenly (some glyphs read as already-
   dimmed while others were still bright partway through). Fixed by
   collapsing to a single Static, with the fade animating that same
   widget's own opacity.
3. Even a single widget's opacity ramp still looked patchy in a real
   terminal -- the suspected cause is opacity *compositing*: different
   glyph colors don't necessarily round to the same apparent brightness
   when the terminal quantizes an intermediate opacity blend. Fixed by
   dropping opacity entirely in favor of discrete color-interpolation:
   `_start_fade`/`_fade_step` recolor the single content widget's Rich
   `Text` through explicit hex steps from each element's resting color
   down to the splash background, all elements moving through their own
   ramp at the same step index in lockstep -- no compositing left for the
   terminal to get inconsistent about, just solid color swaps.

Only fix 3's smoothness can't be judged here: it needs a real terminal,
not this harness (see the module using this, and whoever is reading these
results, for that confirmation). These tests instead pin the parts that
*are* mechanically verifiable -- which widget the fade touches, that
opacity is never touched, that all three ramps share one step index, and
that the sequence still reaches `_finish` and pops the screen.
"""

import os
import tempfile
import unittest
from unittest import mock

from uagents_trace.live import (
    _BRAND_TITLE_LINE,
    _HERO_LINES,
    _LOCKUP_DIVIDER,
    _SIDE_BY_SIDE_LINES,
    _STACKED_HERO_ROW_COUNT,
    _STACKED_LINES,
    SPLASH_HERO_GREEN,
    SPLASH_MIN_WIDTH_SIDE_BY_SIDE,
    SPLASH_MIN_WIDTH_STACKED,
    LiveApp,
    SplashScreen,
)
from uagents_trace.store import init_db
from uagents_trace.wizard import WatchSetup

import uagents_trace.live as live_mod


def _make_app(db_path: str) -> LiveApp:
    setup = WatchSetup(addresses={"a"}, names={"a": "Alice"}, filter_only=False, db_path=db_path)
    return LiveApp(setup)


class SplashScreenFadeTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_splash_dismisses_on_keypress(self):
        # Existing smoke test kept intact: an early keypress should skip
        # the splash immediately, independent of the fade animation.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, SplashScreen)
                await pilot.press("x")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, SplashScreen)

        asyncio.run(run())

    def test_full_timed_fade_pops_exactly_once(self):
        # Let reveal, hold, and fade all run for real (shortened so the
        # test doesn't take the full ~2s production timing), then confirm
        # the splash is gone, the main screen is intact, and nothing
        # crashed -- this is the path that used to raise AttributeError.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            with mock.patch.object(live_mod, "SPLASH_ROW_STAGGER_SECONDS", 0.001), mock.patch.object(
                live_mod, "SPLASH_HOLD_SECONDS", 0.05
            ), mock.patch.object(live_mod, "SPLASH_FADE_SECONDS", 0.1):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    # Generous margin over reveal+hold+fade (~0.2s) to absorb
                    # test-harness startup overhead -- this only needs to
                    # prove the sequence *eventually* settles correctly, not
                    # pin an exact frame.
                    await asyncio.sleep(1.0)
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertNotIsInstance(app.screen, SplashScreen)

        asyncio.run(run())

    def test_keypress_mid_fade_does_not_double_pop(self):
        # Start the fade directly (deterministic -- no race against real
        # timers to land "mid-fade"), dismiss via keypress immediately
        # after while the animation is still in flight, then give that
        # same animation generous real time to actually finish and invoke
        # its on_complete. Before the fix, this on_complete firing after
        # on_key already popped the screen would try to pop again, popping
        # the main screen out from under the app instead.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            with mock.patch.object(live_mod, "SPLASH_FADE_SECONDS", 0.1):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    screen = app.screen
                    self.assertIsInstance(screen, SplashScreen)

                    screen._start_fade()
                    await pilot.press("x")
                    await pilot.pause()
                    self.assertTrue(screen._dismissed)
                    self.assertEqual(len(app.screen_stack), 1)

                    # Give the already-running fade animation time to
                    # finish and attempt its on_complete callback.
                    await asyncio.sleep(0.5)
                    await pilot.pause()

                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertNotIsInstance(app.screen, SplashScreen)

        asyncio.run(run())

    def test_splash_is_a_single_content_widget_not_a_wrapped_container(self):
        # The patchy-fade bug came from a Container wrapping a separate
        # content Static -- two independently-composited widgets, so
        # animating the Container's opacity didn't uniformly dim the
        # Static's glyphs inside it. The fix removes that wrapper: there
        # must be no `#splash-body` (or any other) container between the
        # screen and `#splash-content` -- it's a direct child of the screen.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SplashScreen)
                self.assertEqual(len(screen.query("#splash-body")), 0)
                content = screen.query_one("#splash-content")
                self.assertIs(content.parent, screen)

        asyncio.run(run())

    def test_fade_recolors_the_single_content_widget_never_opacity(self):
        # The fade no longer animates opacity at all -- two prior opacity
        # attempts (a wrapping Container's, then a single widget's) both
        # looked patchy in a real terminal. It now steps `#splash-content`'s
        # own rendered Rich `Text` through explicit, progressively-darker
        # hex colors down to the splash background, applied to the *same*
        # single widget every draw-in row already shares -- never touching
        # `.styles.opacity`, which stays 1.0 throughout.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                content = screen.query_one("#splash-content")
                self.assertEqual(screen._tier, "stacked", "test assumes this width lands in the stacked tier")

                screen._fade_step(0)
                start_style = content.content.spans[0].style
                self.assertIn(SPLASH_HERO_GREEN, start_style)
                self.assertEqual(content.styles.opacity, 1.0)

                mid_step = live_mod.FADE_STEPS // 2
                screen._fade_step(mid_step)
                mid_style = content.content.spans[0].style
                self.assertNotIn(SPLASH_HERO_GREEN, mid_style)
                self.assertNotEqual(mid_style, start_style)
                self.assertEqual(content.styles.opacity, 1.0)

                screen._fade_step(live_mod.FADE_STEPS)
                end_style = content.content.spans[0].style
                self.assertIn(live_mod.SPLASH_BG, end_style)
                self.assertEqual(content.styles.opacity, 1.0)

        asyncio.run(run())

    def test_fade_step_colors_move_hero_divider_and_mark_in_lockstep(self):
        # Every step must recolor the whole lockup at once -- hero, divider,
        # and mark all indexed by the *same* step, not three independent
        # ramps that could drift out of sync with each other. Checked on
        # the side-by-side tier specifically, since it's the one tier where
        # hero and mark actually share rows (and so could visibly desync).
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(160, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                content = screen.query_one("#splash-content")
                self.assertEqual(
                    screen._tier, "side_by_side", "test assumes this width lands in the side-by-side tier"
                )

                step = live_mod.FADE_STEPS // 2
                screen._fade_step(step)
                hero_span, divider_span, mark_span = content.content.spans[:3]
                self.assertEqual(hero_span.style, f"bold {live_mod._HERO_FADE_COLORS[step]}")
                self.assertEqual(divider_span.style, live_mod._DIVIDER_FADE_COLORS[step])
                self.assertEqual(mark_span.style, live_mod._MARK_FADE_COLORS[step])

        asyncio.run(run())

    def test_fade_step_zero_is_the_resting_color_and_last_step_is_background(self):
        # Step 0 must be visually identical to the pre-fade resting state
        # (no visible jump when the fade begins), and the last step must
        # land exactly on the splash background -- the frame right before
        # `_finish` pops the screen should already read as "gone", not
        # merely "dimmer".
        self.assertEqual(live_mod._HERO_FADE_COLORS[0], SPLASH_HERO_GREEN)
        self.assertEqual(live_mod._HERO_FADE_COLORS[live_mod.FADE_STEPS], live_mod.SPLASH_BG)
        self.assertEqual(live_mod._MARK_FADE_COLORS[live_mod.FADE_STEPS], live_mod.SPLASH_BG)
        self.assertEqual(live_mod._DIVIDER_FADE_COLORS[live_mod.FADE_STEPS], live_mod.SPLASH_BG)

    def test_last_fade_step_finishes_and_pops_the_splash(self):
        # The fade must still eventually dismiss the splash -- reaching
        # `FADE_STEPS` is what used to be the animation's `on_complete`.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(app.screen, SplashScreen)

                screen._fade_step(live_mod.FADE_STEPS)
                await pilot.pause()

                self.assertEqual(len(app.screen_stack), 1)
                self.assertNotIsInstance(app.screen, SplashScreen)

        asyncio.run(run())

    def test_start_fade_forces_full_reveal_before_animating(self):
        # A dismiss mid-stagger (or a future timing change that lets the
        # fade timer fire before the last row's own reveal timer) must
        # never start the fade on a partially-drawn body -- `_start_fade`
        # forces the final, fully-revealed state itself before animating
        # opacity, regardless of how much of the stagger had actually run.
        import asyncio

        async def run():
            await init_db(self.db_path)
            app = _make_app(self.db_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertTrue(screen._active_rows, "expected a row-drawing tier at this width")

                # Simulate a mid-stagger state: only the first row drawn.
                screen._reveal(0)
                content = screen.query_one("#splash-content")
                partial = content.content.plain
                full_body = "\n".join(row.plain for row in screen._active_rows)
                self.assertNotEqual(partial, full_body, "test setup didn't actually leave the body partial")

                screen._start_fade()
                self.assertEqual(content.content.plain, full_body)

        asyncio.run(run())


class SplashAlwaysPresentTests(unittest.TestCase):
    """The splash must push on *every* launch, not just the first -- a
    "warm run skips it" regression wouldn't show up mounting the app once,
    since a single mount can't tell "first ever" apart from "subsequent".
    Mounting several times in one process is what would catch a stateful
    module/class-level gate (a flag that flips true after the first push
    and short-circuits every push after) if one were ever introduced.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_splash_present_on_every_mount_in_same_process(self):
        import asyncio

        async def mount_and_check(n: int):
            setup = WatchSetup(addresses={"a"}, names={"a": "Alice"}, filter_only=False, db_path=self.db_path)
            app = LiveApp(setup)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIsInstance(
                    app.screen,
                    SplashScreen,
                    f"splash missing on launch #{n} -- warm-run skip regression",
                )
                self.assertIn(SplashScreen, [type(s) for s in app.screen_stack])

        async def run():
            await init_db(self.db_path)
            # Five fresh app instances, sequentially, in this one test
            # process/interpreter -- nothing about launch 2+ should differ
            # from launch 1 as far as the splash is concerned.
            for n in range(1, 6):
                await mount_and_check(n)

        asyncio.run(run())

    def test_splash_push_is_first_statement_in_on_mount(self):
        # Structural guard for the actual fix: the push must happen before
        # any other on_mount work, so nothing later (title, inspector
        # visibility, bootstrap) can raise and prevent it from ever being
        # attempted. Inspects source rather than behavior because "nothing
        # before the push can throw" isn't otherwise observable from
        # outside -- there's no exception to provoke in a passing setup.
        import inspect

        from uagents_trace.live import LiveApp as _LiveApp

        source = inspect.getsource(_LiveApp.on_mount)
        push_line = next(i for i, line in enumerate(source.splitlines()) if "push_screen(SplashScreen())" in line)
        body_lines = [
            line
            for line in source.splitlines()[1:push_line]
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(
            body_lines,
            [],
            "on_mount does work before pushing the splash -- a failure there "
            "would silently skip the splash",
        )


class SplashBodyStructureTests(unittest.TestCase):
    """Regression coverage for the co-branded, side-by-side lockup: the
    "uAgents Trace" figlet hero (bold) and the full-resolution fetch.ai
    braille mark (normal weight) on shared rows, divided by a thin vertical
    rule -- with a three-tier degrade (side-by-side -> stacked -> title-only)
    as the terminal narrows, and critically exactly one rendering of
    whichever tier is active. The original bug (before the side-by-side
    redesign) rendered the title text twice at once: once folded into the
    logo-row list (because `"uAgent Trace".strip()` is truthy, so the
    centered caption row survived a filter meant to drop only blank rows)
    and once appended again after. Comparing the fully-revealed content
    against the active tier's own source-of-truth line list exactly -- not
    just "does it crash" -- is what catches that shape of regression: an
    extra, duplicated row sneaking back into the body.

    Earlier bugs/iterations fixed along the way: the hero was originally
    rasterized into braille (illegible at letter size), then several
    mixed-case figlet fonts were tried chasing a lowercase "u" -- `smslant`
    (too thin/disconnected), `standard` (upright but still hollow), `big`
    (heavier, but still visually lighter than the fetch.ai mark's solid
    braille fill). `HERO_BANNER` (brand.py) settled on ANSI Shadow -- solid,
    double-line block glyphs, closest in visual weight to the mark, at the
    cost of being all-caps (this font has no lowercase forms; an accepted
    tradeoff) -- but rendered as a single line it was 106 columns, far
    wider than the mark's 72, forcing a lopsided lockup and a very wide
    side-by-side breakpoint. It's now rendered as two words stacked
    vertically ("TRACE" above "UAGENTS", each its own ANSI Shadow banner,
    "TRACE" horizontally centered over "UAGENTS" by a single constant
    left-pad applied to every one of its rows), which roughly halves the
    width (61 cols) at the cost of roughly doubling the row count (12 vs
    the mark's 7) -- a much better width match, and the shorter mark is
    what gets vertically centered against the hero's height now, not the
    other way around. The hero is rendered in `SPLASH_HERO_GREEN`, a
    bright, pre-dim green scoped to just the splash hero -- not the shared,
    deliberately-dimmed `ACCENT`/`SUCCESS` used everywhere else in the live
    TUI -- so the hero reads as the one bright thing on screen next to a
    calm, unbrightened fetch.ai mark. The mark itself uses the
    full-resolution `FETCH_BRAND` art, never the deleted downsampled copy
    (which used to drop dots and read broken). Across all of these font
    swaps the side-by-side lockup structure itself -- hero and mark on
    shared rows, divided by a vertical rule, each centered vertically
    against the other -- has never changed; only the hero's own rendering
    has.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _splash_content(self, width: int, *, force_full_reveal: bool):
        # Drives the splash directly (`_reveal` called synchronously) rather
        # than shortening timers and racing real wall-clock time against
        # them: mounting the app alone was observed to take over a second
        # of real time in this harness, which is longer than a shortened
        # hold/stagger would survive -- the splash would auto-dismiss
        # before the test ever got to inspect it. Calling `_reveal`
        # directly sidesteps that race entirely (mirrors the existing
        # `test_keypress_mid_fade_does_not_double_pop`, which drives
        # `_start_fade` the same way instead of waiting on real timers).
        import asyncio

        async def run():
            await init_db(self.db_path)
            setup = WatchSetup(addresses={"a"}, names={"a": "Alice"}, filter_only=False, db_path=self.db_path)
            app = LiveApp(setup)
            async with app.run_test(size=(width, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, SplashScreen)
                if force_full_reveal:
                    # Whichever tier `on_mount` picked for this width, its
                    # rows are already sitting on `screen._active_rows` --
                    # reveal all of them, regardless of which tier it is.
                    screen._reveal(len(screen._active_rows) - 1)
                content = screen.query_one("#splash-content")
                return content.content

        return asyncio.run(run())

    def _full_reveal_content(self, width: int):
        return self._splash_content(width, force_full_reveal=True)

    def test_full_reveal_renders_side_by_side_body_exactly_once(self):
        # At or above the side-by-side breakpoint, the fully-revealed
        # content must equal the side-by-side tier's own source-of-truth
        # rows line-for-line, with nothing extra appended after them.
        text = self._full_reveal_content(SPLASH_MIN_WIDTH_SIDE_BY_SIDE)
        self.assertEqual(text.plain, "\n".join(_SIDE_BY_SIDE_LINES))
        self.assertEqual(text.plain.count("\n") + 1, len(_SIDE_BY_SIDE_LINES))

    def test_full_reveal_renders_stacked_body_exactly_once(self):
        # Between the two breakpoints, the fully-revealed content must
        # equal the stacked tier's rows instead -- not the side-by-side
        # ones, and not a truncated/duplicated version of either.
        width = (SPLASH_MIN_WIDTH_STACKED + SPLASH_MIN_WIDTH_SIDE_BY_SIDE) // 2
        text = self._full_reveal_content(width)
        self.assertEqual(text.plain, "\n".join(_STACKED_LINES))
        self.assertEqual(text.plain.count("\n") + 1, len(_STACKED_LINES))

    def test_side_by_side_rows_share_both_marks(self):
        # The point of the co-branded lockup: hero and fetch.ai mark sit on
        # the *same* rows (divided by a vertical rule), not one above the
        # other. Every row of the side-by-side tier must carry the divider,
        # including rows where the shorter mark is only blank padding --
        # that's what makes the rule "span the taller of the two" rather
        # than stopping short wherever the shorter mark runs out.
        for row in _SIDE_BY_SIDE_LINES:
            self.assertIn(_LOCKUP_DIVIDER, row)

    def test_side_by_side_marks_are_vertically_centered_against_each_other(self):
        # The hero and the fetch.ai mark differ in height, so whichever one
        # is shorter must be padded top and bottom to center it against the
        # taller one -- not top- or bottom-aligned, which would leave it
        # hugging one edge with all the slack on the other side. Written
        # generically (not assuming which block is shorter): which one that
        # is has flipped before as the hero's font changed (`standard` was
        # shorter than the mark; `big` is taller), and this test should
        # survive that without being rewritten again. An *odd* height
        # difference can't split into equal integer halves (a leftover row
        # has to land somewhere), so top/bottom padding is allowed to differ
        # by at most that one row -- anything more would mean the shorter
        # block isn't actually centered, just padded on one side.
        hero_side = [row.split(_LOCKUP_DIVIDER)[0] for row in _SIDE_BY_SIDE_LINES]
        mark_side = [row.split(_LOCKUP_DIVIDER)[1] for row in _SIDE_BY_SIDE_LINES]
        height = len(_SIDE_BY_SIDE_LINES)

        def _padding(side: list[str]) -> tuple[int, int]:
            content_rows = [i for i, r in enumerate(side) if r.strip()]
            return content_rows[0], height - 1 - content_rows[-1]

        hero_pad_before, hero_pad_after = _padding(hero_side)
        mark_pad_before, mark_pad_after = _padding(mark_side)

        # Exactly one side should be unpadded (occupies every row) -- the
        # taller block -- and the other should carry the height difference
        # as blank padding, split as evenly as an odd remainder allows.
        hero_unpadded = hero_pad_before == 0 and hero_pad_after == 0
        mark_unpadded = mark_pad_before == 0 and mark_pad_after == 0
        self.assertNotEqual(hero_unpadded, mark_unpadded, "exactly one block should be the unpadded, taller one")

        padded_before, padded_after = (mark_pad_before, mark_pad_after) if hero_unpadded else (
            hero_pad_before,
            hero_pad_after,
        )
        self.assertGreater(padded_before + padded_after, 0)
        self.assertLessEqual(
            abs(padded_before - padded_after),
            1,
            f"padding is lopsided: {padded_before} rows above vs {padded_after} below",
        )

    def test_hero_segment_is_bold_and_mark_segment_is_not_in_side_by_side(self):
        # In the side-by-side tier, boldness (and the bright hero color) is
        # per-segment (hero bold + bright, divider/mark normal weight and
        # un-brightened) rather than per-row, since both marks now live on
        # the same rows.
        text = self._full_reveal_content(SPLASH_MIN_WIDTH_SIDE_BY_SIDE)
        lines = text.split("\n")
        for i, (line, plain_row) in enumerate(zip(lines, _SIDE_BY_SIDE_LINES)):
            divider_col = plain_row.index(_LOCKUP_DIVIDER)
            hero_spans = [s for s in line.spans if s.end <= divider_col]
            mark_spans = [s for s in line.spans if s.start > divider_col]
            self.assertTrue(hero_spans, f"row {i} has no styled hero segment")
            self.assertTrue(all("bold" in (s.style or "") for s in hero_spans), f"row {i} hero segment not bold")
            self.assertTrue(
                all(SPLASH_HERO_GREEN in (s.style or "") for s in hero_spans),
                f"row {i} hero segment not rendered in SPLASH_HERO_GREEN",
            )
            self.assertTrue(
                all("bold" not in (s.style or "") for s in mark_spans), f"row {i} mark segment unexpectedly bold"
            )
            self.assertTrue(
                all(SPLASH_HERO_GREEN not in (s.style or "") for s in mark_spans),
                f"row {i} mark segment unexpectedly rendered in the bright hero color",
            )

    def test_hero_rows_are_bold_and_mark_rows_are_not_in_stacked(self):
        # In the stacked tier, hero and mark occupy separate rows, so
        # boldness (and the bright hero color) is per-row: hero rows bold
        # and bright, mark rows (and the blank separator) normal weight and
        # un-brightened.
        width = (SPLASH_MIN_WIDTH_STACKED + SPLASH_MIN_WIDTH_SIDE_BY_SIDE) // 2
        text = self._full_reveal_content(width)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            is_bold = any("bold" in (span.style or "") for span in line.spans)
            is_bright = any(SPLASH_HERO_GREEN in (span.style or "") for span in line.spans)
            expected_hero = i < _STACKED_HERO_ROW_COUNT
            self.assertEqual(is_bold, expected_hero, f"row {i} bold={is_bold}, expected {expected_hero}")
            self.assertEqual(is_bright, expected_hero, f"row {i} bright={is_bright}, expected {expected_hero}")

    def test_title_only_tier_also_uses_bright_hero_color(self):
        # The title-only tier is the hero degraded to plain text, not a
        # different element -- it must keep the same bright color as the
        # other two tiers rather than quietly falling back to ACCENT. This
        # tier sets its style via the `Text(..., style=...)` constructor
        # (a single-run Text, not built up with per-substring `.append`
        # calls), so Rich stores it as the Text's own base `.style`, not as
        # an entry in `.spans` -- unlike the other two tiers' rows.
        text = self._splash_content(SPLASH_MIN_WIDTH_STACKED - 1, force_full_reveal=False)
        self.assertIn(SPLASH_HERO_GREEN, text.style or "")

    def test_splash_hero_green_is_the_pre_dim_success_value_and_scoped_to_the_hero(self):
        # SPLASH_HERO_GREEN must be the bright, pre-dim green -- the same
        # `#4ade80`-family value `wizard.py`'s prompt style still uses --
        # not an arbitrary new color, and using it must not have touched
        # the shared, deliberately-dimmed ACCENT/SUCCESS constants that the
        # rest of the live TUI's color-economy pass depends on staying dim.
        from uagents_trace import network_canvas
        from uagents_trace import wizard

        self.assertEqual(SPLASH_HERO_GREEN, "#4ade80")
        self.assertEqual(SPLASH_HERO_GREEN, wizard.SUCCESS)
        self.assertNotEqual(SPLASH_HERO_GREEN, network_canvas.SUCCESS)
        self.assertEqual(network_canvas.SUCCESS, "#3f8f66", "network_canvas.SUCCESS must stay dim")

    def test_body_is_centered(self):
        # `justify="center"` is what centers the whole lockup block --
        # whichever tier is active -- against its own widest row, so
        # shorter rows read centered instead of left-aligned against
        # column 0.
        text = self._full_reveal_content(SPLASH_MIN_WIDTH_SIDE_BY_SIDE)
        self.assertEqual(text.justify, "center")

    def test_degrade_floor_renders_single_line_title_only(self):
        # Below SPLASH_MIN_WIDTH_STACKED the splash shows only the plain
        # title -- one line, no lockup body, no duplicate. This path never
        # calls `_reveal` at all (it returns straight out of `on_mount`),
        # so the content is already final after mount.
        text = self._splash_content(SPLASH_MIN_WIDTH_STACKED - 1, force_full_reveal=False)
        self.assertEqual(text.plain, _BRAND_TITLE_LINE)
        self.assertEqual(_BRAND_TITLE_LINE, "uAgents Trace")
        self.assertNotIn("\n", text.plain)

    def test_each_degrade_tier_renders_at_its_own_width(self):
        # Each tier must draw in full (not degrade, not crash, not
        # truncate) somewhere within its own width band, and exactly the
        # tier that width band implies -- not one of the others.
        cases = [
            (SPLASH_MIN_WIDTH_SIDE_BY_SIDE, _SIDE_BY_SIDE_LINES),
            (SPLASH_MIN_WIDTH_SIDE_BY_SIDE + 30, _SIDE_BY_SIDE_LINES),
            (SPLASH_MIN_WIDTH_STACKED, _STACKED_LINES),
            (SPLASH_MIN_WIDTH_SIDE_BY_SIDE - 1, _STACKED_LINES),
        ]
        for width, expected_lines in cases:
            with self.subTest(width=width):
                text = self._full_reveal_content(width)
                self.assertEqual(text.plain, "\n".join(expected_lines))

    def test_hero_banner_is_the_expected_stacked_ansi_shadow_art(self):
        # Hardcoded against the actual generated art (not against
        # `_SIDE_BY_SIDE_LINES`/`_STACKED_LINES`, which would just be
        # comparing the banner to itself) -- this is the regression net for
        # the banner's *content*: if `HERO_BANNER` in brand.py is ever
        # hand-edited or regenerated differently and silently breaks, this
        # fails. Generated as two separate word banners --
        # `pyfiglet.figlet_format("Trace", font="ansi_shadow", width=200)`
        # and `("uAgents", font="ansi_shadow", width=200)` -- each with its
        # trailing all-blank filler row dropped, "Trace" (the narrower
        # word) center-padded to "uAgents"'s width by a single constant
        # left-pad applied to every one of its rows, then stacked "Trace"
        # directly above "uAgents" with no separator row between them.
        from uagents_trace.brand import HERO_BANNER

        expected = (
            "          ████████╗██████╗  █████╗  ██████╗███████╗\n"
            "          ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝\n"
            "             ██║   ██████╔╝███████║██║     █████╗\n"
            "             ██║   ██╔══██╗██╔══██║██║     ██╔══╝\n"
            "             ██║   ██║  ██║██║  ██║╚██████╗███████╗\n"
            "             ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝\n"
            "██╗   ██╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗\n"
            "██║   ██║██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝\n"
            "██║   ██║███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗\n"
            "██║   ██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║\n"
            "╚██████╔╝██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║\n"
            " ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝"
        )
        self.assertEqual(HERO_BANNER.strip("\n"), expected)

    def test_hero_words_are_on_separate_row_groups(self):
        # The point of stacking: "TRACE" and "UAGENTS" are two distinct
        # word-blocks, not one run-together line -- the top half of the
        # hero's rows must match the standalone "Trace" banner (once its
        # constant center-pad is stripped), and the bottom half must match
        # the standalone "uAgents" banner exactly, with no row mixing
        # content from both.
        top_half = _HERO_LINES[: len(_HERO_LINES) // 2]
        bottom_half = _HERO_LINES[len(_HERO_LINES) // 2 :]

        expected_trace = [
            "████████╗██████╗  █████╗  ██████╗███████╗",
            "╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝",
            "   ██║   ██████╔╝███████║██║     █████╗",
            "   ██║   ██╔══██╗██╔══██║██║     ██╔══╝",
            "   ██║   ██║  ██║██║  ██║╚██████╗███████╗",
            "   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝",
        ]
        expected_uagents = [
            "██╗   ██╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗",
            "██║   ██║██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝",
            "██║   ██║███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗",
            "██║   ██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║",
            "╚██████╔╝██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║",
            " ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝",
        ]

        pad_left = 10  # (61 - 41) // 2, the constant left-pad every TRACE row shares
        stripped_top = [line[pad_left:].rstrip() for line in top_half]
        self.assertEqual(stripped_top, expected_trace)
        self.assertEqual([l.rstrip() for l in bottom_half], expected_uagents)

    def test_hero_banner_has_no_braille_dot_cells(self):
        # Guards against reverting to the illegible braille rasterization:
        # the banner must be built from full block/box-drawing characters,
        # not U+2800-U+28FF braille dot-cells.
        for ch in _SPLASH_BODY_LINES[0]:
            self.assertFalse(
                0x2800 <= ord(ch) <= 0x28FF,
                f"hero row contains a braille dot-cell {ch!r} -- banner should be block/box-drawing text",
            )

    def test_fetch_mark_is_present_and_centered_below_hero(self):
        # The byline is the fetch.ai mark (still braille -- only the hero
        # moved off braille), directly beneath the hero with one blank
        # separator row, and narrower than the hero so it reads as
        # subordinate rather than competing with it for attention.
        from uagents_trace.brand import FETCH_BRAND_SMALL

        expected_mark_lines = FETCH_BRAND_SMALL.strip("\n").split("\n")
        separator_index = _SPLASH_HERO_ROW_COUNT
        mark_lines = _SPLASH_BODY_LINES[separator_index + 1 :]

        self.assertEqual(_SPLASH_BODY_LINES[separator_index], "")
        self.assertEqual(mark_lines, expected_mark_lines)

        hero_width = max(len(line) for line in _SPLASH_BODY_LINES[:separator_index])
        mark_width = max(len(line) for line in mark_lines)
        self.assertLess(mark_width, hero_width, "fetch.ai byline should read smaller than the hero, not competing")

        # `justify="center"` (already checked in `test_body_is_centered`)
        # centers every row -- including these -- against the widest row,
        # so a narrower byline reads centered under the hero rather than
        # left-aligned against column 0.
        text = self._full_reveal_content(120)
        lines = text.split("\n")
        self.assertEqual([line.plain for line in lines[separator_index + 1 :]], expected_mark_lines)


if __name__ == "__main__":
    unittest.main()
