"""Action executor: turns a guardrail-approved action into something real.

For `notify_customer` this creates an actual Razorpay test-mode Payment Link
via the official SDK — not a mocked log line. Test mode means no real money
ever moves, but the API call, the link, and the later webhook are all real
(project guide §0, §3.1). If no API keys are configured (e.g. running the
batch offline), it falls back to a clearly-labelled dry-run so the pipeline
still works end to end.
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

import razorpay
from razorpay.errors import BadRequestError, GatewayError, ServerError

from agent.schemas import MandateEvent

# Razorpay returns rate limiting as a normal error response, not a distinct
# exception type, and the SDK's own retry only covers ConnectionError — so a
# batch of 40+ links gets throttled and silently loses cases without this.
# Defaults give up after ~30s of backoff (2+4+8+16), which covers a
# per-minute quota reset. Tunable for a large live batch.
RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("RAZORPAY_RETRY_ATTEMPTS", "5"))
RATE_LIMIT_BASE_DELAY_SECONDS = float(os.getenv("RAZORPAY_RETRY_BASE_DELAY", "2.0"))
_RATE_LIMIT_MARKERS = ("too many requests", "rate limit")

_client: "razorpay.Client | None" = None
_KILL_SWITCH_ENV = "RECOVERY_KILL_SWITCH"


def kill_switch_engaged() -> bool:
    """A single config flag that halts all real-world side effects.

    Checked by the pipeline before any action executes (project guide §3.2).
    """
    return os.getenv(_KILL_SWITCH_ENV, "false").lower() in ("1", "true", "yes", "on")


def _get_client() -> "razorpay.Client | None":
    global _client
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    if _client is None:
        _client = razorpay.Client(auth=(key_id, key_secret))
    return _client


def attempt_reference_id(event: MandateEvent) -> str:
    """A unique reference for THIS recovery attempt.

    Razorpay rejects a duplicate `reference_id`, and a mandate legitimately
    gets several recovery attempts over its life — so the mandate id alone
    cannot be the reference. The mandate id stays the prefix (and is also
    sent in `notes`) so the webhook can still resolve the attempt back to
    its mandate.
    """
    return f"{event.mandate_id}-a{event.retry_count}-{uuid4().hex[:8]}"


def _is_rate_limited(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


def _create_with_backoff(client: Any, payload: dict) -> dict[str, Any]:
    """Create a payment link, backing off when Razorpay throttles us."""
    delay = RATE_LIMIT_BASE_DELAY_SECONDS

    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            return client.payment_link.create(payload)
        except (BadRequestError, GatewayError, ServerError) as exc:
            is_last = attempt == RATE_LIMIT_MAX_ATTEMPTS - 1
            if not _is_rate_limited(exc) or is_last:
                raise
            time.sleep(delay)
            delay *= 2

    raise RuntimeError("unreachable")  # pragma: no cover


def create_payment_link(event: MandateEvent) -> dict[str, Any]:
    """Create a real Razorpay test-mode Payment Link for a customer nudge.

    Returns the Payment Link entity (or a dry-run stand-in with the same
    shape) so the caller can log `id` / `short_url` regardless of mode.
    """
    client = _get_client()
    if client is None:
        return {
            "id": f"dryrun_{event.mandate_id}",
            "short_url": None,
            "status": "dry_run",
            "note": "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set — simulated, no API call made",
        }

    amount_paise = int(round(event.amount * 100))
    try:
        return _create_with_backoff(
            client,
            {
                "amount": amount_paise,
                "currency": "INR",
                "description": f"UPI Autopay mandate retry — {event.mandate_id}",
                "reference_id": attempt_reference_id(event),
                "notes": {
                    "mandate_id": event.mandate_id,
                    "customer_id": event.customer_id,
                    "reason": "mandate_recovery_agent",
                },
            },
        )
    except (BadRequestError, GatewayError, ServerError) as exc:
        # A failed action is still an auditable event — record why rather
        # than dropping the case.
        return {
            "id": None,
            "short_url": None,
            "status": "api_error",
            "note": str(exc),
        }


def execute_action(action: str, event: MandateEvent) -> dict[str, Any]:
    """Dispatch a guardrail-approved action to its real-world effect.

    `retry_now` / `retry_scheduled` and `notify_customer` both route through
    the same Payment Link mechanism for this demo (a payment link *is* the
    retry surface for UPI Autopay recovery); `escalate_human` and `stop`
    have no external side effect.
    """
    if kill_switch_engaged():
        return {"status": "kill_switch_engaged", "detail": "no external action executed"}

    if action in ("retry_now", "retry_scheduled", "notify_customer"):
        return create_payment_link(event)

    if action == "escalate_human":
        return {"status": "escalated", "detail": "routed to human review queue"}

    return {"status": "stopped", "detail": "no action taken"}
