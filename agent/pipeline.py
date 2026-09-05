"""Wires the core recovery loop together (project guide §3.1):

    Failed mandate event -> AI reasoning agent -> Guardrail check
                          -> Action executor -> Audit log

Also applies confidence-based human-escalation routing (§4): a low-confidence
AI decision is escalated even when the guardrail layer would have allowed it
to execute as recommended.
"""

from __future__ import annotations

from collections import defaultdict

from agent.audit_log import append_entry
from agent.executor import execute_action
from agent.guardrails import GuardrailContext, apply_guardrails
from agent.reasoning_agent import diagnose
from agent.schemas import AIDecision, AuditLogEntry, GuardrailVerdict, MandateEvent

CONFIDENCE_ESCALATION_THRESHOLD = 0.6

_ACTION_TO_OUTCOME = {
    "retry_now": "pending",
    "retry_scheduled": "pending",
    "notify_customer": "pending",
    "escalate_human": "escalated",
    "stop": "failed",
}


def _apply_confidence_routing(
    decision: AIDecision, verdict: GuardrailVerdict, action: str
) -> tuple[GuardrailVerdict, str]:
    if action in ("escalate_human", "stop"):
        return verdict, action
    if decision.confidence >= CONFIDENCE_ESCALATION_THRESHOLD:
        return verdict, action

    return (
        GuardrailVerdict(
            allowed=False,
            overridden_reason=(
                f"low_confidence_escalation: AI confidence {decision.confidence:.2f} "
                f"is below the {CONFIDENCE_ESCALATION_THRESHOLD:.2f} human-review threshold"
            ),
        ),
        "escalate_human",
    )


def _executor_summary(result: dict) -> dict:
    """Keep the audit-relevant fields of the executor's response.

    Razorpay's Payment Link entity is large and includes customer contact
    details; the audit log only needs enough to trace the action and match
    the later webhook.
    """
    keep = ("id", "short_url", "status", "reference_id", "amount", "detail", "note")
    return {k: result[k] for k in keep if k in result}


def _failed_decision(event: MandateEvent, error: Exception) -> AIDecision:
    """Stand-in decision for a case the model could not decide.

    A recovery run must not die because one response came back malformed or
    the provider was unreachable — the other cases still need processing, and
    the failure itself is an auditable event. Zero confidence routes it
    straight to human review.
    """
    return AIDecision(
        likely_cause="unknown",
        confidence=0.0,
        reasoning=(
            f"Reasoning agent failed to produce a valid decision for "
            f"{event.bank_response_code}: {str(error)[:200]}"
        ),
        recommended_action="escalate_human",
        recommended_channel="none",
        recommended_retry_window=None,
    )


def process_event(event: MandateEvent, context: GuardrailContext) -> AuditLogEntry:
    try:
        decision = diagnose(event)
    except Exception as exc:  # provider errors, malformed output, timeouts
        decision = _failed_decision(event, exc)
    verdict, action = apply_guardrails(event, decision, context)
    verdict, action = _apply_confidence_routing(decision, verdict, action)

    if action in ("retry_now", "retry_scheduled"):
        context.customer_attempts_today += 1
        context.customer_amount_today += event.amount

    result = execute_action(action, event)

    entry = AuditLogEntry(
        mandate_id=event.mandate_id,
        input_signal=event.agent_input(),
        ai_output=decision,
        guardrail_verdict=verdict,
        action_taken=action,
        execution_result=_executor_summary(result),
        outcome=_ACTION_TO_OUTCOME[action],
    )
    append_entry(entry)
    return entry


def process_batch(events: list[MandateEvent]) -> list[AuditLogEntry]:
    """Run the full loop over a batch, tracking per-customer daily exposure
    across the whole batch so the guardrail's exposure cap is enforced
    the way it would be in production, not just per-event.
    """
    contexts: dict[tuple[str, str], GuardrailContext] = defaultdict(GuardrailContext)
    entries: list[AuditLogEntry] = []

    for event in events:
        key = (event.customer_id, event.failure_timestamp.date().isoformat())
        entry = process_event(event, contexts[key])
        entries.append(entry)

    return entries
