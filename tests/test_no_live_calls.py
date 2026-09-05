"""Regression guard: the suite must never reach a live API.

This existed as a real bug — `load_dotenv()` at import time put real Razorpay
credentials into os.environ, so pipeline tests created actual payment links
on the account and exhausted the API rate limit.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

import agent.audit_log as audit_log
import agent.pipeline as pipeline
from agent import executor
from agent.guardrails import GuardrailContext
from agent.schemas import AIDecision, MandateEvent

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event() -> MandateEvent:
    return MandateEvent(
        mandate_id="MIDNOLIVE001",
        customer_id="CUSTNOLIVE1",
        amount=999.0,
        bank_response_code="Z9",
        retry_count=0,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=60),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
    )


def test_razorpay_credentials_are_not_visible_to_tests():
    assert os.getenv("RAZORPAY_KEY_ID") is None
    assert os.getenv("RAZORPAY_KEY_SECRET") is None


def test_executor_takes_dry_run_path_during_tests():
    result = executor.create_payment_link(make_event())

    assert result["status"] == "dry_run"


def test_pipeline_never_constructs_a_live_client(monkeypatch, tmp_path):
    """Belt and braces: if anything tries to build a real Razorpay client
    during a pipeline run, fail loudly rather than silently calling out.
    """
    monkeypatch.setattr(audit_log, "LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(
        pipeline,
        "diagnose",
        lambda event: AIDecision(
            likely_cause="insufficient_funds",
            confidence=0.9,
            reasoning="test",
            recommended_action="retry_now",
            recommended_channel="sms",
            recommended_retry_window=None,
        ),
    )

    def explode():
        raise AssertionError("test attempted to build a live Razorpay client")

    monkeypatch.setattr(executor, "_get_client", explode)

    with pytest.raises(AssertionError, match="live Razorpay client"):
        pipeline.process_event(make_event(), GuardrailContext())
