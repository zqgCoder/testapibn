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


class AccountRiskStore:
    """Persist successful entry opens for daily trade count and symbol cooldown."""

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
                CREATE TABLE IF NOT EXISTS account_risk_opens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    planned_risk_usdt TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_risk_opens_symbol_time ON account_risk_opens(symbol, opened_at)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_opens_time ON account_risk_opens(opened_at)")
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_successful_open(self, signal_key: str, symbol: str, planned_risk_usdt: str | None = None) -> None:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO account_risk_opens(signal_key, symbol, opened_at, planned_risk_usdt)
                VALUES(?, ?, ?, ?)
                """,
                (signal_key, symbol.upper(), now, planned_risk_usdt),
            )
            conn.commit()

    def count_opens_since(self, since_iso: str) -> int:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM account_risk_opens WHERE opened_at >= ?",
                (since_iso,),
            ).fetchone()
            return int(row[0]) if row else 0

    def last_open_at(self, symbol: str) -> datetime | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT opened_at FROM account_risk_opens
                WHERE symbol = ?
                ORDER BY opened_at DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None


class TradeJournalStore:
    EXECUTION_COLUMNS = (
        "signal_key",
        "signal_id",
        "symbol",
        "side",
        "entry_type",
        "risk_mode",
        "position_policy",
        "status",
        "skip_reason",
        "error_message",
        "planned_qty",
        "filled_qty",
        "entry_price",
        "stop_loss_price",
        "target_risk_usdt",
        "estimated_total_loss_at_sl",
        "leverage",
        "account_risk_allowed",
        "account_risk_skip_reason",
        "raw_signal_json",
        "plan_json",
        "account_risk_json",
        "entry_summary_json",
        "protection_summary_json",
        "result_json",
        "created_at",
        "updated_at",
    )

    ORDER_COLUMNS = (
        "execution_id",
        "signal_key",
        "symbol",
        "role",
        "order_id",
        "algo_id",
        "client_order_id",
        "side",
        "order_type",
        "status",
        "price",
        "avg_price",
        "quantity",
        "executed_qty",
        "trigger_price",
        "reduce_only",
        "close_position",
        "raw_order_json",
        "created_at",
    )

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL UNIQUE,
                    signal_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_type TEXT,
                    risk_mode TEXT,
                    position_policy TEXT,
                    status TEXT NOT NULL,
                    skip_reason TEXT,
                    error_message TEXT,
                    planned_qty TEXT,
                    filled_qty TEXT,
                    entry_price TEXT,
                    stop_loss_price TEXT,
                    target_risk_usdt TEXT,
                    estimated_total_loss_at_sl TEXT,
                    leverage INTEGER,
                    account_risk_allowed INTEGER,
                    account_risk_skip_reason TEXT,
                    raw_signal_json TEXT,
                    plan_json TEXT,
                    account_risk_json TEXT,
                    entry_summary_json TEXT,
                    protection_summary_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id INTEGER NOT NULL,
                    signal_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    role TEXT NOT NULL,
                    order_id TEXT,
                    algo_id TEXT,
                    client_order_id TEXT,
                    side TEXT,
                    order_type TEXT,
                    status TEXT,
                    price TEXT,
                    avg_price TEXT,
                    quantity TEXT,
                    executed_qty TEXT,
                    trigger_price TEXT,
                    reduce_only INTEGER,
                    close_position INTEGER,
                    raw_order_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_symbol_time ON trade_executions(symbol, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exec_status_time ON trade_executions(status, created_at)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_created ON trade_executions(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_execution ON trade_orders(execution_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_signal ON trade_orders(signal_key)")
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def insert_execution(self, data: dict) -> int:
        now = self._now()
        values = {col: data.get(col) for col in self.EXECUTION_COLUMNS}
        values["created_at"] = now
        values["updated_at"] = now
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO trade_executions ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def insert_order(self, data: dict) -> int:
        now = self._now()
        values = {col: data.get(col) for col in self.ORDER_COLUMNS}
        values["created_at"] = now
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO trade_orders ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_executions(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        sql = f"SELECT * FROM trade_executions {where} ORDER BY id DESC LIMIT ?"
        with self.lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def list_executions_since(self, since_iso: str, *, limit: int = 500) -> list[dict]:
        cap = max(1, min(limit, 2000))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trade_executions
                WHERE created_at >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (since_iso, cap),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def get_execution(self, execution_id: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM trade_executions WHERE id = ?", (execution_id,)).fetchone()
        return self._row_to_dict(row)

    def list_orders(self, execution_id: int) -> list[dict]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_orders WHERE execution_id = ? ORDER BY id ASC",
                (execution_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def count_by_status(self) -> dict[str, int]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM trade_executions GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["cnt"]) for row in rows}

    def count_by_status_since(self, since_iso: str) -> dict[str, int]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM trade_executions
                WHERE created_at >= ?
                GROUP BY status
                """,
                (since_iso,),
            ).fetchall()
        return {str(row["status"]): int(row["cnt"]) for row in rows}

    def stats_by_symbol(self) -> list[dict]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    symbol,
                    COUNT(*) AS total_executions,
                    SUM(CASE WHEN status = 'protected' THEN 1 ELSE 0 END) AS protected_count,
                    SUM(CASE WHEN status = 'entry_not_filled' THEN 1 ELSE 0 END) AS entry_not_filled_count,
                    SUM(CASE WHEN status = 'blocked_by_account_risk' THEN 1 ELSE 0 END) AS blocked_count,
                    SUM(CASE WHEN status = 'blocked_by_runtime_lock' THEN 1 ELSE 0 END) AS runtime_lock_count,
                    SUM(CASE WHEN status = 'protection_failed' THEN 1 ELSE 0 END) AS protection_failed_count
                FROM trade_executions
                GROUP BY symbol
                ORDER BY total_executions DESC, symbol ASC
                """
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "symbol": row["symbol"],
                    "total_executions": int(row["total_executions"]),
                    "protected_count": int(row["protected_count"] or 0),
                    "entry_not_filled_count": int(row["entry_not_filled_count"] or 0),
                    "blocked_count": int(row["blocked_count"] or 0),
                    "runtime_lock_count": int(row["runtime_lock_count"] or 0),
                    "protection_failed_count": int(row["protection_failed_count"] or 0),
                }
            )
        return result

    def stats_rejections(self, limit: int = 20) -> list[dict]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(skip_reason, status) AS reason,
                    status,
                    COUNT(*) AS count
                FROM trade_executions
                WHERE status IN (
                    'blocked_by_account_risk',
                    'blocked_by_runtime_lock',
                    'skipped_by_position_policy',
                    'entry_not_filled',
                    'protection_failed',
                    'failed'
                )
                GROUP BY COALESCE(skip_reason, status), status
                ORDER BY count DESC, reason ASC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [
            {
                "reason": row["reason"],
                "status": row["status"],
                "count": int(row["count"]),
            }
            for row in rows
        ]


class RuntimeControlStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    locked INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    locked_until TEXT,
                    locked_by TEXT,
                    locked_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_lock_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    reason TEXT,
                    locked_until TEXT,
                    actor TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_created ON runtime_lock_events(created_at)"
            )
            self._ensure_one_shot_columns(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT OR IGNORE INTO runtime_state(id, locked, reason, locked_until, locked_by, locked_at, updated_at)
                VALUES (1, 0, NULL, NULL, NULL, NULL, ?)
                """,
                (now,),
            )
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    @staticmethod
    def _ensure_one_shot_columns(conn) -> None:
        columns = {
            "one_shot_enabled": "INTEGER NOT NULL DEFAULT 0",
            "one_shot_remaining": "INTEGER NOT NULL DEFAULT 0",
            "one_shot_reason": "TEXT",
            "one_shot_operator": "TEXT",
            "one_shot_started_at": "TEXT",
            "one_shot_expires_at": "TEXT",
            "one_shot_consumed_by_signal_id": "TEXT",
            "one_shot_consumed_at": "TEXT",
        }
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(runtime_state)").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE runtime_state ADD COLUMN {name} {definition}")

    @staticmethod
    def _normalize_state(row: sqlite3.Row | None) -> dict:
        state = RuntimeControlStore._row_to_dict(row) or {}
        return {
            "locked": bool(state.get("locked")),
            "reason": state.get("reason"),
            "locked_until": state.get("locked_until"),
            "locked_by": state.get("locked_by"),
            "locked_at": state.get("locked_at"),
            "updated_at": state.get("updated_at"),
            "one_shot_enabled": bool(state.get("one_shot_enabled")),
            "one_shot_remaining": int(state.get("one_shot_remaining") or 0),
            "one_shot_reason": state.get("one_shot_reason"),
            "one_shot_operator": state.get("one_shot_operator"),
            "one_shot_started_at": state.get("one_shot_started_at"),
            "one_shot_expires_at": state.get("one_shot_expires_at"),
            "one_shot_consumed_by_signal_id": state.get("one_shot_consumed_by_signal_id"),
            "one_shot_consumed_at": state.get("one_shot_consumed_at"),
        }

    def get_state(self) -> dict:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()
        return self._normalize_state(row)

    def _clear_one_shot_sql(self) -> str:
        return """
            one_shot_enabled = 0,
            one_shot_remaining = 0,
            one_shot_reason = NULL,
            one_shot_operator = NULL,
            one_shot_started_at = NULL,
            one_shot_expires_at = NULL,
            one_shot_consumed_by_signal_id = NULL,
            one_shot_consumed_at = NULL
        """

    def set_locked(
        self,
        *,
        reason: str | None,
        locked_until: str | None,
        actor: str | None,
    ) -> dict:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE runtime_state
                SET locked = 1, reason = ?, locked_until = ?, locked_by = ?, locked_at = ?, updated_at = ?,
                    {self._clear_one_shot_sql()}
                WHERE id = 1
                """,
                (reason, locked_until, actor, now, now),
            )
            conn.commit()
        return self.get_state()

    def set_unlocked(self, *, actor: str | None) -> dict:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE runtime_state
                SET locked = 0, reason = NULL, locked_until = NULL, locked_by = NULL, locked_at = NULL,
                    updated_at = ?, {self._clear_one_shot_sql()}
                WHERE id = 1
                """,
                (now,),
            )
            conn.commit()
        return self.get_state()

    def set_one_shot_unlock(
        self,
        *,
        reason: str,
        operator: str | None,
        started_at: str,
        expires_at: str,
    ) -> dict:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE runtime_state
                SET locked = 0, reason = NULL, locked_until = NULL, locked_by = NULL, locked_at = NULL,
                    updated_at = ?,
                    one_shot_enabled = 1,
                    one_shot_remaining = 1,
                    one_shot_reason = ?,
                    one_shot_operator = ?,
                    one_shot_started_at = ?,
                    one_shot_expires_at = ?,
                    one_shot_consumed_by_signal_id = NULL,
                    one_shot_consumed_at = NULL
                WHERE id = 1
                """,
                (now, reason, operator, started_at, expires_at),
            )
            conn.commit()
        return self.get_state()

    def consume_one_shot_and_lock(
        self,
        *,
        signal_id: str,
        lock_reason: str,
        actor: str = "system",
    ) -> dict:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_state
                SET locked = 1,
                    reason = ?,
                    locked_until = NULL,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?,
                    one_shot_enabled = 0,
                    one_shot_remaining = 0,
                    one_shot_consumed_by_signal_id = ?,
                    one_shot_consumed_at = ?
                WHERE id = 1
                  AND one_shot_enabled = 1
                  AND one_shot_remaining > 0
                  AND one_shot_consumed_at IS NULL
                """,
                (lock_reason, actor, now, now, signal_id, now),
            )
            conn.commit()
        return self.get_state()

    def expire_one_shot_and_lock(self, *, lock_reason: str, actor: str = "system") -> dict:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_state
                SET locked = 1,
                    reason = ?,
                    locked_until = NULL,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?,
                    one_shot_enabled = 0,
                    one_shot_remaining = 0
                WHERE id = 1
                  AND one_shot_enabled = 1
                  AND one_shot_remaining > 0
                  AND one_shot_consumed_at IS NULL
                """,
                (lock_reason, actor, now, now),
            )
            conn.commit()
        return self.get_state()

    def append_event(
        self,
        *,
        action: str,
        reason: str | None = None,
        locked_until: str | None = None,
        actor: str | None = None,
    ) -> None:
        now = self._now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_lock_events(action, reason, locked_until, actor, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (action, reason, locked_until, actor, now),
            )
            conn.commit()

    def list_events(self, limit: int = 50) -> list[dict]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, action, reason, locked_until, actor, created_at
                FROM runtime_lock_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]
