"""Character-grid canvas for hub-and-spoke network diagrams in the terminal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text

ACCENT = "#34d399"
MUTED = "#6b7280"
# Dimmed on purpose: delivered/completed is the steady state, so it's the
# color on screen the most. Keeping it saturated made everything read as
# "one wall of green" with nothing to focus on. ERROR stays fully bright --
# bright/bold is now reserved for failures and the selected trace, not for
# the common case.
SUCCESS = "#3f8f66"
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

# Fixed layout — orthogonal arrows only (│ ─ ▼), no diagonals.
HUB_ROW = 0
SUBTITLE_ROW = 3
LINE_TOP = 4
BUS_ROW = 6
AGENT_ROW = 10
STATUS_ROW = 13
CANVAS_HEIGHT = 14
BOX_MIN_WIDTH = 10
AGENT_GAP = 14


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
        """Draw a 3-line box; return (width, center_x). Un-bold by default --
        bold is reserved for a caller explicitly flagging failure.

        `double` switches to double-line border glyphs -- the visual marker
        for "this is the currently-selected agent", distinguishable even
        when a box is already bold-red for a failed leg (where a bolder
        weight alone wouldn't read as a different thing).
        """
        w = max(len(label) + 2, BOX_MIN_WIDTH)
        box_style = style or ACCENT
        tl, tr, bl, br, h, v = ("╔", "╗", "╚", "╝", "═", "║") if double else ("┌", "┐", "└", "┘", "─", "│")
        self.text_over(x, y, tl + h * w + tr, box_style)
        self.text_over(x, y + 1, v + label.center(w) + v, box_style)
        self.text_over(x, y + 2, bl + h * w + br, box_style)
        return w, x + w // 2

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


def build_diagram_legend() -> Text:
    """Color key for the diagram -- what each arrow/glyph color means."""
    legend = Text()
    legend.append("● ", style=MUTED)
    legend.append("dispatch", style=MUTED)
    legend.append("   ● ", style=SUCCESS)
    legend.append("delivered", style=SUCCESS)
    legend.append("   ● ", style=WARN)
    legend.append("pending", style=WARN)
    legend.append("   ● ", style=ERROR)
    legend.append("failed", style=ERROR)
    return legend


def build_table_legend() -> Text:
    """Column key for the leg/route table -- what Out/In/Total measure."""
    return Text(
        "Out = send ack  ·  In = return ack  ·  Total = round trip (incl. processing)",
        style=MUTED,
    )


def block_width(text: Text) -> int:
    lines = [line for line in text.plain.split("\n") if line.strip()]
    return max((len(line) for line in lines), default=0)


def _agent_columns(n: int, box_widths: list[int]) -> tuple[list[tuple[int, int]], int]:
    """Return ([(x_start, center_x), ...], total_width) — agents evenly spaced and centered."""
    if n == 0:
        return [], 0
    col_w = max(max(box_widths) + AGENT_GAP, 22)
    total_w = n * col_w
    positions = []
    for i, bw in enumerate(box_widths):
        x0 = i * col_w + (col_w - bw) // 2
        positions.append((x0, x0 + bw // 2))
    return positions, total_w


def _line_style(state: str, pulse: bool) -> str:
    if pulse and state == "pending":
        return WARN
    if state == "completed":
        return SUCCESS
    if state == "failed":
        return f"bold {ERROR}"
    return STATE_STYLE.get(state, ACCENT)


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
    pulse: bool,
) -> None:
    """One orthogonal arrow per sub-agent: stem → bus → drop → ▼."""
    if not agent_centers:
        return

    bus_style = _stem_style(
        [{"state": s} for s in leg_states],
        pulse,
    )
    arrow_tip = AGENT_ROW - 1

    if len(agent_centers) == 1:
        cx = agent_centers[0]
        style = _line_style(leg_states[0], pulse)
        if cx == hub_cx:
            canvas.vline(hub_cx, LINE_TOP, arrow_tip - 1, style)
            canvas.text_over(hub_cx, arrow_tip, "▼", style)
        else:
            canvas.vline(hub_cx, LINE_TOP, BUS_ROW - 1, style)
            lo, hi = min(hub_cx, cx), max(hub_cx, cx)
            for x in range(lo, hi + 1):
                if x == hub_cx:
                    ch = "┬"
                elif x == cx:
                    ch = "┬"
                else:
                    ch = "─"
                canvas.text_over(x, BUS_ROW, ch, style)
            canvas.vline(cx, BUS_ROW + 1, arrow_tip - 1, style)
            canvas.text_over(cx, arrow_tip, "▼", style)
        return

    min_x = min(agent_centers)
    max_x = max(agent_centers)

    canvas.vline(hub_cx, LINE_TOP, BUS_ROW - 1, bus_style)
    for x in range(min_x, max_x + 1):
        ch = _bus_junction_char(x, hub_cx, set(agent_centers))
        canvas.text_over(x, BUS_ROW, ch, bus_style)

    for agent_cx, state in zip(agent_centers, leg_states):
        style = _line_style(state, pulse)
        canvas.vline(agent_cx, BUS_ROW + 1, arrow_tip - 1, style)
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
    hub_box: BoxRegion
    hub_cx: int
    agent_boxes: list[BoxRegion]
    agent_centers: list[int]


def _compute_hub_layout(legs: list[dict[str, Any]], hub_name: str, agent_names: list[str]) -> HubLayout:
    n = len(legs)
    box_widths = [max(len(name) + 2, BOX_MIN_WIDTH) for name in agent_names]
    inner_w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
    columns, agents_w = _agent_columns(n, box_widths)
    total_w = max(agents_w, inner_w + 2 + 8)

    hub_x = (total_w - (inner_w + 2)) // 2
    hub_w = inner_w + 2
    hub_cx = hub_x + hub_w // 2

    offset = (total_w - agents_w) // 2
    agent_boxes: list[BoxRegion] = []
    agent_centers: list[int] = []
    for i, bw in enumerate(box_widths):
        x0, cx = columns[i]
        x0 += offset
        cx += offset
        box_w = bw + 2
        agent_boxes.append((x0, AGENT_ROW, x0 + box_w, AGENT_ROW + 3))
        agent_centers.append(cx)

    return HubLayout(
        total_w=total_w,
        hub_box=(hub_x, HUB_ROW, hub_x + hub_w, HUB_ROW + 3),
        hub_cx=hub_cx,
        agent_boxes=agent_boxes,
        agent_centers=agent_centers,
    )


def build_hub_hit_regions(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
) -> list[BoxRegion]:
    """Per-leg box regions, same order as `legs`/`agent_names` -- for the
    live TUI to hit-test a click against. Excludes the hub's own box: the
    hub isn't a leg, so there's no per-agent detail to show for clicking it.
    """
    if not legs:
        return []
    return _compute_hub_layout(legs, hub_name, agent_names).agent_boxes


def build_hub_topology(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
    *,
    pulse: bool = False,
    selected: str | None = None,
) -> Text:
    """Star topology: boxed hub centered above sub-agents, one arrow each.

    `selected` (an entry of `agent_names`) draws that one agent's box with
    a double-line border so it reads as "highlighted" even when it's
    already bold-red for a failed leg, where weight alone wouldn't show a
    difference.
    """
    if not legs:
        canvas = Canvas(max(len(hub_name) + 6, 28), 6)
        w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
        canvas.draw_box((28 - w) // 2, 1, hub_name)
        canvas.text_over(2, 5, "Waiting for dispatch to sub-agents…", MUTED)
        return canvas.to_text()

    layout = _compute_hub_layout(legs, hub_name, agent_names)
    canvas = Canvas(layout.total_w, CANVAS_HEIGHT)
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
        canvas.text_over(layout.agent_centers[i], STATUS_ROW, glyph, glyph_style)

    _draw_hub_arrows(canvas, layout.hub_cx, layout.agent_centers, leg_states, pulse=pulse)

    return canvas.to_text()


@dataclass
class PeerLayout:
    """Geometry for one peer topology render -- see `HubLayout`."""

    total_w: int
    left_box: BoxRegion
    right_box: BoxRegion
    left_cx: int
    right_cx: int


def _compute_peer_layout(left_name: str, right_name: str) -> PeerLayout:
    left_w = max(len(left_name) + 2, BOX_MIN_WIDTH)
    right_w = max(len(right_name) + 2, BOX_MIN_WIDTH)
    gap = 24
    total_w = left_w + gap + right_w + 4

    lx = (total_w - left_w - gap - right_w) // 2
    rx = lx + left_w + gap
    left_box_w = left_w + 2
    right_box_w = right_w + 2

    return PeerLayout(
        total_w=total_w,
        left_box=(lx, 4, lx + left_box_w, 4 + 3),
        right_box=(rx, 4, rx + right_box_w, 4 + 3),
        left_cx=lx + left_box_w // 2,
        right_cx=rx + right_box_w // 2,
    )


def build_peer_hit_regions(left_name: str, right_name: str) -> tuple[BoxRegion, BoxRegion]:
    """(left_box, right_box) regions -- for the live TUI to hit-test a click
    against. Mirrors `build_hub_hit_regions` for the two-agent case.
    """
    layout = _compute_peer_layout(left_name, right_name)
    return layout.left_box, layout.right_box


def build_peer_topology(
    left_name: str,
    right_name: str,
    *,
    state: str = "completed",
    pulse: bool = False,
    selected: str | None = None,
) -> Text:
    """Two boxed agents with a single outbound arrow — details in the table."""
    style = _line_style(state, pulse)
    box_style = _box_style(state)
    layout = _compute_peer_layout(left_name, right_name)
    canvas = Canvas(layout.total_w, 11)

    lx, ly, _, _ = layout.left_box
    rx, ry, _, _ = layout.right_box
    left_style = _ensure_bold(box_style) if selected == left_name else box_style
    right_style = _ensure_bold(box_style) if selected == right_name else box_style
    canvas.draw_box(lx, ly, left_name, style=left_style, double=selected == left_name)
    canvas.draw_box(rx, ry, right_name, style=right_style, double=selected == right_name)

    canvas.hline(layout.left_cx, layout.right_cx - 1, 2, style)
    canvas.text_over(layout.right_cx, 2, "▶", style)

    glyph, glyph_style = _status_glyph(state, pulse)
    canvas.text_over(layout.left_cx, 8, glyph, glyph_style)
    canvas.text_over(layout.right_cx, 8, glyph, glyph_style)

    return canvas.to_text()


def _centered_line(line: Text, width: int) -> Text:
    pad = max(0, (width - len(line.plain)) // 2)
    result = Text(" " * pad)
    result.append_text(line)
    return result


def assemble_centered_diagram(
    topology: Text,
    table: Text,
    legend: Text,
    table_legend: Text | None = None,
) -> Text:
    """Stack topology, table, and legend(s) centered as one block.

    Centers by padding rather than restyling to `.plain`, so a
    multi-colored legend (e.g. per-status dots) keeps its per-run styles
    instead of collapsing to a single flat color.
    """
    width = block_width(topology)
    width = max(width, block_width(table), len(legend.plain))
    if table_legend is not None:
        width = max(width, len(table_legend.plain))

    result = Text()
    result.append_text(_center_plain_block(topology, width))
    result.append("\n\n")
    result.append_text(_center_plain_block(table, width))
    result.append("\n")
    if table_legend is not None:
        result.append_text(_centered_line(table_legend, width))
        result.append("\n")
    result.append_text(_centered_line(legend, width))
    return result


def center_in_width(block: Text, width: int) -> Text:
    """Center a multi-line block within a given character width."""
    lines = list(block.split("\n"))
    content_width = max((len(line.plain) for line in lines if line.plain.strip()), default=0)
    left_pad = max(0, (width - content_width) // 2)
    result = Text()
    for i, line in enumerate(lines):
        if i:
            result.append("\n")
        if not line.plain.strip():
            continue
        result.append(" " * left_pad)
        result.append_text(line)
    return result


def _center_plain_block(block: Text, width: int) -> Text:
    return center_in_width(block, width)


# Back-compat aliases
build_hub_network = build_hub_topology
build_peer_network = build_peer_topology
