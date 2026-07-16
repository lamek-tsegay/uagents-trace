"""Character-grid canvas for hub-and-spoke network diagrams in the terminal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text

# Canonical "green" for the live TUI -- the same vivid value `live.py` uses
# for the splash hero (imported there as `SPLASH_HERO_GREEN` so the splash's
# own naming stays intact). Diagram box defaults (the hub/placeholder boxes
# below, which aren't any one leg's state) and the connector-line fallback
# read this directly, and so does `SUCCESS` below -- there is exactly one
# green in the live TUI. `ACCENT` is kept, unchanged, only for the handful
# of spots that still want the calmer shade -- wizard.py's CLI prompts and
# the splash's own fetch.ai co-mark (see that call site's comment).
GREEN = "#4ade80"
ACCENT = "#34d399"
MUTED = "#6b7280"
# `SUCCESS` used to be its own, deliberately dimmer green (#3f8f66) so that
# delivered/completed -- the steady state, and so the color on screen the
# most -- didn't read as "one wall of green" with nothing to focus on. That
# traded a real amount of visual hierarchy for uniformity; repointed to
# `GREEN` above by explicit choice (full one-green-everywhere over that
# hierarchy) rather than because the tradeoff stopped existing. The
# completed/failed/pending distinction this constant is part of still
# reads fine without it -- ERROR (red) and WARN (amber) are their own hues,
# not shades of green, and every state also carries its own glyph (✓/✗/⋯)
# and text, so nothing here depended on dim-vs-vivid green as its only
# signal.
SUCCESS = GREEN
ERROR = "#f87171"
WARN = "#facc15"

STATE_STYLE = {
    "completed": SUCCESS,
    "failed": ERROR,
    "pending": WARN,
}

STATUS_GLYPH = {
    "completed": ("✓", SUCCESS),
    "failed": ("✗", ERROR),
    "pending": ("⋯", WARN),
}

# Orthogonal arrows only (│ ─ ▼), no diagonals. Rows that never grow --
# they're sized to their own fixed content (a 3-row box, a 1-row subtitle)
# regardless of how much panel space is available.
HUB_ROW = 0
SUBTITLE_ROW = 3
LINE_TOP = 4
BOX_MIN_WIDTH = 10

# Below: MINIMUMS (the pre-size-aware fixed layout, used whenever a caller
# doesn't pass available_width/available_height -- e.g. a test, or a
# terminal too small to offer anything extra) and MAXIMUMS (so an ultrawide
# terminal doesn't stretch things absurdly). Filling extra space always
# widens gaps/connectors, never the boxes themselves -- see _agent_columns
# and _hub_vertical_spacing.
MIN_AGENT_GAP = 14
MAX_AGENT_GAP = 40
MIN_COL_WIDTH = 22

MIN_STEM_HEIGHT = 2  # hub box bottom -> bus line
MAX_STEM_HEIGHT = 6
MIN_DROP_HEIGHT = 3  # bus line -> agent box top, incl. the arrowhead row
MAX_DROP_HEIGHT = 8
# Rows that don't grow with available height: hub box (3) + subtitle (1)
# + bus line (1) + agent box (3) + status glyph row (1).
_HUB_FIXED_ROWS = 9

MIN_PEER_GAP = 24
MAX_PEER_GAP = 60
MIN_PEER_HEIGHT = 11
MAX_PEER_HEIGHT = 15


class Canvas:
    """Mutable character grid with optional Rich style per cell."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.chars = [[" "] * width for _ in range(height)]
        self.styles: list[list[str | None]] = [[None] * width for _ in range(height)]

    def set(self, x: int, y: int, ch: str, style: str | None = None) -> None:
        if 0 <= x < self.width and 0 <= y < self.height and ch:
            if self.chars[y][x] == " ":
                self.chars[y][x] = ch[0]
                if style:
                    self.styles[y][x] = style

    def text_over(self, x: int, y: int, value: str, style: str | None = None) -> None:
        for i, ch in enumerate(value):
            px = x + i
            if 0 <= px < self.width and 0 <= y < self.height:
                self.chars[y][px] = ch
                if style:
                    self.styles[y][px] = style

    def hline(self, x0: int, x1: int, y: int, style: str | None = None) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.text_over(x, y, "─", style)

    def vline(self, x: int, y0: int, y1: int, style: str | None = None) -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.text_over(x, y, "│", style)

    def line(self, x0: int, y0: int, x1: int, y1: int, style: str | None = None) -> None:
        """Horizontal or vertical line only."""
        if y0 == y1:
            self.hline(x0, x1, y0, style)
        elif x0 == x1:
            self.vline(x0, y0, y1, style)

    def draw_box(
        self,
        x: int,
        y: int,
        label: str,
        style: str | None = None,
        *,
        double: bool = False,
    ) -> tuple[int, int]:
        """Draw a 3-line box; return (total_width, center_x). Un-bold by
        default -- bold is reserved for a caller explicitly flagging failure.

        `double` switches to double-line border glyphs -- the visual marker
        for "this is the currently-selected agent", distinguishable even
        when a box is already bold-red for a failed leg (where a bolder
        weight alone wouldn't read as a different thing).
        """
        w = max(len(label) + 2, BOX_MIN_WIDTH)
        total_w = w + 2  # both border columns included
        box_style = style or GREEN
        tl, tr, bl, br, h, v = ("╔", "╗", "╚", "╝", "═", "║") if double else ("┌", "┐", "└", "┘", "─", "│")
        self.text_over(x, y, tl + h * w + tr, box_style)
        self.text_over(x, y + 1, v + label.center(w) + v, box_style)
        self.text_over(x, y + 2, bl + h * w + br, box_style)
        return total_w, x + total_w // 2

    def to_text(self, default_style: str | None = None) -> Text:
        result = Text()
        for y in range(self.height):
            if y:
                result.append("\n")
            x = 0
            while x < self.width:
                style = self.styles[y][x] or default_style
                run = self.chars[y][x]
                x += 1
                while x < self.width and self.chars[y][x] == run and (self.styles[y][x] or default_style) == style:
                    run += self.chars[y][x]
                    x += 1
                if style:
                    result.append(run, style=style)
                else:
                    result.append(run)
        return result


def format_ms(ms: int | None) -> str:
    if ms is None:
        return "…"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms}ms"


def block_width(text: Text) -> int:
    lines = [line for line in text.plain.split("\n") if line.strip()]
    return max((len(line) for line in lines), default=0)


def _agent_columns(
    n: int, box_widths: list[int], available_width: int | None
) -> tuple[list[tuple[int, int]], int]:
    """Return ([(x_start, center_x), ...], total_width).

    Every column gets the *same* width (`col_w`), and each agent's center
    is pinned to the exact midpoint of its own column (`i*col_w +
    col_w//2`) regardless of that agent's own box width -- so centers form
    a perfect arithmetic sequence (exactly evenly spaced) even when agent
    names have different lengths. A box narrower than its column is
    centered *around* that fixed midpoint, not left-aligned within it.

    `col_w` itself grows from the content-driven minimum up toward
    `available_width` (capped) when there's room -- widening the gap
    between boxes, never the boxes.
    """
    if n == 0:
        return [], 0
    max_box_w = max(box_widths)
    natural_col_w = max(max_box_w + MIN_AGENT_GAP, MIN_COL_WIDTH)
    col_w = natural_col_w
    if available_width:
        max_col_w = max_box_w + MAX_AGENT_GAP
        candidate = min(available_width // n, max_col_w)
        col_w = max(natural_col_w, candidate)

    positions: list[tuple[int, int]] = []
    for bw in box_widths:
        i = len(positions)
        cx = i * col_w + col_w // 2
        total_box_w = bw + 2
        x0 = cx - total_box_w // 2
        positions.append((x0, cx))
    return positions, n * col_w


def _hub_vertical_spacing(available_height: int | None) -> tuple[int, int]:
    """(stem_height, drop_height) -- rows of vertical connector above and
    below the bus line. Growing these (never the boxes) is how the hub
    diagram fills a taller panel; the MIN_* floors match the pre-size-aware
    fixed layout, so a short terminal never looks more cramped than before.
    """
    if not available_height:
        return MIN_STEM_HEIGHT, MIN_DROP_HEIGHT
    slack = max(available_height - _HUB_FIXED_ROWS - MIN_STEM_HEIGHT - MIN_DROP_HEIGHT, 0)
    stem_bonus = min(slack // 2, MAX_STEM_HEIGHT - MIN_STEM_HEIGHT)
    drop_bonus = min(slack - stem_bonus, MAX_DROP_HEIGHT - MIN_DROP_HEIGHT)
    return MIN_STEM_HEIGHT + stem_bonus, MIN_DROP_HEIGHT + drop_bonus


def _line_style(state: str, pulse: bool) -> str:
    if pulse and state == "pending":
        return WARN
    if state == "completed":
        return SUCCESS
    if state == "failed":
        return f"bold {ERROR}"
    return STATE_STYLE.get(state, GREEN)


def _stem_style(legs: list[dict[str, Any]], pulse: bool) -> str:
    """Color for the shared trunk (hub down to the bus) -- deliberately
    never escalates to ERROR just because one of several legs failed. The
    trunk is shared wiring, not any single leg's outcome; painting it red
    on a partial failure makes every successful leg passing through it
    look broken too, drowning out the one arrow that should actually pop.
    Red is reserved for that specific failed leg's own arrow.
    """
    if legs and all(leg.get("state") == "completed" for leg in legs):
        return SUCCESS
    if pulse and any(leg.get("state") == "pending" for leg in legs):
        return WARN
    return MUTED


def _bus_junction_char(x: int, hub_cx: int, agent_centers: set[int]) -> str:
    if x == hub_cx or x in agent_centers:
        return "┬"
    return "─"


def _draw_hub_arrows(
    canvas: Canvas,
    hub_cx: int,
    agent_centers: list[int],
    leg_states: list[str],
    *,
    bus_row: int,
    agent_row: int,
    pulse: bool,
) -> None:
    """One orthogonal arrow per sub-agent: stem → bus → drop → ▼.

    `bus_row`/`agent_row` come from the layout that also placed the boxes
    (see HubLayout), so the junction/drop/arrowhead columns drawn here are
    always the *same* `agent_centers` values used to position the boxes
    themselves -- they can't drift apart.
    """
    if not agent_centers:
        return

    bus_style = _stem_style([{"state": s} for s in leg_states], pulse)
    arrow_tip = agent_row - 1

    if len(agent_centers) == 1:
        cx = agent_centers[0]
        style = _line_style(leg_states[0], pulse)
        if cx == hub_cx:
            canvas.vline(hub_cx, LINE_TOP, arrow_tip - 1, style)
            canvas.text_over(hub_cx, arrow_tip, "▼", style)
        else:
            canvas.vline(hub_cx, LINE_TOP, bus_row - 1, style)
            lo, hi = min(hub_cx, cx), max(hub_cx, cx)
            for x in range(lo, hi + 1):
                ch = "┬" if x in (hub_cx, cx) else "─"
                canvas.text_over(x, bus_row, ch, style)
            canvas.vline(cx, bus_row + 1, arrow_tip - 1, style)
            canvas.text_over(cx, arrow_tip, "▼", style)
        return

    min_x = min(agent_centers)
    max_x = max(agent_centers)

    canvas.vline(hub_cx, LINE_TOP, bus_row - 1, bus_style)
    for x in range(min_x, max_x + 1):
        ch = _bus_junction_char(x, hub_cx, set(agent_centers))
        canvas.text_over(x, bus_row, ch, bus_style)

    for agent_cx, state in zip(agent_centers, leg_states):
        style = _line_style(state, pulse)
        canvas.vline(agent_cx, bus_row + 1, arrow_tip - 1, style)
        canvas.text_over(agent_cx, arrow_tip, "▼", style)


def _status_glyph(state: str, pulse: bool) -> tuple[str, str]:
    if pulse and state == "pending":
        return "◆", WARN
    return STATUS_GLYPH.get(state, ("·", MUTED))


def _box_style(state: str) -> str:
    """Box outline color for one agent, by that agent's own leg state --
    bold only for a failed leg, so a failure's whole column (box, arrow,
    glyph) pops together instead of just the arrow underneath it.
    """
    if state == "failed":
        return f"bold {ERROR}"
    return STATE_STYLE.get(state, WARN)


def _ensure_bold(style: str) -> str:
    return style if style.startswith("bold ") else f"bold {style}"


# (x0, y0, x1, y1) box region, y1/x1 exclusive -- the same convention as
# Python slicing, so `x0 <= x < x1 and y0 <= y < y1` is a hit test.
BoxRegion = tuple[int, int, int, int]


@dataclass
class HubLayout:
    """Geometry for one hub topology render -- computed once and shared by
    the text renderer and the click hit-region builder so a box's drawn
    position and its clickable region can never drift apart.
    """

    total_w: int
    total_h: int
    hub_box: BoxRegion
    hub_cx: int
    agent_boxes: list[BoxRegion]
    agent_centers: list[int]
    bus_row: int
    agent_row: int
    status_row: int


def _compute_hub_layout(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
    available_width: int | None = None,
    available_height: int | None = None,
) -> HubLayout:
    n = len(legs)
    box_widths = [max(len(name) + 2, BOX_MIN_WIDTH) for name in agent_names]
    inner_w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
    columns, agents_w = _agent_columns(n, box_widths, available_width)
    total_w = max(agents_w, inner_w + 2 + 8)

    hub_w = inner_w + 2
    hub_x = (total_w - hub_w) // 2
    hub_cx = hub_x + hub_w // 2

    offset = (total_w - agents_w) // 2
    agent_boxes: list[BoxRegion] = []
    agent_centers: list[int] = []

    stem_height, drop_height = _hub_vertical_spacing(available_height)
    bus_row = LINE_TOP + stem_height
    agent_row = bus_row + 1 + drop_height
    status_row = agent_row + 3
    total_h = status_row + 1

    for i, bw in enumerate(box_widths):
        x0, cx = columns[i]
        x0 += offset
        cx += offset
        box_w = bw + 2
        agent_boxes.append((x0, agent_row, x0 + box_w, agent_row + 3))
        agent_centers.append(cx)

    return HubLayout(
        total_w=total_w,
        total_h=total_h,
        hub_box=(hub_x, HUB_ROW, hub_x + hub_w, HUB_ROW + 3),
        hub_cx=hub_cx,
        agent_boxes=agent_boxes,
        agent_centers=agent_centers,
        bus_row=bus_row,
        agent_row=agent_row,
        status_row=status_row,
    )


def build_hub_hit_regions(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
    available_width: int | None = None,
    available_height: int | None = None,
) -> list[BoxRegion]:
    """Per-leg box regions, same order as `legs`/`agent_names` -- for the
    live TUI to hit-test a click against. Excludes the hub's own box: the
    hub isn't a leg, so there's no per-agent detail to show for clicking it.

    `available_width`/`available_height` must match whatever was passed to
    `build_hub_topology` for the same render, or the hit regions computed
    here will silently diverge from what's actually on screen.
    """
    if not legs:
        return []
    return _compute_hub_layout(legs, hub_name, agent_names, available_width, available_height).agent_boxes


def build_hub_topology(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
    *,
    pulse: bool = False,
    selected: str | None = None,
    available_width: int | None = None,
    available_height: int | None = None,
) -> Text:
    """Star topology: boxed hub centered above sub-agents, one arrow each.

    `selected` (an entry of `agent_names`) draws that one agent's box with
    a double-line border so it reads as "highlighted" even when it's
    already bold-red for a failed leg, where weight alone wouldn't show a
    difference.

    `available_width`/`available_height`, if given, let the layout grow to
    fill more of the panel (wider gaps between boxes, longer connector
    lines) -- see `_agent_columns`/`_hub_vertical_spacing`. Omitting them
    falls back to the same fixed minimum layout as before.
    """
    if not legs:
        placeholder_w = max(len(hub_name) + 6, 28)
        if available_width:
            placeholder_w = max(placeholder_w, available_width)
        canvas = Canvas(placeholder_w, 6)
        w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
        box_total_w = w + 2
        canvas.draw_box((placeholder_w - box_total_w) // 2, 1, hub_name)
        caption = "Waiting for dispatch to sub-agents…"
        canvas.text_over(max((placeholder_w - len(caption)) // 2, 0), 5, caption, MUTED)
        return canvas.to_text()

    layout = _compute_hub_layout(legs, hub_name, agent_names, available_width, available_height)
    canvas = Canvas(layout.total_w, layout.total_h)
    hub_x, hub_y, _, _ = layout.hub_box
    canvas.draw_box(hub_x, hub_y, hub_name)
    subtitle = "orchestrator"
    canvas.text_over(layout.hub_cx - len(subtitle) // 2, SUBTITLE_ROW, subtitle, MUTED)

    leg_states: list[str] = []

    for i, leg in enumerate(legs):
        x0, y0, _, _ = layout.agent_boxes[i]
        leg_state = leg.get("state", "pending")
        is_selected = selected is not None and agent_names[i] == selected
        box_style = _box_style(leg_state)
        if is_selected:
            box_style = _ensure_bold(box_style)
        canvas.draw_box(x0, y0, agent_names[i], style=box_style, double=is_selected)
        leg_states.append(leg_state)
        glyph, glyph_style = _status_glyph(leg_state, pulse)
        canvas.text_over(layout.agent_centers[i], layout.status_row, glyph, glyph_style)

    _draw_hub_arrows(
        canvas,
        layout.hub_cx,
        layout.agent_centers,
        leg_states,
        bus_row=layout.bus_row,
        agent_row=layout.agent_row,
        pulse=pulse,
    )

    return canvas.to_text()


@dataclass
class PeerLayout:
    """Geometry for one peer topology render -- see `HubLayout`."""

    total_w: int
    total_h: int
    left_box: BoxRegion
    right_box: BoxRegion
    left_cx: int
    right_cx: int
    arrow_row: int
    status_row: int


def _compute_peer_layout(
    left_name: str,
    right_name: str,
    available_width: int | None = None,
    available_height: int | None = None,
) -> PeerLayout:
    left_w = max(len(left_name) + 2, BOX_MIN_WIDTH)
    right_w = max(len(right_name) + 2, BOX_MIN_WIDTH)
    natural_total_w = left_w + MIN_PEER_GAP + right_w + 4

    gap = MIN_PEER_GAP
    total_w = natural_total_w
    if available_width and available_width > natural_total_w:
        max_total_w = left_w + MAX_PEER_GAP + right_w + 4
        total_w = min(available_width, max_total_w)
        gap = total_w - left_w - right_w - 4

    lx = (total_w - left_w - gap - right_w) // 2
    rx = lx + left_w + gap
    left_box_w = left_w + 2
    right_box_w = right_w + 2

    total_h = MIN_PEER_HEIGHT
    if available_height:
        total_h = max(MIN_PEER_HEIGHT, min(available_height, MAX_PEER_HEIGHT))
    # The fixed-height layout's rows (arrow@2, box@4, status@8) shift down
    # together by half of any extra height, so the whole cluster stays
    # vertically centered in a taller canvas rather than pinned to the top.
    voffset = (total_h - MIN_PEER_HEIGHT) // 2
    arrow_row = 2 + voffset
    box_row = 4 + voffset
    status_row = 8 + voffset

    return PeerLayout(
        total_w=total_w,
        total_h=total_h,
        left_box=(lx, box_row, lx + left_box_w, box_row + 3),
        right_box=(rx, box_row, rx + right_box_w, box_row + 3),
        left_cx=lx + left_box_w // 2,
        right_cx=rx + right_box_w // 2,
        arrow_row=arrow_row,
        status_row=status_row,
    )


def build_peer_hit_regions(
    left_name: str,
    right_name: str,
    available_width: int | None = None,
    available_height: int | None = None,
) -> tuple[BoxRegion, BoxRegion]:
    """(left_box, right_box) regions -- for the live TUI to hit-test a click
    against. Mirrors `build_hub_hit_regions` for the two-agent case; same
    caveat about matching whatever was passed to `build_peer_topology`.
    """
    layout = _compute_peer_layout(left_name, right_name, available_width, available_height)
    return layout.left_box, layout.right_box


def build_peer_topology(
    left_name: str,
    right_name: str,
    *,
    state: str = "completed",
    pulse: bool = False,
    selected: str | None = None,
    available_width: int | None = None,
    available_height: int | None = None,
) -> Text:
    """Two boxed agents with a single outbound arrow -- a single centered
    horizontal connection that scales its gap (and, modestly, its overall
    height) to `available_width`/`available_height`, same spirit as the
    hub topology but simpler since there's no branching to lay out.
    """
    style = _line_style(state, pulse)
    box_style = _box_style(state)
    layout = _compute_peer_layout(left_name, right_name, available_width, available_height)
    canvas = Canvas(layout.total_w, layout.total_h)

    lx, ly, _, _ = layout.left_box
    rx, ry, _, _ = layout.right_box
    left_style = _ensure_bold(box_style) if selected == left_name else box_style
    right_style = _ensure_bold(box_style) if selected == right_name else box_style
    canvas.draw_box(lx, ly, left_name, style=left_style, double=selected == left_name)
    canvas.draw_box(rx, ry, right_name, style=right_style, double=selected == right_name)

    canvas.hline(layout.left_cx, layout.right_cx - 1, layout.arrow_row, style)
    canvas.text_over(layout.right_cx, layout.arrow_row, "▶", style)

    glyph, glyph_style = _status_glyph(state, pulse)
    canvas.text_over(layout.left_cx, layout.status_row, glyph, glyph_style)
    canvas.text_over(layout.right_cx, layout.status_row, glyph, glyph_style)

    return canvas.to_text()


# Back-compat aliases
build_hub_network = build_hub_topology
build_peer_network = build_peer_topology
