"""Read-only instrumentation for uAgents message flow.

`traced_send` wraps `ctx.send` and `trace` wraps a message handler. Neither
changes what the agent does -- they only record a span describing what
happened around the call they wrap.

Spans are correlated into a trace via the uAgents session id (`ctx.session`).
The framework already propagates one session id through an entire
send -> receive -> reply chain (see `ExternalContext` construction in
uagents.agent), so reusing it as `trace_id` groups a logical flow for free
instead of inventing a parallel correlation scheme.
"""

import asyncio
import contextvars
import functools
import json
import uuid
from typing import Any, Awaitable, Callable, Optional, TypeVar

from uagents import Context, Model

from .protocols import classify_message
from .store import default_db_path, init_db, insert_span, now_ms, update_span

DEFAULT_TIMEOUT_SECONDS = 10

_initialized_dbs: set[str] = set()

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# Set by `trace` to the receive span id of the handler currently executing
# in this asyncio task, read by `traced_send` to record which receive
# *caused* a given send -- see `traced_send`'s and `trace`'s docstrings.
# `asyncio.create_task` copies the current context at creation time (see
# `contextvars` docs), so a task spawned from inside a traced handler still
# sees the parent's value; a task that *isn't* spawned from inside one (a
# timer, a detached background job) sees the default, None -- an honest
# "unknown", not a guess.
_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_span", default=None)


async def _ensure_db(db_path: str) -> None:
    if db_path not in _initialized_dbs:
        await init_db(db_path)
        _initialized_dbs.add(db_path)


def _registration_status(address: str) -> Optional[bool]:
    """Best-effort local registration check; None if it can't be determined.

    `dispatcher.contains` only tells us whether `address` is being served by
    this process (e.g. another agent on the same Bureau). It can't see remote
    Almanac/mailbox registration, so a False here just means "not local" --
    callers should not treat it as "definitely unregistered" unless paired
    with a delivery failure that says so (see `_apply_send_result`).
    """
    try:
        from uagents.dispatch import dispatcher

        return dispatcher.contains(address)
    except Exception:
        return None


def _payload_size(message: Model) -> int:
    try:
        return len(message.model_dump_json().encode("utf-8"))
    except Exception:
        return 0


def payload_summary(message: Model) -> str:
    """Human-readable message body for live display (no truncation)."""
    try:
        text_fn = getattr(message, "text", None)
        if callable(text_fn):
            return str(text_fn())
    except Exception:
        pass

    for field in ("text", "message", "content", "body", "query", "reason"):
        try:
            value = getattr(message, field, None)
            if value is not None and value != "":
                if isinstance(value, (list, dict)):
                    return json.dumps(value, separators=(",", ":"))
                return str(value)
        except Exception:
            continue

    try:
        data = message.model_dump()
        if data:
            return json.dumps(data, separators=(",", ":"))
    except Exception:
        pass
    return type(message).__name__


async def _apply_send_result(db_path: str, span_id: str, dest: str, result: Any, dest_registered: Optional[bool]) -> None:
    status = getattr(result, "status", None)
    status_value = getattr(status, "value", status)
    detail = getattr(result, "detail", None)

    if status_value == "failed":
        detail_lower = (detail or "").lower()
        is_timeout = "timeout" in detail_lower
        unresolved = "resolve" in detail_lower
        await update_span(
            db_path,
            span_id,
            state="timeout" if is_timeout else "dropped",
            acked_at=now_ms(),
            error=detail,
            dest_registered=False if unresolved else dest_registered,
        )
    else:
        await update_span(db_path, span_id, state="delivered", acked_at=now_ms())


async def traced_send(
    ctx: Context,
    destination: str,
    message: Model,
    *,
    protocol: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    db_path: Optional[str] = None,
) -> Any:
    """Record a span for this send, then delegate to `ctx.send`.

    Usage: `await traced_send(ctx, dest, msg)` in place of
    `await ctx.send(dest, msg)`.

    The recorded span's `parent_span_id` is whatever `trace` last set
    `_current_span` to in this asyncio task -- the receive span of the
    handler that's dispatching this send, if any (`None` outside a traced
    handler, e.g. a scenario client's opening message).
    """
    db_path = db_path or default_db_path()
    await _ensure_db(db_path)

    span_id = str(uuid.uuid4())
    source = ctx.agent.address
    dest_registered = _registration_status(destination)
    detected_protocol, detail = classify_message(message)

    await insert_span(
        db_path,
        {
            "id": span_id,
            "trace_id": str(ctx.session),
            "source_agent": source,
            "dest_agent": destination,
            "protocol": protocol or detected_protocol,
            "payload_type": type(message).__name__,
            "payload_size": _payload_size(message),
            "enqueued_at": now_ms(),
            "acked_at": None,
            "state": "pending",
            "source_registered": _registration_status(source),
            "dest_registered": dest_registered,
            "error": None,
            "session_id": str(ctx.session),
            "detail": detail,
            "payload_summary": payload_summary(message),
            "direction": "send",
            "parent_span_id": _current_span.get(),
        },
    )

    try:
        result = await asyncio.wait_for(ctx.send(destination, message, timeout=timeout), timeout=timeout)
    except asyncio.TimeoutError:
        await update_span(
            db_path,
            span_id,
            state="timeout",
            acked_at=now_ms(),
            error=f"No ack within {timeout}s",
        )
        return None
    except Exception as exc:
        await update_span(db_path, span_id, state="dropped", acked_at=now_ms(), error=str(exc))
        raise

    await _apply_send_result(db_path, span_id, destination, result, dest_registered)
    return result


def trace(handler: F) -> F:
    """Decorator that records a span for each message a handler receives.

    The span covers handler execution time (receipt to handler return), so
    it surfaces processing latency distinct from the send-side latency that
    `traced_send` records on the other end of the same trace.

    Decorator order matters: `agent.on_message(...)` registers whichever
    function sits directly beneath it at decoration time, so `trace` must be
    the *inner* decorator to actually be invoked on dispatch:

        @agent.on_message(model=MyModel)
        @trace
        async def handler(ctx, sender, msg): ...

    Placing `@trace` above `@agent.on_message(...)` registers the
    untraced handler and silently no-ops the wrapper, since uAgents captures
    the raw function by reference before `trace` ever sees it.

    Sets `_current_span` to this receive span's id for the duration of the
    handler call, so any `traced_send` made from inside `handler` -- or from
    an `asyncio.create_task` spawned inside it, which copies the current
    context at creation time -- records this span as its cause (see
    `traced_send`).
    """

    @functools.wraps(handler)
    async def wrapper(ctx: Context, sender: str, msg: Model, *args: Any, **kwargs: Any) -> Any:
        db_path = default_db_path()
        await _ensure_db(db_path)

        span_id = str(uuid.uuid4())
        dest = ctx.agent.address
        detected_protocol, detail = classify_message(msg)

        await insert_span(
            db_path,
            {
                "id": span_id,
                "trace_id": str(ctx.session),
                "source_agent": sender,
                "dest_agent": dest,
                "protocol": detected_protocol,
                "payload_type": type(msg).__name__,
                "payload_size": _payload_size(msg),
                "enqueued_at": now_ms(),
                "acked_at": None,
                "state": "pending",
                "source_registered": _registration_status(sender),
                "dest_registered": _registration_status(dest),
                "error": None,
                "session_id": str(ctx.session),
                "detail": detail,
                "payload_summary": payload_summary(msg),
                "direction": "receive",
            },
        )

        token = _current_span.set(span_id)
        try:
            try:
                result = await handler(ctx, sender, msg, *args, **kwargs)
            except Exception as exc:
                await update_span(db_path, span_id, state="dropped", acked_at=now_ms(), error=str(exc))
                raise
        finally:
            _current_span.reset(token)

        await update_span(db_path, span_id, state="delivered", acked_at=now_ms())
        return result

    return wrapper  # type: ignore[return-value]
