"""Postgres backing store for the audit log.

The audit trail is the product's headline claim, so it cannot live only on
an ephemeral container filesystem — a restart wiped a verified recovery in
practice, which is exactly the failure this module removes.

Two things the file backend could not do:

  * `event_id` is a PRIMARY KEY, so a duplicate decision cannot be written
    even if the pipeline is run twice against the same batch. Project guide
    §6 asks for precisely this constraint.
  * The log survives restarts, redeploys and scaling.

psycopg2 is imported lazily so local development and the test suite run
without the driver installed; `DATABASE_URL` selects this backend.
"""

from __future__ import annotations

import json
import os
from typing import Any

_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id    TEXT PRIMARY KEY,
    mandate_id  TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome     TEXT NOT NULL,
    entry       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_mandate_idx ON audit_log (mandate_id);
CREATE INDEX IF NOT EXISTS audit_log_recorded_idx ON audit_log (recorded_at);
"""


def enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    import psycopg2  # imported lazily — not needed for the file backend

    dsn = os.environ["DATABASE_URL"]
    # Render hands out postgres:// URLs; psycopg2 wants postgresql://
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(dsn, connect_timeout=10)


def _ensure_schema(conn) -> None:
    global _initialised
    if _initialised:
        return
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    _initialised = True


def append_entry(entry: dict[str, Any]) -> None:
    """Insert one decision. A repeated event_id is ignored, not duplicated."""
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (event_id, mandate_id, outcome, entry)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (entry["event_id"], entry["mandate_id"], entry["outcome"], json.dumps(entry)),
            )
        conn.commit()


def load_log() -> list[dict[str, Any]]:
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT entry FROM audit_log ORDER BY recorded_at, event_id")
            return [row[0] for row in cur.fetchall()]


def mark_recovered(mandate_id: str, payment_link_id: str | None = None) -> bool:
    """Flip the most recent entry for a mandate to `recovered`.

    Returns False when there is nothing to update or it is already recovered,
    so a duplicate webhook delivery stays a no-op.
    """
    with _connect() as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, outcome, entry FROM audit_log
                WHERE mandate_id = %s
                ORDER BY recorded_at DESC, event_id DESC
                LIMIT 1
                """,
                (mandate_id,),
            )
            row = cur.fetchone()
            if not row:
                return False

            event_id, outcome, entry = row
            if outcome == "recovered":
                return False

            entry["outcome"] = "recovered"
            if payment_link_id:
                entry.setdefault("input_signal", {})["payment_link_id"] = payment_link_id

            cur.execute(
                "UPDATE audit_log SET outcome = 'recovered', entry = %s WHERE event_id = %s",
                (json.dumps(entry), event_id),
            )
        conn.commit()
        return True
