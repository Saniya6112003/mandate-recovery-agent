from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import reasoning_agent
from agent.schemas import MandateEvent
from agent.stub_agent import STUB_MARKER, diagnose

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(code: str, mandate_id: str = "MIDSTUB00001", **overrides) -> MandateEvent:
    defaults = dict(
        mandate_id=mandate_id,
        customer_id="CUSTSTUB01",
        amount=999.0,
        bank_response_code=code,
        retry_count=0,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=60),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
    )
    defaults.update(overrides)
    return MandateEvent(**defaults)


def test_stub_output_is_always_tagged():
    """Stub decisions must be unmistakable in the audit log, so their numbers
    can never be mistaken for real agent output in the pitch.
    """
    decision = diagnose(make_event("Z9"))

    assert decision.reasoning.startswith(STUB_MARKER)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("Z9", "insufficient_funds"),
        ("U67", "bank_timeout"),
        ("Z8", "daily_limit_exceeded"),
        ("ZM", "authentication_failure"),
        ("VA", "mandate_revoked"),
    ],
)
def test_well_known_codes_diagnosed_confidently(code, expected):
    decision = diagnose(make_event(code))

    assert decision.likely_cause == expected
    assert decision.confidence >= 0.8


@pytest.mark.parametrize("code", ["ZA", "U30", "B3"])
def test_ambiguous_codes_get_low_confidence(code):
    """Codes the bank returns with no stated reason should route to human
    review, not a confident guess.
    """
    decision = diagnose(make_event(code))

    assert decision.likely_cause == "unknown"
    assert decision.confidence < 0.6


def test_stub_is_deterministic():
    """Same event in, same decision out — so a demo batch replays identically."""
    first = diagnose(make_event("MA0", mandate_id="MIDREPEAT001"))
    second = diagnose(make_event("MA0", mandate_id="MIDREPEAT001"))

    assert first.model_dump() == second.model_dump()


def test_unrecognised_code_does_not_crash():
    decision = diagnose(make_event("NOT_A_REAL_CODE"))

    assert decision.likely_cause == "unknown"
    assert decision.confidence < 0.6


def test_provider_defaults_to_anthropic(monkeypatch):
    """The real agent must stay the default — offline mode is opt-in only."""
    monkeypatch.delenv("REASONING_PROVIDER", raising=False)

    assert reasoning_agent._provider() == "anthropic"


def test_stub_provider_routes_without_api_key(monkeypatch):
    """With the stub selected, diagnose() must not need ANTHROPIC_API_KEY."""
    monkeypatch.setenv("REASONING_PROVIDER", "stub")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    decision = reasoning_agent.diagnose(make_event("Z9"))

    assert decision.reasoning.startswith(STUB_MARKER)
