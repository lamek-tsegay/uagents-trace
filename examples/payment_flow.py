"""A pay-to-use agent flow over the real uAgents Payment Protocol: a buyer
requesting paid access to a seller's service, instead of a plain custom
Task/Result exchange.

Sequence: RequestPayment -> CommitPayment -> CompletePayment (buyer verifies
the commit locally -- no on-chain call here -- then confirms). Every third
request is for an amount the seller's demo policy rejects, producing a
RejectPayment leg instead, so the failure-outcome rendering has something
real to show.

Exercises trace-uagents's protocol recognition: a trace containing these
messages should render as a labeled "PAYMENT" sequence with the FET amount
and verify outcome, not generic Task/Result.

Run this, then:
    uagents-trace show <trace_id>
"""

import uuid

from uagents import Agent, Bureau, Context
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
)

from uagents_trace import trace, traced_send

buyer = Agent(name="buyer", seed="uagents_trace_demo_buyer_seed")
seller = Agent(name="seller", seed="uagents_trace_demo_seller_seed")

MAX_ACCEPTED_FET = 10.0
_seq = {"n": 0}


@buyer.on_interval(period=5.0)
async def request_access(ctx: Context):
    _seq["n"] += 1
    seq = _seq["n"]
    amount = "15.0" if seq % 3 == 0 else "5.0"  # every 3rd request exceeds the seller's policy

    ctx.logger.info(f"buyer: requesting paid access for {amount} FET (job-{seq})")
    await traced_send(
        ctx,
        seller.address,
        RequestPayment(
            accepted_funds=[Funds(amount=amount, currency="FET")],
            recipient=seller.address,
            deadline_seconds=30,
            reference=f"job-{seq}",
        ),
    )


@seller.on_message(model=RequestPayment)
@trace
async def handle_request(ctx: Context, sender: str, msg: RequestPayment):
    funds = msg.accepted_funds[0]
    if float(funds.amount) > MAX_ACCEPTED_FET:
        ctx.logger.info(f"seller: rejecting {funds.amount} {funds.currency} -- above policy max")
        await traced_send(ctx, sender, RejectPayment(reason=f"amount exceeds {MAX_ACCEPTED_FET} FET policy max"))
        return

    ctx.logger.info(f"seller: committing to {funds.amount} {funds.currency}")
    await traced_send(
        ctx,
        sender,
        CommitPayment(funds=funds, recipient=ctx.agent.address, transaction_id=str(uuid.uuid4()), reference=msg.reference),
    )


@buyer.on_message(model=CommitPayment)
@trace
async def handle_commit(ctx: Context, sender: str, msg: CommitPayment):
    # A real buyer would verify `msg.transaction_id` on-chain here. This demo
    # stays network-free, so it just confirms immediately.
    ctx.logger.info(f"buyer: verified commit {msg.transaction_id}, confirming")
    await traced_send(ctx, sender, CompletePayment(transaction_id=msg.transaction_id))


@seller.on_message(model=CompletePayment)
@trace
async def handle_complete(ctx: Context, sender: str, msg: CompletePayment):
    ctx.logger.info(f"seller: payment complete, tx {msg.transaction_id}")


@buyer.on_message(model=RejectPayment)
@trace
async def handle_reject(ctx: Context, sender: str, msg: RejectPayment):
    ctx.logger.info(f"buyer: request rejected -- {msg.reason}")


bureau = Bureau(agents=[buyer, seller], port=8004)


if __name__ == "__main__":
    print(f"buyer address: {buyer.address}")
    print(f"seller address: {seller.address}")
    print("Spans are written to ./uagents_trace.db (override with UAGENTS_TRACE_DB)")
    bureau.run()
