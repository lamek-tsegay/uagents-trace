"""Interactive setup for the live trace viewer.

Collects agent seeds/addresses and friendly names, saves aliases and watch
config to SQLite, then hands off to the live diagram.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

import questionary
from questionary import Style
from rich.console import Console

from .store import default_db_path, init_db, list_aliases, load_watch_config, save_watch_config, set_alias

ViewMode = Literal["linear", "tree"]

ACCENT = "#34d399"
MUTED = "#6b7280"
SUCCESS = "#4ade80"

PROMPT_STYLE = Style(
    [
        ("qmark", f"fg:{ACCENT} bold"),
        ("question", "bold"),
        ("answer", f"fg:{ACCENT} bold"),
        ("pointer", f"fg:{ACCENT} bold"),
        ("highlighted", f"fg:{ACCENT} bold"),
        ("selected", f"fg:{SUCCESS} bold"),
        ("instruction", f"fg:{MUTED}"),
        ("text", ""),
        ("separator", f"fg:{MUTED}"),
    ]
)

AGENT_COUNT_CHOICES = {
    "2 agents — peer conversation": 2,
    "3 agents — orchestrator + 2 sub-agents": 3,
    "4 agents — orchestrator + 3 sub-agents": 4,
    "Custom number…": None,
}

SEED_HINT = "example_agent_seed"
ADDRESS_HINT = "agent1q…"

console = Console()


@dataclass
class WatchSetup:
    addresses: set[str]
    names: dict[str, str]  # address -> friendly name
    filter_only: bool
    db_path: str
    orchestrator: str | None = None
    view_mode: ViewMode = "linear"


def _looks_like_address(value: str) -> bool:
    return value.startswith("agent1")


def resolve_address(seed_or_address: str) -> str:
    if _looks_like_address(seed_or_address):
        return seed_or_address.strip()
    from uagents.crypto import Identity

    return Identity.from_seed(seed_or_address.strip(), 0).address


def _exit_on_cancel(value) -> None:
    if value is None:
        console.print()
        sys.exit(130)


def _print_header() -> None:
    console.print()
    console.print(f"[bold white]uagents-trace[/] [dim {ACCENT}]·[/] [dim]message flow observer[/]")
    console.print(
        f"[dim]Wrap sends with [bold]traced_send[/] and handlers with [bold]@trace[/] — "
        "this viewer records and displays only.[/]"
    )
    console.print()


def _print_section(title: str) -> None:
    console.print()
    console.print(f"[bold {ACCENT}]›[/] [bold]{title}[/]")


def _print_done(label: str) -> None:
    console.print(f"  [{SUCCESS}]●[/] {label}")


def _print_ready_summary(
    names: dict[str, str],
    addresses: set[str],
    *,
    filter_only: bool,
) -> None:
    _print_section("Watching")
    for address in addresses:
        name = names.get(address, address[:14] + "…")
        _print_done(name)
    if filter_only:
        console.print("  [dim]Scope: registered agents only[/]")
    else:
        console.print("  [dim]Scope: all traces touching these agents[/]")
    _print_go_live()


def _print_go_live() -> None:
    console.print()
    console.print(f"[bold {ACCENT}]Go live[/]")
    console.print("  [dim]→[/] Run your instrumented agents in another terminal")
    console.print("  [dim]→[/] Press Enter here to open the live diagram")
    console.print()


def _wait_for_enter() -> None:
    try:
        input()
    except KeyboardInterrupt:
        console.print()
        sys.exit(130)


async def _prompt_agent_count() -> int:
    choice = await questionary.select(
        "How many agents do you want to watch?",
        choices=list(AGENT_COUNT_CHOICES.keys()),
        style=PROMPT_STYLE,
        use_indicator=True,
        use_shortcuts=False,
    ).ask_async()
    _exit_on_cancel(choice)

    count = AGENT_COUNT_CHOICES[choice]
    if count is not None:
        return count

    while True:
        raw = await questionary.text(
            "Enter number of agents:",
            validate=lambda t: t.strip().isdigit() and int(t.strip()) >= 1 or "Enter an integer ≥ 1",
            style=PROMPT_STYLE,
        ).ask_async()
        _exit_on_cancel(raw)
        return int(raw.strip())


async def _prompt_seed_or_address(agent_index: int, *, orchestrator: bool) -> str:
    role = "orchestrator" if orchestrator else "agent"
    raw = await questionary.text(
        f"{role.capitalize()} seed or address",
        validate=lambda t: bool(t.strip()) or "Required",
        style=PROMPT_STYLE,
        instruction=f"seed e.g. {SEED_HINT}  ·  address e.g. {ADDRESS_HINT}",
    ).ask_async()
    _exit_on_cancel(raw)
    return raw.strip()


async def _prompt_friendly_name(agent_index: int, *, default: str) -> str:
    raw = await questionary.text(
        "Display name",
        default=default,
        style=PROMPT_STYLE,
        instruction="shown on the live diagram",
    ).ask_async()
    _exit_on_cancel(raw)
    value = raw.strip()
    return value or default


async def _prompt_filter_only() -> bool:
    value = await questionary.confirm(
        "Hide traces that don't involve these agents?",
        default=True,
        style=PROMPT_STYLE,
    ).ask_async()
    _exit_on_cancel(value)
    return value


async def _prompt_use_saved_setup() -> bool:
    value = await questionary.confirm(
        "Resume with your previous agent setup?",
        default=True,
        style=PROMPT_STYLE,
    ).ask_async()
    _exit_on_cancel(value)
    return value


async def _restore_saved_setup(db_path: str, saved: dict) -> WatchSetup:
    addresses = set(saved["addresses"])
    names: dict[str, str] = {}
    for alias in await list_aliases(db_path):
        if alias["address"] in addresses:
            names[alias["address"]] = alias["name"]

    _print_section("Restored setup")
    for address in saved["addresses"]:
        _print_done(names.get(address, address[:14] + "…"))
    if saved["filter_only"]:
        console.print("  [dim]Scope: registered agents only[/]")
    _print_go_live()
    _wait_for_enter()

    return WatchSetup(
        addresses=addresses,
        names=names,
        filter_only=saved["filter_only"],
        db_path=db_path,
        orchestrator=saved.get("orchestrator"),
        view_mode=saved.get("view_mode", "linear"),
    )


async def run_wizard(db_path: str | None = None) -> WatchSetup:
    db_path = db_path or default_db_path()
    await init_db(db_path)
    saved = await load_watch_config(db_path)

    _print_header()

    if saved and await _prompt_use_saved_setup():
        return await _restore_saved_setup(db_path, saved)

    _print_section("Add agents")

    count = await _prompt_agent_count()
    addresses: set[str] = set()
    names: dict[str, str] = {}
    orchestrator: str | None = None

    for i in range(1, count + 1):
        is_orch = i == 1 and count >= 2
        if count > 1:
            label = f"Agent {i}/{count}" + (" · orchestrator" if is_orch else "")
            console.print(f"\n  [dim]{label}[/]")

        seed_or_addr = await _prompt_seed_or_address(i, orchestrator=is_orch)
        address = resolve_address(seed_or_addr)

        default_name = "Orchestrator" if is_orch else (f"SubAgent{i - 1}" if count >= 2 else "Agent")
        name = await _prompt_friendly_name(i, default=default_name)

        addresses.add(address)
        names[address] = name
        if i == 1:
            orchestrator = address
        await set_alias(db_path, name, address)
        _print_done(f"{name} saved")

    filter_only = await _prompt_filter_only()
    await save_watch_config(db_path, list(addresses), filter_only, orchestrator)

    _print_ready_summary(names, addresses, filter_only=filter_only)
    _wait_for_enter()

    return WatchSetup(
        addresses=addresses,
        names=names,
        filter_only=filter_only,
        db_path=db_path,
        orchestrator=orchestrator,
        view_mode="linear",
    )
