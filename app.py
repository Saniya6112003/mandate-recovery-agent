"""FastAPI app: webhook receiver + a couple of read-only demo endpoints
(project guide §0). Not required for the demo to work — `run_batch.py` alone
produces the full audit log — but useful if the pitch video wants a live,
browser-visible run instead of only a terminal walkthrough.

Run with: uvicorn app:app --reload
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from agent.audit_log import load_log
from agent.executor import kill_switch_engaged
from agent.pipeline import process_batch
from agent.webhook_handler import router as webhook_router
from data.generate_dataset import load_dataset

app = FastAPI(title="UPI Autopay Mandate Recovery Agent")
app.include_router(webhook_router)


@app.get("/demo/audit-log")
def get_audit_log():
    return {"entries": load_log()}


@app.post("/demo/run-batch")
def run_batch(limit: Optional[int] = None):
    events = load_dataset()
    if limit:
        events = events[:limit]
    entries = process_batch(events)
    return {
        "kill_switch_engaged": kill_switch_engaged(),
        "processed": len(entries),
        "entries": [e.model_dump(mode="json") for e in entries],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
