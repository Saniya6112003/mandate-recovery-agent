from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from razorpay.errors import BadRequestError

from agent import executor
from agent.schemas import MandateEvent

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event() -> MandateEvent:
    return MandateEvent(
        mandate_id="MIDEXEC0001",
        customer_id="CUSTEXEC01",
        amount=499.0,
        bank_response_code="Z9",
        retry_count=0,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=60),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
    )


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """Guarantee tests never hit the live Razorpay API, even if the developer
    running them has real keys in their environment.
    """
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("RECOVERY_KILL_SWITCH", raising=False)
    monkeypatch.setattr(executor, "_client", None)
    yield


@pytest.mark.parametrize("action", ["retry_now", "retry_scheduled", "notify_customer"])
def test_kill_switch_blocks_all_external_actions(action, monkeypatch):
    monkeypatch.setenv("RECOVERY_KILL_SWITCH", "true")

    result = executor.execute_action(action, make_event())

    assert result["status"] == "kill_switch_engaged"


def test_kill_switch_off_by_default():
    assert executor.kill_switch_engaged() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_accepts_common_truthy_values(value, monkeypatch):
    monkeypatch.setenv("RECOVERY_KILL_SWITCH", value)
    assert executor.kill_switch_engaged() is True


def test_payment_link_falls_back_to_dry_run_without_credentials():
    result = executor.create_payment_link(make_event())

    assert result["status"] == "dry_run"
    assert result["id"].startswith("dryrun_")


def test_escalate_human_has_no_external_side_effect():
    result = executor.execute_action("escalate_human", make_event())

    assert result["status"] == "escalated"


def test_stop_has_no_external_side_effect():
    result = executor.execute_action("stop", make_event())

    assert result["status"] == "stopped"


class _ThrottledPaymentLink:
    """Fails with Razorpay's rate-limit error N times, then succeeds."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def create(self, payload):
        self.calls += 1
        if self.calls <= self.failures:
            raise BadRequestError("Too many requests")
        return {"id": "plink_ok", "short_url": "https://rzp.io/i/ok", "status": "created"}


def _throttled_client(monkeypatch, failures: int) -> _ThrottledPaymentLink:
    link = _ThrottledPaymentLink(failures)
    monkeypatch.setattr(executor, "_client", type("C", (), {"payment_link": link})())
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(executor.time, "sleep", lambda _: None)  # keep tests fast
    return link


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    """A 60-case batch gets throttled by Razorpay; without backoff those
    cases are silently lost from the recovery run.
    """
    link = _throttled_client(monkeypatch, failures=2)

    result = executor.create_payment_link(make_event())

    assert result["status"] == "created"
    assert link.calls == 3


def test_rate_limit_gives_up_and_records_the_failure(monkeypatch):
    """Persistent throttling must still produce an auditable entry, not a crash."""
    _throttled_client(monkeypatch, failures=99)

    result = executor.create_payment_link(make_event())

    assert result["status"] == "api_error"
    assert "too many requests" in result["note"].lower()


def test_non_rate_limit_errors_are_not_retried(monkeypatch):
    """Only throttling is retried — a genuine bad request must fail fast."""

    class AlwaysBadRequest:
        calls = 0

        def create(self, payload):
            AlwaysBadRequest.calls += 1
            raise BadRequestError("amount must be at least 100")

    monkeypatch.setattr(executor, "_client", type("C", (), {"payment_link": AlwaysBadRequest()})())
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(executor.time, "sleep", lambda _: None)

    result = executor.create_payment_link(make_event())

    assert result["status"] == "api_error"
    assert AlwaysBadRequest.calls == 1


def test_attempt_reference_id_is_unique_per_call():
    """Razorpay rejects duplicate reference_ids, and a mandate gets several
    recovery attempts — so two attempts on the same mandate must not collide.
    """
    event = make_event()

    first = executor.attempt_reference_id(event)
    second = executor.attempt_reference_id(event)

    assert first != second
    assert first.startswith(event.mandate_id)
    assert second.startswith(event.mandate_id)


def test_payment_link_carries_mandate_id_for_webhook_attribution(monkeypatch):
    """The webhook resolves a payment back to its mandate via notes.mandate_id,
    with the reference_id prefix as fallback. If either breaks, a recovery can
    never be attributed and `recovered` would never be set.
    """
    captured = {}

    class FakePaymentLink:
        def create(self, payload):
            captured.update(payload)
            return {"id": "plink_fake", "short_url": "https://rzp.io/i/fake"}

    class FakeClient:
        payment_link = FakePaymentLink()

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")
    monkeypatch.setattr(executor, "_client", FakeClient())

    event = make_event()
    executor.create_payment_link(event)

    assert captured["notes"]["mandate_id"] == event.mandate_id
    assert captured["reference_id"].startswith(event.mandate_id)
    assert captured["amount"] == int(event.amount * 100)  # paise, not rupees
    assert captured["currency"] == "INR"
