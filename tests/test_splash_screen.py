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
    """Regression coverage for the inverted hero/byline redesign: "uAgent
    Trace" as a large ASCII banner (the hero, bold) with the fetch.ai
    braille mark small underneath (the byline, normal weight) -- and,
    critically, exactly one rendering of that body. The original bug
    rendered the title text twice at once: once folded into the logo-row
    list (because `"uAgent Trace".strip()` is truthy, so the centered
    caption row survived a filter meant to drop only blank rows) and once
    appended again after. Comparing the fully-revealed content against
    `_SPLASH_BODY_LINES` exactly -- not just "does it crash" -- is what
    catches that shape of regression: an extra, duplicated row sneaking
    back into the body.

    A second bug fixed since: the hero was originally rasterized into
    braille too, which only gives each letter a couple of 2x4 dot-cells at
    a legible width and reads as noise, not text. `HERO_BANNER` (brand.py)
    replaces that with a figlet-style block-letter banner (ANSI Shadow),
    solid-filled per character cell instead of sub-cell dots.
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
                    screen._reveal(len(_SPLASH_BODY_LINES) - 1)
                content = screen.query_one("#splash-content")
                return content.content

        return asyncio.run(run())

    def _full_reveal_content(self, width: int):
        return self._splash_content(width, force_full_reveal=True)

    def test_full_reveal_renders_splash_body_exactly_once(self):
        # The strongest form of "no duplicate": the fully-revealed content
        # must equal the single source-of-truth body line-for-line, with
        # nothing extra appended after it.
        text = self._full_reveal_content(120)
        self.assertEqual(text.plain, "\n".join(_SPLASH_BODY_LINES))

    def test_full_reveal_has_no_duplicate_rows(self):
        # A duplicated title would show up as extra lines beyond the
        # expected body -- this is the shape the original bug took (one
        # inside the logo-row loop, one appended after).
        text = self._full_reveal_content(120)
        self.assertEqual(text.plain.count("\n") + 1, len(_SPLASH_BODY_LINES))

    def test_hero_rows_are_bold_and_byline_rows_are_not(self):
        # "uAgent Trace" is the hero (bold, dominant); "fetch.ai" is the
        # subordinate byline (normal weight) directly beneath it.
        text = self._full_reveal_content(120)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            # Rich stores per-substring style in `.spans`, not on `.style`
            # (the Text's own base style, which stays unset here).
            is_bold = any("bold" in (span.style or "") for span in line.spans)
            expected_bold = i < _SPLASH_HERO_ROW_COUNT
            self.assertEqual(is_bold, expected_bold, f"row {i} bold={is_bold}, expected {expected_bold}")

    def test_body_is_centered(self):
        # `justify="center"` is what centers the narrower byline rows (and
        # the blank separator) under the wider hero rows once Textual
        # sizes this auto-width widget to the hero's width -- without it
        # they'd read left-aligned against column 0 instead of centered.
        text = self._full_reveal_content(120)
        self.assertEqual(text.justify, "center")

    def test_degrade_floor_renders_single_line_title_only(self):
        # Below SPLASH_MIN_WIDTH_FOR_LOGO the splash shows only the plain
        # title -- one line, no braille body, no duplicate. This path
        # never calls `_reveal` at all (it returns straight out of
        # `on_mount`), so the content is already final after mount.
        text = self._splash_content(SPLASH_MIN_WIDTH_FOR_LOGO - 1, force_full_reveal=False)
        self.assertEqual(text.plain, _BRAND_TITLE_LINE)
        self.assertNotIn("\n", text.plain)

    def test_full_body_renders_at_several_wide_terminal_widths(self):
        # The hero/byline unit must draw in full (not degrade, not crash,
        # not truncate) at a range of terminal widths at or above the
        # degrade floor.
        for width in (SPLASH_MIN_WIDTH_FOR_LOGO, SPLASH_MIN_WIDTH_FOR_LOGO + 20, 160):
            with self.subTest(width=width):
                text = self._full_reveal_content(width)
                self.assertEqual(text.plain, "\n".join(_SPLASH_BODY_LINES))

    def test_hero_banner_is_the_expected_ansi_shadow_art(self):
        # Hardcoded against the actual generated art (not against
        # `_SPLASH_BODY_LINES`, which would just be comparing the banner to
        # itself) -- this is the regression net for the banner's *content*:
        # if `HERO_BANNER` in brand.py is ever hand-edited or regenerated
        # with a different font/string and silently breaks, this fails.
        from uagents_trace.brand import HERO_BANNER

        expected = (
            "██╗   ██╗ █████╗  ██████╗ ███████╗███╗   ██╗████████╗    ████████╗██████╗  █████╗  ██████╗███████╗\n"
            "██║   ██║██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝    ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝\n"
            "██║   ██║███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║          ██║   ██████╔╝███████║██║     █████╗\n"
            "██║   ██║██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║          ██║   ██╔══██╗██╔══██║██║     ██╔══╝\n"
            "╚██████╔╝██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║          ██║   ██║  ██║██║  ██║╚██████╗███████╗\n"
            " ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝          ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝"
        )
        self.assertEqual(HERO_BANNER.strip("\n"), expected)

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
