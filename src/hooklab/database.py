"""SQLite persistence for HookLab's synthetic events and deliveries."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3


def db_path() -> Path:
    """Return the database location, overridable for tests and local setups."""
    location = Path(os.environ.get("HOOKLAB_DB", "~/.hooklab/hooklab.db")).expanduser()
    location.parent.mkdir(parents=True, exist_ok=True)
    return location


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                behavior TEXT NOT NULL CHECK (behavior IN ('success', 'fail-once', 'always-fail')),
                status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_deliveries_event_id ON deliveries(event_id);
        """)
        # Upgrade databases created by earlier versions without destroying events.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN idempotency_key TEXT")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_key
               ON events(idempotency_key) WHERE idempotency_key IS NOT NULL"""
        )
