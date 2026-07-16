"""Shared pytest fixtures for the test suite.

`LiveApp.on_mount` registers two real, wall-clock `set_interval` callbacks
-- `_poll` (POLL_SECONDS) and `_pulse_tick` (PULSE_SECONDS) -- so the live
TUI can refresh itself against a SQLite file outside of tests. A third,
`_shimmer_tick` (SHIMMER_INTERVAL_SECONDS, much faster), is started/stopped
dynamically instead -- whenever the inspector's empty state is showing, see
`LiveApp._start_shimmer_timer`/`_stop_shimmer_timer` -- rather than once in
`on_mount`, but the same race applies to it once it's running. No test in
this suite exercises any of this periodic refresh: every test either drives
state directly (`_refresh_display`, `_select_trace`, `_refresh_trace_list`,
`_render_inspector_empty_state`, ...) or only cares about a *different*,
one-shot set of timers owned by a child screen (`SplashScreen`'s
reveal/fade `set_timer` calls), which are left untouched here.

Left un-muted, these are a genuine, intermittent race under load: Textual's
`run_test()` shuts the app down when its `async with` block exits, but an
in-flight tick that was already past its own `await` boundary (or, for the
now-sync `_shimmer_tick`, already running) when teardown started can keep
running against a screen whose widgets are mid-unmount -- occasionally
raising `NoMatches` for a widget like `#diagram-col` that's just been torn
down. This reproduces on an unmodified checkout too; it isn't specific to
any one test file. `_shimmer_tick`'s own ~0.15s interval only makes that
race more likely to actually hit during a test run, not less -- muting it
matters at least as much as the other two.

Replacing the callbacks with no-ops (rather than, say, sleeping or
retrying around the race) removes the source of nondeterminism outright:
nothing under test relies on any of their side effects, so this doesn't
weaken any assertion -- it just stops three timers nothing is testing from
ever firing during a test.
"""

from unittest.mock import patch

import pytest

from uagents_trace.live import LiveApp


async def _noop_interval(self: LiveApp) -> None:
    return None


def _noop_shimmer_tick(self: LiveApp) -> None:
    return None


@pytest.fixture(autouse=True)
def _mute_live_app_timers():
    """Auto-applied to every test (including `unittest.TestCase` ones) so
    any current or future `LiveApp`-based TUI test gets this for free.
    """
    with (
        patch.object(LiveApp, "_poll", _noop_interval),
        patch.object(LiveApp, "_pulse_tick", _noop_interval),
        patch.object(LiveApp, "_shimmer_tick", _noop_shimmer_tick),
    ):
        yield
