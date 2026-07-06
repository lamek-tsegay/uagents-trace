"""Character-grid canvas for hub-and-spoke network diagrams in the terminal."""

from __future__ import annotations

from typing import Any

from rich.text import Text

ACCENT = "#34d399"
MUTED = "#6b7280"
SUCCESS = "#4ade80"
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
    return Text("outbound · return · pending", style=MUTED)
