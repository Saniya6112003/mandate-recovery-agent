"""Test-wide safety net.

Importing the agent package runs `load_dotenv()`, which pulls real Razorpay
credentials from .env into os.environ. Without this fixture any test that
reaches the executor creates REAL payment links on the account, burns the
API rate limit, and litters the dashboard with fixture data.

Tests must never touch a live API. This strips the credentials for the whole
session, so the executor always takes its dry-run path unless a test
explicitly injects a fake client of its own.
"""

from __future__ import annotations

import pytest

from agent import executor


@pytest.fixture(autouse=True)
def never_call_live_apis(monkeypatch):
    for name in (
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "ANTHROPIC_API_KEY",
        # Tests must exercise the file backend, never a real database.
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(executor, "_client", None)
    yield
