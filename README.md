# trace-uagents

Drop-in, read-only observability for [uAgents](https://github.com/fetchai/uAgents) message
flow. It records spans for sends and receives to a local SQLite file and shows them live
in the terminal as a diagram — so you can see what got delivered, what timed out, and what
got dropped.

It does not modify agent behavior, generate code, or scaffold anything. It only records
and displays.

## Install

```bash
pip install -e .
```

## Quick start (2 steps)

**1. Instrument your agents** — wrap sends and handlers:

```python
from uagents_trace import trace, traced_send

await traced_send(ctx, dest, msg)   # instead of ctx.send

@agent.on_message(model=MyModel)
@trace                              # must be the inner decorator
async def handler(ctx, sender, msg): ...
```

**2. Watch messages live** — in another terminal:

```bash
uagents-trace
```

You'll be asked a few questions (how many agents, their seeds, friendly names like
"Orchestrator" or "Alice") — no long commands to type. Then a live diagram opens showing
messages as they flow between agents, with the full message text.

Press `q` to quit the live view.

## Run the example

**Terminal 1** — start demo agents:

```bash
python examples/two_agents.py
```

**Terminal 2** — run the interactive viewer:

```bash
uagents-trace
```

Answer the setup questions using the demo seeds (`uagents_trace_demo_agent_a_seed`,
`uagents_trace_demo_agent_b_seed`) and names like `AgentA` / `AgentB`.

## Instrumentation details

Decorator order matters: `agent.on_message(...)` captures whichever function sits directly
beneath it at decoration time, so `trace` must be the *inner* decorator:

```python
@agent.on_message(model=MyModel)
@trace
async def handler(ctx, sender, msg): ...
```

Each call records a span, then updates it to `delivered`, `dropped`, or `timeout` once
the outcome is known. Spans are correlated into a trace using the uAgents session id.

## Advanced commands

For power users, these still work:

```bash
uagents-trace list              # table of recent traces
uagents-trace show              # ASCII waterfall for latest trace
uagents-trace alias add Name --seed my_seed
python -m uagents_trace.server  # web UI at http://localhost:8675
```

## Configuration

- `UAGENTS_TRACE_DB` — path to the SQLite file. Defaults to `./uagents_trace.db`. Set
  the same value for the process running your agent(s) and the viewer.
- `traced_send(..., timeout=10)` — seconds to wait for an ack before marking a span as
  `timeout`. Defaults to 10.

## Span model

Each send/receive produces one row in the `spans` table: agents, payload type, full message
summary, direction (send/receive), timing, and outcome state.

## Non-goals

No auth, no remote/hosted mode, no Almanac/mailbox mocking, no code generation. SQLite
only.
