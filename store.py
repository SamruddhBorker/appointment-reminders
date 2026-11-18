"""SQLite storage for appointments and sent reminders. Standard library only."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  service TEXT NOT NULL DEFAULT '',
  start TEXT NOT NULL,                       -- 'YYYY-MM-DD HH:MM' local time
  status TEXT NOT NULL DEFAULT 'booked',     -- booked|confirmed|cancelled|attended|no_show
  notes TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_start ON appointments(start);
CREATE TABLE IF NOT EXISTS reminders (
  appointment_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,                      -- sent|skipped|failed
  channel TEXT,
  at TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (appointment_id, kind)
);
"""

STATUSES = ("booked", "confirmed", "cancelled", "attended", "no_show")
FMT = "%Y-%m-%d %H:%M"


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, name: str, phone: Optional[str], email: Optional[str], service: str,
            start: datetime, notes: str = "", now: Optional[datetime] = None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO appointments(name,phone,email,service,start,notes,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (name, phone, email, service, start.strftime(FMT), notes,
                 (now or datetime.now()).strftime(FMT)))
            return cur.lastrowid

    def get(self, appt_id: int):
        with self.lock:
            return self.db.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()

    def set_status(self, appt_id: int, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        with self.lock:
            return self.db.execute("UPDATE appointments SET status=? WHERE id=?",
                                   (status, appt_id)).rowcount == 1

    def delete(self, appt_id: int) -> bool:
        with self.lock:
            self.db.execute("DELETE FROM reminders WHERE appointment_id=?", (appt_id,))
            return self.db.execute("DELETE FROM appointments WHERE id=?", (appt_id,)).rowcount == 1

    def between(self, start: datetime, end: datetime):
        with self.lock:
            return self.db.execute(
                "SELECT * FROM appointments WHERE start>=? AND start<? ORDER BY start",
                (start.strftime(FMT), end.strftime(FMT))).fetchall()

    def active_after(self, now: datetime):
        """Booked/confirmed appointments that haven't started yet."""
        with self.lock:
            return self.db.execute(
                "SELECT * FROM appointments WHERE start>? AND status IN ('booked','confirmed') "
                "ORDER BY start", (now.strftime(FMT),)).fetchall()

    def all(self):
        with self.lock:
            return self.db.execute("SELECT * FROM appointments ORDER BY start").fetchall()

    # ---- reminders
    def done_kinds(self, appt_id: int) -> set[str]:
        """Kinds that are finished (sent or deliberately skipped). Failed ones are retried."""
        with self.lock:
            rows = self.db.execute(
                "SELECT kind FROM reminders WHERE appointment_id=? AND status IN ('sent','skipped')",
                (appt_id,)).fetchall()
        return {r["kind"] for r in rows}

    def record(self, appt_id: int, kind: str, status: str, channel: str, now: datetime,
               detail: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO reminders(appointment_id,kind,status,channel,at,detail) "
                "VALUES(?,?,?,?,?,?)", (appt_id, kind, status, channel, now.strftime(FMT), detail[:300]))

    def reminders_for(self, appt_id: int):
        with self.lock:
            return self.db.execute("SELECT * FROM reminders WHERE appointment_id=? ORDER BY at",
                                   (appt_id,)).fetchall()
