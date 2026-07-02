"""Terminal viewer for uagents-trace spans -- no server, no browser.

    uagents-trace list                 # recent traces
    uagents-trace show <trace_id>      # ASCII waterfall for one trace

Reads the same SQLite file as the recorder and the web UI
(UAGENTS_TRACE_DB, default ./uagents_trace.db). Read-only.
"""

import argparse
import asyncio
import os
import signal
import sys
import time
from typing import Any, Optional

from rich import box
from rich.console import Console
from rich.table import Table

from .protocols import build_payment_steps, is_payment_trace
from .shape import HUB, MULTI_LEVEL, build_hub_legs, classify_trace_shape
from .store import (
    default_db_path,
    get_alias_map,
    get_trace_spans,
    init_db,
    list_aliases,
    list_traces,
    remove_alias,
    set_alias,
)

BAR_WIDTH = 50

# Same outcome -> color mapping as the web UI's delivered/timeout/dropped/pending.
ANSI = {
    "delivered": "32",  # green
    "timeout": "33",  # amber
    "dropped": "31",  # red
    "pending": "90",  # grey
}


def supports_color() -> bool:
    return not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def colorize(text: str, code: str, enabled: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if enabled else text


def short_addr(addr: Optional[str]) -> str:
    if not addr:
        return "?"
    return addr if len(addr) <= 14 else f"{addr[:10]}…{addr[-4:]}"


def display_name(addr: Optional[str], alias_map: dict[str, str]) -> str:
    """Alias if one's set for this address, else the short form."""
    if not addr:
        return "?"
    return alias_map.get(addr) or short_addr(addr)


def relative_time(ms: int) -> str:
    delta_s = max(0, time.time() - ms / 1000)
    if delta_s < 60:
        return f"{int(delta_s)}s ago"
    if delta_s < 3600:
        return f"{int(delta_s // 60)}m ago"
    if delta_s < 86400:
        return f"{int(delta_s // 3600)}h ago"
    return f"{int(delta_s // 86400)}d ago"


def reg_label(v: Optional[bool]) -> str:
    if v is True:
        return "registered"
    if v is False:
        return "NOT registered"
    return "unknown"


def explain_failure(span: dict[str, Any]) -> list[str]:
    if span["dest_registered"] is False:
        cause = "likely cause: destination is not registered/reachable"
    elif span["dest_registered"] is True:
        cause = "destination appears registered -- failed for another reason"
    else:
        cause = "destination registration status is unknown"
    return [
        cause,
        f"error: {span['error'] or '(no error message)'}",
        f"source registered: {reg_label(span['source_registered'])}"
        f"  ·  dest registered: {reg_label(span['dest_registered'])}",
    ]


def fmt_duration(ms: int) -> str:
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms} ms"


def print_flat_spans(spans: list[dict[str, Any]], alias_map: dict[str, str], color: bool) -> None:
    """One block per span: a timing bar, latency, state, and -- for
    failures -- the likely cause. Used for peer (two-agent) traces and as
    the fallback for any trace shape not recognized as peer or hub.
    """
    start = min(s["enqueued_at"] for s in spans)
    end = max(s["acked_at"] or s["enqueued_at"] for s in spans)
    span_ms = max(end - start, 1)

    for s in spans:
        latency_ms = (s["acked_at"] - s["enqueued_at"]) if s["acked_at"] is not None else None
        left = int(((s["enqueued_at"] - start) / span_ms) * BAR_WIDTH)
        width = max(1, int((latency_ms / span_ms) * BAR_WIDTH)) if latency_ms is not None else 2
        bar = (" " * left + "#" * width).ljust(BAR_WIDTH)
        bar = colorize(bar, ANSI.get(s["state"], "37"), color)

        src = display_name(s["source_agent"], alias_map)
        dst = display_name(s["dest_agent"], alias_map)
        tag = f"[{s['protocol']}] " if s.get("protocol") else ""
        label = f"{src} -> {dst}  {tag}{s['payload_type']}"
        latency_label = f"{latency_ms} ms" if latency_ms is not None else "pending…"
        print(f"{label:<42} {latency_label:>10}")
        print(f"  [{bar}]  {s['state']}")

        if s["state"] in ("dropped", "timeout"):
            tag = colorize(s["state"].upper(), ANSI[s["state"]], color)
            print(f"  {tag}")
            for line in explain_failure(s):
                print(f"    {line}")
        print()


def print_hub(spans: list[dict[str, Any]], hub: str, alias_map: dict[str, str], color: bool) -> None:
    """One row per subagent leg: a solid dispatch arrow, then either a
    dashed return arrow with latency (completed), a ✗ with the failure
    reason (failed), or "…pending" (still in flight).
    """
    start = min(s["enqueued_at"] for s in spans)
    end = max((s["acked_at"] or s["enqueued_at"]) for s in spans)
    total_ms = max(end - start, 0)

    hub_label = display_name(hub, alias_map)
    print(f"HUB {hub_label}   total: {fmt_duration(total_ms)}\n")

    legs = build_hub_legs(spans, hub)
    for i, leg in enumerate(legs):
        connector = "└─" if i == len(legs) - 1 else "├─"
        sub_label = display_name(leg["subagent"], alias_map)
        dispatch = f"──{leg['dispatch_payload']}──>"

        if leg["state"] == "completed":
            reply = f"╌╌{leg['reply_payload']}╌╌▶"
            line = f" {connector} {dispatch} {sub_label}  {reply}  {fmt_duration(leg['latency_ms'])}"
            line = colorize(line, ANSI["delivered"], color)
        elif leg["state"] == "failed":
            reason = leg.get("reason") or "(no error message)"
            line = f" {connector} {dispatch} {sub_label}  ✗ {reason}"
            line = colorize(line, ANSI["dropped"], color)
        else:
            line = f" {connector} {dispatch} {sub_label}  …pending"
            line = colorize(line, ANSI["pending"], color)
        print(line)
    print()


def print_payment(spans: list[dict[str, Any]], alias_map: dict[str, str], color: bool) -> None:
    """Numbered payment-protocol steps (request -> commit -> complete/reject/
    cancel) with the FET amount and verify outcome, instead of the generic
    flat span view's raw Task/Result-style labels.
    """
    print("PAYMENT\n")
    for i, step in enumerate(build_payment_steps(spans), start=1):
        src = display_name(step["source"], alias_map)
        dst = display_name(step["dest"], alias_map)
        line = f" {i}. {src} -> {dst}  {step['payload_type']}"
        if step["detail"]:
            line += f"  ({step['detail']})"
        if step["outcome"] == "ok":
            line = colorize(line, ANSI["delivered"], color)
        elif step["outcome"] == "failed":
            line = colorize(line, ANSI["dropped"], color)
        print(line)
    print()


async def cmd_list(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    traces = await list_traces(db_path)
    if args.session:
        traces = [t for t in traces if args.session in t["sessions"]]
    if args.limit:
        traces = traces[: args.limit]

    console = Console()
    if not traces:
        console.print("No traces recorded yet. They'll appear once an instrumented agent sends or receives a message.")
        return

    alias_map = await get_alias_map(db_path)

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", expand=False)
    table.add_column("Time", no_wrap=True)
    table.add_column("From -> To", no_wrap=True)
    table.add_column("Messages", no_wrap=True)
    table.add_column("Spans", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Trace", no_wrap=True)

    for t in traces:
        failure = t["has_failure"]
        row_style = "red" if failure else "green"
        participants = " -> ".join(display_name(a, alias_map) for a in t["participants"])
        messages = ", ".join(t["payload_types"])
        status = "✗ FAILURE" if failure else "✓ OK"
        table.add_row(
            relative_time(t["started_at"]),
            participants,
            messages,
            str(t["span_count"]),
            status,
            t["trace_id"][:8],
            style=row_style,
        )

    console.print(table)


def select_trace(traces: list[dict[str, Any]], trace_id: Optional[str], nth: Optional[int]) -> dict[str, Any]:
    """Resolve which trace `show`/`watch` should render.

    `traces` is assumed most-recent-first (what `list_traces` returns).
    Exactly one of `trace_id` / `nth` may be given; neither given means
    "most recent" (nth defaults to 1).
    """
    if trace_id and nth is not None:
        print("Provide either a trace id or -n, not both.", file=sys.stderr)
        sys.exit(1)

    if trace_id:
        matches = [t for t in traces if t["trace_id"].startswith(trace_id)]
        if not matches:
            print(f"No trace found matching '{trace_id}'", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"'{trace_id}' matches {len(matches)} traces -- be more specific:", file=sys.stderr)
            for t in matches:
                print(f"  {t['trace_id']}", file=sys.stderr)
            sys.exit(1)
        return matches[0]

    n = nth if nth is not None else 1
    if n < 1:
        print("-n must be 1 or greater (1 = most recent).", file=sys.stderr)
        sys.exit(1)
    if n > len(traces):
        print(f"Only {len(traces)} trace(s) recorded -- can't show the {n}th most recent.", file=sys.stderr)
        sys.exit(1)
    return traces[n - 1]


async def print_trace_detail(db_path: str, trace_id: str, color: bool) -> None:
    """Shared rendering body for `show` and `watch`: participants, then the
    hub/peer/flat detail view, dispatched by trace shape.
    """
    spans = await get_trace_spans(db_path, trace_id)
    if not spans:
        print("No spans in this trace.")
        return

    alias_map = await get_alias_map(db_path)

    print(f"trace {trace_id}")

    # Session is how ASI:One threads a conversation (ctx.session at the
    # instrumentation point) -- usually equal to the trace id today, but
    # shown explicitly since that's not guaranteed to stay true as more
    # instrumentation points are added. Older, pre-migration spans won't
    # have one.
    session_id = next((s["session_id"] for s in spans if s.get("session_id")), None)
    if session_id:
        print(f"session {session_id}")
    print()

    # Full addresses printed plainly (one per line) so they're easy to select
    # and copy whole, even though every other label below uses the alias or
    # short form.
    participants = list(dict.fromkeys(a for s in spans for a in (s["source_agent"], s["dest_agent"])))
    print("Participants:")
    for addr in participants:
        name = alias_map.get(addr)
        label = f"  {name + '  ' if name else ''}{addr}"
        print(label)
    print()

    if is_payment_trace(spans):
        print_payment(spans, alias_map, color)
        return

    shape, hub_agent = classify_trace_shape(spans)
    if shape == HUB:
        print_hub(spans, hub_agent, alias_map, color)
    else:
        if shape == MULTI_LEVEL:
            print("(multi-level trace, showing flat view)\n")
        print_flat_spans(spans, alias_map, color)


async def cmd_show(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    color = supports_color()

    traces = await list_traces(db_path)
    if not traces:
        print("No traces recorded yet. They'll appear once an instrumented agent sends or receives a message.", file=sys.stderr)
        sys.exit(1)

    trace = select_trace(traces, args.trace_id, args.nth)
    await print_trace_detail(db_path, trace["trace_id"], color)


async def cmd_watch(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    color = supports_color()
    console = Console()

    # A bare `try/except KeyboardInterrupt` around an `asyncio.sleep` loop is
    # unreliable here: SIGINT raised while the event loop is blocked in its
    # selector doesn't reliably surface as KeyboardInterrupt inside this
    # coroutine before `asyncio.run`'s own shutdown sequence takes over,
    # which can stall for many seconds instead of stopping promptly.
    # `loop.add_signal_handler` is the pattern asyncio itself recommends for
    # responsive, graceful Ctrl+C handling.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:
        pass  # e.g. Windows -- falls back to the default KeyboardInterrupt behavior

    while not stop.is_set():
        traces = await list_traces(db_path)
        console.clear()
        console.print("[dim]uagents-trace watch -- updating every ~1s, Ctrl+C to stop[/]\n")
        if not traces:
            console.print("No traces recorded yet. Waiting...")
        else:
            await print_trace_detail(db_path, traces[0]["trace_id"], color)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


async def cmd_alias_add(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    console = Console()

    if args.seed and args.address:
        console.print("[red]Provide either an address or --seed, not both.[/]")
        sys.exit(1)
    if args.seed:
        # Same derivation uAgents itself uses, so pasting the seed an agent
        # was instantiated with reproduces that agent's real address.
        from uagents.crypto import Identity

        address = Identity.from_seed(args.seed, 0).address
    elif args.address:
        address = args.address
    else:
        console.print("[red]Provide either an address or --seed.[/]")
        sys.exit(1)

    await set_alias(db_path, args.name, address)
    console.print(f"[green]✓[/] {args.name} -> {address}")


async def cmd_alias_list(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    console = Console()
    aliases = await list_aliases(db_path)
    if not aliases:
        console.print("No aliases set. Add one with: uagents-trace alias add <name> <address>")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold")
    table.add_column("Name")
    table.add_column("Address")
    for a in aliases:
        table.add_row(a["name"], a["address"])
    console.print(table)


async def cmd_alias_remove(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    console = Console()
    removed = await remove_alias(db_path, args.name)
    if removed:
        console.print(f"[green]✓[/] removed alias '{args.name}'")
    else:
        console.print(f"[red]No alias named '{args.name}'[/]")
        sys.exit(1)


async def cmd_default(args: argparse.Namespace) -> None:
    from .live import run_live
    from .wizard import run_wizard

    setup = await run_wizard(args.db)
    await run_live(setup)


async def cmd_tui(args: argparse.Namespace) -> None:
    db_path = args.db or default_db_path()
    await init_db(db_path)
    from .tui import TraceApp

    await TraceApp(db_path).run_async()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uagents-trace",
        description="Live agent message viewer. Run with no arguments for the interactive setup.",
    )
    parser.add_argument("--db", help="Path to the SQLite file (defaults to $UAGENTS_TRACE_DB or ./uagents_trace.db)")
    sub = parser.add_subparsers(dest="command", required=False)

    list_p = sub.add_parser("list", help="(Advanced) List recent traces as a table")
    list_p.add_argument("-n", "--limit", type=int, default=20, help="Max traces to show (default 20)")
    list_p.add_argument("--session", help="Only show traces containing spans from this session id")
    list_p.set_defaults(func=cmd_list)

    show_p = sub.add_parser("show", help="(Advanced) Show ASCII waterfall for one trace")
    show_p.add_argument(
        "trace_id",
        nargs="?",
        help="Full trace id, or a unique prefix of it (e.g. the first 8 chars). "
        "Omit to show the most recent trace.",
    )
    show_p.add_argument(
        "-n",
        dest="nth",
        type=int,
        default=None,
        help="Show the Nth most recent trace (1 = latest, 2 = second most recent, etc.) "
        "instead of passing a trace id",
    )
    show_p.set_defaults(func=cmd_show)

    alias_p = sub.add_parser("alias", help="(Advanced) Manage friendly names for agent addresses")
    alias_sub = alias_p.add_subparsers(dest="alias_command", required=True)

    alias_add_p = alias_sub.add_parser("add", help="Add or update an alias")
    alias_add_p.add_argument("name", help="Friendly name, e.g. AgentA")
    alias_add_p.add_argument("address", nargs="?", help="Agent address (omit if using --seed)")
    alias_add_p.add_argument(
        "--seed",
        help="Agent seed; the address is derived the same way uAgents does "
        "(Identity.from_seed(seed, 0).address), so pasting the seed an agent "
        "is instantiated with resolves to that agent's real address",
    )
    alias_add_p.set_defaults(func=cmd_alias_add)

    alias_list_p = alias_sub.add_parser("list", help="List all aliases")
    alias_list_p.set_defaults(func=cmd_alias_list)

    alias_remove_p = alias_sub.add_parser("remove", help="Remove an alias")
    alias_remove_p.add_argument("name")
    alias_remove_p.set_defaults(func=cmd_alias_remove)

    watch_p = sub.add_parser("watch", help="(Advanced) Re-render the most recent trace every ~1s")
    watch_p.set_defaults(func=cmd_watch)

    tui_p = sub.add_parser("tui", help="(Advanced) Table-based live trace viewer")
    tui_p.set_defaults(func=cmd_tui)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command is None:
        asyncio.run(cmd_default(args))
    else:
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
