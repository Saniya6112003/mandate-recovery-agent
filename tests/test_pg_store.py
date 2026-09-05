"""The Postgres backend exists because the file backend is not durable.

A container restart on ephemeral hosting destroyed a verified recovery in
practice — the audit log is the product's headline claim, so it cannot live
only on a disk that disappears. These tests pin the backend switch and the
SQL contract without needing a live database.
"""

from __future__ import annotations

import json

import pytest

from agent import audit_log, pg_store


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((" ".join(sql.split()), params))
        if "SELECT entry FROM audit_log" in sql:
            self._result = [(row,) for row in self.conn.rows]
        elif "SELECT event_id, outcome, entry" in sql:
            mandate = params[0]
            matches = [r for r in self.conn.rows if r["mandate_id"] == mandate]
            self._result = (
                [(matches[-1]["event_id"], matches[-1]["outcome"], matches[-1])]
                if matches
                else []
            )

    def fetchall(self):
        return self._result or []

    def fetchone(self):
        return (self._result or [None])[0] if self._result else None


class FakeConn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.statements = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def fresh_schema_flag(monkeypatch):
    monkeypatch.setattr(pg_store, "_initialised", False)
    yield


def test_backend_is_file_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert audit_log.backend() == "file"
    assert pg_store.enabled() is False


def test_backend_switches_to_postgres_with_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    assert audit_log.backend() == "postgres"
    assert pg_store.enabled() is True


def test_append_uses_on_conflict_so_a_decision_cannot_duplicate(monkeypatch):
    """Project guide §6 asks for a unique constraint on event_id. A file
    cannot enforce that; the primary key plus ON CONFLICT can.
    """
    conn = FakeConn()
    monkeypatch.setattr(pg_store, "_connect", lambda: conn)

    pg_store.append_entry(
        {"event_id": "evt-1", "mandate_id": "MID1", "outcome": "pending", "x": 1}
    )

    inserts = [s for s, _ in conn.statements if s.startswith("INSERT")]
    assert len(inserts) == 1
    assert "ON CONFLICT (event_id) DO NOTHING" in inserts[0]
    assert conn.commits >= 1


def test_schema_creates_primary_key_and_is_idempotent():
    assert "event_id    TEXT PRIMARY KEY" in pg_store.SCHEMA
    assert "CREATE TABLE IF NOT EXISTS" in pg_store.SCHEMA
    assert "CREATE INDEX IF NOT EXISTS" in pg_store.SCHEMA


def test_mark_recovered_flips_the_latest_entry(monkeypatch):
    rows = [
        {"event_id": "e1", "mandate_id": "MID1", "outcome": "pending", "input_signal": {}},
    ]
    conn = FakeConn(rows)
    monkeypatch.setattr(pg_store, "_connect", lambda: conn)

    assert pg_store.mark_recovered("MID1", payment_link_id="plink_x") is True

    updates = [(s, p) for s, p in conn.statements if s.startswith("UPDATE")]
    assert len(updates) == 1
    written = json.loads(updates[0][1][0])
    assert written["outcome"] == "recovered"
    assert written["input_signal"]["payment_link_id"] == "plink_x"


def test_mark_recovered_is_idempotent(monkeypatch):
    """A duplicate webhook delivery must not double-count a recovery."""
    rows = [{"event_id": "e1", "mandate_id": "MID1", "outcome": "recovered", "input_signal": {}}]
    conn = FakeConn(rows)
    monkeypatch.setattr(pg_store, "_connect", lambda: conn)

    assert pg_store.mark_recovered("MID1") is False
    assert not [s for s, _ in conn.statements if s.startswith("UPDATE")]


def test_mark_recovered_on_unknown_mandate_is_a_noop(monkeypatch):
    conn = FakeConn([])
    monkeypatch.setattr(pg_store, "_connect", lambda: conn)

    assert pg_store.mark_recovered("NOPE") is False


def test_render_postgres_url_scheme_is_normalised(monkeypatch):
    """Render issues postgres:// URLs; psycopg2 requires postgresql://."""
    captured = {}

    class FakePsycopg2:
        @staticmethod
        def connect(dsn, connect_timeout=None):
            captured["dsn"] = dsn
            return FakeConn()

    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", FakePsycopg2)

    pg_store._connect()

    assert captured["dsn"].startswith("postgresql://")
