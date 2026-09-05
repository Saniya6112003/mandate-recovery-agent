"""Deterministic compliance guardrails for UPI Autopay mandate recovery actions.

These rules are plain Python, not model output — they run *after* the AI
reasoning agent and can override its recommendation. See project guide §5.
Every override must produce a human-readable reason, since that reason is
what lands in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from agent.schemas import AIDecision, GuardrailVerdict, MandateEvent

MAX_RETRY_ATTEMPTS = 3
NOTICE_WINDOW_HOURS = 24

# Per customer, per day. Deliberately conservative for a recovery agent —
# these bound worst-case exposure regardless of what the AI recommends.
DAILY_EXPOSURE_CAP_INR = 5000.0
DAILY_ATTEMPT_CAP = 3


@dataclass
class GuardrailContext:
    """Rolling state the guardrail layer needs beyond the single event.

    In production this would be backed by a real per-customer ledger; for the
    demo batch it's accumulated in-memory by the pipeline as it processes
    events in order, so the exposure cap is enforced across a batch, not just
    within a single case.
    """

    customer_amount_today: float = 0.0
    customer_attempts_today: int = 0


def _check_mandate_state(event: MandateEvent, decision: AIDecision) -> str | None:
    """A revoked or paused mandate cannot be debited, however confident the AI is.

    This reads `mandate_status` — what an authoritative bank/NPCI mandate
    lookup returns — not the AI's diagnosis. That independence is the point:
    the guardrail still catches the unsafe retry even when the AI misdiagnosed
    the failure entirely.
    """
    if event.mandate_status != "active" and decision.recommended_action in (
        "retry_now",
        "retry_scheduled",
    ):
        return (
            f"mandate_{event.mandate_status}: a {event.mandate_status} mandate cannot be "
            "debited, so retry is not technically possible; routing to "
            "notify_customer/escalate_human instead"
        )
    return None


def _check_retry_cap(event: MandateEvent, decision: AIDecision) -> str | None:
    if decision.recommended_action in ("retry_now", "retry_scheduled") and (
        event.retry_count >= MAX_RETRY_ATTEMPTS
    ):
        return (
            f"retry_cap_exceeded: {event.retry_count} attempts already made, "
            f"max is {MAX_RETRY_ATTEMPTS}"
        )
    return None


def _check_notice_window(event: MandateEvent, decision: AIDecision) -> str | None:
    """RBI's 24-hour pre-debit notice must be satisfied before any retry executes."""
    if decision.recommended_action != "retry_now":
        return None
    if event.notice_sent_at is None:
        return "notice_window_unsatisfied: no pre-debit notice on record"

    elapsed = event.failure_timestamp - event.notice_sent_at
    if elapsed < timedelta(hours=NOTICE_WINDOW_HOURS):
        hours = elapsed.total_seconds() / 3600
        return (
            f"notice_window_unsatisfied: only {hours:.1f}h since notice, "
            f"{NOTICE_WINDOW_HOURS}h required before retry_now"
        )
    return None


def _check_exposure_cap(
    event: MandateEvent, decision: AIDecision, context: GuardrailContext
) -> str | None:
    if decision.recommended_action not in ("retry_now", "retry_scheduled"):
        return None

    if context.customer_attempts_today + 1 > DAILY_ATTEMPT_CAP:
        return (
            f"daily_attempt_cap_exceeded: customer already has "
            f"{context.customer_attempts_today} attempts today, cap is {DAILY_ATTEMPT_CAP}"
        )

    projected = context.customer_amount_today + event.amount
    if projected > DAILY_EXPOSURE_CAP_INR:
        return (
            f"daily_exposure_cap_exceeded: retrying ₹{event.amount:,.0f} would take this "
            f"customer's same-day retry exposure to ₹{projected:,.0f}, above the "
            f"₹{DAILY_EXPOSURE_CAP_INR:,.0f} cap"
        )
    return None


# Order matters only for which reason surfaces first when several rules would
# fire — mandate state is checked first since it's an absolute, non-negotiable
# block rather than a threshold.
_RULES = [
    _check_mandate_state,
    _check_retry_cap,
    _check_notice_window,
]


def _fallback_action(event: MandateEvent, decision: AIDecision) -> str:
    """What actually happens when a guardrail blocks the AI's recommendation."""
    if event.mandate_status != "active":
        return "escalate_human" if decision.confidence < 0.6 else "notify_customer"
    return "notify_customer"


def apply_guardrails(
    event: MandateEvent,
    decision: AIDecision,
    context: GuardrailContext | None = None,
) -> tuple[GuardrailVerdict, str]:
    """Check an AI decision against deterministic rules.

    Returns the guardrail verdict (for the audit log) and the actual action
    that should execute — which is `decision.recommended_action` when allowed,
    or a safe fallback when a rule blocks it.
    """
    context = context or GuardrailContext()

    for rule in _RULES:
        reason = rule(event, decision)
        if reason:
            return GuardrailVerdict(allowed=False, overridden_reason=reason), _fallback_action(
                event, decision
            )

    reason = _check_exposure_cap(event, decision, context)
    if reason:
        return GuardrailVerdict(allowed=False, overridden_reason=reason), "escalate_human"

    return GuardrailVerdict(allowed=True, overridden_reason=None), decision.recommended_action
