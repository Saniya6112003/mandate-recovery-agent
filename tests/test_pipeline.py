from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agent.audit_log as audit_log
import agent.pipeline as pipeline
from agent.guardrails import GuardrailContext
from agent.schemas import AIDecision, MandateEvent

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(**overrides) -> MandateEvent:
    defaults = dict(
        mandate_id="MID0000000001",
        customer_id="CUST000001",
        amount=999.0,
        bank_response_code="Z9",
        retry_count=0,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=90),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
        is_hard_case=False,
    )
    defaults.update(overrides)
    return MandateEvent(**defaults)


def make_decision(**overrides) -> AIDecision:
    defaults = dict(
        likely_cause="insufficient_funds",
        confidence=0.85,
        reasoning="test",
        recommended_action="retry_now",
        recommended_channel="sms",
        recommended_retry_window=None,
    )
    defaults.update(overrides)
    return AIDecision(**defaults)


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """Never let tests touch the real logs/audit_log.jsonl."""
    monkeypatch.setattr(audit_log, "LOG_PATH", tmp_path / "audit_log.jsonl")
    yield


def test_process_event_writes_audit_entry(monkeypatch):
    monkeypatch.setattr(pipeline, "diagnose", lambda event: make_decision())

    event = make_event()
    entry = pipeline.process_event(event, GuardrailContext())

    assert entry.mandate_id == event.mandate_id
    assert entry.action_taken == "retry_now"
    assert entry.guardrail_verdict.allowed is True
    logged = audit_log.load_log()
    assert len(logged) == 1
    assert logged[0]["mandate_id"] == event.mandate_id


def test_process_event_escalates_low_confidence_even_if_guardrail_allows(monkeypatch):
    monkeypatch.setattr(
        pipeline, "diagnose", lambda event: make_decision(confidence=0.3, recommended_action="retry_now")
    )

    event = make_event()
    entry = pipeline.process_event(event, GuardrailContext())

    assert entry.action_taken == "escalate_human"
    assert entry.guardrail_verdict.allowed is False
    assert "low_confidence_escalation" in entry.guardrail_verdict.overridden_reason
    assert entry.outcome == "escalated"


def test_process_event_guardrail_overrides_revoked_mandate(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "diagnose",
        lambda event: make_decision(
            likely_cause="mandate_revoked", confidence=0.9, recommended_action="retry_now"
        ),
    )

    event = make_event(mandate_status="revoked", true_cause="mandate_revoked")
    entry = pipeline.process_event(event, GuardrailContext())

    assert entry.action_taken != "retry_now"
    assert entry.guardrail_verdict.allowed is False
    assert entry.outcome != "recovered"


def test_process_batch_enforces_exposure_cap_across_events(monkeypatch):
    """Two same-day retries for the same customer that together exceed the
    exposure ceiling should have the second one blocked, even though each
    individually looks fine and the AI is confident both times.
    """
    monkeypatch.setattr(
        pipeline, "diagnose", lambda event: make_decision(confidence=0.9, recommended_action="retry_now")
    )

    from agent.guardrails import DAILY_EXPOSURE_CAP_INR

    big_amount = DAILY_EXPOSURE_CAP_INR * 0.7
    events = [
        make_event(mandate_id="MIDAAA", customer_id="CUSTSAME", amount=big_amount),
        make_event(mandate_id="MIDBBB", customer_id="CUSTSAME", amount=big_amount),
    ]

    entries = pipeline.process_batch(events)

    assert entries[0].action_taken == "retry_now"
    assert entries[1].action_taken == "escalate_human"
    assert "daily_exposure_cap_exceeded" in entries[1].guardrail_verdict.overridden_reason


def test_execution_result_is_recorded_in_audit_log(monkeypatch):
    """The audit trail must tie a decision to what the executor actually did,
    otherwise a payment link can't be traced back to the decision that made it.
    """
    monkeypatch.setattr(pipeline, "diagnose", lambda event: make_decision())
    monkeypatch.setattr(
        pipeline,
        "execute_action",
        lambda action, event: {
            "id": "plink_test123",
            "short_url": "https://rzp.io/i/test",
            "status": "created",
            "reference_id": event.mandate_id,
            "customer": {"email": "should-not-be-logged@example.com"},
        },
    )

    event = make_event()
    entry = pipeline.process_event(event, GuardrailContext())

    assert entry.execution_result["id"] == "plink_test123"
    assert entry.execution_result["short_url"] == "https://rzp.io/i/test"
    assert entry.execution_result["reference_id"] == event.mandate_id
    # Contact details from the provider response must not leak into the log.
    assert "customer" not in entry.execution_result

    assert audit_log.load_log()[-1]["execution_result"]["id"] == "plink_test123"


def test_model_failure_becomes_an_escalation_not_a_crash(monkeypatch):
    """A malformed model response killed a 60-case run at case 36 in practice.
    One bad case must not destroy the batch — it becomes an auditable
    escalation instead.
    """
    def boom(event):
        raise RuntimeError("tool_use_failed: confidence was '0. nine'")

    monkeypatch.setattr(pipeline, "diagnose", boom)

    entry = pipeline.process_event(make_event(), GuardrailContext())

    assert entry.action_taken == "escalate_human"
    assert entry.outcome == "escalated"
    assert entry.ai_output.confidence == 0.0
    assert "failed to produce a valid decision" in entry.ai_output.reasoning
    assert audit_log.load_log()[-1]["mandate_id"] == entry.mandate_id


def test_batch_continues_past_a_failing_case(monkeypatch):
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider exploded")
        return make_decision()

    monkeypatch.setattr(pipeline, "diagnose", flaky)

    events = [
        make_event(mandate_id="MIDOK1", customer_id="C1"),
        make_event(mandate_id="MIDBAD", customer_id="C2"),
        make_event(mandate_id="MIDOK2", customer_id="C3"),
    ]
    entries = pipeline.process_batch(events)

    assert len(entries) == 3
    assert entries[1].action_taken == "escalate_human"
    assert entries[2].action_taken == "retry_now"


def test_recovered_outcome_only_set_by_webhook(monkeypatch):
    """API-call success alone must never mark a case recovered — only a
    verified webhook does (project guide §0, §6).
    """
    monkeypatch.setattr(pipeline, "diagnose", lambda event: make_decision())

    event = make_event()
    entry = pipeline.process_event(event, GuardrailContext())
    assert entry.outcome == "pending"

    updated = audit_log.mark_recovered(event.mandate_id, payment_link_id="plink_test123")
    assert updated is True

    logged = audit_log.load_log()
    assert logged[-1]["outcome"] == "recovered"

    # A duplicate webhook delivery for the same mandate must be a no-op.
    duplicate = audit_log.mark_recovered(event.mandate_id, payment_link_id="plink_test123")
    assert duplicate is False
