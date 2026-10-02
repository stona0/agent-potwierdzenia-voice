"""Prosty magazyn połączeń w SQLite. Jeden wiersz = jedno spotkanie do potwierdzenia."""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Statusy końcowe - po nich nie dzwonimy już w sprawie danego spotkania
FINAL_STATUSES = {"CONFIRMED", "RESCHEDULE", "CANCELLED", "WRONG_PERSON", "UNCLEAR", "SMS_SENT", "UNREACHED", "FAILED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    event_id        TEXT PRIMARY KEY,
    calendar_id     TEXT NOT NULL,
    start_at        TEXT NOT NULL,          -- ISO 8601, UTC
    phone           TEXT NOT NULL,
    client_name     TEXT NOT NULL,
    advisor_name    TEXT NOT NULL,
    location        TEXT NOT NULL,
    status          TEXT NOT NULL,          -- PENDING, RETRY, CALLING albo jeden z FINAL_STATUSES
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    call_sid        TEXT,
    call_token      TEXT,
    note            TEXT NOT NULL DEFAULT '',
    transcript      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);
"""


@dataclass
class Call:
    event_id: str
    calendar_id: str
    start_at: datetime
    phone: str
    client_name: str
    advisor_name: str
    location: str
    status: str
    attempts: int
    next_attempt_at: datetime
    call_sid: str | None
    call_token: str | None
    note: str
    transcript: str
    updated_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_call(row: sqlite3.Row) -> Call:
    data = dict(row)
    for key in ("start_at", "next_attempt_at", "updated_at"):
        data[key] = datetime.fromisoformat(data[key])
    return Call(**data)


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def upsert_meeting(
        self,
        *,
        event_id: str,
        calendar_id: str,
        start_at: datetime,
        phone: str,
        client_name: str,
        advisor_name: str,
        location: str,
        first_attempt_at: datetime,
    ) -> None:
        """Dodaje spotkanie. Jeśli już jest i jeszcze nie dzwoniliśmy - aktualizuje dane z kalendarza."""
        now = utcnow().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO calls (event_id, calendar_id, start_at, phone, client_name, advisor_name,
                                   location, status, next_attempt_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    start_at = excluded.start_at,
                    phone = excluded.phone,
                    client_name = excluded.client_name,
                    advisor_name = excluded.advisor_name,
                    location = excluded.location,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
                WHERE calls.status = 'PENDING'
                """,
                (event_id, calendar_id, start_at.isoformat(), phone, client_name, advisor_name,
                 location, first_attempt_at.isoformat(), now),
            )

    def get(self, event_id: str) -> Call | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE event_id = ?", (event_id,)).fetchone()
        return _row_to_call(row) if row else None

    def get_by_call_sid(self, call_sid: str) -> Call | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
        return _row_to_call(row) if row else None

    def due_calls(self, now: datetime) -> list[Call]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE status IN ('PENDING', 'RETRY') AND next_attempt_at <= ?"
                " ORDER BY start_at",
                (now.isoformat(),),
            ).fetchall()
        return [_row_to_call(r) for r in rows]

    def stale_calling(self, older_than: datetime) -> list[Call]:
        """Połączenia, które utknęły w CALLING (np. zgubiony webhook statusu).
        Dla wiersza w CALLING next_attempt_at to moment rozpoczęcia połączenia."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE status = 'CALLING' AND next_attempt_at <= ?", (older_than.isoformat(),)
            ).fetchall()
        return [_row_to_call(r) for r in rows]

    def recent(self, since: datetime) -> list[Call]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls WHERE start_at >= ? ORDER BY start_at", (since.isoformat(),)
            ).fetchall()
        return [_row_to_call(r) for r in rows]

    def update(self, event_id: str, **fields) -> None:
        fields["updated_at"] = utcnow()
        values = [v.isoformat() if isinstance(v, datetime) else v for v in fields.values()]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE calls SET {assignments} WHERE event_id = ?", (*values, event_id))
