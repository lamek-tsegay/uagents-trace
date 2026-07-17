"""Shared pytest fixtures for the test suite.

`LiveApp.on_mount` registers two real, wall-clock `set_interval` callbacks
-- `_poll` (POLL_SECONDS) and `_pulse_tick` (PULSE_SECONDS) -- so the live
TUI can refresh itself against a SQLite file outside of tests. The
empty-state logo/shimmer/celebration animation chain (see LogoPhase in
live.py) schedules several more, all dynamically instead of once in
`on_mount`: `_start_logo_appearance` (one-shot, `LOGO_APPEAR_DELAY_SECONDS`
after the splash is confirmed gone), `_shimmer_sweep_tick` (the fast,
~0.15s interval driving one shimmer pass, started by
`LiveApp._trigger_shimmer_sweep` itself fired every `SHIMMER_PERIOD_SECONDS`
by `_start_shimmer_timer`/`_stop_shimmer_timer`), and `_celebration_tick`
(same fast interval, started by
`on_inspector_canvas_star_link_clicked`). No test in this suite exercises
any of this periodic refresh: every test either drives state directly
(`_refresh_display`, `_select_trace`, `_refresh_trace_list`,
`_render_inspector_empty_state`, or setting `_logo_phase` directly, ...) or
only cares about a *different*, one-shot set of timers owned by a child
screen (`SplashScreen`'s reveal/fade `set_timer` calls), which are left
untouched here.

Left un-muted, these are a genuine, intermittent race under load: Textual's
`run_test()` shuts the app down when its `async with` block exits, but an
in-flight tick that was already past its own `await` boundary (or, for
these now-sync tick callbacks, already running) when teardown started can
keep running against a screen whose widgets are mid-unmount -- occasionally
raising `NoMatches` for a widget like `#diagram-col` that's just been torn
down. This reproduces on an unmodified checkout too; it isn't specific to
any one test file. The fast (~0.15s) intervals only make that race more
likely to actually hit during a test run, not less -- muting them matters
at least as much as `_poll`/`_pulse_tick`.

Only the *leaf* callback actually passed to `set_timer`/`set_interval` is
muted in each chain, not the "scheduler" method that sets the timer up
(`_on_screen_change`, `_start_shimmer_timer`, `_trigger_shimmer_sweep`,
`on_inspector_canvas_star_link_clicked` are all real code, callable
directly by a test to verify they schedule the right thing) -- muting a
method replaces it globally for the life of the test, so muting a
scheduler would make it impossible for a test to observe that it
scheduled anything at all. A real timer landing on a muted leaf is safe
either way: the leaf does nothing, so it doesn't matter whether it fires
during the test, after teardown starts, or never.

Replacing the callbacks with no-ops (rather than, say, sleeping or
retrying around the race) removes the source of nondeterminism outright:
nothing under test relies on any of their side effects, so this doesn't
weaken any assertion -- it just stops these timers from ever doing
anything during a test.
"""

from unittest.mock import patch

import pytest

from uagents_trace.live import LiveApp


async def _noop_interval(self: LiveApp) -> None:
    return None


def _noop_tick(self: LiveApp) -> None:
    return None


@pytest.fixture(autouse=True)
def _mute_live_app_timers():
    """Auto-applied to every test (including `unittest.TestCase` ones) so
    any current or future `LiveApp`-based TUI test gets this for free.
    """
    with (
        patch.object(LiveApp, "_poll", _noop_interval),
        patch.object(LiveApp, "_pulse_tick", _noop_interval),
        patch.object(LiveApp, "_start_logo_appearance", _noop_tick),
        patch.object(LiveApp, "_shimmer_sweep_tick", _noop_tick),
        patch.object(LiveApp, "_celebration_tick", _noop_tick),
    ):
        yield
