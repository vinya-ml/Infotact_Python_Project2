"""Database Repository Interface & SQLite Implementation for ChronosMatch Trades (Member 3).

Persists matched trades with:
- Idempotent deduplication (PRIMARY KEY on trade_id + INSERT OR IGNORE).
- High-concurrency WAL mode and busy_timeout locks handling.
- Efficient batch insertion transactions.
- Retry logic with exponential backoff on transient database lock contentions.
- Query methods for audit, verification, and UI reporting.
"""

from __future__ import annotations

import abc
import datetime
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TradeRecord:
    """Canonical persistent trade record representation."""
    trade_id: str
    buy_order_id: int
    sell_order_id: int
    price: float
    quantity: int
    notional: float
    timestamp_ns: int
    symbol: str = "DEFAULT"
    created_at: str = ""
    persisted_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not self.persisted_at:
            self.persisted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if self.notional <= 0.0 and self.price > 0.0 and self.quantity > 0:
            self.notional = round(self.price * self.quantity, 4)


class BaseTradeRepository(abc.ABC):
    """Abstract trade repository interface for pluggable storage backends."""

    @abc.abstractmethod
    def initialize(self) -> None:
        """Create tables, indexes, and prepare storage backend."""

    @abc.abstractmethod
    def save_trades_batch(self, trades: list[TradeRecord]) -> tuple[int, int]:
        """Save a batch of trades. Returns (inserted_count, duplicate_count)."""

    @abc.abstractmethod
    def get_trade_count(self) -> int:
        """Return total number of trades persisted in storage."""

    @abc.abstractmethod
    def get_trades(self, limit: int = 100, offset: int = 0) -> list[TradeRecord]:
        """Fetch historical trades for audit/inspection."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release database connections and resources."""


class SQLiteTradeRepository(BaseTradeRepository):
    """Production-grade SQLite trade repository with WAL mode and resilient batching."""

    def __init__(
        self,
        db_path: str = "chronosmatch_trades.db",
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_backoff: float = 0.05,
    ) -> None:
        self.db_path = db_path
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff = float(retry_backoff)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._is_closed = False

        self.initialize()

    def _get_connection(self) -> sqlite3.Connection:
        """Get or initialize SQLite connection with WAL optimization."""
        if self._conn is None or self._is_closed:
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)

            self._conn = sqlite3.connect(
                self.db_path,
                timeout=self.timeout,
                check_same_thread=False,
            )
            # Enable WAL mode for non-blocking concurrent reads and writes
            # If :memory:, WAL is ignored
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)};")
            self._conn.execute("PRAGMA cache_size=-4000;")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def initialize(self) -> None:
        """Create schema and secondary indices."""
        with self._lock:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS matched_trades (
                        trade_id TEXT PRIMARY KEY,
                        buy_order_id INTEGER NOT NULL,
                        sell_order_id INTEGER NOT NULL,
                        price REAL NOT NULL,
                        quantity INTEGER NOT NULL,
                        notional REAL NOT NULL,
                        timestamp_ns INTEGER NOT NULL,
                        symbol TEXT NOT NULL DEFAULT 'DEFAULT',
                        created_at TEXT NOT NULL,
                        persisted_at TEXT NOT NULL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON matched_trades(timestamp_ns);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_buy_order ON matched_trades(buy_order_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_sell_order ON matched_trades(sell_order_id);")

    def save_trades_batch(self, trades: list[TradeRecord]) -> tuple[int, int]:
        """Idempotently insert a batch of trades within a single database transaction.

        Returns (inserted_count, duplicate_count).
        Handles temporary SQLite busy/lock conditions with exponential retries.
        """
        if not trades:
            return 0, 0

        rows = []
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for t in trades:
            persisted_ts = t.persisted_at or now_iso
            rows.append((
                t.trade_id,
                t.buy_order_id,
                t.sell_order_id,
                t.price,
                t.quantity,
                t.notional,
                t.timestamp_ns,
                t.symbol,
                t.created_at or now_iso,
                persisted_ts,
            ))

        sql = """
            INSERT OR IGNORE INTO matched_trades (
                trade_id, buy_order_id, sell_order_id, price, quantity,
                notional, timestamp_ns, symbol, created_at, persisted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        with self._lock:
            conn = self._get_connection()
            last_err: Exception | None = None

            for attempt in range(self.max_retries):
                try:
                    changes_before = conn.total_changes
                    with conn:
                        conn.executemany(sql, rows)
                    inserted = conn.total_changes - changes_before
                    duplicates = len(rows) - inserted
                    return inserted, duplicates
                except sqlite3.OperationalError as exc:
                    last_err = exc
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        # Exponential backoff
                        time.sleep(self.retry_backoff * (2 ** attempt))
                        continue
                    raise
                except Exception as exc:
                    last_err = exc
                    raise

            if last_err is not None:
                raise last_err
            return 0, 0

    def get_trade_count(self) -> int:
        """Return total count of persisted trades."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM matched_trades;")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_trades(self, limit: int = 100, offset: int = 0) -> list[TradeRecord]:
        """Fetch paginated trade records."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_id, buy_order_id, sell_order_id, price, quantity, notional, "
                "timestamp_ns, symbol, created_at, persisted_at "
                "FROM matched_trades ORDER BY rowid ASC LIMIT ? OFFSET ?;",
                (limit, offset),
            )
            records = []
            for row in cur.fetchall():
                records.append(TradeRecord(
                    trade_id=str(row["trade_id"]),
                    buy_order_id=int(row["buy_order_id"]),
                    sell_order_id=int(row["sell_order_id"]),
                    price=float(row["price"]),
                    quantity=int(row["quantity"]),
                    notional=float(row["notional"]),
                    timestamp_ns=int(row["timestamp_ns"]),
                    symbol=str(row["symbol"]),
                    created_at=str(row["created_at"]),
                    persisted_at=str(row["persisted_at"]),
                ))
            return records

    def get_latest_trades(self, limit: int = 10) -> list[TradeRecord]:
        """Fetch the most recently persisted trades."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_id, buy_order_id, sell_order_id, price, quantity, notional, "
                "timestamp_ns, symbol, created_at, persisted_at "
                "FROM matched_trades ORDER BY rowid DESC LIMIT ?;",
                (limit,),
            )
            records = []
            for row in cur.fetchall():
                records.append(TradeRecord(
                    trade_id=str(row["trade_id"]),
                    buy_order_id=int(row["buy_order_id"]),
                    sell_order_id=int(row["sell_order_id"]),
                    price=float(row["price"]),
                    quantity=int(row["quantity"]),
                    notional=float(row["notional"]),
                    timestamp_ns=int(row["timestamp_ns"]),
                    symbol=str(row["symbol"]),
                    created_at=str(row["created_at"]),
                    persisted_at=str(row["persisted_at"]),
                ))
            return list(reversed(records))

    def query_aggregate_stats(self) -> dict[str, Any]:
        """Return total volume, notional, and VWAP from database."""
        with self._lock:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), SUM(quantity), SUM(notional) FROM matched_trades;")
            row = cur.fetchone()
            count = int(row[0]) if row and row[0] is not None else 0
            volume = int(row[1]) if row and row[1] is not None else 0
            notional = float(row[2]) if row and row[2] is not None else 0.0
            vwap = (notional / volume) if volume > 0 else 0.0
            return {
                "trade_count": count,
                "total_volume": volume,
                "total_notional": notional,
                "vwap": vwap,
            }

    def close(self) -> None:
        """Close SQLite connection safely."""
        with self._lock:
            if self._is_closed:
                return
            self._is_closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
