"""Immutable, append-only audit log (project guide §6).

One row per decision. Two interchangeable backends behind one interface:

  * **Postgres** when `DATABASE_URL` is set — durable across restarts, with
    `event_id` as a PRIMARY KEY so a duplicate decision cannot be written.
  * **JSONL file** otherwise — zero setup for local development and tests.

Both are idempotent: a duplicate webhook delivery for an already-recovered
mandate is a no-op, never a double-counted recovery.

The file backend is fine locally but is *not* durable on ephemeral hosting —
a container restart destroyed a verified recovery in practice, which is why
the Postgres backend exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import pg_store
from agent.schemas import AuditLogEntry

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "audit_log.jsonl"


def backend() -> str:
    return "postgres" if pg_store.enabled() else "file"


def append_entry(entry: AuditLogEntry) -> None:
    if pg_store.enabled():
        pg_store.append_entry(entry.model_dump(mode="json"))
        return

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.model_dump(mode="json")) + "\n")


def load_log() -> list[dict]:
    if pg_store.enabled():
        return pg_store.load_log()

    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def mark_recovered(mandate_id: str, payment_link_id: str | None = None) -> bool:
    """Flip the most recent entry for a mandate to `recovered`.

    Only a signature-verified webhook (see webhook_handler.py) should call
    this. Returns False if there is nothing to update or it is already
    recovered, so a duplicate delivery is a safe no-op.
    """
    if pg_store.enabled():
        return pg_store.mark_recovered(mandate_id, payment_link_id)

    entries = load_log()
    target_index = None
    for i in range(len(entries) - 1, -1, -1):
        if entries[i]["mandate_id"] == mandate_id:
            target_index = i
            break

    if target_index is None or entries[target_index]["outcome"] == "recovered":
        return False

    entries[target_index]["outcome"] = "recovered"
    if payment_link_id:
        entries[target_index].setdefault("input_signal", {})["payment_link_id"] = payment_link_id

    with LOG_PATH.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return True
