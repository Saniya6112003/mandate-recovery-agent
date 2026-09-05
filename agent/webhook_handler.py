"""FastAPI route for Razorpay webhooks.

Only a signature-verified `payment_link.paid` event is allowed to flip a case
to `outcome: recovered` in the audit log — never the API-call success of
creating the link itself (project guide §0, §6). This is what makes the
"₹ recovered" number in the metrics report defensible rather than
self-reported.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import APIRouter, Header, HTTPException, Request

from agent.audit_log import mark_recovered

router = APIRouter()


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
):
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="RAZORPAY_WEBHOOK_SECRET not configured")

    body = await request.body()
    if not x_razorpay_signature or not _verify_signature(body, x_razorpay_signature, secret):
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    payload = await request.json()
    event = payload.get("event")

    if event == "payment_link.paid":
        entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        mandate_id = _resolve_mandate_id(entity)
        payment_link_id = entity.get("id")
        if mandate_id:
            # Idempotent: a duplicate webhook delivery for an already-recovered
            # mandate is a no-op, not a double-count (project guide §6).
            updated = mark_recovered(mandate_id=mandate_id, payment_link_id=payment_link_id)
            return {"status": "ok", "mandate_id": mandate_id, "updated": updated}

    return {"status": "ignored", "event": event}


def _resolve_mandate_id(entity: dict) -> str | None:
    """Map a paid Payment Link back to the mandate it was recovering.

    `reference_id` is per-attempt (mandate id + attempt + nonce), so the
    mandate id is read from `notes` where the executor puts it explicitly,
    falling back to the reference's prefix.
    """
    notes = entity.get("notes") or {}
    if notes.get("mandate_id"):
        return notes["mandate_id"]

    reference_id = entity.get("reference_id") or ""
    return reference_id.split("-a")[0] or None
