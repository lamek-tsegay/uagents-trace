"""Live diagram + rolling message feed for trace-uagents.

Opened after the setup wizard. Polls SQLite and shows agent-to-agent
messages as they happen — one active trace at a time, bounded feed.

Every widget here (diagram, table, inspector, feed, sidebar) renders from
a single `shape.TraceState` computed once per refresh (see
`shape.build_trace_state`) rather than each recomputing status/latency
from raw spans on its own -- that's what keeps them from disagreeing about
what happened to a given trace.
"""

import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from .brand import BRAND_PANEL_WIDTH, FETCH_BRAND, HERO_BANNER
from .cli import display_name
from .network_canvas import (
    ACCENT,
    ERROR,
    GREEN,
    SUCCESS,
    WARN,
    block_width,
    build_hub_hit_regions,
    build_hub_topology,
    build_peer_hit_regions,
    build_peer_topology,
    format_ms,
)
from .shape import HUB, Hop, TraceState, TreeNode, build_hops, build_trace_state
from .store import get_alias_map, get_recent_spans, get_trace_spans, list_traces, save_watch_config
from .wizard import ViewMode, WatchSetup

# Poll SQLite for new spans; 3s keeps the UI calm without feeling laggy for
# typical multi-agent round trips (often seconds, not milliseconds).
POLL_SECONDS = 3.0
# Pending-indicator blink — slower than poll so the diagram is not constantly redrawing.
PULSE_SECONDS = 1.5
# The empty-state logo shimmer's own, much faster clock (see
# _shimmer_logo_text / LiveApp._shimmer_tick) -- riding PULSE_SECONDS made
# a full 12-row sweep take ~18s and read as barely moving. Only ticks while
# the empty state is actually showing (LiveApp._start_shimmer_timer/
# _stop_shimmer_timer), so it doesn't burn cycles once an agent is
# selected.
SHIMMER_INTERVAL_SECONDS = 0.15
MAX_EVENTS = 15
MAX_TRACE_LIST = 25
TRACE_WIDGET_PREFIX = "trace-"

MUTED = "#6b7280"

# Inspector column -- third, fixed-width column right of the diagram. Below
# MIN_WIDTH_FOR_INSPECTOR it hides itself entirely rather than shrink.
#
# Now that #diagram-scroll (see its own CSS/comment below) lets the diagram
# render at its natural size and scroll horizontally instead of being
# force-squeezed to fit, this threshold no longer has to reserve
# worst-case width for the widest realistic trace -- a diagram that
# doesn't fit the reduced diagram-col width just scrolls (hub-centered,
# boxes intact), it doesn't clip or overlap. So this reserves a
# comfortable MINIMUM diagram width instead of a worst case:
#   180 = sidebar (46) + minimum diagram (50, the 2-agent/peer floor --
#         see network_canvas._agent_columns/_compute_peer_layout -- + 8
#         for #diagram-col's own border/padding overhead, matching
#         _diagram_panel_width's own -8) + inspector (76, BRAND_PANEL_WIDTH
#         incl. border)
# A trace with more agents than that comfortably fits is still fully
# usable with the inspector open -- it scrolls. (Was 228, calibrated
# pre-scrolling to avoid ever needing to scroll; see the scrollable-diagram
# work's 3-slice history for the measurements behind both numbers.)
MIN_WIDTH_FOR_INSPECTOR = 180

# Live-messages feed -- height in terminal rows (2 border rows + N message
# lines). 5 messages (height 7) is the target size; below
# MIN_HEIGHT_FOR_TALL_FEED there isn't room for that alongside #main-row's
# own 17-row floor (see #main-row's CSS comment) within a short terminal's
# fixed vertical budget, so the feed falls back to its previous, tighter
# size instead of clipping either panel's border -- the same
# hide-rather-than-shrink approach MIN_WIDTH_FOR_INSPECTOR above takes on
# the width axis.
#   budget = terminal height - 1 (Header) - 1 (Footer)
#   tall:  budget >= 17 (#main-row floor) + 7 (5-message feed) == 24  =>  H >= 26
#   short: budget >= 17 (#main-row floor) + 5 (3-message feed) == 22  =>  H >= 24
# An 80x24 terminal (H=24) clears the "short" floor but not "tall", so it
# keeps showing 3 messages there -- unchanged from before this existed.
EVENTS_PANEL_TALL_HEIGHT = 7
EVENTS_PANEL_SHORT_HEIGHT = 5
MIN_HEIGHT_FOR_TALL_FEED = 26

# Textual CSS can't interpolate a Python constant into #inspector-col's
# `width:` (braces in an f-string would collide with CSS block syntax), so
# that value is hardcoded there -- this just catches the two drifting apart.
assert BRAND_PANEL_WIDTH == 76, "update #inspector-col's CSS width alongside brand.BRAND_PANEL_WIDTH"
# Content width inside #inspector-scroll: BRAND_PANEL_WIDTH minus its
# border (1+1) and padding (2+2). Used for the selected-agent footer's
# divider rule, which should span the panel's actual width, not a guess.
INSPECTOR_CONTENT_WIDTH = BRAND_PANEL_WIDTH - 6


def _trace_widget_id(trace_id: str) -> str:
    """Textual widget ids must not start with a digit — UUIDs need a prefix."""
    return f"{TRACE_WIDGET_PREFIX}{trace_id}"


def _trace_id_from_widget_id(widget_id: str) -> str:
    if widget_id.startswith(TRACE_WIDGET_PREFIX):
        return widget_id[len(TRACE_WIDGET_PREFIX) :]
    return widget_id


REPLY_PAYLOAD_TYPES = frozenset(
    {
        "Reply",
        "Pong",
        "Result",
        "ChatAcknowledgement",
        "CompletePayment",
        "RejectPayment",
        "CancelPayment",
    }
)

STATE_STYLE = {
    "delivered": SUCCESS,
    "timeout": WARN,
    "dropped": ERROR,
    "pending": MUTED,
}

STATE_ICON = {
    "delivered": "✓",
    "timeout": "⏱",
    "dropped": "✗",
    "pending": "…",
}


def message_label(payload_type: str) -> str:
    """Semantic label: Message for outbound, Reply for responses."""
    ptype = payload_type or ""
    if ptype in REPLY_PAYLOAD_TYPES:
        return "Reply"
    if ptype.endswith("Reply") or ptype.endswith("Acknowledgement"):
        return "Reply"
    return "Message"


def _message_text(hop: Hop) -> str:
    if hop.message:
        return hop.message
    if hop.detail:
        return hop.detail
    return hop.payload_type or ""


def _format_payload(hop: Hop) -> str:
    label = message_label(hop.payload_type)
    body = _message_text(hop)
    if body:
        return f'{label}: "{body}"'
    return label


def _styled_icon(state: str) -> Text:
    icon = STATE_ICON.get(state, "·")
    style = STATE_STYLE.get(state, "white")
    weight = "bold " if state == "dropped" else ""
    return Text(icon, style=f"{weight}{style}")


def format_event_line(hop: Hop, alias_map: dict[str, str]) -> Text:
    """One logical hop: [Alice] → [Bob]  Message: \"Hi Bob!\"  (12ms)

    `hop` is already deduplicated (see `shape.build_hops`) -- one line per
    logical message, not one per underlying send/receive span, so a single
    ping/pong round trip shows up as two feed lines, not four.
    """
    src = display_name(hop.source, alias_map)
    dst = display_name(hop.dest, alias_map)
    payload = _format_payload(hop)
    latency = format_ms(hop.latency_ms)
    state = hop.state or "pending"

    parts: list[Any] = [
        _styled_icon(state),
        f" [{src}] → [{dst}]  {payload}  ({latency})",
    ]
    if state in ("dropped", "timeout") and hop.error:
        parts.append(f"  — {hop.error}")

    line = Text.assemble(*parts)
    weight = "bold " if state == "dropped" else ""
    line.stylize(f"{weight}{STATE_STYLE.get(state, 'white')}")
    return line


def _flash_line(line: Text) -> Text:
    """A copy of `line` with bold layered on top, for a newly-arrived feed
    line's one-pulse-cycle flash -- a copy, not an in-place `stylize`, so
    the original in `LiveApp._events` stays unflashed for later re-renders.
    """
    flashed = line.copy()
    flashed.stylize("bold")
    return flashed


def render_agent_box(label: str, width: int | None = None) -> list[str]:
    content = label
    w = width or max(len(content) + 2, 10)
    inner = content.center(w)
    return [
        "┌" + "─" * w + "┐",
        "│" + inner + "│",
        "└" + "─" * w + "┘",
    ]


def _latest_peer_round_trip(hops: list[Hop]) -> tuple[Hop | None, Hop | None]:
    """Most recent Message hop and matching Reply hop (if any)."""
    if not hops:
        return None, None
    if len(hops) >= 2:
        prev, latest = hops[-2], hops[-1]
        if (
            prev.source == latest.dest
            and prev.dest == latest.source
            and message_label(prev.payload_type) == "Message"
            and message_label(latest.payload_type) == "Reply"
        ):
            return prev, latest
    return hops[-1], None


DiagramPieces = tuple[Text, dict[str, tuple[int, int, int, int]]]


def _hub_diagram_pieces(
    state: TraceState,
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
    selected: str | None = None,
    available_width: int | None = None,
    available_height: int | None = None,
) -> DiagramPieces:
    """(topology, hit_regions) for a hub trace. `hit_regions` maps each
    subagent's *address* to its clickable box region; `selected`, if given,
    is the currently-selected agent's address, highlighted with a double
    border in the topology. `available_width`/`available_height` are the
    panel's real size (see `LiveApp._diagram_panel_width/_height`) -- passed
    to both the renderer and the hit-region builder so a box's drawn
    position and its clickable region never diverge.
    """
    legs = state.legs
    orch_name = display_name(state.hub, alias_map)
    agent_names = [display_name(leg["subagent"], alias_map) for leg in legs]
    selected_name = display_name(selected, alias_map) if selected else None
    topology = build_hub_topology(
        legs,
        orch_name,
        agent_names,
        pulse=pulse,
        selected=selected_name,
        available_width=available_width,
        available_height=available_height,
    )
    if not legs:
        return topology, {}
    regions = build_hub_hit_regions(legs, orch_name, agent_names, available_width, available_height)
    hit_regions = {leg["subagent"]: region for leg, region in zip(legs, regions)}
    return topology, hit_regions


def _peer_diagram_pieces(
    hops: list[Hop],
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
    selected: str | None = None,
    available_width: int | None = None,
    available_height: int | None = None,
) -> DiagramPieces:
    """(topology, hit_regions) for a peer trace -- mirrors `_hub_diagram_pieces`."""
    if not hops:
        return (
            Text(
                "  Waiting for messages…\n\n"
                "  Start your instrumented agents\n"
                "  in another terminal.",
                style="dim",
            ),
            {},
        )

    outbound, reply = _latest_peer_round_trip(hops)
    if outbound is None:
        return Text("  Waiting for messages…", style="dim"), {}

    left = display_name(outbound.source, alias_map)
    right = display_name(outbound.dest, alias_map)
    leg_state = (
        "completed"
        if outbound.state == "delivered" and reply
        else ("failed" if outbound.state in ("dropped", "timeout") else "pending")
    )
    selected_name = display_name(selected, alias_map) if selected else None

    topology = build_peer_topology(
        left,
        right,
        state=leg_state,
        pulse=pulse,
        selected=selected_name,
        available_width=available_width,
        available_height=available_height,
    )
    left_box, right_box = build_peer_hit_regions(left, right, available_width, available_height)
    hit_regions = {outbound.source: left_box, outbound.dest: right_box}
    return topology, hit_regions


def _node_status_label(node: TreeNode) -> str:
    if node.state == "completed":
        lat = format_ms(node.latency_ms)
        return f"{STATE_ICON['delivered']} {lat}"
    if node.state == "failed":
        icon = STATE_ICON["dropped"]
        reason = node.reason or "failed"
        return f"{icon} {reason}"
    if node.state == "pending":
        return f"{STATE_ICON['pending']} pending"
    return ""


def _append_tree_children(
    diagram: Text,
    node: TreeNode,
    alias_map: dict[str, str],
    prefix: str,
    pad: str,
) -> None:
    for i, child in enumerate(node.children):
        is_last = i == len(node.children) - 1
        branch = "└── " if is_last else "├── "
        continuation = "    " if is_last else "│   "
        label = display_name(child.agent, alias_map)
        tag = f"[{child.payload_type}] " if child.payload_type else ""
        msg = f'"{child.message}"' if child.message else ""
        detail = " ".join(p for p in (tag + msg, _node_status_label(child)) if p).strip()
        state = child.state or "pending"
        line = f"{prefix}{branch}{label}  {detail}".rstrip()
        diagram.append(pad + line + "\n", style=STATE_STYLE.get(state, "white"))
        if child.children:
            _append_tree_children(diagram, child, alias_map, prefix + continuation, pad)


def build_hub_tree_diagram(
    tree: TreeNode,
    alias_map: dict[str, str],
) -> Text:
    """Top-down fan-out: orchestrator root, sub-agents as branches below."""
    orch_name = display_name(tree.agent, alias_map)
    pad = "  "
    diagram = Text()

    for line in render_agent_box(orch_name):
        diagram.append(pad + line + "\n")

    if not tree.children:
        diagram.append(pad + "  Waiting for dispatch to sub-agents…", style="dim")
        return diagram

    diagram.append("\n")
    _append_tree_children(diagram, tree, alias_map, "", pad)

    if diagram.plain.endswith("\n"):
        diagram.plain = diagram.plain.rstrip("\n")

    return diagram


def _sub_title_for(setup: WatchSetup, view_mode: ViewMode, *, follow: bool) -> str:
    """Status line -- a static usage hint (real keybindings a new user
    wouldn't otherwise discover), not a restatement of current state. The
    sidebar and diagram already show which trace is active and how; this
    line doesn't need to repeat that. No product name here -- the user
    already knows what app they're in (the splash said so); `App.title`
    (see `on_mount`) is kept short for the same reason, so the composed
    Header line (`title — sub_title`) doesn't restate it twice either.
    """
    return "to follow latest trace: press f  ·  to pin a trace: click it  ·  to switch to tree diagram: press v"


def _trace_matches_watch(trace: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return any(a in addresses for a in trace["participants"])


def _span_in_watch(span: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return span["source_agent"] in addresses or span["dest_agent"] in addresses


LATENCY_BAR_WIDTH = 5
# Ceiling for a "full" bar -- typical demo round trips run well under this,
# so most bars sit partway full and only genuinely slow traces peg it.
LATENCY_BAR_SCALE_MS = 2000
LATENCY_BAR_FILLED = "▮"
LATENCY_BAR_EMPTY = "▯"

# Sidebar row markers -- a failed-everything trace and a partially-degraded
# one both read WARN/ERROR via `style`, but only the marker glyph tells them
# apart from a scan of the list without reading the fraction text. Both are
# the same width (glyph + space) -- MARKER_WIDTH pads the absent case to
# match, so line 1's header lands in the same column whether or not a
# marker is present (a plain "" here would jog the header 2 cols sideways
# only on the rows that happen to carry one).
FAILURE_MARKER = "⚑ "
DEGRADED_MARKER = "⚠ "
MARKER_WIDTH = 2

# Per-trace index shown at the start of each row (see `LiveApp._trace_seq`
# for how it's assigned/kept stable). Cyan/blue rather than any of
# green/amber/red/MUTED -- it isn't a status, it's a position in "the order
# traces arrived", and using a status color for it would read as a fifth
# outcome that doesn't exist. Bold, unlike the id next to it, so it carries
# more visual weight despite being the same character height (a terminal
# can't do font-size).
INDEX_COLOR = "#38bdf8"
ID_DISPLAY_WIDTH = 6
NUMBER_WIDTH = 3  # right-justified -- fits 1-999 without the id column drifting
# Column where line 1's header text starts: the number field, a 2-space
# gap, the id, " · ", then the (always-reserved, see MARKER_WIDTH above)
# marker column. Line 2's fraction is indented to this same column (see
# sidebar_label) so it lands directly under the header on every row,
# regardless of marker presence or how many digits the row's number has.
_SIDEBAR_HEADER_COL = NUMBER_WIDTH + 2 + ID_DISPLAY_WIDTH + 3 + MARKER_WIDTH


def _latency_bar(duration_ms: int) -> str:
    """Small inline bar for a trace's duration, scanned at a glance instead
    of having to read the raw ms/s number -- the number stays alongside it
    (see `sidebar_label`) so the exact value is still there when it matters.
    Its scale (LATENCY_BAR_SCALE_MS) isn't self-evident from the bar alone
    and, unlike an earlier version of this docstring claimed, isn't
    surfaced anywhere in the UI either -- `#trace-list`'s border_title (see
    `LiveApp.compose`) spends its one line on the bar's *color* meaning
    (speed, via `_latency_bar_style`) instead, since that reads at a glance
    and the exact scale mostly doesn't matter once the bar and its color
    already say "fast" or "slow".
    """
    if duration_ms <= 0:
        return LATENCY_BAR_EMPTY * LATENCY_BAR_WIDTH
    filled = round((duration_ms / LATENCY_BAR_SCALE_MS) * LATENCY_BAR_WIDTH)
    filled = max(1, min(LATENCY_BAR_WIDTH, filled))
    return LATENCY_BAR_FILLED * filled + LATENCY_BAR_EMPTY * (LATENCY_BAR_WIDTH - filled)


def _latency_bar_style(bar: str) -> str:
    """Bar color encodes speed by fill level -- a completely different axis
    from the row's own outcome color, which is what `style` (success/
    failed/partial) means everywhere else in this row. Reuses the same
    green/amber/red hues rather than inventing new ones, but a fast,
    failed trace still shows a green bar next to red failure text, and a
    slow success shows a red bar next to green text -- the two colors are
    independent by design, not a second encoding of the same thing.
    """
    filled = bar.count(LATENCY_BAR_FILLED)
    if filled <= 2:
        return GREEN
    if filled <= 4:
        return WARN
    return ERROR


def sidebar_label(
    trace_id: str, state: TraceState, alias_map: dict[str, str], number: int | None = None
) -> tuple[Text, str]:
    """(text, style) for one two-line sidebar row -- a fractional rollup,
    not a binary all-or-nothing ✓/✗, so a hub trace that's 3/4 done doesn't
    read as a total failure just because one leg is still broken.

        line 1 (identity, scanned to find a trace): "{number}  {id} · {marker}{header}"
        line 2 (status, aligned under the header):   "{indent}{done}/{total} ✓  {bar} {duration}"

    `number` is the trace's stable, oldest-first index (see
    `LiveApp._trace_seq`) -- `None` renders as blank padding rather than
    omitting the field, so the header still lands at the same column
    (`_SIDEBAR_HEADER_COL`) whether or not a caller has one to show (e.g. a
    test that doesn't care about numbering).

    Color is fractional too: red is reserved for a trace where *every*
    leg failed. A trace with some legs ok and some failed (or still
    pending) reads amber -- "needs a look", not "everything is broken".
    Only a fully clean trace (nothing failed or pending) reads green. The
    trace id on line 1 always stays neutral/dim regardless of outcome;
    only the header and all of line 2's fraction carry the semantic color,
    bold only for a fully-failed trace (style == ERROR) -- everything
    else, including a fully-delivered trace, recedes at normal weight. The
    latency bar on line 2 carries its *own*, independent color (see
    `_latency_bar_style`) -- speed, not outcome.

    A trace with *any* failed leg always carries a marker (⚑ if every leg
    failed, ⚠ if only some did) so it stands out from a scan of the list
    without having to read the x/y fraction -- a partially-failed trace
    must never look like a plain in-progress one.
    """
    number_field = f"{number:>{NUMBER_WIDTH}}" if number is not None else " " * NUMBER_WIDTH

    if state.total == 0:
        text = Text()
        text.append(number_field, style=f"bold {INDEX_COLOR}")
        text.append(f"  {trace_id[:6]} · waiting for spans…", style=MUTED)
        return text, MUTED

    if state.shape == HUB and state.hub:
        hub_name = display_name(state.hub, alias_map)
        header = f"{hub_name}→{state.total}"
    else:
        names = [display_name(a, alias_map) for a in state.participants[:2]]
        header = "↔".join(names) if len(names) == 2 else (names[0] if names else "?")

    duration = format_ms(state.duration_ms)
    bar = _latency_bar(state.duration_ms)
    bar_style = _latency_bar_style(bar)

    if state.failed and state.failed == state.total:
        style = ERROR
        marker = FAILURE_MARKER
    elif state.completed == state.total:
        style = SUCCESS
        marker = ""
    elif state.failed:
        style = WARN
        marker = DEGRADED_MARKER
    else:
        style = WARN
        marker = ""

    text_style = f"bold {style}" if style == ERROR else style
    marker_padded = marker.ljust(MARKER_WIDTH)

    text = Text()
    text.append(number_field, style=f"bold {INDEX_COLOR}")
    text.append(f"  {trace_id[:6]}", style=MUTED)
    text.append(" · ", style=MUTED)
    text.append(f"{marker_padded}{header}\n", style=text_style)
    text.append(" " * _SIDEBAR_HEADER_COL, style=text_style)
    text.append(f"{state.completed}/{state.total} ✓  ", style=text_style)
    text.append(bar, style=bar_style)
    text.append(f" {duration}", style=text_style)
    return text, style


def _find_span(spans: list[dict[str, Any]], source: str, dest: str, direction: str = "send") -> dict[str, Any] | None:
    """Earliest raw span for one leg-phase (dispatch or reply) -- spans are
    already enqueued_at-ordered by the store, so first match is earliest,
    matching `shape.py`'s own send-side matching rule.
    """
    for s in spans:
        if s["source_agent"] == source and s["dest_agent"] == dest and (s.get("direction") or "send") == direction:
            return s
    return None


def _relative_ms(t: int, started_at: int) -> int:
    return max(t - started_at, 0)


_LEG_ICON = {"completed": STATE_ICON["delivered"], "failed": STATE_ICON["dropped"], "pending": STATE_ICON["pending"]}
_LEG_STYLE = {"completed": SUCCESS, "failed": ERROR, "pending": WARN}

# Section header color -- the same vivid green as the splash hero
# (`network_canvas.GREEN`, aliased below as `SPLASH_HERO_GREEN`). Used to
# read as the calmer `ACCENT` on the theory that a persistent panel
# shouldn't share the splash's one-time bright flash; that reservation no
# longer holds -- the live TUI now uses one green everywhere, not two.
_SECTION_STYLE = f"bold {GREEN}"
_SECTION_LABEL_WIDTH = 15


@dataclass
class SessionStats:
    """Aggregate rollup across every trace currently loaded in the sidebar
    (`LiveApp._trace_rollup_cache` -- up to MAX_TRACE_LIST, the same
    "loaded" the sidebar itself uses, not the full unbounded db history).
    Computed here rather than in shape.py: it's a display-layer aggregation
    over `TraceState`/`Hop` fields shape.py already computes, not new
    trace-shape logic. Shared, verbatim, by the inspector's empty state
    (`_build_empty_state_text`) and the selected-agent footer
    (`_append_session_footer`) -- see the module's own coherence note --
    defined up here (ahead of `build_agent_inspector_text`, which needs it
    as a parameter type) rather than down by its two actual call sites.
    """

    trace_count: int
    agent_count: int
    success_rate: float | None  # None -- no legs/hops recorded anywhere yet
    slowest_ms: int | None
    fastest_ms: int | None
    average_ms: int | None
    failed_count: int
    top_error: tuple[str, int] | None  # (message, occurrences), most frequent first


def _compute_session_stats(states: list[TraceState]) -> SessionStats:
    agents: set[str] = set()
    completed = failed = total = 0
    durations: list[int] = []
    error_counts: Counter[str] = Counter()

    for state in states:
        agents.update(state.participants)
        completed += state.completed
        failed += state.failed
        total += state.total
        if state.duration_ms > 0:
            durations.append(state.duration_ms)
        for hop in state.hops:
            if hop.error:
                error_counts[hop.error] += 1

    return SessionStats(
        trace_count=len(states),
        agent_count=len(agents),
        success_rate=(completed / total * 100) if total else None,
        slowest_ms=max(durations) if durations else None,
        fastest_ms=min(durations) if durations else None,
        average_ms=round(sum(durations) / len(durations)) if durations else None,
        failed_count=failed,
        top_error=error_counts.most_common(1)[0] if error_counts else None,
    )


def _session_block_lines(stats: SessionStats) -> list[str]:
    if stats.trace_count == 0:
        return ["waiting for traces…"]
    rate = f"{stats.success_rate:.0f}% success" if stats.success_rate is not None else "no legs yet"
    return [f"{stats.trace_count} traces · {stats.agent_count} agents · {rate}"]


def _timing_block_lines(stats: SessionStats) -> list[str]:
    if stats.average_ms is None:
        return ["no completed round-trips yet"]
    return [
        f"slowest {format_ms(stats.slowest_ms)} · fastest {format_ms(stats.fastest_ms)} "
        f"· avg {format_ms(stats.average_ms)}"
    ]


# Keeps the failures block's error line to one row at the inspector's
# ~66-col usable width -- a hard cap rather than relying on Static's own
# auto-wrap, so the block's row count (and therefore the layout-drop math
# in _build_empty_state_text/_append_session_footer) is deterministic
# instead of depending on exactly how long today's error message happens
# to be.
MAX_ERROR_PREVIEW = 52


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _failures_block_lines(stats: SessionStats) -> list[str]:
    if stats.failed_count == 0:
        return ["none 🎉"]
    lines = [f"{stats.failed_count} failed"]
    if stats.top_error:
        message, count = stats.top_error
        lines.append(f"most common ({count}×): {_truncate(message, MAX_ERROR_PREVIEW)}")
    return lines


def _format_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB"


def _message_section(payload_label: str, protocol: str | None) -> Text:
    """Message section: what was sent and over which protocol."""
    block = Text()
    block.append("Message\n", style=_SECTION_STYLE)
    if payload_label:
        block.append(f"  {payload_label}\n", style=MUTED)
    block.append(f"  protocol: {protocol or '—'}\n", style=MUTED)
    return block


def _timing_section(rows: list[tuple[str, int | None, int | None, int | None]]) -> Text:
    """Aligned mini-table, one row per phase: (label, start_rel_ms,
    end_rel_ms, delta_ms). A row with no start/end (just a delta) renders
    as a plain summary row -- e.g. "total" -- instead of a dangling arrow
    to nowhere.
    """
    block = Text()
    block.append("Timing\n", style=_SECTION_STYLE)
    col_label, col_time = 9, 9
    for label, start_rel, end_rel, delta in rows:
        delta_s = f"Δ{delta}ms" if delta is not None else ""
        if start_rel is None and end_rel is None:
            line = f"  {label:<{col_label}}{'':<{col_time}}  {'':<{col_time}}{delta_s:>8}\n"
        else:
            start_s = f"+{start_rel}ms" if start_rel is not None else ""
            end_s = f"+{end_rel}ms" if end_rel is not None else "…"
            line = f"  {label:<{col_label}}{start_s:<{col_time}}→ {end_s:<{col_time}}{delta_s:>8}\n"
        block.append(line, style=MUTED)
    return block


def _registration_row(label: str, registered: bool | None) -> Text:
    """✓/✗ network-registration indicator -- usually the actual reason a
    message never arrived, so a False here is styled as a warning, not
    just more muted status text.
    """
    prefix = f"  {label:<{_SECTION_LABEL_WIDTH}}"
    if registered is None:
        return Text(f"{prefix}—\n", style=MUTED)
    if registered:
        return Text(f"{prefix}✓ registered\n", style=MUTED)
    return Text(f"{prefix}✗ not registered\n", style=f"bold {WARN}")


def _delivery_section(
    payload_size: int | None,
    source_label: str,
    source_registered: bool | None,
    dest_label: str,
    dest_registered: bool | None,
) -> Text:
    """Delivery section: the "why didn't my agent receive this" signals --
    payload size and whether each side was actually registered on the
    network at send time.
    """
    block = Text()
    block.append("Delivery\n", style=_SECTION_STYLE)
    block.append(f"  {'payload size':<{_SECTION_LABEL_WIDTH}}{_format_bytes(payload_size)}\n", style=MUTED)
    block.append_text(_registration_row(source_label, source_registered))
    block.append_text(_registration_row(dest_label, dest_registered))
    return block


def _error_section(reason: str, when_rel: int | None) -> Text:
    block = Text()
    block.append("Error\n", style=f"bold {ERROR}")
    block.append(f"  {reason}\n", style=f"bold {ERROR}")
    if when_rel is not None:
        block.append(f"  at +{when_rel}ms\n", style=ERROR)
    return block


def _hub_leg_detail(
    leg: dict[str, Any],
    spans: list[dict[str, Any]],
    hub: str,
    started_at: int,
    alias_map: dict[str, str],
) -> Text:
    """Full, sectioned detail for one hub leg: Message, Timing, Delivery,
    and (if failed) Error. `spans` supplies protocol, exact timestamps, and
    the delivery fields (payload size, registration) that `leg` itself
    doesn't carry -- see `_find_span`.
    """
    subagent = leg["subagent"]
    name = display_name(subagent, alias_map)
    hub_name = display_name(hub, alias_map)
    state = leg.get("state", "pending")
    style = _LEG_STYLE.get(state, WARN)
    icon = _LEG_ICON.get(state, "·")

    dispatch = _find_span(spans, hub, subagent, "send")
    reply = _find_span(spans, subagent, hub, "send")

    block = Text()
    block.append(f"{icon} {name}\n\n", style=f"bold {style}")

    ptype = leg.get("dispatch_payload")
    payload = leg.get("dispatch_message")
    payload_label = f'{ptype}: "{payload}"' if payload else (ptype or "")
    protocol = dispatch.get("protocol") if dispatch else None
    block.append_text(_message_section(payload_label, protocol))
    block.append("\n")

    rows: list[tuple[str, int | None, int | None, int | None]] = []
    dispatch_end_rel: int | None = None
    if dispatch is not None:
        dispatch_start_rel = _relative_ms(dispatch["enqueued_at"], started_at)
        dispatch_acked = dispatch.get("acked_at")
        dispatch_delta = max(dispatch_acked - dispatch["enqueued_at"], 0) if dispatch_acked is not None else None
        if dispatch_acked is not None:
            dispatch_end_rel = _relative_ms(dispatch_acked, started_at)
        rows.append(("dispatch", dispatch_start_rel, dispatch_end_rel, dispatch_delta))
    if state == "completed" and reply is not None:
        reply_start_rel = _relative_ms(reply["enqueued_at"], started_at)
        reply_acked = reply.get("acked_at")
        reply_end_rel = _relative_ms(reply_acked, started_at) if reply_acked is not None else None
        reply_delta = max(reply_acked - reply["enqueued_at"], 0) if reply_acked is not None else None
        rows.append(("reply", reply_start_rel, reply_end_rel, reply_delta))
        rows.append(("total", None, None, leg.get("latency_ms")))
    if rows:
        block.append_text(_timing_section(rows))
        block.append("\n")

    block.append_text(
        _delivery_section(
            dispatch.get("payload_size") if dispatch else None,
            hub_name,
            dispatch.get("source_registered") if dispatch else None,
            name,
            dispatch.get("dest_registered") if dispatch else None,
        )
    )

    if state == "failed":
        block.append("\n")
        reason = leg.get("reason") or "(no error message)"
        block.append_text(_error_section(reason, dispatch_end_rel))
    elif state == "pending":
        block.append("\n  waiting for reply…\n", style=WARN)

    return block


def _peer_hop_detail(hop: Hop, spans: list[dict[str, Any]], started_at: int, alias_map: dict[str, str]) -> Text:
    """Full, sectioned detail for one peer hop -- mirrors `_hub_leg_detail`
    but reads straight off the already-deduplicated `Hop`, except for
    payload size, which `Hop` doesn't carry -- that one field still needs
    a raw-span lookup (`_find_span`), same as the hub path.
    """
    src = display_name(hop.source, alias_map)
    dst = display_name(hop.dest, alias_map)
    state = hop.state or "pending"
    style = STATE_STYLE.get(state, "white")
    icon = STATE_ICON.get(state, "·")

    block = Text()
    block.append(f"{icon} {src} → {dst}\n\n", style=f"bold {style}")

    block.append_text(_message_section(_format_payload(hop), hop.protocol))
    block.append("\n")

    enq_rel = _relative_ms(hop.enqueued_at, started_at)
    ack_rel = _relative_ms(hop.acked_at, started_at) if hop.acked_at is not None else None
    block.append_text(_timing_section([("hop", enq_rel, ack_rel, hop.latency_ms)]))
    block.append("\n")

    raw = _find_span(spans, hop.source, hop.dest, "send")
    block.append_text(
        _delivery_section(
            raw.get("payload_size") if raw else None,
            src,
            hop.source_registered,
            dst,
            hop.dest_registered,
        )
    )

    if state in ("dropped", "timeout"):
        block.append("\n")
        reason = hop.error or "(no error message)"
        block.append_text(_error_section(reason, ack_rel if ack_rel is not None else enq_rel))

    return block


INSPECTOR_EMPTY_HINT = "click an agent for details"


def _inspector_logo_text() -> Text:
    """Compact empty-state brand mark -- just the hero wordmark from
    `brand.HERO_BANNER` (61 cols), reusing the exact same pre-rendered rows
    the splash's stacked tier is built from (`_HERO_LINES_PADDED`) rather
    than inventing new art. The splash's full co-branded lockup (hero +
    fetch.ai mark) measures 72-78 cols depending on tier -- wider than
    `#inspector-col`'s ~70-col usable width -- so this is hero-only, the
    smaller of the two marks already in `brand.py`. Styled in the calm
    `ACCENT` green (not `SPLASH_HERO_GREEN`, reserved for the splash's
    one-time flash) since this sits on screen persistently.
    """
    text = Text(justify="center")
    for i, line in enumerate(_HERO_LINES_PADDED):
        if i:
            text.append("\n")
        text.append(line, style=f"bold {ACCENT}")
    text.append("\n\n")
    text.append(INSPECTOR_EMPTY_HINT, style="dim")
    return text


def build_agent_inspector_text(
    agent: str,
    trace_id: str,
    state: TraceState,
    spans: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> Text:
    """Deep, sectioned detail for exactly one clicked agent. Nothing else
    from the trace leaks in here; that's the point of click-to-reveal
    instead of dumping every agent's detail into the panel at once.
    """
    text = Text()
    text.append(f"Session  {trace_id}\n\n", style=MUTED)

    if state.shape == HUB and state.hub:
        leg = next((leg for leg in state.legs if leg["subagent"] == agent), None)
        if leg is None:
            text.append("No detail for this agent in the current trace.", style="dim")
            return text
        text.append_text(_hub_leg_detail(leg, spans, state.hub, state.started_at, alias_map))
        return text

    outbound, reply = _latest_peer_round_trip(state.hops)
    if outbound is None:
        text.append("No detail for this agent in the current trace.", style="dim")
        return text
    if agent == outbound.source:
        text.append_text(_peer_hop_detail(outbound, spans, state.started_at, alias_map))
    elif reply is not None and agent == outbound.dest:
        text.append_text(_peer_hop_detail(reply, spans, state.started_at, alias_map))
    elif agent == outbound.dest:
        # Outbound sent, no reply yet -- still show what we know about the
        # outbound leg (message/timing/delivery) rather than nothing.
        name = display_name(agent, alias_map)
        src_name = display_name(outbound.source, alias_map)
        text.append(f"… {name}\n\n", style=f"bold {WARN}")
        text.append_text(_message_section(_format_payload(outbound), outbound.protocol))
        text.append("\n")
        enq_rel = _relative_ms(outbound.enqueued_at, state.started_at)
        text.append_text(_timing_section([("sent", enq_rel, None, None)]))
        text.append("\n")
        raw = _find_span(spans, outbound.source, outbound.dest, "send")
        text.append_text(
            _delivery_section(
                raw.get("payload_size") if raw else None,
                src_name,
                outbound.source_registered,
                name,
                outbound.dest_registered,
            )
        )
        text.append("\n  waiting for reply…", style=WARN)
    else:
        text.append("No detail for this agent in the current trace.", style="dim")
    return text


SPLASH_ROW_STAGGER_SECONDS = 0.04
SPLASH_HOLD_SECONDS = 1.5
SPLASH_FADE_SECONDS = 0.4

# Splash screen background -- also the fade's target color (see
# `_fade_step` below). Kept as its own constant, interpolated into the CSS
# below via an f-string, so the two can't drift apart the way a hardcoded
# `background: #0a0f0d;` and a hardcoded fade target could.
SPLASH_BG = "#0a0f0d"

# Discrete color steps for the fade, not an opacity ramp. Two prior opacity
# attempts (first a wrapping Container's opacity, then a single widget's
# opacity, both via `Widget.styles.animate`) both looked patchy in a real
# terminal -- different glyphs landing on visibly different brightness
# levels within the same frame, rather than dissolving together. The
# suspected cause is opacity *compositing*: the terminal has to blend each
# cell's foreground color against the background at whatever intermediate
# opacity the animator computed for that frame, and different glyph colors
# (bright green hero, muted divider, dimmer fetch.ai mark) don't necessarily
# round to the same apparent brightness step when quantized to the
# terminal's actual color resolution. Feeding the terminal explicit, already
# -blended hex colors at each step sidesteps that: there's no compositing
# left for the terminal to get inconsistent about, just a solid color swap.
# All rows/segments jump to the same step index at the same instant (see
# `_fade_step`), so the whole lockup still dims as one unit, in lockstep --
# just via discrete recoloring instead of a continuous opacity blend.
FADE_STEPS = 12
SPLASH_FADE_STEP_SECONDS = SPLASH_FADE_SECONDS / FADE_STEPS


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp_hex(start: str, end: str, t: float) -> str:
    """One color `t` of the way (0=start, 1=end) from `start` to `end`."""
    r0, g0, b0 = _hex_to_rgb(start)
    r1, g1, b1 = _hex_to_rgb(end)
    r = round(r0 + (r1 - r0) * t)
    g = round(g0 + (g1 - g0) * t)
    b = round(b0 + (b1 - b0) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _fade_color_steps(start: str) -> list[str]:
    """`FADE_STEPS + 1` colors from `start` (step 0, unchanged) down to
    `SPLASH_BG` (the last step) -- one such list per lockup color (hero,
    divider, mark), so every step recolors all of them together.
    """
    return [_lerp_hex(start, SPLASH_BG, i / FADE_STEPS) for i in range(FADE_STEPS + 1)]

_FETCH_BRAND_LINES = FETCH_BRAND.strip("\n").split("\n")
_BRAND_TITLE_LINE = _FETCH_BRAND_LINES[-1].strip()
# Logo rows only -- the last element of FETCH_BRAND is the wordmark caption,
# not a braille row, so it's sliced off *before* filtering blanks. Filtering
# first (the previous approach) doesn't work: the caption is centered with
# padding spaces, but `"uAgent Trace".strip()` is non-empty, so it would
# survive the blank filter and get treated as an extra braille row.
_FETCH_LOGO_LINES = [line for line in _FETCH_BRAND_LINES[:-1] if line.strip()]
# Splash body: a co-branded lockup of the "Trace uAgents" figlet hero (bold)
# and the full-resolution fetch.ai braille mark (normal weight) -- not the
# downsampled `FETCH_BRAND_SMALL`, which drops dots and reads broken at any
# size worth showing. Three degrade tiers, widest to narrowest:
#
#   side-by-side -- both marks on shared rows, divided by a thin vertical
#     rule spanning the taller of the two, each mark centered vertically
#     against the other. This is the primary, co-branded lockup.
#   stacked -- too narrow for side-by-side but still roomy: hero on top,
#     fetch.ai mark centered directly beneath it (one blank row between).
#   title-only -- below both: a single plain-text line, no row-by-row draw.
#
# Whichever tier `SplashScreen.on_mount` selects for the current terminal
# width, there is exactly one Static and one render path (`_reveal`) drawing
# it -- no second, separately appended copy of any row.
#
# The hero is rendered in this bright green rather than the shared
# `ACCENT` imported from `network_canvas` above -- that one stays the calm,
# separate shade (see its own comment) for the handful of spots that still
# want it, and must not be repointed just because the splash wants one
# bright moment. The value itself is `network_canvas.GREEN` -- the same
# constant the diagram/inspector chrome, this file's Header CSS, and (as of
# the live TUI's full green-unification) `network_canvas.SUCCESS` now all
# use too -- aliased here under its original name so the splash-specific
# code below didn't need to change. The fetch.ai mark next to it keeps
# rendering in `ACCENT`,
# unbrightened, so the hero reads as the one bright thing on screen against
# a calm co-mark.
SPLASH_HERO_GREEN = GREEN

_HERO_LINES = HERO_BANNER.strip("\n").split("\n")
_HERO_WIDTH = max(len(line) for line in _HERO_LINES)
_HERO_LINES_PADDED = [line.ljust(_HERO_WIDTH) for line in _HERO_LINES]

_MARK_WIDTH = max(len(line) for line in _FETCH_LOGO_LINES)
_MARK_LINES_PADDED = [line.ljust(_MARK_WIDTH) for line in _FETCH_LOGO_LINES]

_LOCKUP_GAP = "  "
_LOCKUP_DIVIDER = "│"
_STACKED_HERO_ROW_COUNT = len(_HERO_LINES_PADDED)


def _vpad(lines: list[str], width: int, height: int) -> list[str]:
    """Center `lines` vertically within `height` rows of blank, `width`-wide
    padding rows -- an odd leftover row goes on the bottom, so a shorter
    mark reads centered against a taller one rather than top-heavy.
    """
    pad = height - len(lines)
    top = pad // 2
    bottom = pad - top
    blank = " " * width
    return [blank] * top + lines + [blank] * bottom


def _build_side_by_side_rows(
    *,
    hero_color: str = SPLASH_HERO_GREEN,
    divider_color: str = MUTED,
    mark_color: str = ACCENT,
) -> tuple[list[str], list[Text]]:
    """(plain rows, styled rows) for the side-by-side tier -- hero (bold)
    and fetch.ai mark (normal weight) share every row, separated by a
    divider that spans the full, taller height of the two (padding rows
    still carry the divider, not just rows where a mark has content).

    Colors are parameters, not hardcoded, so the fade (`_fade_step`) can
    call this again at each of its discrete color steps -- the layout math
    is identical either way, only the three style strings change.
    """
    height = max(len(_HERO_LINES_PADDED), len(_MARK_LINES_PADDED))
    hero_rows = _vpad(_HERO_LINES_PADDED, _HERO_WIDTH, height)
    mark_rows = _vpad(_MARK_LINES_PADDED, _MARK_WIDTH, height)

    plain: list[str] = []
    styled: list[Text] = []
    for hero_row, mark_row in zip(hero_rows, mark_rows):
        plain.append(f"{hero_row}{_LOCKUP_GAP}{_LOCKUP_DIVIDER}{_LOCKUP_GAP}{mark_row}")
        row = Text()
        row.append(hero_row, style=f"bold {hero_color}")
        row.append(_LOCKUP_GAP)
        row.append(_LOCKUP_DIVIDER, style=divider_color)
        row.append(_LOCKUP_GAP)
        row.append(mark_row, style=mark_color)
        styled.append(row)
    return plain, styled


def _build_stacked_rows(
    *,
    hero_color: str = SPLASH_HERO_GREEN,
    mark_color: str = ACCENT,
) -> tuple[list[str], list[Text]]:
    """(plain rows, styled rows) for the stacked tier -- hero rows (bold)
    directly above the fetch.ai mark rows (normal weight), one blank
    separator row between them, mirroring `_build_side_by_side_rows`.
    Colors are parameters for the same reason as there.
    """
    plain = _HERO_LINES_PADDED + [""] + _MARK_LINES_PADDED
    styled = (
        [Text(line, style=f"bold {hero_color}") for line in _HERO_LINES_PADDED]
        + [Text("")]
        + [Text(line, style=mark_color) for line in _MARK_LINES_PADDED]
    )
    return plain, styled


_SIDE_BY_SIDE_LINES, _SIDE_BY_SIDE_ROWS = _build_side_by_side_rows()
_STACKED_LINES, _STACKED_ROWS = _build_stacked_rows()

# Precomputed per-step colors for the fade -- one list per lockup color,
# each `FADE_STEPS + 1` long, step 0 equal to the resting color and the
# last step equal to `SPLASH_BG`. `_fade_step(i)` indexes all three lists
# with the same `i`, which is what keeps hero/divider/mark moving through
# their ramps in lockstep rather than any one of them lagging behind.
_HERO_FADE_COLORS = _fade_color_steps(SPLASH_HERO_GREEN)
_DIVIDER_FADE_COLORS = _fade_color_steps(MUTED)
_MARK_FADE_COLORS = _fade_color_steps(ACCENT)

# Margin beyond each tier's own rendered width so the lockup never touches
# the terminal edge. Below `SPLASH_MIN_WIDTH_STACKED` there's nothing left
# to draw row-by-row -- the splash degrades straight to the plain title.
_SPLASH_MARGIN = 6
SPLASH_MIN_WIDTH_SIDE_BY_SIDE = max(len(line) for line in _SIDE_BY_SIDE_LINES) + _SPLASH_MARGIN
SPLASH_MIN_WIDTH_STACKED = max(len(line) for line in _STACKED_LINES) + _SPLASH_MARGIN


class SplashScreen(Screen):
    """Full-screen startup mark, shown once while the main screen mounts
    underneath. Purely decorative -- dismissing it (by timeout or keypress)
    never blocks or delays `LiveApp`'s own bootstrap, which starts in
    parallel via `on_mount`.
    """

    CSS = f"""
    SplashScreen {{
        align: center middle;
        background: {SPLASH_BG};
    }}
    #splash-content {{
        width: auto;
        height: auto;
        content-align: center middle;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._dismissed = False
        # Which tier `on_mount` picked for the current terminal width --
        # both `_reveal` (which just draws these precomputed, already-
        # styled rows) and `_fade_step` (which needs to know how to
        # *rebuild* those rows at a new color, since the fade recolors
        # what `_reveal` already fully drew) key off this.
        self._tier: str = "title"
        self._active_rows: list[Text] = []

    def compose(self) -> ComposeResult:
        # A single widget, not a Container wrapping a separate content
        # widget: every glyph the splash ever draws -- bright hero, muted
        # divider, calm fetch.ai mark -- is a Rich `Text` segment painted by
        # this one Static's own render pass. That's what makes the fade
        # (see `_start_fade`) land on every glyph in lockstep -- animating
        # a *parent* Container's opacity around a separate child Static
        # left the fade uneven/patchy in practice (some glyphs read as
        # already-dimmed while others were still bright partway through),
        # since the parent and child are two independent paints composited
        # together rather than one. One widget removes that seam entirely.
        yield Static(id="splash-content")

    def on_mount(self) -> None:
        content = self.query_one("#splash-content", Static)
        width = self.size.width or 80

        if width >= SPLASH_MIN_WIDTH_SIDE_BY_SIDE:
            self._tier = "side_by_side"
            self._active_rows = _SIDE_BY_SIDE_ROWS
        elif width >= SPLASH_MIN_WIDTH_STACKED:
            self._tier = "stacked"
            self._active_rows = _STACKED_ROWS
        else:
            # Title-only tier is the hero degraded to plain text, not a
            # different element -- it keeps the same bright hero color as
            # the other two tiers rather than falling back to ACCENT.
            self._tier = "title"
            content.update(Text(_BRAND_TITLE_LINE, style=f"bold {SPLASH_HERO_GREEN}"))
            self.set_timer(SPLASH_HOLD_SECONDS, self._start_fade)
            return

        # Row 0 draws immediately -- a zero-delay timer trips a division-by-
        # zero in Textual's timer skip-catchup path under accelerated test
        # clocks, so the first row is drawn directly instead of scheduled.
        self._reveal(0)
        for i in range(1, len(self._active_rows)):
            self.set_timer(i * SPLASH_ROW_STAGGER_SECONDS, lambda upto=i: self._reveal(upto))

        reveal_done = len(self._active_rows) * SPLASH_ROW_STAGGER_SECONDS
        self.set_timer(reveal_done + SPLASH_HOLD_SECONDS, self._start_fade)

    def _render_rows(self, rows: list[Text]) -> Text:
        """Join `rows` into one centered block -- the one rendering path
        shared by `_reveal` (a partial prefix, during draw-in) and
        `_fade_step` (always the full set, during the fade), so there's
        still only one place that assembles what `#splash-content` shows.

        `justify="center"` centers each shorter row (the blank separator in
        the stacked tier, or a shorter padding row in the side-by-side
        tier) against the widest row once Textual sizes this auto-width
        widget to that longest line -- without it, Rich left-aligns every
        row against column 0 instead of centering the block as a whole.
        """
        text = Text(justify="center")
        for i, row in enumerate(rows):
            if i:
                text.append("\n")
            text.append_text(row)
        return text

    def _reveal(self, upto: int) -> None:
        # The one and only place the splash body is *drawn in*: every row
        # up to `upto`, each already fully styled by whichever tier's
        # builder produced `_active_rows` (`_build_side_by_side_rows` or
        # `_build_stacked_rows`) -- never a second append of any row after
        # this loop.
        if self._dismissed:
            return
        content = self.query_one("#splash-content", Static)
        content.update(self._render_rows(self._active_rows[: upto + 1]))

    def _start_fade(self) -> None:
        if self._dismissed:
            return
        if self._active_rows:
            # Force the draw-in stagger to its final, fully-revealed state
            # before the fade begins. The fade timer is already scheduled
            # to fire only after every row's own reveal timer (see
            # `on_mount`: `reveal_done` is `len(rows) * stagger`, strictly
            # later than the last row's `(len - 1) * stagger`), so this
            # should already be a no-op in practice -- but making it
            # explicit here means a future change to those timings can't
            # quietly start the fade on a partially-drawn body. (Step 0 of
            # the fade below re-renders the full body anyway, at the
            # unchanged resting color, so this is now doubly redundant --
            # but it's kept because a partially-drawn body should never
            # even momentarily exist between "reveal ends" and "fade
            # starts", not just be quickly overwritten a moment later.)
            self._reveal(len(self._active_rows) - 1)

        # Discrete color steps, not an opacity ramp -- see `FADE_STEPS`'s
        # own comment for why. Step 0 (full brightness, the unchanged
        # resting color) draws immediately for the same reason row 0 of
        # the reveal stagger does: a zero-delay timer trips a division-by-
        # zero in Textual's timer skip-catchup path under accelerated test
        # clocks.
        self._fade_step(0)
        for i in range(1, FADE_STEPS + 1):
            self.set_timer(i * SPLASH_FADE_STEP_SECONDS, lambda step=i: self._fade_step(step))

    def _fade_step(self, step: int) -> None:
        # The one and only place the fade is drawn: every glyph the active
        # tier has -- hero, divider, mark, or (title-only) the plain title
        # line -- recolored to this same step index at once, via the
        # *same* per-color ramps (`_HERO_FADE_COLORS` etc.), so hero,
        # divider, and mark all move through their ramps in lockstep. That
        # single shared step index, applied to a full re-render rather
        # than any partial/per-row update, is what keeps the dissolve
        # reading as one unit instead of a patchwork of independently
        # fading pieces.
        if self._dismissed:
            return
        content = self.query_one("#splash-content", Static)
        hero_color = _HERO_FADE_COLORS[step]

        if self._tier == "title":
            content.update(Text(_BRAND_TITLE_LINE, style=f"bold {hero_color}"))
        else:
            mark_color = _MARK_FADE_COLORS[step]
            if self._tier == "side_by_side":
                divider_color = _DIVIDER_FADE_COLORS[step]
                _, rows = _build_side_by_side_rows(
                    hero_color=hero_color, divider_color=divider_color, mark_color=mark_color
                )
            else:
                _, rows = _build_stacked_rows(hero_color=hero_color, mark_color=mark_color)
            content.update(self._render_rows(rows))

        if step >= FADE_STEPS:
            self._finish()

    def _finish(self) -> None:
        # Reached from `_fade_step`'s last step. `_dismissed` still guards
        # this: a keypress can dismiss (see `on_key`) while fade-step
        # timers for steps not yet fired are still pending -- those timers
        # go on to call `_fade_step` regardless, but its own `_dismissed`
        # check stops each one from reaching this method a second time, so
        # `pop_screen` is never called twice for one screen (which would
        # pop the main screen underneath it too).
        if self._dismissed:
            return
        self._dismissed = True
        self.app.pop_screen()

    def on_key(self, event: events.Key) -> None:
        if self._dismissed:
            return
        event.stop()
        self._dismissed = True
        self.app.pop_screen()




# Shimmer step indices into _HERO_FADE_COLORS (0 = full SPLASH_HERO_GREEN,
# FADE_STEPS = SPLASH_BG) -- reusing the splash's own precomputed fade ramp
# for the logo's idle animation instead of a second one, so the two share
# one notion of "how green fades to background".
_SHIMMER_NEAR = 4
_SHIMMER_FAR = 8


def _shimmer_logo_text(tick: int) -> Text:
    """The hero wordmark with a bright band swept down it, one row per
    `tick` -- `LiveApp._shimmer_tick_count`, advanced by its own dedicated,
    fast `set_interval` (`SHIMMER_INTERVAL_SECONDS`), not the ~1.5s pulse
    tick everything else in the live TUI shares. A full sweep at one row
    per *pulse* tick took ~18s and read as barely moving; this needs its
    own faster clock to read as continuous motion instead. Wraps back to
    row 0 after the last row (treating the rows as circular, so the sweep
    loops smoothly instead of jumping back to the top). The center row of
    the band renders at full brightness, its two neighbors a medium shade,
    everything else dim -- deliberately a clearly-visible "scanning"
    shimmer, not a subtle one-step flicker, per "playful = noticeable".
    """
    rows = len(_HERO_LINES_PADDED)
    center = tick % rows
    text = Text(justify="center")
    for i, line in enumerate(_HERO_LINES_PADDED):
        if i:
            text.append("\n")
        distance = min(abs(i - center), rows - abs(i - center))
        if distance == 0:
            color = _HERO_FADE_COLORS[0]
        elif distance == 1:
            color = _HERO_FADE_COLORS[_SHIMMER_NEAR]
        else:
            color = _HERO_FADE_COLORS[_SHIMMER_FAR]
        text.append(line, style=f"bold {color}")
    return text


STAR_URL = "https://github.com/fetchai/uAgents"


# Alternating glyph/color pairs for the star-link's localized celebration
# flash (see _star_link_text's celebration_frame) -- frame parity picks
# both, so consecutive frames read as a flash instead of a static image.
_CELEBRATION_GLYPHS = ["✨ ★", "⋆ ✦"]


def _star_link_text(*, celebration_frame: int | None = None) -> Text:
    """Clickable "star the repo" line. A real `Style(link=...)` object, not
    a `"...link=URL"` style *string* -- Rich's string-style parser (what
    `Text.append(style=str)` normally goes through) has no syntax for a
    link attribute at all, and raises trying to parse one; the link has to
    be built as an actual `Style` object instead. It makes this a real
    OSC 8 hyperlink -- cmd/ctrl-click opens it directly in terminals that
    render one (iTerm2/Kitty/WezTerm). Some terminals (VS Code's
    integrated one, notably) don't act on OSC 8 links at all -- there's no
    code fix for that, it's the terminal's own choice -- so the literal URL
    is *always* printed on its own line right below, never replaced by
    anything else (celebration included, see below), so the destination is
    always there to copy-paste regardless of what the terminal does with
    the link above it.

    `celebration_frame`, if not `None`, replaces just the clickable label
    above with a brief flashing thank-you instead of the panel-wide
    takeover this used to be -- everything else in the empty state (logo,
    stat blocks, and this line's own URL right below) stays exactly where
    it is and fully visible throughout, per the user's own complaint that
    the old full-panel version wiped out what they were looking at. This
    is *separate* from -- and in addition to -- `InspectorCanvas`'s own
    click detection (`star_link_rows`), which is what actually drives the
    celebration; a terminal's native OSC 8 handling firing (or not)
    doesn't affect it either way.
    """
    text = Text(justify="center")
    if celebration_frame is None:
        text.append("★ Star uAgents on GitHub", style=Style(bold=True, color=GREEN, link=STAR_URL))
    else:
        glyphs = _CELEBRATION_GLYPHS[celebration_frame % len(_CELEBRATION_GLYPHS)]
        color = GREEN if celebration_frame % 2 == 0 else WARN
        text.append(f"{glyphs}  thanks for the click!  {glyphs}", style=f"bold {color}")
    text.append("\n")
    text.append(STAR_URL, style=Style(dim=True, link=STAR_URL))
    return text


# ~1.5s total at SHIMMER_INTERVAL_SECONDS (0.15s/tick, see LiveApp) -- a
# brief flash, not the old full-panel takeover's several-second hold.
CELEBRATION_TICKS = 10


def _build_empty_state_text(
    stats: SessionStats,
    *,
    tick: int,
    available_height: int,
    celebration_frame: int | None,
) -> tuple[Text, tuple[int, int] | None]:
    """The inspector's empty-state content: animated logo, hint, star link,
    then as many of the Session/Timing/Failures stat blocks as fit in
    `available_height` (rows) -- dropped from the bottom, in that order,
    when they don't all fit (logo and star link are never dropped).

    Deliberate, consistent vertical rhythm rather than stacked text: one
    blank line between the logo/hint/star-link group's own pieces and
    between each stat block, and *two* blank lines (a clearly bigger gap)
    where the "action" area (hint + star link) hands off to the "stats"
    area -- everything above that gap is about what to *do*, everything
    below it is read-only data, and the gap is what tells you that's
    happening. Each block costs `header_gap + len(content_lines)` rows (a
    blank separator, the header itself, then its own content lines, all at
    a consistent header+indented-content shape); a block is included only
    if adding it wouldn't push the total past `available_height`.

    `celebration_frame`, if not `None`, is passed straight through to
    `_star_link_text` -- it overlays a brief flash on just that line, so
    it doesn't change this function's row accounting at all (the
    celebration was rebuilt specifically so it *wouldn't* need to -- see
    that function's own docstring for why). Returns `star_link_rows` as
    `None` while celebrating, since there's nothing to click back into
    mid-flash.

    Returns `(text, star_link_rows)`, where `star_link_rows` is the
    `(start, end)` 0-indexed, inclusive line range the star link landed on
    -- for `InspectorCanvas` to hit-test clicks against.
    """
    text = Text(justify="center")
    text.append_text(_shimmer_logo_text(tick))
    text.append("\n\n")
    text.append(INSPECTOR_EMPTY_HINT, style="dim")
    text.append("\n\n")

    star_start_row = text.plain.count("\n")
    text.append_text(_star_link_text(celebration_frame=celebration_frame))
    star_end_row = text.plain.count("\n")
    star_link_rows = None if celebration_frame is not None else (star_start_row, star_end_row)

    _append_session_stat_blocks(text, stats, available_height)

    return text, star_link_rows


def _append_session_stat_blocks(
    text: Text, stats: SessionStats, available_height: int, *, first_block_gap: int = 3
) -> None:
    """Appends as many of the Session/Timing/Failures blocks (in that
    order) as fit within `available_height` rows total -- including
    whatever `text` already contains -- dropped from the bottom (Failures
    first) when they don't all fit. Each block costs `header_gap +
    len(content_lines)` rows (a blank separator, the header itself, then
    its own indented content lines).

    Shared by the empty state (`_build_empty_state_text`, which wants
    `first_block_gap`'s default of 3 newlines / 2 blank lines -- a clearly
    bigger gap marking the action-area-to-stats-area handoff) and the
    selected-agent footer (`_append_session_footer`, which passes
    `first_block_gap=2` since its own divider rule already marks that
    handoff and a second, bigger gap on top of it would be redundant) --
    so the two render identically otherwise: same computation, same
    styling, not two copies that could drift apart. See the module's own
    coherence note.
    """
    first_block = True
    for header, content_lines in (
        ("Session", _session_block_lines(stats)),
        ("Timing", _timing_block_lines(stats)),
        ("Failures", _failures_block_lines(stats)),
    ):
        header_gap = first_block_gap if first_block else 2
        cost = header_gap + len(content_lines)
        projected_rows = text.plain.count("\n") + cost + 1
        if projected_rows > available_height:
            break
        text.append("\n" * header_gap)
        text.append(header, style=f"bold {GREEN}")
        for line in content_lines:
            text.append("\n  " + line, style=MUTED)
        first_block = False


def _append_session_footer(text: Text, stats: SessionStats, available_height: int) -> None:
    """Divider + the same Session/Timing/Failures blocks the empty state
    shows (see `_append_session_stat_blocks`), appended to `text` only if
    at least the divider and the first block (Session) actually fit -- a
    lone divider with nothing below it would read as a rendering glitch,
    not a deliberate "there's more" signal, so the whole footer is
    skipped rather than partially shown down to just a rule line. This is
    the lowest layout priority in the selected-agent view: the per-agent
    detail above it always wins when space is short (see
    `build_agent_inspector_text`).
    """
    used_rows = text.plain.count("\n")
    footer = Text()
    footer.append("\n")
    footer.append("─" * INSPECTOR_CONTENT_WIDTH, style=MUTED)
    rows_before_blocks = footer.plain.count("\n")
    _append_session_stat_blocks(footer, stats, available_height - used_rows, first_block_gap=2)
    if footer.plain.count("\n") == rows_before_blocks:
        return
    text.append_text(footer)


class DiagramCanvas(Static):
    """The diagram widget -- hit-tests clicks against the agent box regions
    computed alongside the last render (see `LiveApp._refresh_display`) and
    posts `AgentClicked` for the app to handle. Kept as a small subclass
    rather than an app-level `on_click` so hit-testing stays colocated with
    the widget whose regions it's testing against.
    """

    class AgentClicked(Message):
        def __init__(self, agent: str) -> None:
            self.agent = agent
            super().__init__()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # address -> (x0, y0, x1, y1), in the topology's own local
        # coordinates -- the same frame `event.x`/`event.y` already arrive
        # in below. Textual translates a click into a widget's own local
        # content space before `on_click` ever sees it, transparently
        # absorbing both CSS alignment offset (#diagram-scroll's `align:
        # center middle`) and horizontal scroll offset -- so no manual
        # correction is needed here, unlike the old `left_pad` scheme this
        # replaced (which broke under scroll, since it only accounted for
        # centering).
        self.hit_regions: dict[str, tuple[int, int, int, int]] = {}

    def on_click(self, event: events.Click) -> None:
        x = event.x
        y = event.y
        for agent, (x0, y0, x1, y1) in self.hit_regions.items():
            if x0 <= x < x1 and y0 <= y < y1:
                event.stop()
                self.post_message(self.AgentClicked(agent))
                return


class InspectorCanvas(Static):
    """The inspector's content widget -- hit-tests clicks against the
    empty-state star link's row range (set alongside each render, see
    `LiveApp._render_inspector_empty_state`) and posts `StarLinkClicked`,
    mirroring `DiagramCanvas.hit_regions` above. `star_link_rows` is
    `None` (so a click here is a no-op) whenever an agent is selected or
    the celebration animation is already playing -- there's nothing to
    click back into in either case.
    """

    class StarLinkClicked(Message):
        pass

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.star_link_rows: tuple[int, int] | None = None

    def on_click(self, event: events.Click) -> None:
        if self.star_link_rows is None:
            return
        start, end = self.star_link_rows
        if start <= event.y <= end:
            event.stop()
            self.post_message(self.StarLinkClicked())


class LiveApp(App):
    """Live network diagram + trace list + rolling message feed."""

    CSS = """
    Screen {
        background: #0a0f0d;
    }
    Header {
        background: #111916;
        /* Matches SPLASH_HERO_GREEN/network_canvas.GREEN -- hardcoded
           rather than interpolated because this whole CSS block is a plain
           (not f-) string, same as Screen's #0a0f0d == SPLASH_BG above. */
        color: #4ade80;
    }
    Footer {
        background: #111916;
        color: #6b7280;
    }
    #main-row {
        height: 1fr;
        /* 17 = the hub diagram's own hard floor (_HUB_FIXED_ROWS=9 +
           MIN_STEM_HEIGHT=2 + MIN_DROP_HEIGHT=3 = 14 rows, see
           network_canvas._hub_vertical_spacing, which never renders
           shorter regardless of available_height) + #diagram-col's own
           2-row border + 1 row for #diagram-scroll's horizontal
           scrollbar, which is visible (not scrollbar-size-horizontal: 0)
           and reserves an interior row whenever the diagram overflows its
           viewport -- reserved unconditionally here rather than only when
           scrolling is active, so a diagram that starts fitting and later
           grows past the viewport (more agents join the trace) doesn't
           shift the whole layout by a row mid-session. Below this, the
           diagram's bottom row clips against whatever's below it on
           screen -- #diagram-col isn't vertically scrollable. #events-panel's
           height is sized to still fit alongside this minimum on a small
           (~80x24) terminal. */
        min-height: 17;
    }
    #trace-list {
        width: 46;
        height: 100%;
        border: round #1f3d32;
        background: #080c0a;
        padding: 0 1;
        /* Border titles have no default color rule in Textual, so this one
           was inheriting the border's own line color (#1f3d32) -- barely
           readable against the near-black background behind it. Matches
           #trace-list Label's own color below for a consistent "legible
           secondary text" tone, bold so it still reads as a heading. */
        border-title-color: #9ca3af;
        border-title-style: bold;
    }
    #trace-list > ListView {
        height: 100%;
    }
    #trace-list ListItem {
        height: 3;
    }
    #trace-list Label {
        color: #9ca3af;
    }
    #trace-list .-highlight {
        background: #1a2e26;
        text-style: bold;
    }
    #diagram-col {
        width: 1fr;
        height: 100%;
        border: round #1f3d32;
        background: #0d1210;
        padding: 0 2;
    }
    #diagram-scroll {
        width: 100%;
        height: 1fr;
        /* Centers the diagram when it's narrower than the viewport;
           inert (scroll position governs instead) when it's wider --
           see DiagramCanvas/#diagram-content below. Moved here from
           #diagram-col now that #diagram-col holds a scroll container
           instead of the canvas directly. */
        align: center middle;
    }
    #diagram-content {
        /* auto, not 100% -- the core fix. A 100%-wide canvas gets
           force-squeezed to the panel width, which truncates each ASCII
           row of the diagram independently instead of scrolling, causing
           boxes to visually overlap/spill on a wide (e.g. 5-agent) hub
           trace. auto lets the canvas render at its true natural size
           (network_canvas's own grow/floor/cap logic, unchanged) and
           #diagram-scroll handles the rest: centered if it fits, scrolls
           if it doesn't. */
        width: auto;
        height: auto;
    }
    #inspector-col {
        width: 76;
        height: 100%;
    }
    #inspector-scroll {
        height: 1fr;
        border: round #1f3d32;
        background: #0d1210;
        padding: 1 2;
        scrollbar-size-vertical: 1;
    }
    #inspector-scroll.inspector-empty {
        align: center middle;
    }
    #inspector-content {
        width: 100%;
        height: auto;
        color: #6b7280;
    }
    #events-panel {
        /* Static fallback (3 messages) for the brief window before
           on_mount's _apply_events_panel_height runs -- kept at the old
           safe value rather than the taller EVENTS_PANEL_TALL_HEIGHT so a
           short (~80x24) terminal is never even momentarily asked to fit
           #main-row's 17-row floor (see #main-row's own comment) plus a
           7-row feed in a 22-row budget (24 - 1 header - 1 footer). Past
           that first frame, height is set in Python per-resize between
           EVENTS_PANEL_SHORT_HEIGHT (this value) and
           EVENTS_PANEL_TALL_HEIGHT (5 messages, on terminals tall enough
           per MIN_HEIGHT_FOR_TALL_FEED) -- see both constants' own comment
           for the exact budget math. */
        height: 5;
        border: round #1f3d32;
        background: #080c0a;
        padding: 0 1;
    }
    Vertical {
        height: 100%;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("v", "cycle_view", "View"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("[", "prev_trace", "Older trace", show=False),
        Binding("]", "next_trace", "Newer trace", show=False),
    ]

    def __init__(self, setup: WatchSetup):
        super().__init__()
        self.setup = setup
        self.db_path = setup.db_path
        self.addresses = setup.addresses if setup.filter_only else None
        self.view_mode: ViewMode = setup.view_mode
        self._span_states: dict[str, str] = {}
        self._logged_hop_ids: set[str] = set()
        self._events: deque[Text] = deque(maxlen=MAX_EVENTS)
        self._active_trace_id: str | None = None
        self._trace_ids: list[str] = []
        self._trace_rollup_cache: dict[str, TraceState] = {}
        # Sequential, oldest-first index shown on each sidebar row --
        # assigned once per trace_id the first time `_refresh_trace_list`
        # ever sees it, then never reassigned. Deliberately not derived
        # from the row's current position (which is newest-first and
        # reshuffles every poll) or recomputed from `started_at` on each
        # refresh (which would also reshuffle if a trace's spans ever
        # arrived out of order) -- this dict is the one stable record of
        # "first-seen order" for the life of the app.
        self._trace_seq: dict[str, int] = {}
        self._next_trace_seq = 1
        self._alias_map: dict[str, str] = {}
        self._bootstrapped = False
        self._follow_latest = True
        self._pulse_on = False
        # Whether the newest feed line is currently rendered in its
        # brighter "just arrived" style -- set when a line is appended,
        # cleared (and the log re-rendered plain) on the next pulse tick,
        # so a burst of activity flashes for exactly one pulse interval
        # rather than staying bright indefinitely.
        self._flash_active = False
        self._trace_state: TraceState | None = None
        # Address of the agent whose box was last clicked -- None means
        # nothing selected, so the inspector shows the empty-state hint
        # instead of any agent's detail. Reset whenever the active trace
        # changes, since a selection from a different trace's agents
        # wouldn't mean anything here.
        self._selected_agent: str | None = None

    def _hub_hint(self) -> str | None:
        """Force hub-style rendering once the wizard has told us who the
        orchestrator is and configured 3+ watched agents -- same rule the
        pre-refactor code used, kept so early-trace rendering (before
        enough spans exist to auto-classify) still shows the fan-out shape.
        """
        if self.setup.orchestrator and len(self.setup.addresses) >= 3:
            return self.setup.orchestrator
        return None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with Horizontal(id="main-row"):
                trace_list = ListView(id="trace-list")
                # A one-time hint for the latency bar's color (see
                # _latency_bar_style) -- discoverable without repeating it
                # on every row or adding a dedicated legend row. This used
                # to read "Traces · bar caps @2s" (the bar's *scale*, not
                # its color meaning) -- now that the bar's color is its own
                # signal (speed, independent of the row's outcome color),
                # that's the more useful thing to spend this line on. Both
                # would technically still fit in #trace-list's 46-col
                # width, but packing both clauses in made the line denser
                # to read, working against the point of fixing its
                # legibility -- so the scale detail was dropped rather than
                # squeezed in.
                trace_list.border_title = "Traces — bar = speed (green→red)"
                yield trace_list
                with Vertical(id="diagram-col"):
                    with HorizontalScroll(id="diagram-scroll"):
                        yield DiagramCanvas("", id="diagram-content")
                with Vertical(id="inspector-col"):
                    with VerticalScroll(id="inspector-scroll", classes="inspector-empty"):
                        yield InspectorCanvas("", id="inspector-content")
            events_log = RichLog(id="events-panel", highlight=False, markup=False, auto_scroll=True)
            events_log.border_title = "Live messages"
            yield events_log
        yield Footer()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_inspector_visibility(event.size.width)
        self._apply_events_panel_height(event.size.height)

    def _apply_inspector_visibility(self, width: int) -> None:
        try:
            panel = self.query_one("#inspector-col")
        except Exception:
            return
        panel.display = width >= MIN_WIDTH_FOR_INSPECTOR

    def _apply_events_panel_height(self, height: int) -> None:
        try:
            panel = self.query_one("#events-panel")
        except Exception:
            return
        panel.styles.height = (
            EVENTS_PANEL_TALL_HEIGHT if height >= MIN_HEIGHT_FOR_TALL_FEED else EVENTS_PANEL_SHORT_HEIGHT
        )

    async def on_mount(self) -> None:
        # Pushed first, before anything else in this method, and awaited
        # (not fire-and-forget) so the splash is unconditional and
        # deterministic: every launch pushes it, fully mounted, before any
        # later setup step (title, inspector visibility, bootstrap) runs --
        # none of those can ever cause it to be skipped, since none of them
        # get a chance to run first or raise before the push happens. The
        # widgets on the main screen underneath stay reachable via
        # `query_one` the whole time (pushing a screen doesn't unmount the
        # one below it), so none of the calls after this need to change.
        await self.push_screen(SplashScreen())

        # Not "trace-uagents live" -- the splash screen (just dismissed)
        # already established the app's identity; naming it again here
        # would restate it a third time once combined with the sub-title
        # hint below (Header renders "{title} — {sub_title}" on one line).
        self.title = "live"
        self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
        events_log = self.query_one("#events-panel", RichLog)
        events_log.write(Text("  Waiting for message flow…", style="#6b7280"))
        self._apply_inspector_visibility(self.size.width)
        self._apply_events_panel_height(self.size.height)
        await self._bootstrap()
        self.set_interval(POLL_SECONDS, self._poll)
        self.set_interval(PULSE_SECONDS, self._pulse_tick)

    async def _pulse_tick(self) -> None:
        if not self._bootstrapped:
            return
        self._pulse_on = not self._pulse_on
        if self._flash_active:
            # One pulse interval of flash is up -- revert the newest feed
            # line to its normal style. Only re-renders the log when a
            # flash is actually pending, so an idle feed doesn't get its
            # scroll position reset every pulse for no reason.
            self._flash_active = False
            self._render_events_log(flash=False)
        await self._refresh_display(pulse_only=True)

    async def _bootstrap(self) -> None:
        self._alias_map = await get_alias_map(self.db_path)
        spans = await get_recent_spans(self.db_path, limit=200, addresses=self.addresses)
        for span in spans:
            self._span_states[span["id"]] = span["state"]
        await self._refresh_trace_list()
        if self._trace_ids:
            self._active_trace_id = self._trace_ids[0]
            await self._reload_feed_for_active_trace()
        self._bootstrapped = True
        await self._refresh_display()

    async def _refresh_trace_list(self) -> None:
        traces = await list_traces(self.db_path)
        if self.addresses:
            traces = [t for t in traces if _trace_matches_watch(t, self.addresses)]
        traces = traces[:MAX_TRACE_LIST]
        new_ids = [t["trace_id"] for t in traces]

        # Assign sequence numbers to any trace seen for the first time this
        # refresh -- sorted oldest-first (by `started_at`) among just the
        # newcomers, not in `traces`' own newest-first order, so a batch of
        # several brand-new traces in one poll (e.g. everything already in
        # the db at startup) still gets numbered 1, 2, 3... in the order
        # they actually happened, not the order this loop happens to visit
        # them.
        newcomers = sorted((t for t in traces if t["trace_id"] not in self._trace_seq), key=lambda t: t["started_at"])
        for t in newcomers:
            self._trace_seq[t["trace_id"]] = self._next_trace_seq
            self._next_trace_seq += 1

        trace_list = self.query_one("#trace-list", ListView)

        if new_ids != self._trace_ids:
            self._trace_ids = new_ids
            await trace_list.clear()
            for t in traces:
                widget_id = _trace_widget_id(t["trace_id"])
                trace_list.append(ListItem(Label(""), id=widget_id))
            if self._active_trace_id and self._active_trace_id in self._trace_ids:
                trace_list.index = self._trace_ids.index(self._active_trace_id)

        # Prune rollup cache to what's still visible, then refresh each
        # row's label. A trace with no pending legs left is cached and
        # never refetched -- only in-flight traces cost a query per poll.
        self._trace_rollup_cache = {k: v for k, v in self._trace_rollup_cache.items() if k in new_ids}

        for t in traces:
            cached = self._trace_rollup_cache.get(t["trace_id"])
            if cached is not None and cached.pending == 0:
                state = cached
            else:
                spans = await get_trace_spans(self.db_path, t["trace_id"])
                state = build_trace_state(spans, hub_hint=self._hub_hint())
                self._trace_rollup_cache[t["trace_id"]] = state

            label_text, _style = sidebar_label(t["trace_id"], state, self._alias_map, self._trace_seq.get(t["trace_id"]))
            try:
                item = trace_list.query_one(f"#{_trace_widget_id(t['trace_id'])}", ListItem)
                label_widget = item.query_one(Label)
                label_widget.update(label_text)
            except Exception:
                pass

    def _render_events_log(self, *, flash: bool) -> None:
        """Redraw the bounded feed from `self._events` -- the one rendering
        path for both a normal reload and a flash/revert, so the on-screen
        log can never hold more (or differently-styled) lines than
        `self._events` says it should. When `flash` is set, only the
        newest line (last in the bounded deque) gets the brighter style.
        """
        events_log = self.query_one("#events-panel", RichLog)
        events_log.clear()
        lines = list(self._events)
        last_index = len(lines) - 1
        for i, line in enumerate(lines):
            events_log.write(_flash_line(line) if flash and i == last_index else line)

    async def _append_new_feed_events(self) -> bool:
        """Record any not-yet-logged, terminal hops for the active trace.
        Shared by the initial load and by polling so the feed always
        derives from one authoritative, fully-deduplicated hop list.
        """
        if not self._active_trace_id:
            return False
        spans = await get_trace_spans(self.db_path, self._active_trace_id)
        if self.addresses:
            spans = [s for s in spans if _span_in_watch(s, self.addresses)]

        appended = False
        for hop in build_hops(spans):
            if hop.state not in ("delivered", "dropped", "timeout"):
                continue
            if hop.id in self._logged_hop_ids:
                continue
            self._logged_hop_ids.add(hop.id)
            self._events.append(format_event_line(hop, self._alias_map))
            appended = True

        if appended:
            self._flash_active = True
            self._render_events_log(flash=True)

        return appended

    async def _reload_feed_for_active_trace(self) -> None:
        self._events.clear()
        self._logged_hop_ids.clear()
        self._flash_active = False
        self._render_events_log(flash=False)
        await self._append_new_feed_events()

    async def _select_trace(self, trace_id: str, *, follow: bool | None = None) -> None:
        if follow is not None:
            self._follow_latest = follow
        if trace_id == self._active_trace_id:
            self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
            return
        self._active_trace_id = trace_id
        # A selection from the previous trace's agents doesn't mean
        # anything for a different trace -- back to the empty-state hint
        # until the user clicks an agent in this one.
        self._selected_agent = None
        await self._reload_feed_for_active_trace()
        self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
        await self._refresh_display()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id:
            trace_id = _trace_id_from_widget_id(str(event.item.id))
            await self._select_trace(trace_id, follow=False)

    async def on_diagram_canvas_agent_clicked(self, message: DiagramCanvas.AgentClicked) -> None:
        self._selected_agent = message.agent
        await self._refresh_display()

    async def action_toggle_follow(self) -> None:
        self._follow_latest = not self._follow_latest
        if self._follow_latest and self._trace_ids:
            await self._select_trace(self._trace_ids[0], follow=True)
        else:
            self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)

    async def action_prev_trace(self) -> None:
        if not self._trace_ids or not self._active_trace_id:
            return
        idx = self._trace_ids.index(self._active_trace_id)
        if idx + 1 < len(self._trace_ids):
            await self._select_trace(self._trace_ids[idx + 1], follow=False)

    async def action_next_trace(self) -> None:
        if not self._trace_ids or not self._active_trace_id:
            return
        idx = self._trace_ids.index(self._active_trace_id)
        if idx > 0:
            await self._select_trace(self._trace_ids[idx - 1], follow=False)

    async def _poll(self) -> None:
        if not self._bootstrapped:
            return
        self._alias_map = await get_alias_map(self.db_path)
        prev_latest = self._trace_ids[0] if self._trace_ids else None
        await self._refresh_trace_list()

        spans = await get_recent_spans(self.db_path, limit=100, addresses=self.addresses)
        changed_trace_ids: set[str] = set()
        for span in spans:
            prev = self._span_states.get(span["id"])
            current = span["state"]
            if prev == current:
                continue
            self._span_states[span["id"]] = current
            if current in ("delivered", "dropped", "timeout"):
                changed_trace_ids.add(span["trace_id"])

        if self._follow_latest and self._trace_ids and self._trace_ids[0] != self._active_trace_id:
            await self._select_trace(self._trace_ids[0], follow=True)
            changed_trace_ids.add(self._trace_ids[0])

        new_events = False
        if self._active_trace_id in changed_trace_ids:
            new_events = await self._append_new_feed_events()

        if new_events or prev_latest != (self._trace_ids[0] if self._trace_ids else None):
            await self._refresh_display()

    async def action_cycle_view(self) -> None:
        self.view_mode = "tree" if self.view_mode == "linear" else "linear"
        self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
        await save_watch_config(
            self.db_path,
            list(self.setup.addresses),
            self.setup.filter_only,
            self.setup.orchestrator,
            view_mode=self.view_mode,
        )
        await self._refresh_display()

    def _diagram_panel_width(self) -> int:
        col = self.query_one("#diagram-col")
        # Inner width: subtract horizontal padding (2+2) and border (1+1).
        return max(col.size.width - 8, 0)

    def _diagram_panel_height(self) -> int:
        col = self.query_one("#diagram-col")
        # Inner height: subtract border (1+1) -- no vertical padding on
        # #diagram-col. Fed to network_canvas as available_height so the
        # diagram's connector lines can grow to use the panel's real height
        # instead of sitting at a small fixed size inside it.
        return max(col.size.height - 2, 0)

    async def _refresh_display(self, *, pulse_only: bool = False) -> None:
        content = self.query_one("#diagram-content", DiagramCanvas)
        inspector = self.query_one("#inspector-content", Static)
        inspector_scroll = self.query_one("#inspector-scroll", VerticalScroll)

        if not self._active_trace_id:
            if not pulse_only:
                content.update(
                    Text(
                        "Waiting for messages…\n\n"
                        "Start your instrumented agents\n"
                        "in another terminal.",
                        style="dim",
                    )
                )
                content.hit_regions = {}
                inspector_scroll.set_class(True, "inspector-empty")
                inspector.update(_inspector_logo_text())
            return

        spans = await get_trace_spans(self.db_path, self._active_trace_id)
        if self.addresses:
            spans = [s for s in spans if _span_in_watch(s, self.addresses)]

        state = build_trace_state(spans, hub_hint=self._hub_hint())
        self._trace_state = state

        pulse = self._pulse_on and state.pending > 0
        hit_regions: dict[str, tuple[int, int, int, int]] = {}
        available_width = self._diagram_panel_width()
        available_height = self._diagram_panel_height()

        # Only the star/hub topology centers its hub horizontally within
        # the diagram's total width -- peer topology has no hub, and the
        # tree view left-anchors its root instead of centering it -- so
        # only this branch is a candidate for the hub-centered initial
        # scroll below.
        is_star_hub_topology = False

        if state.total == 0:
            topology = Text("Waiting for messages in this trace…", style="dim")
        elif state.shape == HUB and state.hub:
            if self.view_mode == "tree" and state.tree is not None:
                topology = build_hub_tree_diagram(state.tree, self._alias_map)
            else:
                is_star_hub_topology = True
                topology, hit_regions = _hub_diagram_pieces(
                    state,
                    self._alias_map,
                    pulse=pulse,
                    selected=self._selected_agent,
                    available_width=available_width,
                    available_height=available_height,
                )
        else:
            topology, hit_regions = _peer_diagram_pieces(
                state.hops,
                self._alias_map,
                pulse=pulse,
                selected=self._selected_agent,
                available_width=available_width,
                available_height=available_height,
            )

        content.update(topology)
        content.hit_regions = hit_regions

        if not pulse_only:
            # Hub-centered initial scroll: when the star/hub topology
            # overflows its viewport, land on its horizontal center (where
            # the hub sits, per network_canvas's own centered layout)
            # instead of #diagram-scroll's default left-edge start -- a
            # first look at a wide fan-out trace should show the hub, not
            # blank space and the leftmost sub-agent box. Scoped to
            # is_star_hub_topology specifically: a peer diagram has no hub
            # to center on (centering on its overall midpoint instead just
            # shows the seam between the two boxes), and the tree view
            # left-anchors its root rather than centering it, so applying
            # this there would scroll *away* from the root. `block_width
            # (topology)` reads the natural width straight off the Text
            # we're about to render rather than querying the widget's own
            # (possibly not yet relaid-out) size. Also scoped to non-pulse
            # refreshes (new spans, trace switches) rather than every
            # ~1.5s pulse tick, so it doesn't repeatedly yank a user's own
            # scroll position back to center while they're reading a busy
            # trace.
            diagram_total_w = block_width(topology)
            if is_star_hub_topology and diagram_total_w > available_width:
                target_x = max(0, (diagram_total_w - available_width) // 2)
                # Deferred via call_after_refresh: #diagram-scroll's
                # scrollable bounds (virtual_size/max_scroll_x) are only
                # recomputed from the new, wider content once Textual
                # processes the layout triggered by content.update() above
                # -- calling scroll_to() synchronously here would clamp
                # target_x against the *previous* (narrower or empty)
                # bounds and silently no-op.
                def _center_on_hub(x: int = target_x) -> None:
                    self.query_one("#diagram-scroll", HorizontalScroll).scroll_to(x=x, animate=False)

                self.call_after_refresh(_center_on_hub)

            # Selection is per-agent, not per-trace-dump: nothing selected
            # yet (or the trace changed under it) shows the empty-state
            # hint; a click shows that one agent's detail and nothing else.
            if self._selected_agent is not None:
                inspector_scroll.set_class(False, "inspector-empty")
                inspector.update(
                    build_agent_inspector_text(
                        self._selected_agent, self._active_trace_id, state, spans, self._alias_map
                    )
                )
            else:
                inspector_scroll.set_class(True, "inspector-empty")
                inspector.update(_inspector_logo_text())

            trace_list = self.query_one("#trace-list", ListView)
            if self._active_trace_id in self._trace_ids:
                trace_list.index = self._trace_ids.index(self._active_trace_id)


async def run_live(setup: WatchSetup) -> None:
    app = LiveApp(setup)
    await app.run_async()
