"""FastAPI app: audit console, webhook receiver, and demo endpoints.

Run locally with:  uvicorn app:app --reload
Deployed, the webhook URL is  https://<host>/webhook
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from agent.audit_log import load_log
from agent.dashboard_data import build_payload
from agent.executor import kill_switch_engaged
from agent.pipeline import process_batch
from agent.reasoning_agent import _provider
from agent.webhook_handler import router as webhook_router
from data.generate_dataset import load_dataset

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="UPI Autopay Mandate Recovery Agent",
    description="Explainable, RBI-bounded recovery for failed UPI Autopay mandate debits.",
)
app.include_router(webhook_router)


@app.get("/", include_in_schema=False)
def dashboard():
    """Audit console — every decision, verdict and outcome."""
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/dashboard")
def dashboard_data():
    return build_payload()


@app.get("/demo/audit-log")
def get_audit_log():
    return {"entries": load_log()}


@app.post("/demo/run-batch")
def run_batch(limit: Optional[int] = None, resume: bool = False):
    """Run the recovery loop over the batch.

    `resume=true` skips mandates already in the audit log, so a deployed
    instance can be filled up across several calls without re-deciding — and
    without double-counting them in the metrics.
    """
    events = load_dataset()
    if limit:
        events = events[:limit]

    skipped = 0
    if resume:
        done = {entry["mandate_id"] for entry in load_log()}
        before = len(events)
        events = [e for e in events if e.mandate_id not in done]
        skipped = before - len(events)

    entries = process_batch(events)
    return {
        "provider": _provider(),
        "kill_switch_engaged": kill_switch_engaged(),
        "skipped_already_done": skipped,
        "processed": len(entries),
        "entries": [e.model_dump(mode="json") for e in entries],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "provider": _provider(), "kill_switch": kill_switch_engaged()}
