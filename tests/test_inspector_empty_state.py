"""Tests for the live TUI inspector's empty state: session-wide stats
aggregated across every currently-loaded trace, the layout-first block
drop order when they don't all fit, the logo shimmer, and the star-link
click celebration -- now a localized flash on the star link's own line,
not a full-panel takeover (see _star_link_text/_build_empty_state_text).
Also covers _append_session_footer, the selected-agent view's session-
stats footer -- it shares _append_session_stat_blocks with the empty
state, so the two are expected to render identical stat-block text (see
the coherence tests below); the per-agent detail it's appended to is
covered in test_inspector_panel.py. Click-to-celebrate wiring
(InspectorCanvas.on_click -> LiveApp.on_inspector_canvas_star_link_clicked)
is also covered there; this file is the pure-function layer underneath it.
"""

import unittest

from rich.text import Text

from uagents_trace.live import (
    CELEBRATION_TICKS,
    INSPECTOR_EMPTY_HINT,
    MAX_ERROR_PREVIEW,
    STAR_URL,
    SessionStats,
    _append_session_footer,
    _build_empty_state_text,
    _compute_session_stats,
    _failures_block_lines,
    _session_block_lines,
    _shimmer_logo_text,
    _star_link_text,
    _timing_block_lines,
    _truncate,
)
from uagents_trace.network_canvas import GREEN, WARN
from uagents_trace.shape import HUB, Hop, TraceState


def _hop(*, source="a", dest="b", state="delivered", error=None, latency_ms=10) -> Hop:
    return Hop(
        id=f"{source}-{dest}",
        source=source,
        dest=dest,
        payload_type="Task",
        message=None,
        protocol="P1",
        detail=None,
        state=state,
        error=error,
        enqueued_at=0,
        acked_at=latency_ms,
        latency_ms=latency_ms,
    )


def _trace_state(
    *, participants, hops, duration_ms, completed, failed, pending=0, total=None
) -> TraceState:
    return TraceState(
        shape=HUB,
        hub=participants[0] if participants else None,
        hops=hops,
        legs=[],
        tree=None,
        participants=participants,
        started_at=0,
        duration_ms=duration_ms,
        completed=completed,
        failed=failed,
        pending=pending,
        total=total if total is not None else completed + failed + pending,
    )


class SessionStatsTests(unittest.TestCase):
    def test_no_traces_yields_all_none_not_a_crash(self):
        stats = _compute_session_stats([])
        self.assertEqual(stats.trace_count, 0)
        self.assertEqual(stats.agent_count, 0)
        self.assertIsNone(stats.success_rate)
        self.assertIsNone(stats.slowest_ms)
        self.assertIsNone(stats.fastest_ms)
        self.assertIsNone(stats.average_ms)
        self.assertEqual(stats.failed_count, 0)
        self.assertIsNone(stats.top_error)

    def test_aggregates_across_multiple_traces(self):
        fast = _trace_state(
            participants=["orch", "sub1"],
            hops=[_hop(source="orch", dest="sub1", state="delivered")],
            duration_ms=100,
            completed=1,
            failed=0,
        )
        slow_failed = _trace_state(
            participants=["orch", "sub2", "sub3"],
            hops=[
                _hop(source="orch", dest="sub2", state="dropped", error="timeout waiting for ack"),
                _hop(source="orch", dest="sub3", state="dropped", error="timeout waiting for ack"),
            ],
            duration_ms=3000,
            completed=0,
            failed=2,
        )
        stats = _compute_session_stats([fast, slow_failed])

        self.assertEqual(stats.trace_count, 2)
        # orch, sub1, sub2, sub3 -- union across both traces, not a sum.
        self.assertEqual(stats.agent_count, 4)
        # 1 completed out of 3 total legs across the session.
        self.assertAlmostEqual(stats.success_rate, 100 / 3)
        self.assertEqual(stats.slowest_ms, 3000)
        self.assertEqual(stats.fastest_ms, 100)
        self.assertEqual(stats.average_ms, round((100 + 3000) / 2))
        self.assertEqual(stats.failed_count, 2)
        self.assertEqual(stats.top_error, ("timeout waiting for ack", 2))

    def test_most_common_error_wins_over_a_rarer_one(self):
        state = _trace_state(
            participants=["a", "b", "c"],
            hops=[
                _hop(source="a", dest="b", state="dropped", error="common failure"),
                _hop(source="a", dest="c", state="dropped", error="common failure"),
                _hop(source="a", dest="d", state="dropped", error="rare failure"),
            ],
            duration_ms=50,
            completed=0,
            failed=3,
        )
        stats = _compute_session_stats([state])
        self.assertEqual(stats.top_error, ("common failure", 2))

    def test_zero_duration_traces_excluded_from_timing_not_treated_as_instant(self):
        # A trace with duration_ms == 0 (nothing resolved yet) shouldn't
        # drag the average down to 0 or masquerade as the fastest trace.
        unresolved = _trace_state(participants=["a", "b"], hops=[], duration_ms=0, completed=0, failed=0, pending=1)
        resolved = _trace_state(
            participants=["a", "b"],
            hops=[_hop(state="delivered")],
            duration_ms=500,
            completed=1,
            failed=0,
        )
        stats = _compute_session_stats([unresolved, resolved])
        self.assertEqual(stats.slowest_ms, 500)
        self.assertEqual(stats.fastest_ms, 500)
        self.assertEqual(stats.average_ms, 500)


class BlockLinesTests(unittest.TestCase):
    def test_session_block_empty_case(self):
        stats = _compute_session_stats([])
        self.assertEqual(_session_block_lines(stats), ["waiting for traces…"])

    def test_timing_block_empty_case(self):
        stats = _compute_session_stats([])
        self.assertEqual(_timing_block_lines(stats), ["no completed round-trips yet"])

    def test_failures_block_empty_case_is_celebratory_not_a_bare_zero(self):
        stats = _compute_session_stats([])
        self.assertEqual(_failures_block_lines(stats), ["none 🎉"])

    def test_failures_block_includes_top_error_when_present(self):
        state = _trace_state(
            participants=["a", "b"],
            hops=[_hop(state="dropped", error="Unable to resolve destination endpoint")],
            duration_ms=10,
            completed=0,
            failed=1,
        )
        stats = _compute_session_stats([state])
        lines = _failures_block_lines(stats)
        self.assertIn("1 failed", lines[0])
        self.assertIn("Unable to resolve destination endpoint", lines[1])

    def test_truncate_keeps_short_text_untouched(self):
        self.assertEqual(_truncate("short", 52), "short")

    def test_truncate_caps_long_text_with_ellipsis(self):
        long_message = "x" * 100
        result = _truncate(long_message, MAX_ERROR_PREVIEW)
        self.assertLessEqual(len(result), MAX_ERROR_PREVIEW)
        self.assertTrue(result.endswith("…"))


class EmptyStateLayoutTests(unittest.TestCase):
    """_build_empty_state_text's layout-first block-drop order: logo and
    star link never drop; Session, Timing, Failures drop from the bottom
    (Failures first) as available_height shrinks.
    """

    def _stats_with_all_blocks_populated(self) -> SessionStats:
        state = _trace_state(
            participants=["a", "b"],
            hops=[_hop(state="dropped", error="Unable to resolve destination endpoint")],
            duration_ms=500,
            completed=0,
            failed=1,
        )
        return _compute_session_stats([state])

    def test_tall_terminal_shows_every_block(self):
        stats = self._stats_with_all_blocks_populated()
        text, star_link_rows = _build_empty_state_text(stats, tick=0, available_height=100, celebration_frame=None)
        plain = text.plain
        self.assertIn("Session", plain)
        self.assertIn("Timing", plain)
        self.assertIn("Failures", plain)
        self.assertIsNotNone(star_link_rows)

    def test_short_terminal_keeps_logo_and_link_drops_all_blocks(self):
        stats = self._stats_with_all_blocks_populated()
        text, star_link_rows = _build_empty_state_text(stats, tick=0, available_height=1, celebration_frame=None)
        plain = text.plain
        self.assertIn(INSPECTOR_EMPTY_HINT, plain)
        self.assertIn("Star uAgents on GitHub", plain)
        self.assertNotIn("Session", plain)
        self.assertNotIn("Timing", plain)
        self.assertNotIn("Failures", plain)
        # The star link itself is never dropped, so it's still clickable.
        self.assertIsNotNone(star_link_rows)

    def test_medium_terminal_drops_from_the_bottom_failures_first(self):
        stats = self._stats_with_all_blocks_populated()
        # Enough room for the logo/link/hint plus exactly one stat block.
        logo_only_text, _ = _build_empty_state_text(stats, tick=0, available_height=1, celebration_frame=None)
        base_rows = logo_only_text.plain.count("\n") + 1
        # 3, not 2 -- the first block (Session) pays the bigger "action ->
        # stats" gap (2 blank lines) rather than the 1-blank-line gap
        # between peer blocks; see _build_empty_state_text's own comment.
        session_cost = 3 + len(_session_block_lines(stats))

        text, _ = _build_empty_state_text(
            stats, tick=0, available_height=base_rows + session_cost, celebration_frame=None
        )
        plain = text.plain
        self.assertIn("Session", plain)
        self.assertNotIn("Timing", plain)
        self.assertNotIn("Failures", plain)

    def test_celebration_is_localized_not_a_panel_takeover(self):
        # The key behavior change from the old full-panel celebration: the
        # logo and every stat block must still be there during the flash --
        # only the star link's own clickable label is replaced.
        stats = self._stats_with_all_blocks_populated()
        normal_text, _ = _build_empty_state_text(stats, tick=0, available_height=100, celebration_frame=None)
        celebrating_text, star_link_rows = _build_empty_state_text(
            stats, tick=0, available_height=100, celebration_frame=0
        )
        plain = celebrating_text.plain

        self.assertIn("Session", plain)
        self.assertIn("Timing", plain)
        self.assertIn("Failures", plain)
        self.assertIn(INSPECTOR_EMPTY_HINT, plain)
        # The URL line is never touched by the celebration -- it must stay
        # visible and copy-pasteable regardless (some terminals don't act
        # on the OSC 8 link above it at all).
        self.assertIn(STAR_URL, plain)
        # The clickable label is what actually changes.
        self.assertNotIn("Star uAgents on GitHub", plain)
        self.assertIn("thanks for the click!", plain)
        # Disarmed -- nothing to click back into mid-flash.
        self.assertIsNone(star_link_rows)
        # Same row count as the non-celebrating render -- "everything else
        # stays put", not just present but reflowed around the flash.
        self.assertEqual(celebrating_text.plain.count("\n"), normal_text.plain.count("\n"))


class ShimmerAndCelebrationTests(unittest.TestCase):
    def test_shimmer_center_row_is_brightest(self):
        from uagents_trace.live import _HERO_FADE_COLORS, _HERO_LINES_PADDED

        rows = len(_HERO_LINES_PADDED)
        for tick in range(rows):
            text = _shimmer_logo_text(tick)
            center_line = text.plain.split("\n")[tick]
            style_at_center = next(
                run.style for run in text.spans if run.start <= text.plain.index(center_line) < run.end
            )
            self.assertIn(_HERO_FADE_COLORS[0], style_at_center)

    def test_shimmer_wraps_around_circularly(self):
        # tick == rows should land back on row 0, same as tick == 0.
        from uagents_trace.live import _HERO_LINES_PADDED

        rows = len(_HERO_LINES_PADDED)
        self.assertEqual(_shimmer_logo_text(0).plain, _shimmer_logo_text(rows).plain)

    def test_celebration_alternates_color_by_frame_parity(self):
        even = _star_link_text(celebration_frame=0)
        odd = _star_link_text(celebration_frame=1)
        even_style = even.spans[0].style
        odd_style = odd.spans[0].style
        self.assertIn(GREEN, even_style)
        self.assertIn(WARN, odd_style)

    def test_celebration_leaves_the_url_line_untouched(self):
        normal = _star_link_text()
        celebrating = _star_link_text(celebration_frame=0)
        normal_url_line = normal.plain.split("\n")[1]
        celebrating_url_line = celebrating.plain.split("\n")[1]
        self.assertEqual(normal_url_line, celebrating_url_line)
        self.assertEqual(normal_url_line, STAR_URL)

    def test_celebration_ticks_constant_is_positive(self):
        # Sanity guard: a 0 or negative value would mean the celebration
        # never plays a single frame.
        self.assertGreater(CELEBRATION_TICKS, 0)

    def test_star_url_points_at_the_real_repo(self):
        self.assertEqual(STAR_URL, "https://github.com/fetchai/uAgents")


class SessionFooterTests(unittest.TestCase):
    """_append_session_footer -- the selected-agent view's session-stats
    footer: a divider + the same blocks the empty state shows, dropped as
    a whole (never a lone divider with nothing under it) when there isn't
    room even for the divider + Session.
    """

    def _stats_with_all_blocks_populated(self) -> SessionStats:
        state = TraceState(
            shape=HUB,
            hub="a",
            hops=[Hop(
                id="a-b", source="a", dest="b", payload_type="Task", message=None, protocol="P1",
                detail=None, state="dropped", error="Unable to resolve destination endpoint",
                enqueued_at=0, acked_at=500, latency_ms=500,
            )],
            legs=[],
            tree=None,
            participants=["a", "b"],
            started_at=0,
            duration_ms=500,
            completed=0,
            failed=1,
            pending=0,
            total=1,
        )
        return _compute_session_stats([state])

    def test_tall_enough_shows_divider_and_every_block(self):
        stats = self._stats_with_all_blocks_populated()
        text = Text()
        text.append("some per-agent detail")
        _append_session_footer(text, stats, available_height=100)
        plain = text.plain
        self.assertIn("─" * 10, plain)
        self.assertIn("Session", plain)
        self.assertIn("Timing", plain)
        self.assertIn("Failures", plain)

    def test_too_short_for_even_the_divider_appends_nothing(self):
        stats = self._stats_with_all_blocks_populated()
        text = Text()
        text.append("some per-agent detail")
        before = text.plain
        # available_height already consumed by the one line above -- no
        # room left for a divider, let alone a block under it.
        _append_session_footer(text, stats, available_height=1)
        self.assertEqual(text.plain, before)

    def test_a_lone_divider_with_nothing_under_it_is_never_shown(self):
        stats = self._stats_with_all_blocks_populated()
        text = Text()
        text.append("x" * 5)
        before = text.plain
        # One row short of fitting the divider + Session together --
        # confirmed empirically that this height is exactly where the
        # footer flips from "nothing" to "divider + Session" for this
        # stats fixture, so this is the sharpest check that there's no
        # in-between state showing a bare divider with nothing under it.
        _append_session_footer(text, stats, available_height=4)
        self.assertEqual(text.plain, before, "a bare divider with no block under it must not be shown")

    def test_medium_height_shows_session_only(self):
        stats = self._stats_with_all_blocks_populated()
        text = Text()
        _append_session_footer(text, stats, available_height=6)
        plain = text.plain
        self.assertIn("Session", plain)
        self.assertNotIn("Timing", plain)
        self.assertNotIn("Failures", plain)

    def test_footer_and_empty_state_share_identical_stat_lines(self):
        # Same computation, same styling, in both surfaces -- switching
        # selection must not feel like two different panels.
        stats = self._stats_with_all_blocks_populated()
        empty_text, _ = _build_empty_state_text(stats, tick=0, available_height=100, celebration_frame=None)
        footer_text = Text()
        _append_session_footer(footer_text, stats, available_height=100)
        for line in _session_block_lines(stats) + _timing_block_lines(stats) + _failures_block_lines(stats):
            self.assertIn(line, empty_text.plain)
            self.assertIn(line, footer_text.plain)


if __name__ == "__main__":
    unittest.main()
