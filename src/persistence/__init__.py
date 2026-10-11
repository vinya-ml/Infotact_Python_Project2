"""ChronosMatch Background Persistence Layer (Member 3).

Persists matched trades asynchronously to a permanent audit ledger (SQLite)
without blocking the latency-critical matching engine.
"""

from .repository import BaseTradeRepository, SQLiteTradeRepository, TradeRecord
from .worker import BackgroundPersistenceWorker, WorkerState, PersistenceMetrics

__all__ = [
    "BaseTradeRepository",
    "SQLiteTradeRepository",
    "TradeRecord",
    "BackgroundPersistenceWorker",
    "WorkerState",
    "PersistenceMetrics",
]
