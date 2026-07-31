"""SQLite schema and read/write helpers for trace spans.

A fresh connection is opened per call. This is an observability tool with
low write volume (one row per message send/receive), so connection pooling
would be premature; WAL mode is enabled so the recorder (writer) and the
server (reader) can use the same file concurrently from separate processes.
"""

import os
import time
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    dest_agent TEXT NOT NULL,
    protocol TEXT,
    payload_type TEXT NOT NULL,
    payload_size INTEGER NOT NULL,
    enqueued_at INTEGER NOT NULL,
    acked_at INTEGER,
    state TEXT NOT NULL,
    source_registered INTEGER,
    dest_registered INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_enqueued_at ON spans(enqueued_at);

CREATE TABLE IF NOT EXISTS aliases (
    address TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_name ON aliases(name);
"""

# Columns added after the initial release. `CREATE TABLE IF NOT EXISTS`
# above won't add columns to a `spans` table that already exists on disk,
# so these are migrated in via `ALTER TABLE` in `init_db` instead.
#   session_id: ctx.session at the instrumentation point -- how ASI:One
#     threads a conversation; usually equal to trace_id today (both derive
#     from the same session), but kept as its own column since the two
#     concepts may diverge as instrumentation points grow (see `kind`).
#   kind: None/"message" for the existing send/receive spans, or "routing"
#     for a `@trace_routing`-recorded decision (not a send at all).
#   detail: short free-form label for protocol-specific context that
#     doesn't fit the generic source/dest/payload_type shape -- e.g. a
#     payment's amount/outcome, or a routing decision's query.
SPAN_COLUMNS_V2 = {
    "session_id": "TEXT",
    "kind": "TEXT",
    "detail": "TEXT",
}

# payload_summary: human-readable message body for live display.
# direction: "send" (traced_send) or "receive" (@trace handler).
SPAN_COLUMNS_V3 = {
    "payload_summary": "TEXT",
    "direction": "TEXT",
}

# parent_span_id: id of the receive span whose handler was executing (per
# the `recorder._current_span` contextvar) when this span was written --
# only ever set on "send" spans (see `recorder.traced_send`), pointing back
# to the *sender's own* receive span that caused the send. NULL means
# "unknown" (no handler context was active -- a timer, a detached
# background task, or the trace's true entry point), same as the existing
# registered=True/False/None philosophy: not something to guess at.
SPAN_COLUMNS_V4 = {
    "parent_span_id": "TEXT",
}

WATCH_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Every traced agent *process* is its own writer against the same file (not
# just the one recorder + one server the module docstring calls out), so
# concurrent writes -- especially several processes' first message all
# racing to run `init_db`'s migrations at once -- are the normal case, not
# an edge case. sqlite3's default connection timeout (5s, what a bare
# `aiosqlite.connect(db_path)` with no `timeout=` kwarg gets) is tight
# enough for that to occasionally lose and raise "database is locked"
# outright instead of just waiting a little longer; every connection in
# this module sets this explicitly instead of trusting the driver default.
DB_TIMEOUT_SECONDS = 30


def default_db_path() -> str:
    return os.environ.get("UAGENTS_TRACE_DB", "./uagents_trace.db")


def now_ms() -> int:
    return int(time.time() * 1000)


async def _migrate_aliases_table(db: aiosqlite.Connection) -> None:
    """Flip `aliases`' primary key from `name` to `address` on databases
    created before this change. An agent's address is its stable identity
    (a re-aliased address should replace that address's old name, not spawn
    a second row) -- see `set_alias`'s docstring. `ALTER TABLE` can't change
    a primary key in SQLite, so this rebuilds the table when needed; a
    freshly created table (via `SCHEMA` above) is already correct and
    `PRAGMA table_info` reports its pk column as `address` immediately, so
    this is a one-time no-op after the first migrated run.
    """
    cursor = await db.execute("PRAGMA table_info(aliases)")
    columns = await cursor.fetchall()
    if not columns:
        return
    pk_column = next((col[1] for col in columns if col[5] == 1), None)
    if pk_column == "address":
        return
    await db.executescript(
        """
        ALTER TABLE aliases RENAME TO aliases_old;
        CREATE TABLE aliases (
            address TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_name ON aliases(name);
        INSERT INTO aliases (address, name) SELECT address, name FROM aliases_old;
        DROP TABLE aliases_old;
        """
    )


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(SCHEMA)
        await db.commit()

        cursor = await db.execute("PRAGMA table_info(spans)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column, sql_type in {**SPAN_COLUMNS_V2, **SPAN_COLUMNS_V3, **SPAN_COLUMNS_V4}.items():
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE spans ADD COLUMN {column} {sql_type}")
        await _migrate_aliases_table(db)
        await db.commit()
        await db.executescript(WATCH_CONFIG_SCHEMA)
        await db.commit()


async def insert_span(db_path: str, span: dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.execute(
            """
            INSERT INTO spans (
                id, trace_id, source_agent, dest_agent, protocol,
                payload_type, payload_size, enqueued_at, acked_at, state,
                source_registered, dest_registered, error,
                session_id, kind, detail, payload_summary, direction,
                parent_span_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span["id"],
                span["trace_id"],
                span["source_agent"],
                span["dest_agent"],
                span.get("protocol"),
                span["payload_type"],
                span["payload_size"],
                span["enqueued_at"],
                span.get("acked_at"),
                span["state"],
                span.get("source_registered"),
                span.get("dest_registered"),
                span.get("error"),
                span.get("session_id"),
                span.get("kind"),
                span.get("detail"),
                span.get("payload_summary"),
                span.get("direction"),
                span.get("parent_span_id"),
            ),
        )
        await db.commit()


async def update_span(db_path: str, span_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [span_id]
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.execute(f"UPDATE spans SET {columns} WHERE id = ?", values)
        await db.commit()


def _row_to_span(row: aiosqlite.Row) -> dict[str, Any]:
    span = dict(row)
    # SQLite has no boolean type, so these come back as 0/1/None ints;
    # coerce to real booleans so the JSON API and UI can do `=== true/false`.
    for key in ("source_registered", "dest_registered"):
        if span[key] is not None:
            span[key] = bool(span[key])
    return span


async def list_traces(db_path: str) -> list[dict[str, Any]]:
    """One summary row per trace, including which agents and payload types
    were involved. Aggregated in Python rather than SQL `GROUP_CONCAT` so the
    participant set (drawn from both source_agent and dest_agent) and the
    payload type list both come out as ordered, deduplicated lists -- this
    tool's write volume is low enough that a full scan per request is fine.
    """
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT trace_id, source_agent, dest_agent, payload_type, state, enqueued_at, session_id "
            "FROM spans ORDER BY trace_id, enqueued_at"
        )
        rows = await cursor.fetchall()

    traces: dict[str, dict[str, Any]] = {}
    for row in rows:
        t = traces.setdefault(
            row["trace_id"],
            {
                "trace_id": row["trace_id"],
                "started_at": row["enqueued_at"],
                "span_count": 0,
                "has_failure": False,
                "participants": [],
                "payload_types": [],
                "sessions": [],
            },
        )
        t["started_at"] = min(t["started_at"], row["enqueued_at"])
        t["span_count"] += 1
        if row["state"] in ("dropped", "timeout"):
            t["has_failure"] = True
        for addr in (row["source_agent"], row["dest_agent"]):
            if addr not in t["participants"]:
                t["participants"].append(addr)
        if row["payload_type"] not in t["payload_types"]:
            t["payload_types"].append(row["payload_type"])
        if row["session_id"] and row["session_id"] not in t["sessions"]:
            t["sessions"].append(row["session_id"])

    return sorted(traces.values(), key=lambda t: t["started_at"], reverse=True)


async def get_trace_spans(db_path: str, trace_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY enqueued_at ASC",
            (trace_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_span(row) for row in rows]


async def get_spans_by_session(db_path: str, session_id: str) -> list[dict[str, Any]]:
    """All spans sharing a session id, across traces if need be -- session is
    how ASI:One threads a conversation, which today coincides with trace_id
    (both come from ctx.session) but is kept as its own filter since that
    may not always hold as more instrumentation points are added.
    """
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM spans WHERE session_id = ? ORDER BY enqueued_at ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_span(row) for row in rows]


async def set_alias(db_path: str, name: str, address: str) -> Optional[str]:
    """Upsert `address` -> `name`. `address` is the primary key -- an
    agent's address is its stable identity, so re-aliasing an address that
    already has a different name replaces that address's own old name
    (rename), it never touches any *other* address's row.

    Returns a warning string if `name` is already in use by a *different*
    address (that other address's alias is left alone, not silently
    dropped, so both addresses end up sharing the display name until a
    caller resolves the collision) -- None otherwise.
    """
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT address FROM aliases WHERE name = ? AND address != ?", (name, address))
        collision = await cursor.fetchone()
        warning = (
            f"'{name}' is already the display name for {collision['address']} -- both addresses now show as '{name}'."
            if collision
            else None
        )
        await db.execute(
            "INSERT INTO aliases (address, name) VALUES (?, ?) "
            "ON CONFLICT(address) DO UPDATE SET name = excluded.name",
            (address, name),
        )
        await db.commit()
    return warning


async def remove_alias(db_path: str, name: str) -> bool:
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        cursor = await db.execute("DELETE FROM aliases WHERE name = ?", (name,))
        await db.commit()
        return cursor.rowcount > 0


async def list_aliases(db_path: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT name, address FROM aliases ORDER BY name")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_alias_map(db_path: str) -> dict[str, str]:
    """address -> name, for resolving display names wherever an address shows up."""
    return {a["address"]: a["name"] for a in await list_aliases(db_path)}


def _span_matches_addresses(span: dict[str, Any], addresses: set[str] | None) -> bool:
    if not addresses:
        return True
    return span["source_agent"] in addresses or span["dest_agent"] in addresses


async def get_spans_since(
    db_path: str,
    since_ms: int,
    addresses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Spans enqueued or acked after `since_ms`, for live polling.

    Includes acked_at updates so pending spans that later deliver are picked up.
    """
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM spans
            WHERE enqueued_at > ? OR (acked_at IS NOT NULL AND acked_at > ?)
            ORDER BY enqueued_at ASC
            """,
            (since_ms, since_ms),
        )
        rows = await cursor.fetchall()
    spans = [_row_to_span(row) for row in rows]
    if addresses:
        spans = [s for s in spans if _span_matches_addresses(s, addresses)]
    return spans


async def get_recent_spans(
    db_path: str,
    limit: int = 50,
    addresses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Most recent spans, oldest first within the window."""
    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM spans ORDER BY enqueued_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    spans = [_row_to_span(row) for row in reversed(rows)]
    if addresses:
        spans = [s for s in spans if _span_matches_addresses(s, addresses)]
    return spans


async def save_watch_config(
    db_path: str,
    addresses: list[str],
    filter_only: bool,
    orchestrator: str | None = None,
    view_mode: str | None = None,
) -> None:
    import json

    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.executescript(WATCH_CONFIG_SCHEMA)
        await db.execute(
            "INSERT INTO watch_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("addresses", json.dumps(addresses)),
        )
        await db.execute(
            "INSERT INTO watch_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("filter_only", "true" if filter_only else "false"),
        )
        if orchestrator:
            await db.execute(
                "INSERT INTO watch_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("orchestrator", orchestrator),
            )
        if view_mode:
            await db.execute(
                "INSERT INTO watch_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("view_mode", view_mode),
            )
        await db.commit()


async def load_watch_config(db_path: str) -> dict[str, Any] | None:
    import json

    async with aiosqlite.connect(db_path, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.executescript(WATCH_CONFIG_SCHEMA)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, value FROM watch_config")
        rows = await cursor.fetchall()
    if not rows:
        return None
    data = {row["key"]: row["value"] for row in rows}
    if "addresses" not in data:
        return None
    return {
        "addresses": json.loads(data["addresses"]),
        "filter_only": data.get("filter_only", "true") == "true",
        "orchestrator": data.get("orchestrator"),
        "view_mode": data.get("view_mode", "overview"),
    }
