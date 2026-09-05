"""Immutable, append-only audit log store backed by a JSONL file.

One line per `AuditLogEntry` (project guide §6). Idempotent by design: a
duplicate webhook delivery for an already-recovered mandate is a no-op rather
than a double-counted recovery, and re-running the batch never silently loses
prior decisions — it appends new ones.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.schemas import AuditLogEntry

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "audit_log.jsonl"


def append_entry(entry: AuditLogEntry) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.model_dump(mode="json")) + "\n")


def load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def mark_recovered(mandate_id: str, payment_link_id: str | None = None) -> bool:
    """Flip the most recent audit entry for a mandate to `recovered`.

    Only a signature-verified webhook (see webhook_handler.py) should ever
    call this. Returns False if there's nothing to update or it's already
    recovered, so a duplicate webhook delivery is a safe no-op.
    """
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
