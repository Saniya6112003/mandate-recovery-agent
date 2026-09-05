from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import gemini_agent, reasoning_agent
from agent.schemas import MandateEvent

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event() -> MandateEvent:
    return MandateEvent(
        mandate_id="MIDGEMINI001",
        customer_id="CUSTGEM0001",
        amount=1999.0,
        bank_response_code="Z9",
        retry_count=1,
        mandate_type="revocable",
        mandate_created_at=NOW - timedelta(days=120),
        failure_timestamp=NOW,
        notice_sent_at=NOW - timedelta(hours=30),
        true_cause="insufficient_funds",
    )


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _ok_payload(**overrides):
    args = {
        "likely_cause": "insufficient_funds",
        "confidence": 0.87,
        "reasoning": "Z9 is an explicit insufficient-funds decline on the remitter account.",
        "recommended_action": "retry_scheduled",
        "recommended_channel": "sms",
        "recommended_retry_window": "2026-06-03T09:00:00+05:30",
    }
    args.update(overrides)
    return {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": "record_decision", "args": args}}]}}
        ]
    }


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    yield


def test_diagnose_parses_forced_function_call(monkeypatch):
    monkeypatch.setattr(gemini_agent.httpx, "post", lambda *a, **k: FakeResponse(200, _ok_payload()))

    decision = gemini_agent.diagnose(make_event())

    assert decision.likely_cause == "insufficient_funds"
    assert decision.confidence == 0.87
    assert decision.recommended_action == "retry_scheduled"
    assert decision.recommended_retry_window is not None


def test_empty_retry_window_becomes_none(monkeypatch):
    """Gemini's schema has no null type, so the empty string must normalise."""
    monkeypatch.setattr(
        gemini_agent.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, _ok_payload(recommended_retry_window="")),
    )

    decision = gemini_agent.diagnose(make_event())

    assert decision.recommended_retry_window is None


def test_request_forces_the_tool_call(monkeypatch):
    """If forcing is lost, the model can reply with prose and the schema
    guarantee disappears — the whole point of the design.
    """
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(gemini_agent.httpx, "post", fake_post)
    gemini_agent.diagnose(make_event())

    cfg = captured["body"]["tool_config"]["function_calling_config"]
    assert cfg["mode"] == "ANY"
    assert cfg["allowed_function_names"] == ["record_decision"]
    assert captured["headers"]["x-goog-api-key"] == "fake-test-key"


def test_ground_truth_is_never_sent_to_the_model(monkeypatch):
    """true_cause is an evaluation label — leaking it would make the whole
    accuracy number meaningless.
    """
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(gemini_agent.httpx, "post", fake_post)
    gemini_agent.diagnose(make_event())

    sent = json.dumps(captured["body"])
    assert "true_cause" not in sent
    assert "is_hard_case" not in sent
    assert "mandate_status" not in sent


def test_rate_limit_is_retried(monkeypatch):
    """The free tier throttles per minute; without backoff a 60-case batch
    loses cases.
    """
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return FakeResponse(429, text="quota exceeded")
        return FakeResponse(200, _ok_payload())

    monkeypatch.setattr(gemini_agent.httpx, "post", fake_post)
    monkeypatch.setattr(gemini_agent.time, "sleep", lambda _: None)

    decision = gemini_agent.diagnose(make_event())

    assert decision.likely_cause == "insufficient_funds"
    assert calls["n"] == 3


def test_missing_function_call_raises(monkeypatch):
    """A prose answer must fail loudly, not silently produce a bad decision."""
    payload = {"candidates": [{"content": {"parts": [{"text": "I think it is a funds issue"}]}}]}
    monkeypatch.setattr(gemini_agent.httpx, "post", lambda *a, **k: FakeResponse(200, payload))

    with pytest.raises(RuntimeError, match="did not return a function call"):
        gemini_agent.diagnose(make_event())


def test_missing_api_key_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="aistudio.google.com"):
        gemini_agent.diagnose(make_event())


def test_provider_switch_routes_to_gemini(monkeypatch):
    monkeypatch.setenv("REASONING_PROVIDER", "gemini")
    monkeypatch.setattr(gemini_agent.httpx, "post", lambda *a, **k: FakeResponse(200, _ok_payload()))

    decision = reasoning_agent.diagnose(make_event())

    assert decision.likely_cause == "insufficient_funds"
