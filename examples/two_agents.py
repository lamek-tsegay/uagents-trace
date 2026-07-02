"""Two local agents passing messages, fully instrumented with uagents-trace.

Agent A pings Agent B every few seconds via `traced_send`; B replies via
`traced_send` too. Both sides also use the `@trace` decorator on their
message handlers, so every hop of the round trip gets a span.

Every third ping is sent to a address that was never registered with this
Bureau, to produce an intentionally broken span that the UI should flag as
dropped.

Run this, then in another terminal:
    python -m uagents_trace.server
and open http://localhost:8675.
"""

from uagents import Agent, Bureau, Context, Model
from uagents.crypto import Identity

from uagents_trace import trace, traced_send


class Ping(Model):
    text: str
    seq: int


class Pong(Model):
    text: str
    seq: int


agent_a = Agent(name="agent_a", seed="uagents_trace_demo_agent_a_seed")
agent_b = Agent(name="agent_b", seed="uagents_trace_demo_agent_b_seed")

# A well-formed but unreachable address for the intentionally broken send
# below. Deriving it from an `Identity` rather than a live `Agent` matters:
# constructing an `Agent` registers its address with uAgents' process-wide
# dispatcher immediately (regardless of whether it's ever added to a Bureau
# or run), which would make it resolve as a local delivery instead of failing.
BROKEN_DESTINATION = Identity.from_seed("uagents_trace_demo_ghost_seed", 0).address

_seq = {"n": 0}


@agent_a.on_interval(period=4.0)
async def send_ping(ctx: Context):
    _seq["n"] += 1
    seq = _seq["n"]

    if seq % 3 == 0:
        ctx.logger.info(f"sending ping #{seq} to unregistered ghost address (expect failure)")
        await traced_send(ctx, BROKEN_DESTINATION, Ping(text="ping-to-nowhere", seq=seq))
        return

    ctx.logger.info(f"sending ping #{seq} to agent_b")
    await traced_send(ctx, agent_b.address, Ping(text="ping", seq=seq))


@agent_b.on_message(model=Ping)
@trace
async def handle_ping(ctx: Context, sender: str, msg: Ping):
    ctx.logger.info(f"agent_b received '{msg.text}' #{msg.seq} from {sender}")
    await traced_send(ctx, sender, Pong(text="pong", seq=msg.seq))


@agent_a.on_message(model=Pong)
@trace
async def handle_pong(ctx: Context, sender: str, msg: Pong):
    ctx.logger.info(f"agent_a received '{msg.text}' #{msg.seq} from {sender}")


# No `endpoint` is passed, so the Bureau skips Almanac/ledger registration
# entirely (uagents only schedules that loop when an agent has endpoints) --
# this keeps the example fully local and network-free.
bureau = Bureau(agents=[agent_a, agent_b], port=8001)


if __name__ == "__main__":
    print(f"agent_a address: {agent_a.address}")
    print(f"agent_b address: {agent_b.address}")
    print(f"unregistered ghost address used for the broken send: {BROKEN_DESTINATION}")
    print("Spans are written to ./uagents_trace.db (override with UAGENTS_TRACE_DB)")
    print("Run `python -m uagents_trace.server` in another terminal, then open http://localhost:8675")
    # `bureau.run()` reuses the event loop bound at construction time; wrapping
    # `bureau.run_async()` in a fresh `asyncio.run()` here would bind the
    # agents' internal tasks to a second, different loop and deadlock.
    bureau.run()
