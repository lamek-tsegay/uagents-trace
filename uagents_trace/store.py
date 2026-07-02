"""SQLite schema and read/write helpers for trace spans.

A fresh connection is opened per call. This is an observability tool with
low write volume (one row per message send/receive), so connection pooling
would be premature; WAL mode is enabled so the recorder (writer) and the
server (reader) can use the same file concurrently from separate processes.
"""

import os
import time
from typing import Any

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
    name TEXT PRIMARY KEY,
    address TEXT NOT NULL UNIQUE
);
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

WATCH_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get("UAGENTS_TRACE_DB", "./uagents_trace.db")


def now_ms() -> int:
    return int(time.time() * 1000)


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(SCHEMA)
        await db.commit()

        cursor = await db.execute("PRAGMA table_info(spans)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column, sql_type in {**SPAN_COLUMNS_V2, **SPAN_COLUMNS_V3}.items():
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE spans ADD COLUMN {column} {sql_type}")
        await db.executescript(WATCH_CONFIG_SCHEMA)
        await db.commit()


async def insert_span(db_path: str, span: dict[str, Any]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO spans (
                id, trace_id, source_agent, dest_agent, protocol,
                payload_type, payload_size, enqueued_at, acked_at, state,
                source_registered, dest_registered, error,
                session_id, kind, detail, payload_summary, direction
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        await db.commit()


async def update_span(db_path: str, span_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [span_id]
    async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM spans WHERE session_id = ? ORDER BY enqueued_at ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_span(row) for row in rows]


async def set_alias(db_path: str, name: str, address: str) -> None:
    """Upsert `name` -> `address`. `address` is unique, so re-aliasing an
    address that already has a different name replaces that old entry
    instead of erroring -- one name per address, last write wins.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM aliases WHERE address = ? AND name != ?", (address, name))
        await db.execute(
            "INSERT INTO aliases (name, address) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET address = excluded.address",
            (name, address),
        )
        await db.commit()


async def remove_alias(db_path: str, name: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM aliases WHERE name = ?", (name,))
        await db.commit()
        return cursor.rowcount > 0


async def list_aliases(db_path: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
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
) -> None:
    import json

    async with aiosqlite.connect(db_path) as db:
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
        await db.commit()


async def load_watch_config(db_path: str) -> dict[str, Any] | None:
    import json

    async with aiosqlite.connect(db_path) as db:
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
    }
