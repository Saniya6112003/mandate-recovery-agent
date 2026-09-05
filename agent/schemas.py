from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

LikelyCause = Literal[
    "insufficient_funds",
    "bank_timeout",
    "mandate_revoked",
    "daily_limit_exceeded",
    "authentication_failure",
    "unknown",
]

RecommendedAction = Literal[
    "retry_now",
    "retry_scheduled",
    "notify_customer",
    "escalate_human",
    "stop",
]

RecommendedChannel = Literal["sms", "whatsapp", "email", "none"]

MandateType = Literal["revocable", "non-revocable"]

# What an authoritative mandate-status lookup (bank/NPCI) returns. The
# guardrail layer reads this directly; the reasoning agent never sees it,
# which is what lets the guardrail catch a retry the AI wrongly recommended.
MandateStatus = Literal["active", "revoked", "paused"]

Outcome = Literal["recovered", "failed", "pending", "escalated"]


class MandateEvent(BaseModel):
    """A single failed UPI Autopay mandate debit, as it would arrive from the bank/NPCI side."""

    mandate_id: str
    customer_id: str
    amount: float
    bank_response_code: str
    retry_count: int
    mandate_type: MandateType
    mandate_created_at: datetime
    failure_timestamp: datetime
    notice_sent_at: Optional[datetime] = None
    mandate_status: MandateStatus = "active"

    true_cause: LikelyCause = Field(
        description="Ground-truth label for dataset evaluation only — never shown to the reasoning agent."
    )
    is_hard_case: bool = False

    def agent_input(self) -> dict:
        """The subset of fields the reasoning agent is actually allowed to see."""
        return {
            "bank_response_code": self.bank_response_code,
            "amount_inr": self.amount,
            "retry_count": self.retry_count,
            "mandate_type": self.mandate_type,
            "mandate_created_at": self.mandate_created_at.isoformat(),
            "failure_timestamp": self.failure_timestamp.isoformat(),
            "notice_sent_at": self.notice_sent_at.isoformat() if self.notice_sent_at else None,
        }


class AIDecision(BaseModel):
    likely_cause: LikelyCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    recommended_action: RecommendedAction
    recommended_channel: RecommendedChannel
    recommended_retry_window: Optional[datetime] = None


class GuardrailVerdict(BaseModel):
    allowed: bool
    overridden_reason: Optional[str] = None


class AuditLogEntry(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mandate_id: str
    input_signal: dict
    ai_output: AIDecision
    guardrail_verdict: GuardrailVerdict
    action_taken: str
    # What the executor actually did in the real world — e.g. the Razorpay
    # Payment Link id and short_url. Without this the trail stops at "we
    # decided to notify" and can't be tied to the link that was created.
    execution_result: dict = Field(default_factory=dict)
    outcome: Outcome
