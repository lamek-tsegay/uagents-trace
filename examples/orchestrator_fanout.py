"""An orchestrator dispatching to four subagents -- a "hub" shaped trace
(one source fanning out to several destinations in a single logical flow),
as opposed to two_agents.py's simple two-party ping/pong.

Subagents reply with staggered delay so the four legs have visibly
different latencies. The fourth subagent's address is never actually run,
so its leg fails -- exercising the hub renderer's failed-leg case.

Run this, then in another terminal:
    uagents-trace show <trace_id>      # or `uagents-trace tui` and press Enter
"""

import asyncio

from uagents import Agent, Bureau, Context, Model
from uagents.crypto import Identity

from uagents_trace import trace, traced_send


class Task(Model):
    job: str


class Result(Model):
    job: str


orchestrator = Agent(name="orchestrator", seed="uagents_trace_demo_orchestrator_seed")
sub1 = Agent(name="sub1", seed="uagents_trace_demo_sub1_seed")
sub2 = Agent(name="sub2", seed="uagents_trace_demo_sub2_seed")
sub3 = Agent(name="sub3", seed="uagents_trace_demo_sub3_seed")

# sub4 is never constructed as a live `Agent` or added to the Bureau, so this
# address is well-formed but unreachable -- the dispatch to it will fail,
# producing the intentionally broken leg in the hub view. Derived from an
# `Identity` rather than a live `Agent` for the same reason as
# `two_agents.py`'s ghost address: constructing an `Agent` registers it with
# the process-wide dispatcher immediately, which would make it resolve as a
# local delivery instead of failing.
SUB4_ADDRESS = Identity.from_seed("uagents_trace_demo_sub4_seed", 0).address

SUBAGENT_DELAYS = {
    sub1.address: 0.05,
    sub2.address: 0.4,
    sub3.address: 1.1,
}


@orchestrator.on_interval(period=6.0)
async def dispatch(ctx: Context):
    ctx.logger.info("dispatching a task to all 4 subagents")
    for i, dest in enumerate([sub1.address, sub2.address, sub3.address, SUB4_ADDRESS], start=1):
        await traced_send(ctx, dest, Task(job=f"job-{i}"))


def _make_subagent_handler(delay: float):
    async def handle_task(ctx: Context, sender: str, msg: Task):
        await asyncio.sleep(delay)
        await traced_send(ctx, sender, Result(job=msg.job))

    return trace(handle_task)


sub1.on_message(model=Task)(_make_subagent_handler(SUBAGENT_DELAYS[sub1.address]))
sub2.on_message(model=Task)(_make_subagent_handler(SUBAGENT_DELAYS[sub2.address]))
sub3.on_message(model=Task)(_make_subagent_handler(SUBAGENT_DELAYS[sub3.address]))


@orchestrator.on_message(model=Result)
@trace
async def handle_result(ctx: Context, sender: str, msg: Result):
    ctx.logger.info(f"got result for {msg.job} from {sender}")


bureau = Bureau(agents=[orchestrator, sub1, sub2, sub3], port=8002)


if __name__ == "__main__":
    print(f"orchestrator address: {orchestrator.address}")
    print(f"sub1 address: {sub1.address}")
    print(f"sub2 address: {sub2.address}")
    print(f"sub3 address: {sub3.address}")
    print(f"unreachable sub4 address used to produce a failed leg: {SUB4_ADDRESS}")
    print("Spans are written to ./uagents_trace.db (override with UAGENTS_TRACE_DB)")
    bureau.run()
