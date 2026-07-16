"""Three dummy agents for testing trace-uagents.

Alice fans out to Bob and John every 5 seconds (hub-shaped trace).
Both reply back to Alice.
"""

import os
import socket
import sys

from uagents import Agent, Bureau, Context, Model
from uagents_trace import trace, traced_send

DEFAULT_PORT = int(os.environ.get("UAGENTS_BUREAU_PORT", "8010"))


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _resolve_port(requested: int) -> int:
    if not _port_in_use(requested):
        return requested
    for alt in range(requested + 1, requested + 10):
        if not _port_in_use(alt):
            print(f"Port {requested} is already in use — using {alt} instead.")
            print("Tip: stop the other agent with Ctrl+C in its terminal.")
            return alt
    print(f"ERROR: ports {requested}–{requested + 9} are all in use.", file=sys.stderr)
    print("Another my_test_agents.py is probably still running.", file=sys.stderr)
    print("Stop it with Ctrl+C in that terminal, then try again.", file=sys.stderr)
    sys.exit(1)


class Hello(Model):
    text: str
    count: int


class Reply(Model):
    text: str
    count: int


alice = Agent(name="alice", seed="my_test_alice_seed_123")
bob = Agent(name="bob", seed="my_test_bob_seed_456")
john = Agent(name="john", seed="my_test_john_seed_789")


@alice.on_interval(period=5.0)
async def alice_fanout(ctx: Context):
    count = getattr(alice_fanout, "_count", 0) + 1
    alice_fanout._count = count

    ctx.logger.info(f"Alice sending to Bob and John #{count}")
    await traced_send(ctx, bob.address, Hello(text="Hi Bob!", count=count))
    await traced_send(ctx, john.address, Hello(text="Hi John!", count=count))


@bob.on_message(model=Hello)
@trace
async def bob_handles_hello(ctx: Context, sender: str, msg: Hello):
    ctx.logger.info(f"Bob got: {msg.text} (#{msg.count})")
    await traced_send(ctx, sender, Reply(text="Hi Alice, from Bob!", count=msg.count))


@john.on_message(model=Hello)
@trace
async def john_handles_hello(ctx: Context, sender: str, msg: Hello):
    ctx.logger.info(f"John got: {msg.text} (#{msg.count})")
    await traced_send(ctx, sender, Reply(text="Hi Alice, from John!", count=msg.count))


@alice.on_message(model=Reply)
@trace
async def alice_handles_reply(ctx: Context, sender: str, msg: Reply):
    ctx.logger.info(f"Alice got reply: {msg.text} (#{msg.count})")


_bureau_port = _resolve_port(DEFAULT_PORT)
bureau = Bureau(agents=[alice, bob, john], port=_bureau_port)

if __name__ == "__main__":
    print(f"Alice: {alice.address}")
    print(f"Bob:   {bob.address}")
    print(f"John:  {john.address}")
    print(f"Bureau port: {_bureau_port}")
    print("Traces go to: ./uagents_trace.db")
    print("Open another terminal and run: uagents-trace")
    print()
    print("Wizard setup:")
    print("  Orchestrator: my_test_alice_seed_123")
    print("  SubAgent1:    my_test_bob_seed_456")
    print("  SubAgent2:    my_test_john_seed_789")
    bureau.run()
