"""Offline stand-in for the LLM reasoning agent.

This exists so the rest of the pipeline — guardrails, executor, webhook,
audit log, metrics — can be developed, tested and demoed without an API key
or any spend. It is a DEVELOPMENT FIXTURE, not the product:

  * Every decision it returns is tagged `[OFFLINE STUB]` in the reasoning
    text, so stub output can never be mistaken for real agent output in the
    audit log or the CSV export.
  * Numbers produced in this mode are NOT valid pitch metrics. Run the real
    agent (`REASONING_PROVIDER=anthropic`) for anything you report.

It deliberately imitates a plausible failure profile rather than being an
oracle: it's confident on the well-known codes, hesitant on the obscure ones,
and it sometimes misreads a mandate-state code as a funds problem — which is
exactly the case the guardrail layer has to catch.
"""

from __future__ import annotations

from hashlib import sha256

from agent.schemas import AIDecision, MandateEvent

STUB_MARKER = "[OFFLINE STUB]"

# The codes a competent analyst (or a model that knows the UPI code book)
# would read confidently.
WELL_KNOWN = {
    "Z9": "insufficient_funds",
    "U67": "bank_timeout",
    "UT": "bank_timeout",
    "U28": "bank_timeout",
    "Z8": "daily_limit_exceeded",
    "Z7": "daily_limit_exceeded",
    "ZU": "daily_limit_exceeded",
    "M2": "daily_limit_exceeded",
    "ZM": "authentication_failure",
    "Z6": "authentication_failure",
    "AM": "authentication_failure",
    "VA": "mandate_revoked",
}

# Real but rarely-seen codes. A model that hasn't memorised the NPCI list can
# plausibly get these wrong, so the stub does too — that's what gives the
# guardrail layer something real to catch.
OBSCURE = {
    "IE": "insufficient_funds",
    "XY": "bank_timeout",
    "IR": "bank_timeout",
    "QA": "mandate_revoked",
    "MA0": "mandate_revoked",
}

# Codes the bank returns without saying why — genuinely unresolvable.
AMBIGUOUS = {"ZA", "U30", "B3"}


def _jitter(event: MandateEvent, salt: str) -> float:
    """Deterministic pseudo-randomness, so a batch replays identically."""
    digest = sha256(f"{event.mandate_id}:{salt}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _action_for(cause: str, event: MandateEvent) -> tuple[str, str]:
    """Pick an intervention and a channel from the diagnosed cause."""
    if cause == "insufficient_funds":
        return ("retry_scheduled" if event.retry_count >= 1 else "retry_now"), "sms"
    if cause == "bank_timeout":
        return "retry_now", "none"
    if cause == "daily_limit_exceeded":
        return "retry_scheduled", "whatsapp"
    if cause == "authentication_failure":
        return "notify_customer", "whatsapp"
    if cause == "mandate_revoked":
        return "notify_customer", "email"
    return "escalate_human", "none"


def diagnose(event: MandateEvent) -> AIDecision:
    code = event.bank_response_code

    if code in WELL_KNOWN:
        cause = WELL_KNOWN[code]
        confidence = 0.82 + _jitter(event, "conf") * 0.16
        note = f"response code {code} maps cleanly to {cause}"

    elif code in OBSCURE:
        # Misread roughly a third of these, the way a model unfamiliar with
        # the rarer NPCI codes would.
        if _jitter(event, "misread") < 0.34:
            cause = "insufficient_funds"
            confidence = 0.61 + _jitter(event, "conf") * 0.24
            note = (
                f"uncommon code {code}; read as a funds problem given the debit "
                "failed at the remitter side"
            )
        else:
            cause = OBSCURE[code]
            confidence = 0.55 + _jitter(event, "conf") * 0.20
            note = f"uncommon code {code}, best match is {cause}"

    elif code in AMBIGUOUS:
        cause = "unknown"
        confidence = 0.30 + _jitter(event, "conf") * 0.22
        note = f"code {code} carries no stated reason from the bank"

    else:
        cause = "unknown"
        confidence = 0.35
        note = f"unrecognised code {code}"

    action, channel = _action_for(cause, event)

    return AIDecision(
        likely_cause=cause,
        confidence=round(confidence, 3),
        reasoning=(
            f"{STUB_MARKER} {note}; retry_count={event.retry_count}, "
            f"amount=₹{event.amount:,.0f}, mandate_type={event.mandate_type}."
        ),
        recommended_action=action,
        recommended_channel=channel,
        recommended_retry_window=None,
    )
