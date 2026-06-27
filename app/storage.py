from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class SignalStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    signal_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    symbol TEXT,
                    side TEXT,
                    payload TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def exists(self, signal_key: str) -> bool:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM signals WHERE signal_key = ?", (signal_key,)).fetchone()
            return row is not None

    def mark_received(self, signal_key: str, symbol: str, side: str, payload: str) -> None:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signals(signal_key, status, symbol, side, payload, error, created_at, updated_at)
                VALUES(?, 'received', ?, ?, ?, NULL, ?, ?)
                """,
                (signal_key, symbol, side, payload, now, now),
            )
            conn.commit()

    def mark_done(self, signal_key: str) -> None:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE signals SET status = 'done', updated_at = ?, error = NULL WHERE signal_key = ?",
                (now, signal_key),
            )
            conn.commit()

    def mark_failed(self, signal_key: str, error: str) -> None:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE signals SET status = 'failed', updated_at = ?, error = ? WHERE signal_key = ?",
                (now, error[:2000], signal_key),
            )
            conn.commit()
