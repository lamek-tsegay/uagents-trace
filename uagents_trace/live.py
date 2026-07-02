"""Live diagram + rolling message feed for uagents-trace.

Opened after the setup wizard. Polls SQLite and shows agent-to-agent
messages as they happen — one active trace at a time, bounded feed.
"""

from collections import deque
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, RichLog, Static

from .cli import display_name
from .shape import HUB, build_hub_legs, classify_trace_shape
from .store import get_alias_map, get_recent_spans, get_trace_spans
from .wizard import WatchSetup

POLL_SECONDS = 1.0
MAX_EVENTS = 15

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
    "delivered": "green",
    "timeout": "yellow",
    "dropped": "red",
    "pending": "grey58",
}

STATE_ICON = {
    "delivered": "✓",
    "timeout": "⏱",
    "dropped": "✗",
    "pending": "…",
}


def message_label(span: dict[str, Any]) -> str:
    """Semantic label: Message for outbound, Reply for responses."""
    ptype = span.get("payload_type") or ""
    if ptype in REPLY_PAYLOAD_TYPES:
        return "Reply"
    if ptype.endswith("Reply") or ptype.endswith("Acknowledgement"):
        return "Reply"
    return "Message"


def _message_text(span: dict[str, Any]) -> str:
    summary = span.get("payload_summary")
    if summary:
        return summary
    detail = span.get("detail")
    if detail:
        return detail
    return span.get("payload_type") or ""


def _format_payload(span: dict[str, Any]) -> str:
    label = message_label(span)
    body = _message_text(span)
    if body:
        return f'{label}: "{body}"'
    return label


def format_latency(span: dict[str, Any]) -> str:
    ack = span.get("acked_at")
    enq = span.get("enqueued_at")
    if ack is None or enq is None:
        return "…"
    ms = max(ack - enq, 0)
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms}ms"


def _styled_icon(state: str) -> Text:
    icon = STATE_ICON.get(state, "·")
    style = STATE_STYLE.get(state, "white")
    return Text(icon, style=f"bold {style}")


def format_event_line(span: dict[str, Any], alias_map: dict[str, str]) -> Text:
    """One send hop: [Alice] → [Bob]  Message: \"Hi Bob!\"  (12ms)"""
    src = display_name(span["source_agent"], alias_map)
    dst = display_name(span["dest_agent"], alias_map)
    payload = _format_payload(span)
    latency = format_latency(span)
    state = span.get("state") or "pending"

    parts: list[Any] = [
        _styled_icon(state),
        f" [{src}] → [{dst}]  {payload}  ({latency})",
    ]
    if state in ("dropped", "timeout") and span.get("error"):
        parts.append(f"  — {span['error']}")

    line = Text.assemble(*parts)
    line.stylize(STATE_STYLE.get(state, "white"))
    return line


def render_agent_box(label: str, width: int | None = None) -> list[str]:
    content = label
    w = width or max(len(content) + 2, 10)
    inner = content.center(w)
    return [
        "┌" + "─" * w + "┐",
        "│" + inner + "│",
        "└" + "─" * w + "┘",
    ]


def _hstack_blocks(blocks: list[list[str]], gap: str = "  ") -> list[str]:
    if not blocks:
        return []
    height = max(len(b) for b in blocks)
    widths = [max(len(line) for line in b) for b in blocks]
    padded = []
    for block, w in zip(blocks, widths):
        rows = block + [" " * w] * (height - len(block))
        padded.append([line.ljust(w) for line in rows])
    merged: list[str] = []
    for row_idx in range(height):
        parts = [padded[i][row_idx] for i in range(len(blocks))]
        merged.append(gap.join(parts))
    return merged


def _hop_arrow_block(icon: str, latency: str) -> list[str]:
    mid = f"{icon} {latency}".strip()
    return ["", f"─ {mid} ─▶", ""]


def build_peer_diagram(
    sends: list[dict[str, Any]],
    alias_map: dict[str, str],
) -> Text:
    """Horizontal hop rows stacked vertically — scroll for full trace history."""
    if not sends:
        return Text(
            "  Waiting for messages…\n\n"
            "  Start your instrumented agents\n"
            "  in another terminal.",
            style="dim",
        )

    diagram = Text()
    pad = "  "

    for i, span in enumerate(sends):
        if i > 0:
            diagram.append("\n\n")

        src = display_name(span["source_agent"], alias_map)
        dst = display_name(span["dest_agent"], alias_map)
        body = _message_text(span)
        label = message_label(span)
        latency = format_latency(span)
        state = span.get("state", "pending")
        icon = STATE_ICON.get(state, "·")

        sender_label = f'{src}: "{body}"' if body else f"{src} ({label})"
        src_box = render_agent_box(sender_label)
        arrow = _hop_arrow_block(icon, latency)
        dst_box = render_agent_box(dst)

        row_lines = _hstack_blocks([src_box, arrow, dst_box])
        for line in row_lines:
            diagram.append(pad)
            if icon in line and state in STATE_STYLE:
                before, _, after = line.partition(icon)
                diagram.append(before)
                diagram.append_text(_styled_icon(state))
                diagram.append(after)
            else:
                diagram.append(line, style=STATE_STYLE.get(state, "white"))
            diagram.append("\n")

    if diagram.plain.endswith("\n"):
        diagram.plain = diagram.plain.rstrip("\n")

    return diagram


def _format_ms(ms: int | None) -> str:
    if ms is None:
        return "…"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms}ms"


def build_hub_diagram(
    spans: list[dict[str, Any]],
    hub: str,
    alias_map: dict[str, str],
) -> Text:
    """Orchestrator fan-out: dispatch to sub-agents, success returns on the right."""
    orch_name = display_name(hub, alias_map)
    legs = build_hub_legs(spans, hub)
    pad = "  "
    diagram = Text()

    if not legs:
        for line in render_agent_box(orch_name):
            diagram.append(pad + line + "\n")
        diagram.append(pad + "  Waiting for dispatch to sub-agents…", style="dim")
        return diagram

    for i, leg in enumerate(legs):
        if i > 0:
            diagram.append("\n\n")

        sub = display_name(leg["subagent"], alias_map)
        state = leg.get("state", "pending")
        orch_box = render_agent_box(orch_name)
        sub_box = render_agent_box(sub)

        dispatch_lat = _format_ms(leg.get("dispatch_ms"))
        if state == "failed":
            dispatch_state = "dropped"
            dispatch_icon = STATE_ICON["dropped"]
        elif state == "completed":
            dispatch_state = "delivered"
            dispatch_icon = STATE_ICON["delivered"]
        else:
            dispatch_state = "pending"
            dispatch_icon = STATE_ICON["pending"]

        dispatch_arrow = _hop_arrow_block(dispatch_icon, dispatch_lat)

        if state == "completed":
            reply_lat = _format_ms(leg.get("reply_ms"))
            reply_arrow = _hop_arrow_block(STATE_ICON["delivered"], f"success {reply_lat}")
            return_box = render_agent_box(orch_name)
            row_lines = _hstack_blocks([orch_box, dispatch_arrow, sub_box, reply_arrow, return_box])
            row_style = "green"
        elif state == "failed":
            row_lines = _hstack_blocks([orch_box, dispatch_arrow, sub_box])
            row_style = "red"
        else:
            row_lines = _hstack_blocks([orch_box, dispatch_arrow, sub_box])
            row_style = "grey58"

        for line in row_lines:
            diagram.append(pad)
            if dispatch_icon in line and state != "pending":
                before, _, after = line.partition(dispatch_icon)
                diagram.append(before)
                diagram.append_text(_styled_icon(dispatch_state))
                diagram.append(after, style=row_style)
            elif state == "completed" and STATE_ICON["delivered"] in line and "success" in line:
                before, _, after = line.partition(STATE_ICON["delivered"])
                diagram.append(before)
                diagram.append_text(_styled_icon("delivered"))
                diagram.append(after, style="green")
            else:
                diagram.append(line, style=row_style)
            diagram.append("\n")

        if state == "failed" and leg.get("reason"):
            diagram.append(pad + f"  ✗ {leg['reason']}\n", style="red")

    if diagram.plain.endswith("\n"):
        diagram.plain = diagram.plain.rstrip("\n")

    return diagram


def _span_in_watch(span: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return span["source_agent"] in addresses or span["dest_agent"] in addresses


def _is_send_event(span: dict[str, Any]) -> bool:
    return span.get("direction") == "send" or span.get("direction") is None


class LiveApp(App):
    """Live architecture diagram + rolling message feed for one trace."""

    CSS = """
    #diagram-scroll {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }
    #diagram-content {
        width: 100%;
        padding: 1 1;
    }
    #events-panel {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    Vertical {
        height: 100%;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, setup: WatchSetup):
        super().__init__()
        self.setup = setup
        self.db_path = setup.db_path
        self.addresses = setup.addresses if setup.filter_only else None
        self._span_states: dict[str, str] = {}
        self._events: deque[Text] = deque(maxlen=MAX_EVENTS)
        self._active_trace_id: str | None = None
        self._alias_map: dict[str, str] = {}
        self._bootstrapped = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with VerticalScroll(id="diagram-scroll"):
                yield Static("", id="diagram-content")
            yield RichLog(id="events-panel", highlight=False, markup=False, auto_scroll=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "uagents-trace live"
        names = ", ".join(self.setup.names.values()) if self.setup.names else "all agents"
        self.sub_title = f"watching {names}  ·  updates every 1s  ·  q quit"
        events_log = self.query_one("#events-panel", RichLog)
        events_log.write(Text("  Live messages will appear here…", style="dim"))
        await self._bootstrap()
        self.set_interval(POLL_SECONDS, self._poll)

    async def _bootstrap(self) -> None:
        self._alias_map = await get_alias_map(self.db_path)
        spans = await get_recent_spans(self.db_path, limit=200, addresses=self.addresses)
        for span in spans:
            self._span_states[span["id"]] = span["state"]
        if spans:
            self._active_trace_id = spans[-1]["trace_id"]
        self._bootstrapped = True
        await self._refresh_display()

    def _switch_trace(self, trace_id: str) -> None:
        if self._active_trace_id == trace_id:
            return
        self._active_trace_id = trace_id
        self._events.clear()
        events_log = self.query_one("#events-panel", RichLog)
        events_log.clear()
        events_log.write(Text("  New trace — live messages:", style="bold"))

    async def _poll(self) -> None:
        if not self._bootstrapped:
            return
        self._alias_map = await get_alias_map(self.db_path)
        spans = await get_recent_spans(self.db_path, limit=100, addresses=self.addresses)
        events_log = self.query_one("#events-panel", RichLog)
        new_events = False

        for span in spans:
            prev = self._span_states.get(span["id"])
            current = span["state"]
            if prev == current:
                continue
            self._span_states[span["id"]] = current

            if current not in ("delivered", "dropped", "timeout"):
                continue
            if not _is_send_event(span):
                continue
            if not _span_in_watch(span, self.addresses if self.setup.filter_only else None):
                continue

            self._switch_trace(span["trace_id"])
            if self._active_trace_id == span["trace_id"]:
                line = format_event_line(span, self._alias_map)
                self._events.append(line)
                events_log.write(line)
                new_events = True

        if new_events or self._active_trace_id:
            await self._refresh_display()

    async def _refresh_display(self) -> None:
        content = self.query_one("#diagram-content", Static)
        scroll = self.query_one("#diagram-scroll", VerticalScroll)

        if not self._active_trace_id:
            content.update(
                Text(
                    "  Waiting for messages…\n\n"
                    "  Start your instrumented agents\n"
                    "  in another terminal.",
                    style="dim",
                )
            )
            scroll.scroll_end(animate=False)
            return

        spans = await get_trace_spans(self.db_path, self._active_trace_id)
        if self.addresses:
            spans = [s for s in spans if _span_in_watch(s, self.addresses)]

        sends = [
            s
            for s in spans
            if s.get("direction") == "send" and s.get("state") in ("delivered", "dropped", "timeout")
        ]

        if sends:
            shape, hub = classify_trace_shape(spans)
            hub_addr = self.setup.orchestrator or hub
            use_hub = shape == HUB and hub_addr
            if not use_hub and self.setup.orchestrator and len(self.setup.addresses) >= 3:
                use_hub = True
                hub_addr = self.setup.orchestrator
            if use_hub and hub_addr:
                renderable = build_hub_diagram(spans, hub_addr, self._alias_map)
            else:
                renderable = build_peer_diagram(sends, self._alias_map)
        else:
            renderable = Text("  Waiting for messages in this trace…", style="dim")

        content.update(renderable)
        scroll.scroll_end(animate=False)


async def run_live(setup: WatchSetup) -> None:
    app = LiveApp(setup)
    await app.run_async()
