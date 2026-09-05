"""Free-tier reasoning provider: Google Gemini via the REST API.

Uses the same forced-function-calling discipline as the Anthropic path — the
model must return the decision through a declared function schema, so a
malformed or prose response is impossible by construction (project guide §4).

Called over REST with httpx rather than the official SDK deliberately:
`google-genai` requires Python 3.9+ and this project runs on 3.8, and the
REST surface gives direct control over `tool_config` forcing. httpx is
already a dependency.

Get a free key at aistudio.google.com/apikey — no credit card required.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from agent.npci_codes import render_reference
from agent.schemas import AIDecision, MandateEvent

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.6-flash"

# The free tier is rate limited per minute, so a 60-case batch will be
# throttled without backoff — same failure shape as the Razorpay executor.
RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("GEMINI_RETRY_ATTEMPTS", "5"))
RATE_LIMIT_BASE_DELAY_SECONDS = float(os.getenv("GEMINI_RETRY_BASE_DELAY", "4.0"))
# Reasoning models can take a while to answer, and this runs on consumer
# connections — a short timeout drops cases that would have succeeded.
REQUEST_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "180"))

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

# Gemini's schema dialect has no ["string", "null"] union, so the retry window
# is a plain string and an empty value is normalised to None below.
FUNCTION_DECLARATION: dict[str, Any] = {
    "name": "record_decision",
    "description": (
        "Record the diagnosis and recommended intervention for a failed "
        "UPI Autopay mandate debit."
    ),
    "parameters": {
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
                "description": "Between 0.0 and 1.0. Self-reported, not a calibrated probability.",
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences, plain language, specific to this case.",
            },
            "recommended_action": {
                "type": "string",
                "enum": [
                    "retry_now",
                    "retry_scheduled",
                    "notify_customer",
                    "escalate_human",
                    "stop",
                ],
            },
            "recommended_channel": {
                "type": "string",
                "enum": ["sms", "whatsapp", "email", "none"],
            },
            "recommended_retry_window": {
                "type": "string",
                "description": "ISO 8601 timestamp, or an empty string if not applicable.",
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


def _model() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and add it to .env"
        )
    return key


def _build_request(event: MandateEvent) -> dict[str, Any]:
    return {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Diagnose this failed UPI Autopay mandate debit:\n"
                            f"{json.dumps(event.agent_input(), indent=2)}"
                        )
                    }
                ],
            }
        ],
        "tools": [{"function_declarations": [FUNCTION_DECLARATION]}],
        # ANY + a single allowed name is Gemini's equivalent of forcing the
        # tool call, which is what makes the schema non-negotiable.
        "tool_config": {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": ["record_decision"],
            }
        },
    }


def _extract_call_args(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(payload)[:400]}")

    parts = candidates[0].get("content", {}).get("parts") or []
    for part in parts:
        if "functionCall" in part:
            return part["functionCall"].get("args") or {}

    raise RuntimeError(
        "Gemini did not return a function call (forced tool use failed): "
        f"{json.dumps(payload)[:400]}"
    )


def _to_decision(args: dict[str, Any]) -> AIDecision:
    window = args.get("recommended_retry_window") or None
    return AIDecision.model_validate({**args, "recommended_retry_window": window})


def diagnose(event: MandateEvent) -> AIDecision:
    url = f"{API_ROOT}/{_model()}:generateContent"
    headers = {"x-goog-api-key": _api_key(), "Content-Type": "application/json"}
    body = _build_request(event)

    delay = RATE_LIMIT_BASE_DELAY_SECONDS
    last_error: Exception | None = None

    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            response = httpx.post(
                url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # A dropped connection mid-batch would otherwise lose the case
            # entirely rather than retrying it.
            last_error = exc
            if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"Gemini unreachable after retries: {exc}") from exc

        if response.status_code == 200:
            return _to_decision(_extract_call_args(response.json()))

        # 429 = free-tier quota, 5xx = transient. Both are worth retrying.
        if response.status_code == 429 or response.status_code >= 500:
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
                continue

        raise RuntimeError(f"Gemini request failed HTTP {response.status_code}: {response.text[:400]}")

    raise RuntimeError(f"Gemini still failing after retries: {last_error}")
