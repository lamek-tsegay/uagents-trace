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

    def draw_box(self, x: int, y: int, label: str, style: str | None = None) -> tuple[int, int]:
        """Draw a 3-line box; return (width, center_x)."""
        w = max(len(label) + 2, BOX_MIN_WIDTH)
        box_style = style or f"bold {ACCENT}"
        self.text_over(x, y, "┌" + "─" * w + "┐", box_style)
        self.text_over(x, y + 1, "│" + label.center(w) + "│", box_style)
        self.text_over(x, y + 2, "└" + "─" * w + "┘", box_style)
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


def _block_width(text: Text) -> int:
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


def build_hub_topology(
    legs: list[dict[str, Any]],
    hub_name: str,
    agent_names: list[str],
    *,
    pulse: bool = False,
) -> Text:
    """Star topology: boxed hub centered above sub-agents, one arrow each."""
    if not legs:
        canvas = Canvas(max(len(hub_name) + 6, 28), 6)
        w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
        canvas.draw_box((28 - w) // 2, 1, hub_name)
        canvas.text_over(2, 5, "Waiting for dispatch to sub-agents…", MUTED)
        return canvas.to_text()

    n = len(legs)
    box_widths = [max(len(name) + 2, BOX_MIN_WIDTH) for name in agent_names]
    inner_w = max(len(hub_name) + 2, BOX_MIN_WIDTH)
    columns, agents_w = _agent_columns(n, box_widths)
    total_w = max(agents_w, inner_w + 2 + 8)

    hub_x = (total_w - (inner_w + 2)) // 2
    canvas = Canvas(total_w, CANVAS_HEIGHT)
    _, hub_cx = canvas.draw_box(hub_x, HUB_ROW, hub_name)
    subtitle = "orchestrator"
    canvas.text_over(hub_cx - len(subtitle) // 2, SUBTITLE_ROW, subtitle, MUTED)

    agent_centers: list[int] = []
    leg_states: list[str] = []

    for i, leg in enumerate(legs):
        x0, agent_cx = columns[i]
        offset = (total_w - agents_w) // 2
        x0 += offset
        agent_cx += offset
        canvas.draw_box(x0, AGENT_ROW, agent_names[i])
        agent_centers.append(agent_cx)
        leg_states.append(leg.get("state", "pending"))
        glyph, glyph_style = _status_glyph(leg.get("state", "pending"), pulse)
        canvas.text_over(agent_cx, STATUS_ROW, glyph, glyph_style)

    _draw_hub_arrows(canvas, hub_cx, agent_centers, leg_states, pulse=pulse)

    return canvas.to_text()


def build_peer_topology(
    left_name: str,
    right_name: str,
    *,
    state: str = "completed",
    pulse: bool = False,
) -> Text:
    """Two boxed agents with a single outbound arrow — details in the table."""
    style = _line_style(state, pulse)
    left_w = max(len(left_name) + 2, BOX_MIN_WIDTH)
    right_w = max(len(right_name) + 2, BOX_MIN_WIDTH)
    gap = 24
    total_w = left_w + gap + right_w + 4
    canvas = Canvas(total_w, 11)

    lx = (total_w - left_w - gap - right_w) // 2
    rx = lx + left_w + gap
    _, left_cx = canvas.draw_box(lx, 4, left_name)
    _, right_cx = canvas.draw_box(rx, 4, right_name)

    canvas.hline(left_cx, right_cx - 1, 2, style)
    canvas.text_over(right_cx, 2, "▶", style)

    glyph, glyph_style = _status_glyph(state, pulse)
    canvas.text_over(left_cx, 8, glyph, glyph_style)
    canvas.text_over(right_cx, 8, glyph, glyph_style)

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
    width = _block_width(topology)
    width = max(width, _block_width(table), len(legend.plain))
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
