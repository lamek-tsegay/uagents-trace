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

from .store import default_db_path, init_db, list_aliases, load_watch_config, save_watch_config, set_alias

ViewMode = Literal["linear", "tree"]

INDENT = "  "

PROMPT_STYLE = Style([("qmark", "fg:cyan bold"), ("question", "bold"), ("answer", "fg:cyan bold")])

AGENT_COUNT_CHOICES = {
    "2 agents — peer conversation": 2,
    "3 agents — orchestrator + 2 sub-agents": 3,
    "4 agents — orchestrator + 3 sub-agents": 4,
    "Custom number…": None,
}


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
        print()
        sys.exit(130)


async def _prompt_agent_count() -> int:
    choice = await questionary.select(
        "How many agents do you want to watch?",
        choices=list(AGENT_COUNT_CHOICES.keys()),
        style=PROMPT_STYLE,
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
    label = "Seed or address" + (" (orchestrator)" if orchestrator else "")
    raw = await questionary.text(
        label,
        validate=lambda t: bool(t.strip()) or "Required",
        style=PROMPT_STYLE,
    ).ask_async()
    _exit_on_cancel(raw)
    return raw.strip()


async def _prompt_friendly_name(agent_index: int, *, default: str) -> str:
    raw = await questionary.text(
        "Friendly name",
        default=default,
        style=PROMPT_STYLE,
    ).ask_async()
    _exit_on_cancel(raw)
    value = raw.strip()
    return value or default


async def _prompt_filter_only() -> bool:
    value = await questionary.confirm(
        "Show only these agents (hide other traces)?",
        default=True,
        style=PROMPT_STYLE,
    ).ask_async()
    _exit_on_cancel(value)
    return value


async def _prompt_use_saved_setup() -> bool:
    value = await questionary.confirm(
        "Use your last setup?",
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

    label = ", ".join(names.get(a, a[:12] + "…") for a in saved["addresses"])
    print(f"\n{INDENT}Watching: {label}")
    print(f"{INDENT}Start your agents, then press Enter to open the live view…")
    try:
        input()
    except KeyboardInterrupt:
        print()
        sys.exit(130)

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

    print()
    print("=" * 56)
    print("  uagents-trace — live agent message viewer")
    print("=" * 56)
    print()
    print(f"{INDENT}Your agents must use traced_send and @trace in their code.")
    print(f"{INDENT}This tool only watches — it does not run agents for you.")
    print()

    if saved and await _prompt_use_saved_setup():
        return await _restore_saved_setup(db_path, saved)

    count = await _prompt_agent_count()
    addresses: set[str] = set()
    names: dict[str, str] = {}
    orchestrator: str | None = None

    for i in range(1, count + 1):
        is_orch = i == 1 and count >= 2
        print(f"\n{INDENT}Agent {i}" + (" (orchestrator)" if is_orch else ""))
        seed_or_addr = await _prompt_seed_or_address(i, orchestrator=is_orch)
        address = resolve_address(seed_or_addr)
        default_name = "Orchestrator" if is_orch else (f"SubAgent{i - 1}" if count >= 2 else "Agent")
        name = await _prompt_friendly_name(i, default=default_name)
        addresses.add(address)
        names[address] = name
        if i == 1:
            orchestrator = address
        await set_alias(db_path, name, address)
        print(f"{INDENT}✓ {name} registered")

    filter_only = await _prompt_filter_only()
    await save_watch_config(db_path, list(addresses), filter_only, orchestrator)

    label = ", ".join(names[a] for a in addresses)
    print(f"\n{INDENT}Watching: {label}")
    print(f"{INDENT}Start your agents in another terminal, then press Enter…")
    try:
        input()
    except KeyboardInterrupt:
        print()
        sys.exit(130)

    return WatchSetup(
        addresses=addresses,
        names=names,
        filter_only=filter_only,
        db_path=db_path,
        orchestrator=orchestrator,
        view_mode="linear",
    )
