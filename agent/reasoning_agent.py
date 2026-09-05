from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from agent.npci_codes import render_reference  # noqa: E402
from agent.schemas import AIDecision, MandateEvent  # noqa: E402

load_dotenv()

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# Which reasoning backend to use:
#   "anthropic" (default) — Claude, paid per token
#   "gemini"              — Google AI Studio free tier, real model, no cost
#   "stub"                — offline fixture, no network; tagged output that
#                           is NOT valid for reported metrics
def _provider() -> str:
    return os.getenv("REASONING_PROVIDER", "anthropic").lower()


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# Forcing this tool via tool_choice, rather than asking for JSON in a prompt,
# is what makes a malformed response impossible by construction (see project
# guide §4) instead of an occasional prompt-drift failure mid-demo.
DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record the diagnosis and recommended intervention for a failed UPI Autopay mandate debit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "likely_cause": {
                "type": "string",
                "enum": [
                    "insufficient_funds",
                    "bank_timeout",
                    "mandate_revoked",
                    "daily_limit_exceeded",
                    "authentication_failure",
                    "unknown",
                ],
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Self-reported confidence, not a calibrated probability.",
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences, plain language, specific to this case.",
            },
            "recommended_action": {
                "type": "string",
                "enum": ["retry_now", "retry_scheduled", "notify_customer", "escalate_human", "stop"],
            },
            "recommended_channel": {
                "type": "string",
                "enum": ["sms", "whatsapp", "email", "none"],
            },
            "recommended_retry_window": {
                "type": ["string", "null"],
                "description": "ISO 8601 timestamp, or null if not applicable.",
            },
        },
        "required": [
            "likely_cause",
            "confidence",
            "reasoning",
            "recommended_action",
            "recommended_channel",
            "recommended_retry_window",
        ],
    },
}

SYSTEM_PROMPT = f"""You are a payments recovery analyst specializing in UPI Autopay mandate failures in India.

For each failed mandate debit you are given the raw bank/NPCI response code (not a pre-labeled cause), the amount,
retry history, mandate type, and timing. Diagnose the most likely cause of this specific failure, state your
confidence, and recommend one bounded intervention. Reason about this case specifically, not generically.

NPCI/UPI response code reference:
{render_reference()}

Some codes are declines the bank issues without stating a reason. Where the code does not identify a cause,
say so through a low confidence and the `unknown` cause rather than guessing a specific one.

Weigh the surrounding signals, not just the code: retry count, amount, how long the mandate has existed,
whether the 24-hour pre-debit notice window is satisfied, and whether the mandate is revocable.

A downstream compliance guardrail independently checks your recommendation against RBI mandate rules before
anything executes, so recommend what you believe is correct even if it might later be overridden."""


def diagnose(event: MandateEvent) -> AIDecision:
    provider = _provider()

    if provider == "stub":
        from agent.stub_agent import diagnose as stub_diagnose

        return stub_diagnose(event)

    if provider == "gemini":
        from agent.gemini_agent import diagnose as gemini_diagnose

        return gemini_diagnose(event)

    if provider == "groq":
        from agent.groq_agent import diagnose as groq_diagnose

        return groq_diagnose(event)

    client = _get_client()

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_decision"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Diagnose this failed UPI Autopay mandate debit:\n"
                    f"{json.dumps(event.agent_input(), indent=2)}"
                ),
            }
        ],
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return AIDecision.model_validate(tool_use.input)


if __name__ == "__main__":
    from data.generate_dataset import load_dataset

    events = load_dataset()[:12]
    correct = 0
    for event in events:
        decision = diagnose(event)
        is_match = decision.likely_cause == event.true_cause
        correct += is_match
        mark = "MATCH" if is_match else "MISS "
        print(
            f"[{mark}] mandate={event.mandate_id} code={event.bank_response_code:<20} "
            f"true={event.true_cause:<22} predicted={decision.likely_cause:<22} "
            f"conf={decision.confidence:.2f} action={decision.recommended_action}"
        )
        print(f"        reasoning: {decision.reasoning}")

    print(f"\n{correct}/{len(events)} matched ground-truth cause label.")
