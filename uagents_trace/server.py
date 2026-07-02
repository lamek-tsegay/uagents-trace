"""Read-only API + UI server for spans recorded by uagents_trace.recorder.

Run with: python -m uagents_trace.server
Reads from the SQLite file at UAGENTS_TRACE_DB (default ./uagents_trace.db) --
the same file the recorder writes to. This process never writes to it.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .store import default_db_path, get_trace_spans, init_db, list_aliases, list_traces, remove_alias, set_alias

HOST = "127.0.0.1"
PORT = 8675

UI_PATH = Path(__file__).parent / "ui" / "index.html"


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
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
