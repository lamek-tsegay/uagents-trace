"""Interactive live-updating trace viewer.

    uagents-trace tui

Polls the same SQLite file the CLI and web UI read (no writes), so new
traces appear at the top within ~1s of being recorded. Arrow keys move
between rows; Enter on a trace row expands it inline into the per-span
waterfall `show` produces; q quits.
"""

from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header

from .cli import display_name, relative_time
from .store import get_alias_map, get_trace_spans, list_traces

POLL_SECONDS = 1.0
BAR_WIDTH = 28

STATE_COLOR = {
    "delivered": "green",
    "timeout": "yellow",
    "dropped": "red",
    "pending": "grey58",
}


def _trace_row_style(trace: dict[str, Any]) -> str:
    return "red" if trace["has_failure"] else "green"


class TraceApp(App):
    """`uagents-trace tui` -- live trace table with inline waterfall expansion."""

    CSS = """
    DataTable { height: 1fr; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.expanded: set[str] = set()
        self.span_cache: dict[str, list[dict[str, Any]]] = {}
        self.alias_map: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        table = DataTable(cursor_type="row", zebra_stripes=False)
        table.add_column("Time", width=10)
        table.add_column("From -> To", width=70)
        table.add_column("Messages", width=20)
        table.add_column("Spans", width=6)
        table.add_column("Status", width=12)
        table.add_column("Trace", width=10)
        yield table
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "uagents-trace"
        self.sub_title = "↑/↓ move   enter expand/collapse   q quit"
        await self.refresh_data()
        self.set_interval(POLL_SECONDS, self.refresh_data)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None or ":" in key:
            return  # a span sub-row, not a trace row -- nothing to toggle
        if key in self.expanded:
            self.expanded.discard(key)
        else:
            self.expanded.add(key)
            self.span_cache[key] = await get_trace_spans(self.db_path, key)
        await self.refresh_data()

    async def refresh_data(self) -> None:
        table = self.query_one(DataTable)

        cursor_key: Optional[str] = None
        if table.row_count:
            try:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                cursor_key = row_key.value
            except Exception:
                cursor_key = None

        traces = await list_traces(self.db_path)
        self.alias_map = await get_alias_map(self.db_path)

        live_trace_ids = {t["trace_id"] for t in traces}
        self.expanded &= live_trace_ids
        for trace_id in self.expanded:
            self.span_cache[trace_id] = await get_trace_spans(self.db_path, trace_id)

        table.clear()
        for t in traces:
            self._add_trace_row(table, t)
            if t["trace_id"] in self.expanded:
                self._add_span_rows(table, t["trace_id"])

        if cursor_key is not None:
            try:
                table.move_cursor(row=table.get_row_index(cursor_key))
            except Exception:
                pass

    def _add_trace_row(self, table: DataTable, t: dict[str, Any]) -> None:
        style = _trace_row_style(t)
        marker = "▾" if t["trace_id"] in self.expanded else "▸"
        participants = " -> ".join(display_name(a, self.alias_map) for a in t["participants"])
        status = "✗ FAILURE" if t["has_failure"] else "✓ OK"
        table.add_row(
            Text(relative_time(t["started_at"]), style=style),
            Text(f"{marker} {participants}", style=style),
            Text(", ".join(t["payload_types"]), style=style),
            Text(str(t["span_count"]), style=style, justify="right"),
            Text(status, style=f"bold {style}"),
            Text(t["trace_id"][:8], style=style),
            key=t["trace_id"],
        )

    def _add_span_rows(self, table: DataTable, trace_id: str) -> None:
        spans = self.span_cache.get(trace_id) or []
        if not spans:
            table.add_row(Text(""), Text("  (no spans)", style="dim"), Text(""), Text(""), Text(""), Text(""), key=f"{trace_id}:empty")
            return

        start = min(s["enqueued_at"] for s in spans)
        end = max((s["acked_at"] or s["enqueued_at"]) for s in spans)
        span_ms = max(end - start, 1)

        for s in spans:
            latency_ms = (s["acked_at"] - s["enqueued_at"]) if s["acked_at"] is not None else None
            left = int(((s["enqueued_at"] - start) / span_ms) * BAR_WIDTH)
            width = max(1, int((latency_ms / span_ms) * BAR_WIDTH)) if latency_ms is not None else 2
            color = STATE_COLOR.get(s["state"], "white")
            bar = Text(" " * left + "█" * width, style=color)

            src = display_name(s["source_agent"], self.alias_map)
            dst = display_name(s["dest_agent"], self.alias_map)
            label = Text(f"    {src} -> {dst}  {s['payload_type']}", style=color)
            latency_label = Text(f"{latency_ms} ms" if latency_ms is not None else "pending…", style=color)

            table.add_row(
                Text(""),
                label,
                bar,
                Text(""),
                Text(s["state"], style=color),
                latency_label,
                key=f"{trace_id}:{s['id']}",
            )

            if s["state"] in ("dropped", "timeout"):
                reason = s["error"] or "(no error message)"
                table.add_row(
                    Text(""),
                    Text(f"      ⚠ {s['state'].upper()}: {reason}", style=f"bold {color}"),
                    Text(""),
                    Text(""),
                    Text(""),
                    Text(""),
                    key=f"{trace_id}:{s['id']}:reason",
                )


def main() -> None:
    import asyncio
    import sys

    from .store import default_db_path

    db_path = sys.argv[1] if len(sys.argv) > 1 else default_db_path()
    asyncio.run(TraceApp(db_path).run_async())


if __name__ == "__main__":
    main()
