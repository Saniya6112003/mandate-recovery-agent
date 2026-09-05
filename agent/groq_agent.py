"""Free-tier reasoning provider: Groq.

Groq's free tier allows far more requests per day than Gemini's 20/model,
which is what makes a full 60-case batch practical without paying. The API is
OpenAI-compatible, so the decision schema is declared as a function and forced
with `tool_choice` — the same guarantee as the other providers: the model
cannot answer with prose or malformed JSON.

Called over REST with httpx rather than an SDK: this project runs on Python
3.8, several vendor SDKs require 3.9+, and httpx is already a dependency.

Get a free key at console.groq.com/keys — no credit card required.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from agent.npci_codes import render_reference
from agent.schemas import AIDecision, MandateEvent

API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS_URL = "https://api.groq.com/openai/v1/models"
DEFAULT_MODEL = "openai/gpt-oss-120b"

RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("GROQ_RETRY_ATTEMPTS", "5"))
RATE_LIMIT_BASE_DELAY_SECONDS = float(os.getenv("GROQ_RETRY_BASE_DELAY", "3.0"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT_SECONDS", "120"))

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

TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
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
                    "description": "Between 0.0 and 1.0. Self-reported, not calibrated.",
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
    },
}


def _model() -> str:
    return os.getenv("GROQ_MODEL", DEFAULT_MODEL)


def _api_key() -> str:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Get a free key at "
            "https://console.groq.com/keys and add it to .env"
        )
    return key


def list_models() -> list[str]:
    """Available model ids — model names change, so check rather than guess."""
    response = httpx.get(
        MODELS_URL,
        headers={"Authorization": f"Bearer {_api_key()}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return sorted(m["id"] for m in response.json().get("data", []))


def _build_body(event: MandateEvent) -> dict[str, Any]:
    return {
        "model": _model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Diagnose this failed UPI Autopay mandate debit:\n"
                    f"{json.dumps(event.agent_input(), indent=2)}"
                ),
            },
        ],
        "tools": [TOOL],
        # Forcing this specific function is what makes the schema
        # non-negotiable rather than a request the model may ignore.
        "tool_choice": {"type": "function", "function": {"name": "record_decision"}},
        "temperature": 0.2,
    }


def _extract_call_args(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"Groq returned no choices: {json.dumps(payload)[:400]}")

    calls = choices[0].get("message", {}).get("tool_calls") or []
    if not calls:
        raise RuntimeError(
            "Groq did not return a function call (forced tool use failed): "
            f"{json.dumps(payload)[:400]}"
        )

    # Arguments arrive as a JSON string, never as an object.
    return json.loads(calls[0]["function"]["arguments"])


def _to_decision(args: dict[str, Any]) -> AIDecision:
    window = args.get("recommended_retry_window") or None
    confidence = args.get("confidence")
    # Some models emit 0-100 rather than 0-1 despite the description.
    if isinstance(confidence, (int, float)) and confidence > 1:
        confidence = confidence / 100.0
    return AIDecision.model_validate(
        {**args, "confidence": confidence, "recommended_retry_window": window}
    )


def diagnose(event: MandateEvent) -> AIDecision:
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    body = _build_body(event)

    delay = RATE_LIMIT_BASE_DELAY_SECONDS
    last_error: Exception | None = None

    for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
        try:
            response = httpx.post(
                API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"Groq unreachable after retries: {exc}") from exc

        if response.status_code == 200:
            return _to_decision(_extract_call_args(response.json()))

        # `tool_use_failed` means the model emitted malformed arguments — seen
        # in practice as `"confidence": 0. nine`. The forced schema did its job
        # by rejecting it, but this is a sampling glitch, not a bad request, so
        # it is worth another attempt rather than failing the case.
        if response.status_code == 400 and "tool_use_failed" in response.text:
            last_error = RuntimeError(f"malformed tool arguments: {response.text[:200]}")
            if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
                continue

        if response.status_code == 429 or response.status_code >= 500:
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            if attempt < RATE_LIMIT_MAX_ATTEMPTS - 1:
                # Groq returns the exact wait in this header; honour it rather
                # than guessing with pure exponential backoff.
                retry_after = response.headers.get("retry-after")
                sleep_for = float(retry_after) if retry_after else delay
                time.sleep(sleep_for)
                delay *= 2
                continue

        raise RuntimeError(f"Groq request failed HTTP {response.status_code}: {response.text[:400]}")

    raise RuntimeError(f"Groq still failing after retries: {last_error}")
