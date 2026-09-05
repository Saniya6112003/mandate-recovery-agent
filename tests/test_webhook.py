from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import agent.audit_log as audit_log
from agent.audit_log import AuditLogEntry
from agent.schemas import AIDecision, GuardrailVerdict

SECRET = "test_webhook_secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _seed_pending_entry(mandate_id: str) -> None:
    entry = AuditLogEntry(
        mandate_id=mandate_id,
        input_signal={"amount_inr": 999.0},
        ai_output=AIDecision(
            likely_cause="insufficient_funds",
            confidence=0.8,
            reasoning="seed",
            recommended_action="notify_customer",
            recommended_channel="sms",
            recommended_retry_window=None,
        ),
        guardrail_verdict=GuardrailVerdict(allowed=True, overridden_reason=None),
        action_taken="notify_customer",
        outcome="pending",
    )
    audit_log.append_entry(entry)


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_log, "LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    import app as app_module

    return TestClient(app_module.app)


def test_webhook_rejects_bad_signature(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = json.dumps({"event": "payment_link.paid"}).encode()

    resp = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": "not-the-real-signature"})

    assert resp.status_code == 400


def test_webhook_resolves_mandate_from_reference_id_prefix(monkeypatch, tmp_path):
    """Fallback path: no notes on the entity, so the mandate id must be
    recovered from the per-attempt reference_id prefix.
    """
    client = _client(monkeypatch, tmp_path)
    _seed_pending_entry("MIDWEBHOOK003")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_prefix1",
                    "reference_id": "MIDWEBHOOK003-a2-9f8e7d6c",
                }
            }
        },
    }
    body = json.dumps(payload).encode()

    resp = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": _sign(body)})

    assert resp.json()["mandate_id"] == "MIDWEBHOOK003"
    assert resp.json()["updated"] is True
    assert audit_log.load_log()[-1]["outcome"] == "recovered"


def test_webhook_marks_recovered_on_valid_signature(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_pending_entry("MIDWEBHOOK001")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_abc123",
                    "reference_id": "MIDWEBHOOK001-a0-1a2b3c4d",
                    "notes": {"mandate_id": "MIDWEBHOOK001"},
                }
            }
        },
    }
    body = json.dumps(payload).encode()
    signature = _sign(body)

    resp = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": signature})

    assert resp.status_code == 200
    assert resp.json()["updated"] is True

    logged = audit_log.load_log()
    assert logged[-1]["outcome"] == "recovered"


def test_webhook_duplicate_delivery_is_idempotent(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_pending_entry("MIDWEBHOOK002")

    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_xyz789",
                    "reference_id": "MIDWEBHOOK002-a1-5e6f7a8b",
                    "notes": {"mandate_id": "MIDWEBHOOK002"},
                }
            }
        },
    }
    body = json.dumps(payload).encode()
    signature = _sign(body)

    first = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": signature})
    second = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": signature})

    assert first.json()["updated"] is True
    assert second.json()["updated"] is False


def test_webhook_ignores_unrelated_events(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    payload = {"event": "payment.failed"}
    body = json.dumps(payload).encode()
    signature = _sign(body)

    resp = client.post("/webhook", content=body, headers={"X-Razorpay-Signature": signature})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
