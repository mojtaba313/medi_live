#!/usr/bin/env python3
"""Glossary storage: per-room (per-class) term sets in SQLite.

Why SQLite and not PostgreSQL?
  - Single-node, single-writer app (FastAPI on one i5 box, local-only).
  - Zero ops: no daemon, no users/passwords, no extra backup story — one
    file (server/app.db) next to rooms.json and archive/, backed up the
    same way. Python stdlib sqlite3, no new dependency.
  - Glossary workload is tiny (hundreds of terms, read once per session,
    written only from the admin panel).

PostgreSQL would only pay off with multiple app servers, heavy concurrent
writes, or full-text search at scale — none of which apply here. If that
day comes, only this module needs swapping: everything else talks to the
GlossaryStore interface (get_terms / add_term / delete_term /
replace_terms / delete_room / counts).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("db")

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("APP_DB", str(HERE / "app.db")))


def _clean_term(t: str) -> str:
    t = " ".join(str(t or "").split())
    return t[:120]


class GlossaryStore:
    """Thread-safe SQLite store: room_id -> set of canonical terms."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        # check_same_thread=False: FastAPI runs sync endpoints in a threadpool.
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._con.execute(
                """CREATE TABLE IF NOT EXISTS glossary_terms (
                     room_id   TEXT NOT NULL,
                     term      TEXT NOT NULL,
                     created_at REAL NOT NULL,
                     PRIMARY KEY (room_id, term)
                   )"""
            )
            self._con.commit()

    # -- reads --

    def get_terms(self, room_id: str) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT term FROM glossary_terms WHERE room_id=? ORDER BY term",
                (room_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_all(self) -> dict[str, list[str]]:
        """room_id -> terms, for warming the corrector cache at startup."""
        with self._lock:
            rows = self._con.execute(
                "SELECT room_id, term FROM glossary_terms ORDER BY room_id, term"
            ).fetchall()
        out: dict[str, list[str]] = {}
        for rid, term in rows:
            out.setdefault(rid, []).append(term)
        return out

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._con.execute(
                "SELECT room_id, COUNT(*) FROM glossary_terms GROUP BY room_id"
            ).fetchall()
        return {rid: n for rid, n in rows}

    # -- writes (admin only) --

    def add_term(self, room_id: str, term: str) -> bool:
        """Insert one term. Returns True if it was new."""
        term = _clean_term(term)
        if not term:
            return False
        with self._lock:
            cur = self._con.execute(
                "INSERT OR IGNORE INTO glossary_terms VALUES (?,?,?)",
                (room_id, term, time.time()),
            )
            self._con.commit()
            return cur.rowcount > 0

    def add_many(self, room_id: str, terms: list[str]) -> int:
        """Bulk add (paste a list). Returns number of newly added terms."""
        now = time.time()
        rows = [(room_id, t, now) for t in (_clean_term(x) for x in terms) if t]
        if not rows:
            return 0
        with self._lock:
            before = self._con.total_changes
            self._con.executemany(
                "INSERT OR IGNORE INTO glossary_terms VALUES (?,?,?)", rows
            )
            self._con.commit()
            return self._con.total_changes - before

    def replace_terms(self, room_id: str, terms: list[str]) -> int:
        """Replace the room's whole set (bulk paste from class slides)."""
        terms = [_clean_term(x) for x in terms]
        terms = [t for t in terms if t]
        now = time.time()
        with self._lock:
            self._con.execute("DELETE FROM glossary_terms WHERE room_id=?", (room_id,))
            self._con.executemany(
                "INSERT OR IGNORE INTO glossary_terms VALUES (?,?,?)",
                [(room_id, t, now) for t in terms],
            )
            self._con.commit()
            return len(set(terms))

    def delete_term(self, room_id: str, term: str) -> bool:
        with self._lock:
            cur = self._con.execute(
                "DELETE FROM glossary_terms WHERE room_id=? AND term=?",
                (room_id, _clean_term(term)),
            )
            self._con.commit()
            return cur.rowcount > 0

    def delete_room(self, room_id: str) -> None:
        """Drop a room's terms when the room itself is deleted."""
        with self._lock:
            self._con.execute("DELETE FROM glossary_terms WHERE room_id=?", (room_id,))
            self._con.commit()

    def close(self) -> None:
        with self._lock:
            self._con.close()
