"""Regression tests for `SplashScreen`'s fade-out.

Animating `Screen`/`Widget.opacity` directly crashes: it's a read-only,
ancestor-derived property (see `Widget.opacity`), not the settable CSS
value -- that lives on `widget.styles.opacity`. The fix animates a child
container's `.styles.opacity` instead. These tests run the fade for real
(through Textual's actual animator) rather than asserting on the code
directly, so a regression back to animating the wrong attribute is caught
by an actual crash, the same way the original bug was found.
"""

import os
import tempfile
import unittest
from unittest import mock

from uagents_trace.live import LiveApp, SplashScreen
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


if __name__ == "__main__":
    unittest.main()
