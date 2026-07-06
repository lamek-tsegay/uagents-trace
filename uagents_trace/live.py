"""Live diagram + rolling message feed for uagents-trace.

Opened after the setup wizard. Polls SQLite and shows agent-to-agent
messages as they happen — one active trace at a time, bounded feed.
"""

from collections import deque
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from .cli import display_name
from .network_canvas import (
    ERROR,
    SUCCESS,
    WARN,
    assemble_centered_diagram,
    build_diagram_legend,
    build_hub_topology,
    build_peer_topology,
    center_in_width,
    format_ms,
)
from .shape import HUB, TreeNode, build_hub_legs, build_interaction_tree, classify_trace_shape
from .store import get_alias_map, get_recent_spans, get_trace_spans, list_traces, save_watch_config
from .wizard import WatchSetup, ViewMode

POLL_SECONDS = 1.0
MAX_EVENTS = 15
MAX_TRACE_LIST = 25
TRACE_WIDGET_PREFIX = "trace-"


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


def build_hub_leg_table(legs: list[dict[str, Any]], agent_names: list[str]) -> Text:
    """Fixed-column summary of per-agent latencies and status."""
    col_agent, col_out, col_in, col_total, col_status = 12, 8, 8, 8, 10
    header = (
        f"{'Agent':<{col_agent}}"
        f"{'Out':>{col_out}}"
        f"{'In':>{col_in}}"
        f"{'Total':>{col_total}}"
        f"{'Status':>{col_status}}"
    )
    table = Text(header + "\n", style=MUTED)
    for leg, name in zip(legs, agent_names):
        state = leg.get("state", "pending")
        out_ms = format_ms(leg.get("dispatch_ms"))
        in_ms = format_ms(leg.get("reply_ms")) if state == "completed" else "…"
        total_ms = format_ms(leg.get("latency_ms")) if state == "completed" else "…"
        if state == "completed":
            status = "✓ done"
            row_style = SUCCESS
        elif state == "failed":
            status = "✗ failed"
            row_style = ERROR
        else:
            status = "⋯ waiting"
            row_style = WARN
        row = (
            f"{name:<{col_agent}}"
            f"{out_ms:>{col_out}}"
            f"{in_ms:>{col_in}}"
            f"{total_ms:>{col_total}}"
            f"{status:>{col_status}}"
        )
        table.append(row + "\n", style=row_style)
    return table


def build_peer_leg_table(
    left_name: str,
    right_name: str,
    *,
    message_ms: int | None,
    reply_ms: int | None,
    state: str,
) -> Text:
    """Two-row table for peer round-trip."""
    col_route, col_dir, col_ms, col_status = 20, 6, 8, 10
    header = (
        f"{'Route':<{col_route}}"
        f"{'Dir':>{col_dir}}"
        f"{'Time':>{col_ms}}"
        f"{'Status':>{col_status}}"
    )
    table = Text(header + "\n", style=MUTED)
    route_out = f"{left_name} → {right_name}"
    route_in = f"{right_name} → {left_name}"

    out_status = "✓ done" if state != "failed" else "✗ failed"
    out_style = ERROR if state == "failed" else (WARN if state == "pending" else SUCCESS)
    row_out = (
        f"{route_out:<{col_route}}"
        f"{'out':>{col_dir}}"
        f"{format_ms(message_ms):>{col_ms}}"
        f"{out_status:>{col_status}}"
    )
    table.append(row_out + "\n", style=out_style)

    if reply_ms is not None:
        row_in = (
            f"{route_in:<{col_route}}"
            f"{'in':>{col_dir}}"
            f"{format_ms(reply_ms):>{col_ms}}"
            f"{'✓ done':>{col_status}}"
        )
        table.append(row_in + "\n", style=SUCCESS)
    elif state == "pending":
        row_in = (
            f"{route_in:<{col_route}}"
            f"{'in':>{col_dir}}"
            f"{'…':>{col_ms}}"
            f"{'⋯ waiting':>{col_status}}"
        )
        table.append(row_in + "\n", style=WARN)

    return table


MUTED = "#6b7280"


def build_hub_detail_summary(
    hub_name: str,
    legs: list[dict[str, Any]],
    agent_names: list[str],
    trace_id: str,
) -> str:
    n = len(legs)
    complete = sum(1 for leg in legs if leg.get("state") == "completed")
    failed = sum(1 for leg in legs if leg.get("state") == "failed")
    names = ", ".join(agent_names)
    latencies = [leg["latency_ms"] for leg in legs if leg.get("latency_ms") is not None]
    parts = [f"{hub_name} dispatched to {names} · {complete}/{n} complete"]
    if failed:
        parts.append(f"{failed} failed")
    if latencies:
        parts.append(f"round-trip {format_ms(max(latencies))} max")
    parts.append(f"trace {trace_id[:8]}")
    return "  ·  ".join(parts)



def build_hub_network_diagram(
    spans: list[dict[str, Any]],
    hub: str,
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
) -> Text:
    """Hub topology + leg summary table."""
    legs = build_hub_legs(spans, hub)
    orch_name = display_name(hub, alias_map)
    agent_names = [display_name(leg["subagent"], alias_map) for leg in legs]
    topology = build_hub_topology(legs, orch_name, agent_names, pulse=pulse)
    if not legs:
        return topology
    table = build_hub_leg_table(legs, agent_names)
    legend = build_diagram_legend()
    return assemble_centered_diagram(topology, table, legend)


def _latest_peer_round_trip(
    sends: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Most recent Message send and matching Reply (if any)."""
    if not sends:
        return None, None
    if len(sends) >= 2:
        prev, latest = sends[-2], sends[-1]
        if (
            prev["source_agent"] == latest["dest_agent"]
            and prev["dest_agent"] == latest["source_agent"]
            and message_label(prev) == "Message"
            and message_label(latest) == "Reply"
        ):
            return prev, latest
    return sends[-1], None


def build_peer_network_diagram(
    sends: list[dict[str, Any]],
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
) -> Text:
    """Two-agent bidirectional network view for the latest hop pair."""
    if not sends:
        return Text(
            "  Waiting for messages…\n\n"
            "  Start your instrumented agents\n"
            "  in another terminal.",
            style="dim",
        )

    outbound, reply = _latest_peer_round_trip(sends)
    if outbound is None:
        return Text("  Waiting for messages…", style="dim")

    left = display_name(outbound["source_agent"], alias_map)
    right = display_name(outbound["dest_agent"], alias_map)
    state = outbound.get("state", "pending")
    leg_state = "completed" if state == "delivered" and reply else ("failed" if state in ("dropped", "timeout") else "pending")

    def _lat(span: dict[str, Any] | None) -> int | None:
        if not span or span.get("acked_at") is None:
            return None
        return max(span["acked_at"] - span["enqueued_at"], 0)

    diagram = build_peer_topology(
        left,
        right,
        state=leg_state,
        pulse=pulse,
    )
    table = build_peer_leg_table(
        left,
        right,
        message_ms=_lat(outbound),
        reply_ms=_lat(reply) if reply else None,
        state=leg_state,
    )
    legend = build_diagram_legend()
    return assemble_centered_diagram(diagram, table, legend)


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


def _node_status_label(node: TreeNode) -> str:
    if node.state == "completed":
        lat = _format_ms(node.latency_ms)
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


def _view_label(view_mode: ViewMode) -> str:
    return "tree" if view_mode == "tree" else "network"


def _sub_title_for(setup: WatchSetup, view_mode: ViewMode, *, follow: bool) -> str:
    names = ", ".join(setup.names.values()) if setup.names else "all agents"
    follow_hint = "follow" if follow else "pinned"
    return f"{names}  ·  {_view_label(view_mode)}  ·  {follow_hint}  ·  v view  f follow  q quit"



    names = ", ".join(setup.names.values()) if setup.names else "all agents"
    follow_hint = "follow" if follow else "pinned"
    return f"{names}  ·  {_view_label(view_mode)}  ·  {follow_hint}  ·  v view  f follow  q quit"



    names = ", ".join(setup.names.values()) if setup.names else "all agents"
    return f"watching {names}  ·  view: {view_mode}  ·  v toggle  ·  q quit"


def _trace_matches_watch(trace: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return any(a in addresses for a in trace["participants"])


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

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("v", "cycle_view", "View"),
    ]

    def __init__(self, setup: WatchSetup):
        super().__init__()
        self.setup = setup
        self.db_path = setup.db_path
        self.addresses = setup.addresses if setup.filter_only else None
        self.view_mode: ViewMode = setup.view_mode
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
        self.sub_title = _sub_title_for(self.setup, self.view_mode)
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

    async def action_cycle_view(self) -> None:
        self.view_mode = "tree" if self.view_mode == "linear" else "linear"
        self.sub_title = _sub_title_for(self.setup, self.view_mode)
        await save_watch_config(
            self.db_path,
            list(self.setup.addresses),
            self.setup.filter_only,
            self.setup.orchestrator,
            view_mode=self.view_mode,
        )
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
                if self.view_mode == "tree":
                    tree = build_interaction_tree(spans, hub_addr)
                    renderable = build_hub_tree_diagram(tree, self._alias_map)
                else:
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
