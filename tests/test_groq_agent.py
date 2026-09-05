from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import groq_agent, reasoning_agent
from agent.schemas import MandateEvent

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event() -> MandateEvent:
    return MandateEvent(
        mandate_id="MIDGROQ00001",
        customer_id="CUSTGROQ001",
        amount=1499.0,
        bank_response_code="IE",
        retry_count=1,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=90),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
    )


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


def _ok_payload(**overrides):
    args = {
        "likely_cause": "insufficient_funds",
        "confidence": 0.88,
        "reasoning": "IE means funds are blocked against another mandate.",
        "recommended_action": "retry_scheduled",
        "recommended_channel": "sms",
        "recommended_retry_window": "",
    }
    args.update(overrides)
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "record_decision", "arguments": json.dumps(args)}}
                    ]
                }
            }
        ]
    }


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    yield


def test_diagnose_parses_forced_tool_call(monkeypatch):
    monkeypatch.setattr(groq_agent.httpx, "post", lambda *a, **k: FakeResponse(200, _ok_payload()))

    decision = groq_agent.diagnose(make_event())

    assert decision.likely_cause == "insufficient_funds"
    assert decision.confidence == 0.88
    assert decision.recommended_retry_window is None


def test_request_forces_the_tool_call(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        captured["headers"] = headers
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(groq_agent.httpx, "post", fake_post)
    groq_agent.diagnose(make_event())

    assert captured["body"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_decision"},
    }
    assert captured["headers"]["Authorization"] == "Bearer fake-groq-key"


def test_percentage_confidence_is_normalised(monkeypatch):
    """Some models emit 0-100 despite the schema description; a raw 85 would
    fail pydantic validation and lose the case.
    """
    monkeypatch.setattr(
        groq_agent.httpx, "post", lambda *a, **k: FakeResponse(200, _ok_payload(confidence=85))
    )

    decision = groq_agent.diagnose(make_event())

    assert decision.confidence == 0.85


def test_ground_truth_is_never_sent(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(groq_agent.httpx, "post", fake_post)
    groq_agent.diagnose(make_event())

    sent = json.dumps(captured["body"])
    assert "true_cause" not in sent
    assert "mandate_status" not in sent


def test_retry_after_header_is_honoured(monkeypatch):
    """Groq states the exact wait; ignoring it and guessing wastes quota."""
    slept = []
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, text="rate limited", headers={"retry-after": "7"})
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(groq_agent.httpx, "post", fake_post)
    monkeypatch.setattr(groq_agent.time, "sleep", lambda s: slept.append(s))

    groq_agent.diagnose(make_event())

    assert slept == [7.0]


def test_missing_tool_call_raises(monkeypatch):
    payload = {"choices": [{"message": {"content": "probably a funds issue"}}]}
    monkeypatch.setattr(groq_agent.httpx, "post", lambda *a, **k: FakeResponse(200, payload))

    with pytest.raises(RuntimeError, match="did not return a function call"):
        groq_agent.diagnose(make_event())


def test_missing_api_key_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="console.groq.com"):
        groq_agent.diagnose(make_event())


def test_provider_switch_routes_to_groq(monkeypatch):
    monkeypatch.setenv("REASONING_PROVIDER", "groq")
    monkeypatch.setattr(groq_agent.httpx, "post", lambda *a, **k: FakeResponse(200, _ok_payload()))

    decision = reasoning_agent.diagnose(make_event())

    assert decision.likely_cause == "insufficient_funds"
