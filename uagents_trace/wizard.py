"""Interactive Q&A setup for the live trace viewer.

Replaces manual `uagents-trace alias add` commands with a short wizard that
collects agent seeds/addresses and friendly names, then saves aliases and
watch config to SQLite.
"""

from dataclasses import dataclass

from .store import default_db_path, init_db, load_watch_config, save_watch_config, set_alias

INDENT = "  "


@dataclass
class WatchSetup:
    addresses: set[str]
    names: dict[str, str]  # address -> friendly name
    filter_only: bool
    db_path: str
    orchestrator: str | None = None


def _looks_like_address(value: str) -> bool:
    return value.startswith("agent1")


def resolve_address(seed_or_address: str) -> str:
    if _looks_like_address(seed_or_address):
        return seed_or_address.strip()
    from uagents.crypto import Identity

    return Identity.from_seed(seed_or_address.strip(), 0).address


def _prompt(text: str, *, default: str | None = None, example: str | None = None) -> str:
    if example:
        line = f"{INDENT}{text} (e.g. {example}): "
    else:
        line = f"{INDENT}{text}: "

    while True:
        value = input(line).strip()
        if value:
            return value
        if default is not None:
            return default
        print(f"{INDENT}Please enter a value.")


def _prompt_yes_no(text: str, default_yes: bool = True) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    value = input(f"{INDENT}{text} [{hint}]: ").strip().lower()
    if not value:
        return default_yes
    return value in ("y", "yes")


async def run_wizard(db_path: str | None = None) -> WatchSetup:
    db_path = db_path or default_db_path()
    await init_db(db_path)

    print()
    print("=" * 56)
    print("  uagents-trace — live agent message viewer")
    print("=" * 56)
    print()
    print(f"{INDENT}Your agents must use traced_send and @trace in their code.")
    print(f"{INDENT}This tool only watches — it does not run agents for you.")
    print()

    saved = await load_watch_config(db_path)
    if saved and _prompt_yes_no("Use your last setup?", default_yes=True):
        addresses = set(saved["addresses"])
        names: dict[str, str] = {}
        from .store import list_aliases

        for alias in await list_aliases(db_path):
            if alias["address"] in addresses:
                names[alias["address"]] = alias["name"]
        filter_only = saved["filter_only"]
        orchestrator = saved.get("orchestrator")
        label = ", ".join(names.get(a, a[:12] + "…") for a in saved["addresses"])
        print(f"\n{INDENT}Watching: {label}")
        print(f"{INDENT}Start your agents, then press Enter to open the live view…")
        input()
        return WatchSetup(
            addresses=addresses,
            names=names,
            filter_only=filter_only,
            db_path=db_path,
            orchestrator=orchestrator,
        )

    while True:
        count_str = _prompt("How many agents do you want to watch?", default="2", example="2")
        try:
            count = int(count_str)
            if count < 1:
                raise ValueError
            break
        except ValueError:
            print(f"{INDENT}Enter a number 1 or greater.")

    addresses: set[str] = set()
    names: dict[str, str] = {}
    orchestrator: str | None = None

    for i in range(1, count + 1):
        example_name = "Orchestrator" if i == 1 else f"SubAgent{i - 1}"
        print(f"\n{INDENT}Agent {i}" + (" (orchestrator)" if i == 1 else ""))
        seed_or_addr = _prompt("Seed or address")
        address = resolve_address(seed_or_addr)
        name = _prompt("Friendly name", default=example_name, example=example_name)
        addresses.add(address)
        names[address] = name
        if i == 1:
            orchestrator = address
        await set_alias(db_path, name, address)
        print(f"{INDENT}✓ {name} registered")

    filter_only = _prompt_yes_no("Show only these agents (hide other traces)?", default_yes=True)
    await save_watch_config(db_path, list(addresses), filter_only, orchestrator)

    label = ", ".join(names[a] for a in addresses)
    print(f"\n{INDENT}Watching: {label}")
    print(f"{INDENT}Start your agents in another terminal, then press Enter…")
    input()

    return WatchSetup(
        addresses=addresses,
        names=names,
        filter_only=filter_only,
        db_path=db_path,
        orchestrator=orchestrator,
    )
