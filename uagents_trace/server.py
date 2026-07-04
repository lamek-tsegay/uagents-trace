"""Read-only API + UI server for spans recorded by uagents_trace.recorder.

Run with: python -m uagents_trace.server
Reads from the SQLite file at UAGENTS_TRACE_DB (default ./uagents_trace.db) --
the same file the recorder writes to. This process never writes to it.
"""

import json
import os
import socket
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .shape import HUB, build_hub_legs, build_interaction_tree, classify_trace_shape, tree_node_to_dict
from .store import default_db_path, get_trace_spans, init_db, list_aliases, list_traces, remove_alias, set_alias

HOST = "127.0.0.1"
DEFAULT_PORT = 8675
DEBUG_LOG_PATH = Path(__file__).resolve().parents[1] / ".cursor" / "debug-7a0a2f.log"

UI_PATH = Path(__file__).parent / "ui" / "index.html"


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": "7a0a2f",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except Exception:
        pass
    # #endregion


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) == 0


def _server_already_running(host: str, port: int) -> bool:
    if not _port_in_use(host, port):
        return False
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/traces", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def resolve_port(host: str = HOST, requested: int = DEFAULT_PORT) -> int:
    """Pick a bind port; exit cleanly if our web UI is already up."""
    env_port = os.environ.get("UAGENTS_TRACE_PORT")
    if env_port:
        requested = int(env_port)

    _debug_log(
        "C",
        "server.py:resolve_port",
        "port resolution start",
        {"host": host, "requested": requested, "env_port": env_port},
    )

    if _server_already_running(host, requested):
        _debug_log(
            "A",
            "server.py:resolve_port",
            "existing uagents-trace server detected",
            {"host": host, "port": requested},
        )
        print(f"uagents-trace web UI is already running at http://{host}:{requested}")
        sys.exit(0)

    in_use = _port_in_use(host, requested)
    _debug_log(
        "A",
        "server.py:resolve_port",
        "default port availability",
        {"host": host, "port": requested, "in_use": in_use},
    )

    if not in_use:
        return requested

    for alt in range(requested + 1, requested + 10):
        if not _port_in_use(host, alt):
            _debug_log(
                "B",
                "server.py:resolve_port",
                "using alternate port",
                {"host": host, "requested": requested, "selected": alt},
            )
            print(f"Port {requested} is already in use — using {alt} instead.")
            print(f"Open http://{host}:{alt}")
            return alt

    _debug_log(
        "D",
        "server.py:resolve_port",
        "no free port in range",
        {"host": host, "requested": requested},
    )
    print(f"ERROR: ports {requested}–{requested + 9} are all in use.", file=sys.stderr)
    print(
        f"Tip: stop the other process or set UAGENTS_TRACE_PORT to a free port.",
        file=sys.stderr,
    )
    sys.exit(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Creates the spans table if the DB file doesn't exist yet -- e.g. the
    # UI was opened before any agent using the recorder has run. Read-only
    # with respect to span data; just guarantees queries don't 500 on an
    # empty setup.
    await init_db(default_db_path())
    yield


app = FastAPI(title="uagents-trace", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(UI_PATH)


@app.get("/api/traces")
async def api_list_traces() -> list[dict]:
    return await list_traces(default_db_path())


@app.get("/api/traces/{trace_id}")
async def api_get_trace(trace_id: str) -> list[dict]:
    spans = await get_trace_spans(default_db_path(), trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="Trace not found")
    return spans


@app.get("/api/traces/{trace_id}/tree")
async def api_get_trace_tree(trace_id: str) -> dict:
    db_path = default_db_path()
    spans = await get_trace_spans(db_path, trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="Trace not found")
    shape, hub = classify_trace_shape(spans)
    if shape != HUB or not hub:
        raise HTTPException(status_code=404, detail="Trace is not hub-shaped")
    tree = build_interaction_tree(spans, hub)
    return tree_node_to_dict(tree)


@app.get("/api/traces/{trace_id}/hub-legs")
async def api_get_hub_legs(trace_id: str) -> dict:
    db_path = default_db_path()
    spans = await get_trace_spans(db_path, trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="Trace not found")
    shape, hub = classify_trace_shape(spans)
    if shape != HUB or not hub:
        raise HTTPException(status_code=404, detail="Trace is not hub-shaped")
    return {"hub": hub, "legs": build_hub_legs(spans, hub)}


class AliasIn(BaseModel):
    name: str
    address: str


@app.get("/api/aliases")
async def api_list_aliases() -> list[dict]:
    return await list_aliases(default_db_path())


@app.put("/api/aliases")
async def api_set_alias(alias: AliasIn) -> dict:
    await set_alias(default_db_path(), alias.name, alias.address)
    return {"name": alias.name, "address": alias.address}


@app.delete("/api/aliases/{address}")
async def api_remove_alias(address: str) -> dict:
    db_path = default_db_path()
    aliases = await list_aliases(db_path)
    match = next((a for a in aliases if a["address"] == address), None)
    if match is None:
        return {"removed": False}
    await remove_alias(db_path, match["name"])
    return {"removed": True}


def main() -> None:
    port = resolve_port()
    _debug_log(
        "E",
        "server.py:main",
        "starting uvicorn",
        {"host": HOST, "port": port},
    )
    uvicorn.run(app, host=HOST, port=port)


if __name__ == "__main__":
    main()
