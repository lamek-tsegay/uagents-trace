"""Recognize known uAgents protocols (Chat Protocol, Payment Protocol) by
message class identity, purely to label and group spans more usefully in
the trace views. This never changes what gets recorded -- it only fills in
`protocol`/`detail` on a span already being written, and helps trace
renderers pick a more specific view than the generic flat/peer/hub ones.

Recognition is by `isinstance` against the real model classes from
`uagents_core`, not by string-matching arbitrary class names -- so a user's
own `Task`/`Result` models are never mistaken for these.
"""

from typing import Any, Optional

try:
    from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage
except ImportError:  # older uagents_core without the chat protocol contrib module
    ChatMessage = ChatAcknowledgement = None

try:
    from uagents_core.contrib.protocols.payment import (
        CancelPayment,
        CommitPayment,
        CompletePayment,
        RejectPayment,
        RequestPayment,
    )
except ImportError:
    RequestPayment = CommitPayment = CompletePayment = RejectPayment = CancelPayment = None

CHAT_PROTOCOL = "Chat Protocol"
PAYMENT_PROTOCOL = "Payment Protocol"

_PAYMENT_OUTCOME = {
    "CompletePayment": "ok",
    "RejectPayment": "failed",
    "CancelPayment": "failed",
}


def _funds_label(funds: Any) -> Optional[str]:
    if funds is None:
        return None
    return f"{funds.amount} {funds.currency}"


def classify_message(message: Any) -> tuple[Optional[str], Optional[str]]:
    """(protocol_label, detail) for a message instance, or (None, None) if
    it's not a recognized protocol message.
    """
    if ChatMessage is not None and isinstance(message, (ChatMessage, ChatAcknowledgement)):
        return CHAT_PROTOCOL, None

    if RequestPayment is not None and isinstance(message, RequestPayment):
        funds = message.accepted_funds[0] if message.accepted_funds else None
        return PAYMENT_PROTOCOL, _funds_label(funds)

    if CommitPayment is not None and isinstance(message, CommitPayment):
        return PAYMENT_PROTOCOL, _funds_label(message.funds)

    if CompletePayment is not None and isinstance(message, CompletePayment):
        return PAYMENT_PROTOCOL, "verified"

    if RejectPayment is not None and isinstance(message, RejectPayment):
        return PAYMENT_PROTOCOL, f"rejected: {message.reason or 'no reason given'}"

    if CancelPayment is not None and isinstance(message, CancelPayment):
        return PAYMENT_PROTOCOL, f"canceled: {message.reason or 'no reason given'}"

    return None, None


def is_payment_trace(spans: list[dict[str, Any]]) -> bool:
    return any(s.get("protocol") == PAYMENT_PROTOCOL for s in spans)


def build_payment_steps(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per payment hop, chronological, with a render-agnostic
    outcome ("ok" | "failed" | None) so callers can color it:

        {"source": addr, "dest": addr, "payload_type": str, "detail": str | None, "outcome": str | None}

    Each hop is recorded as two spans -- `traced_send`'s send-side span and
    the receiving end's `@trace`-decorated receive-side span -- which carry
    the same (source, dest, payload_type) since they describe the same
    logical event. Deduplicated on that key, keeping the earlier span, so
    a payment step appears once rather than twice.
    """
    payment_spans = sorted(
        (s for s in spans if s.get("protocol") == PAYMENT_PROTOCOL),
        key=lambda s: s["enqueued_at"],
    )
    seen: set[tuple[str, str, str]] = set()
    steps = []
    for s in payment_spans:
        key = (s["source_agent"], s["dest_agent"], s["payload_type"])
        if key in seen:
            continue
        seen.add(key)
        steps.append(
            {
                "source": s["source_agent"],
                "dest": s["dest_agent"],
                "payload_type": s["payload_type"],
                "detail": s.get("detail"),
                "outcome": _PAYMENT_OUTCOME.get(s["payload_type"]),
            }
        )
    return steps
