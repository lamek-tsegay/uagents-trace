"""Two agents talking over the real uAgents Chat Protocol -- the same
ChatMessage/ChatAcknowledgement models ASI:One uses to thread a
conversation -- instead of two_agents.py's plain custom Ping/Pong models.

Exercises uagents-trace's protocol recognition: spans for these messages
should show up labeled "Chat Protocol" rather than just the bare class name,
and grouped by session (uAgents' `ctx.session`, which is how ASI:One threads
a single conversation across turns).

Run this, then:
    uagents-trace show <trace_id>
"""

from uagents import Agent, Bureau, Context
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from uagents_trace import trace, traced_send

user_proxy = Agent(name="user_proxy", seed="uagents_trace_demo_user_proxy_seed")
assistant = Agent(name="assistant", seed="uagents_trace_demo_assistant_seed")

_turns = ["What's the weather in Lisbon?", "And tomorrow?", "Thanks!"]
_seq = {"n": 0}


@user_proxy.on_interval(period=5.0)
async def send_turn(ctx: Context):
    n = _seq["n"]
    if n >= len(_turns):
        return
    _seq["n"] += 1

    message = ChatMessage(content=[TextContent(text=_turns[n])])
    ctx.logger.info(f"user_proxy: \"{_turns[n]}\"")
    await traced_send(ctx, assistant.address, message)


@assistant.on_message(model=ChatMessage)
@trace
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"assistant received: \"{msg.text()}\"")
    await traced_send(ctx, sender, ChatAcknowledgement(timestamp=msg.timestamp, acknowledged_msg_id=msg.msg_id))


@user_proxy.on_message(model=ChatAcknowledgement)
@trace
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"user_proxy: ack received for {msg.acknowledged_msg_id}")


bureau = Bureau(agents=[user_proxy, assistant], port=8003)


if __name__ == "__main__":
    print(f"user_proxy address: {user_proxy.address}")
    print(f"assistant address: {assistant.address}")
    print("Spans are written to ./uagents_trace.db (override with UAGENTS_TRACE_DB)")
    bureau.run()
