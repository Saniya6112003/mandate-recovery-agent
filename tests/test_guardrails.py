from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.guardrails import (
    DAILY_ATTEMPT_CAP,
    DAILY_EXPOSURE_CAP_INR,
    MAX_RETRY_ATTEMPTS,
    GuardrailContext,
    apply_guardrails,
)
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


def test_revoked_mandate_blocks_retry_now():
    """Headline demo case (project guide §3.1, §5): the AI recommends
    retry_now on a revoked mandate; the guardrail must catch it and never
    let a retry execute against a mandate that can't technically be debited.
    """
    event = make_event(
        mandate_status="revoked", true_cause="mandate_revoked", mandate_type="revocable"
    )
    decision = make_decision(likely_cause="mandate_revoked", recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert verdict.overridden_reason is not None
    assert "mandate_revoked" in verdict.overridden_reason
    assert action != "retry_now"
    assert action in ("notify_customer", "escalate_human")


def test_revoked_mandate_blocks_retry_even_when_ai_misdiagnoses():
    """The guardrail must not depend on the AI getting the diagnosis right:
    here the AI thinks it's a funds problem and confidently wants to retry,
    but the authoritative mandate status says the mandate is revoked.
    """
    event = make_event(mandate_status="revoked", true_cause="mandate_revoked")
    decision = make_decision(
        likely_cause="insufficient_funds", confidence=0.95, recommended_action="retry_now"
    )

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert action != "retry_now"


def test_paused_mandate_blocks_retry_scheduled_too():
    event = make_event(mandate_status="paused", true_cause="mandate_revoked")
    decision = make_decision(recommended_action="retry_scheduled")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert "mandate_paused" in verdict.overridden_reason
    assert action != "retry_scheduled"


def test_retry_cap_blocks_after_max_attempts():
    event = make_event(retry_count=MAX_RETRY_ATTEMPTS)
    decision = make_decision(recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert "retry_cap_exceeded" in verdict.overridden_reason
    assert action != "retry_now"


def test_retry_under_cap_is_allowed():
    event = make_event(retry_count=MAX_RETRY_ATTEMPTS - 1)
    decision = make_decision(recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is True
    assert action == "retry_now"


def test_notice_window_blocks_retry_now_when_too_recent():
    event = make_event(notice_sent_at=NOW - timedelta(hours=6))
    decision = make_decision(recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert "notice_window_unsatisfied" in verdict.overridden_reason
    assert action != "retry_now"


def test_notice_window_blocks_retry_now_when_notice_missing():
    event = make_event(notice_sent_at=None)
    decision = make_decision(recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is False
    assert "notice_window_unsatisfied" in verdict.overridden_reason


def test_notice_window_satisfied_allows_retry_now():
    event = make_event(notice_sent_at=NOW - timedelta(hours=25))
    decision = make_decision(recommended_action="retry_now")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is True
    assert action == "retry_now"


def test_notice_window_does_not_apply_to_non_retry_actions():
    event = make_event(notice_sent_at=NOW - timedelta(hours=1))
    decision = make_decision(recommended_action="notify_customer")

    verdict, action = apply_guardrails(event, decision)

    assert verdict.allowed is True
    assert action == "notify_customer"


def test_daily_attempt_cap_blocks_extra_retries():
    event = make_event(amount=100.0)
    decision = make_decision(recommended_action="retry_now")
    context = GuardrailContext(customer_attempts_today=DAILY_ATTEMPT_CAP)

    verdict, action = apply_guardrails(event, decision, context)

    assert verdict.allowed is False
    assert "daily_attempt_cap_exceeded" in verdict.overridden_reason
    assert action == "escalate_human"


def test_daily_exposure_cap_blocks_when_amount_exceeds_ceiling():
    event = make_event(amount=DAILY_EXPOSURE_CAP_INR)
    decision = make_decision(recommended_action="retry_now")
    context = GuardrailContext(customer_amount_today=1.0)

    verdict, action = apply_guardrails(event, decision, context)

    assert verdict.allowed is False
    assert "daily_exposure_cap_exceeded" in verdict.overridden_reason


def test_exposure_cap_not_checked_for_non_retry_actions():
    event = make_event(amount=DAILY_EXPOSURE_CAP_INR)
    decision = make_decision(recommended_action="notify_customer")
    context = GuardrailContext(customer_amount_today=DAILY_EXPOSURE_CAP_INR)

    verdict, action = apply_guardrails(event, decision, context)

    assert verdict.allowed is True
    assert action == "notify_customer"


@pytest.mark.parametrize("action", ["notify_customer", "escalate_human", "stop"])
def test_non_retry_actions_pass_through_when_no_rule_applies(action):
    event = make_event()
    decision = make_decision(recommended_action=action)

    verdict, final_action = apply_guardrails(event, decision)

    assert verdict.allowed is True
    assert final_action == action
