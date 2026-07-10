"""Live diagram + rolling message feed for uagents-trace.

Opened after the setup wizard. Polls SQLite and shows agent-to-agent
messages as they happen — one active trace at a time, bounded feed.

Every widget here (diagram, table, inspector, feed, sidebar) renders from
a single `shape.TraceState` computed once per refresh (see
`shape.build_trace_state`) rather than each recomputing status/latency
from raw spans on its own -- that's what keeps them from disagreeing about
what happened to a given trace.
"""

from collections import deque
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from .brand import BRAND_PANEL_WIDTH, FETCH_BRAND
from .cli import display_name
from .network_canvas import (
    ACCENT,
    ERROR,
    SUCCESS,
    WARN,
    assemble_centered_diagram,
    block_width,
    build_diagram_legend,
    build_hub_hit_regions,
    build_hub_topology,
    build_peer_hit_regions,
    build_peer_topology,
    build_table_legend,
    center_in_width,
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
MAX_EVENTS = 15
MAX_TRACE_LIST = 25
TRACE_WIDGET_PREFIX = "trace-"

MUTED = "#6b7280"

# Inspector column -- third, fixed-width column right of the diagram. Below
# MIN_WIDTH_FOR_INSPECTOR it hides itself entirely rather than shrink, since
# the diagram (a 4-subagent hub trace renders ~100 cols wide, see
# network_canvas.build_hub_topology) must stay legible first. 228 = sidebar
# (~48 incl. border) + a diagram column wide enough for that hub layout
# without cramming (~106) + the inspector column (BRAND_PANEL_WIDTH=76 incl.
# border).
MIN_WIDTH_FOR_INSPECTOR = 228

# Textual CSS can't interpolate a Python constant into #inspector-col's
# `width:` (braces in an f-string would collide with CSS block syntax), so
# that value is hardcoded there -- this just catches the two drifting apart.
assert BRAND_PANEL_WIDTH == 76, "update #inspector-col's CSS width alongside brand.BRAND_PANEL_WIDTH"


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
    """Fixed-column summary of per-agent latencies and status.

    A failed leg's `dispatch_ms` is time-to-failure (how long uAgents took
    to give up trying to deliver), not an outbound ack latency -- showing
    it under "Out" reads as "the send was slow" when the send never
    actually landed. Failed legs show "—" for Out/In/Total instead, with
    the failure duration folded into the Status cell next to the ✗.
    """
    col_agent, col_out, col_in, col_total, col_status = 12, 8, 8, 8, 17
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
        if state == "failed":
            out_ms = "—"
            in_ms = "—"
            total_ms = "—"
            status = f"✗ failed {format_ms(leg.get('dispatch_ms'))}"
            row_style = f"bold {ERROR}"
        elif state == "completed":
            out_ms = format_ms(leg.get("dispatch_ms"))
            in_ms = format_ms(leg.get("reply_ms"))
            total_ms = format_ms(leg.get("latency_ms"))
            status = "✓ done"
            row_style = SUCCESS
        else:
            out_ms = format_ms(leg.get("dispatch_ms"))
            in_ms = "…"
            total_ms = "…"
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
    out_style = f"bold {ERROR}" if state == "failed" else (WARN if state == "pending" else SUCCESS)
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
    state: TraceState,
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
) -> Text:
    """Hub topology + leg summary table, driven entirely by `state.legs`."""
    legs = state.legs
    orch_name = display_name(state.hub, alias_map)
    agent_names = [display_name(leg["subagent"], alias_map) for leg in legs]
    topology = build_hub_topology(legs, orch_name, agent_names, pulse=pulse)
    if not legs:
        return topology
    table = build_hub_leg_table(legs, agent_names)
    legend = build_diagram_legend()
    return assemble_centered_diagram(topology, table, legend, table_legend=build_table_legend())


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


def build_peer_network_diagram(
    hops: list[Hop],
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
) -> Text:
    """Two-agent bidirectional network view for the latest hop pair."""
    if not hops:
        return Text(
            "  Waiting for messages…\n\n"
            "  Start your instrumented agents\n"
            "  in another terminal.",
            style="dim",
        )

    outbound, reply = _latest_peer_round_trip(hops)
    if outbound is None:
        return Text("  Waiting for messages…", style="dim")

    left = display_name(outbound.source, alias_map)
    right = display_name(outbound.dest, alias_map)
    state = outbound.state
    leg_state = "completed" if state == "delivered" and reply else ("failed" if state in ("dropped", "timeout") else "pending")

    diagram = build_peer_topology(left, right, state=leg_state, pulse=pulse)
    table = build_peer_leg_table(
        left,
        right,
        message_ms=outbound.latency_ms,
        reply_ms=reply.latency_ms if reply else None,
        state=leg_state,
    )
    legend = build_diagram_legend()
    return assemble_centered_diagram(diagram, table, legend, table_legend=build_table_legend())


def _assemble_table_block(table: Text, legend: Text, table_legend: Text) -> Text:
    """Table + its legend lines as one block, un-centered -- the piece that
    renders in the leg/route-table widget, directly beneath (and centered
    independently from) the topology widget above it.
    """
    block = Text()
    block.append_text(table)
    block.append_text(table_legend)
    block.append("\n")
    block.append_text(legend)
    return block


DiagramPieces = tuple[Text, "Text | None", dict[str, tuple[int, int, int, int]]]


def _hub_diagram_pieces(
    state: TraceState,
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
    selected: str | None = None,
) -> DiagramPieces:
    """(topology, table_block, hit_regions) for a hub trace, split for the
    live TUI's two-widget layout -- table_block is None when there are no
    legs yet. `hit_regions` maps each subagent's *address* to its clickable
    box region; `selected`, if given, is the currently-selected agent's
    address, highlighted with a double border in the topology.
    """
    legs = state.legs
    orch_name = display_name(state.hub, alias_map)
    agent_names = [display_name(leg["subagent"], alias_map) for leg in legs]
    selected_name = display_name(selected, alias_map) if selected else None
    topology = build_hub_topology(legs, orch_name, agent_names, pulse=pulse, selected=selected_name)
    if not legs:
        return topology, None, {}
    table = build_hub_leg_table(legs, agent_names)
    table_block = _assemble_table_block(table, build_diagram_legend(), build_table_legend())
    regions = build_hub_hit_regions(legs, orch_name, agent_names)
    hit_regions = {leg["subagent"]: region for leg, region in zip(legs, regions)}
    return topology, table_block, hit_regions


def _peer_diagram_pieces(
    hops: list[Hop],
    alias_map: dict[str, str],
    *,
    pulse: bool = False,
    selected: str | None = None,
) -> DiagramPieces:
    """(topology, table_block, hit_regions) for a peer trace -- mirrors
    `_hub_diagram_pieces`.
    """
    if not hops:
        return (
            Text(
                "  Waiting for messages…\n\n"
                "  Start your instrumented agents\n"
                "  in another terminal.",
                style="dim",
            ),
            None,
            {},
        )

    outbound, reply = _latest_peer_round_trip(hops)
    if outbound is None:
        return Text("  Waiting for messages…", style="dim"), None, {}

    left = display_name(outbound.source, alias_map)
    right = display_name(outbound.dest, alias_map)
    leg_state = (
        "completed"
        if outbound.state == "delivered" and reply
        else ("failed" if outbound.state in ("dropped", "timeout") else "pending")
    )
    selected_name = display_name(selected, alias_map) if selected else None

    topology = build_peer_topology(left, right, state=leg_state, pulse=pulse, selected=selected_name)
    table = build_peer_leg_table(
        left,
        right,
        message_ms=outbound.latency_ms,
        reply_ms=reply.latency_ms if reply else None,
        state=leg_state,
    )
    table_block = _assemble_table_block(table, build_diagram_legend(), build_table_legend())
    left_box, right_box = build_peer_hit_regions(left, right)
    hit_regions = {outbound.source: left_box, outbound.dest: right_box}
    return topology, table_block, hit_regions


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


def _view_label(view_mode: ViewMode) -> str:
    return "tree view" if view_mode == "tree" else "diagram view"


def _sub_title_for(setup: WatchSetup, view_mode: ViewMode, *, follow: bool) -> str:
    """Status line only -- keybindings live in the Footer, not repeated here."""
    names = ", ".join(setup.names.values()) if setup.names else "all agents"
    follow_hint = "following latest" if follow else "pinned to one trace"
    return f"{names}  ·  {_view_label(view_mode)}  ·  {follow_hint}"


def _trace_matches_watch(trace: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return any(a in addresses for a in trace["participants"])


def _span_in_watch(span: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return span["source_agent"] in addresses or span["dest_agent"] in addresses


def sidebar_label(trace_id: str, state: TraceState, alias_map: dict[str, str]) -> tuple[str, str]:
    """(text, style) for one sidebar row -- a fractional rollup, not a
    binary all-or-nothing ✓/✗, so a hub trace that's 3/4 done doesn't read
    as a total failure just because one leg is still broken.

    Color is fractional too: red is reserved for a trace where *every*
    leg failed. A trace with some legs ok and some failed (or still
    pending) reads amber -- "needs a look", not "everything is broken".
    Only a fully clean trace (nothing failed or pending) reads green.
    """
    if state.total == 0:
        return f"{trace_id[:6]} · waiting for spans…", MUTED

    if state.shape == HUB and state.hub:
        hub_name = display_name(state.hub, alias_map)
        header = f"{hub_name}→{state.total}"
    else:
        names = [display_name(a, alias_map) for a in state.participants[:2]]
        header = "↔".join(names) if len(names) == 2 else (names[0] if names else "?")

    duration = format_ms(state.duration_ms)
    label = f"{trace_id[:6]} · {header} · {state.completed}/{state.total} ✓ · {duration}"

    if state.failed and state.failed == state.total:
        style = ERROR
    elif state.completed == state.total:
        style = SUCCESS
    else:
        style = WARN
    return label, style


def _sidebar_markup(label_text: str, style: str) -> str:
    """Rich markup for one sidebar row: the trace id stays neutral/dim
    regardless of outcome, and only the status-bearing remainder (header ·
    fraction · duration) carries the semantic color.
    """
    id_part, sep, rest = label_text.partition(" · ")
    if not sep:
        return f"[{MUTED}]{label_text}[/]"
    return f"[{MUTED}]{id_part}[/] · [{style}]{rest}[/]"


class LiveApp(App):
    """Live network diagram + trace list + rolling message feed."""

    CSS = """
    Screen {
        background: #0a0f0d;
    }
    Header {
        background: #111916;
        color: #34d399;
    }
    Footer {
        background: #111916;
        color: #6b7280;
    }
    #main-row {
        height: 26;
    }
    #trace-list {
        width: 46;
        height: 100%;
        border: round #1f3d32;
        background: #080c0a;
        padding: 0 1;
    }
    #trace-list > ListView {
        height: 100%;
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
        border: round #34d399;
        background: #0d1210;
        padding: 1 2;
    }
    #diagram-content {
        width: 100%;
        height: auto;
    }
    #brand-panel {
        width: 76;
        height: 100%;
        border: round #34d399;
        background: #0d1210;
        color: #34d399;
        content-align: center middle;
    }
    #detail-bar {
        height: 1;
        padding: 0 2;
        color: #6b7280;
        background: #0a0f0d;
    }
    #events-header {
        height: 1;
        padding: 0 2;
        color: #34d399;
        text-style: bold;
        background: #0a0f0d;
    }
    #events-panel {
        height: 1fr;
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
        self._alias_map: dict[str, str] = {}
        self._bootstrapped = False
        self._follow_latest = True
        self._pulse_on = False
        self._detail_text = "Select a trace or wait for messages…"
        self._trace_state: TraceState | None = None

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
                yield ListView(id="trace-list")
                with Vertical(id="diagram-col"):
                    yield Static("", id="diagram-content")
                yield Static(_BRAND_TEXT, id="brand-panel")
            yield Static("", id="detail-bar")
            yield Static("Live messages", id="events-header")
            yield RichLog(id="events-panel", highlight=False, markup=False, auto_scroll=True)
        yield Footer()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_brand_panel_visibility(event.size.width)

    def _apply_brand_panel_visibility(self, width: int) -> None:
        try:
            panel = self.query_one("#brand-panel", Static)
        except Exception:
            return
        panel.display = width >= MIN_WIDTH_FOR_BRAND_PANEL

    async def on_mount(self) -> None:
        self.title = "uagents-trace live"
        self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
        events_log = self.query_one("#events-panel", RichLog)
        events_log.write(Text("  Waiting for message flow…", style="#6b7280"))
        self.query_one("#detail-bar", Static).update(self._detail_text)
        self._apply_brand_panel_visibility(self.size.width)
        await self._bootstrap()
        self.set_interval(POLL_SECONDS, self._poll)
        self.set_interval(PULSE_SECONDS, self._pulse_tick)

    async def _pulse_tick(self) -> None:
        if not self._bootstrapped:
            return
        self._pulse_on = not self._pulse_on
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

            label_text, style = sidebar_label(t["trace_id"], state, self._alias_map)
            try:
                item = trace_list.query_one(f"#{_trace_widget_id(t['trace_id'])}", ListItem)
                label_widget = item.query_one(Label)
                label_widget.update(_sidebar_markup(label_text, style))
            except Exception:
                pass

    async def _append_new_feed_events(self) -> bool:
        """Write any not-yet-logged, terminal hops for the active trace.
        Shared by the initial load and by polling so the feed always
        derives from one authoritative, fully-deduplicated hop list.
        """
        if not self._active_trace_id:
            return False
        events_log = self.query_one("#events-panel", RichLog)
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
            line = format_event_line(hop, self._alias_map)
            self._events.append(line)
            events_log.write(line)
            appended = True

        return appended

    async def _reload_feed_for_active_trace(self) -> None:
        events_log = self.query_one("#events-panel", RichLog)
        events_log.clear()
        self._events.clear()
        self._logged_hop_ids.clear()
        await self._append_new_feed_events()

    async def _select_trace(self, trace_id: str, *, follow: bool | None = None) -> None:
        if follow is not None:
            self._follow_latest = follow
        if trace_id == self._active_trace_id:
            self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
            return
        self._active_trace_id = trace_id
        await self._reload_feed_for_active_trace()
        self.sub_title = _sub_title_for(self.setup, self.view_mode, follow=self._follow_latest)
        await self._refresh_display()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id:
            trace_id = _trace_id_from_widget_id(str(event.item.id))
            await self._select_trace(trace_id, follow=False)

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

    def _center_for_panel(self, renderable: Text) -> Text:
        width = self._diagram_panel_width()
        if width < 20:
            return renderable
        return center_in_width(renderable, width)

    async def _refresh_display(self, *, pulse_only: bool = False) -> None:
        content = self.query_one("#diagram-content", Static)
        detail = self.query_one("#detail-bar", Static)

        if not self._active_trace_id:
            if not pulse_only:
                content.update(
                    self._center_for_panel(
                        Text(
                            "Waiting for messages…\n\n"
                            "Start your instrumented agents\n"
                            "in another terminal.",
                            style="dim",
                        )
                    )
                )
            return

        spans = await get_trace_spans(self.db_path, self._active_trace_id)
        if self.addresses:
            spans = [s for s in spans if _span_in_watch(s, self.addresses)]

        state = build_trace_state(spans, hub_hint=self._hub_hint())
        self._trace_state = state

        pulse = self._pulse_on and state.pending > 0

        if state.total == 0:
            renderable = Text("Waiting for messages in this trace…", style="dim")
        elif state.shape == HUB and state.hub:
            if self.view_mode == "tree" and state.tree is not None:
                renderable = build_hub_tree_diagram(state.tree, self._alias_map)
            else:
                renderable = build_hub_network_diagram(state, self._alias_map, pulse=pulse)
        else:
            renderable = build_peer_network_diagram(state.hops, self._alias_map, pulse=pulse)

        content.update(self._center_for_panel(renderable))

        if not pulse_only:
            if state.shape == HUB and state.hub and self.view_mode != "tree":
                orch_name = display_name(state.hub, self._alias_map)
                agent_names = [display_name(leg["subagent"], self._alias_map) for leg in state.legs]
                self._detail_text = build_hub_detail_summary(
                    orch_name, state.legs, agent_names, self._active_trace_id
                )
            else:
                self._detail_text = (
                    f"trace {self._active_trace_id[:8]}  ·  "
                    f"{state.completed} delivered  ·  {state.pending} pending  ·  {state.failed} failed  ·  "
                    f"[ ] switch trace"
                )
            detail.update(self._detail_text)

            trace_list = self.query_one("#trace-list", ListView)
            if self._active_trace_id in self._trace_ids:
                trace_list.index = self._trace_ids.index(self._active_trace_id)


async def run_live(setup: WatchSetup) -> None:
    app = LiveApp(setup)
    await app.run_async()
