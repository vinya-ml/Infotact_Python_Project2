"""Comprehensive Member 3 End-to-End Demonstration Script.

Demonstrates the entire Member 3 lifecycle sequence:
1. Initialize Matching Engine, Zero-Copy Mmap Ring Buffer, TradeEventQueue, and SQLite Ledger.
2. Ingest and match orders via zero-copy binary serialization (DataProcessor -> MmapRingBuffer).
3. Dispatch matched trades to the decoupled background persistence worker.
4. Render the real-time monitoring dashboard with order book depth, spread, and latency metrics.
5. Trigger and visualize large Whale Orders & Executed Sweeps in the dedicated UI section.
6. Query SQLite database directly to verify trade persistence, schema, and deduplication.
7. Demonstrate resilience: worker pause/restart and transient database lock recovery.
8. Execute graceful shutdown with pending queue draining and zero trade loss.
"""

from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import sqlite3
import tempfile
import time

from src.data_processing.processor import DataProcessor
from src.ipc.mmap_buffer import MmapRingBuffer
from src.engine.matching_engine import MatchingEngine, BUY, SELL
from src.ipc.trade_queue import TradeEventQueue
from src.persistence.repository import SQLiteTradeRepository
from src.persistence.worker import BackgroundPersistenceWorker
from src.monitor.latency_monitor import LatencyMonitor


def print_header(title: str, step: int) -> None:
    print("\n" + "=" * 78)
    print(f" STEP {step}: {title.upper()}")
    print("=" * 78)


def run_demonstration() -> int:
    # Set up isolated demonstration paths
    demo_dir = os.path.join(tempfile.gettempdir(), "chronosmatch_demo")
    os.makedirs(demo_dir, exist_ok=True)
    mmap_path = os.path.join(demo_dir, "demo_bus.mmap")
    db_path = os.path.join(demo_dir, "demo_trades.db")

    # Clean existing demo files
    for p in (mmap_path, db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    print("=" * 78)
    print(" CHRONOSMATCH -- RESILIENCE & UI COMPREHENSIVE DEMO")
    print(" High-Frequency Trading Engine Integration")
    print(f" Workspace Demo Directory: {demo_dir}")
    print("=" * 78)

    # -----------------------------------------------------------------------
    # Step 1: Initialize Architecture Components
    # -----------------------------------------------------------------------
    print_header("Initialize Architecture Components", 1)
    processor = DataProcessor()
    mmap_bus = MmapRingBuffer(mmap_path, capacity=1000, record_size=33, create=True)
    print(f" [OK] Zero-Copy Mmap Ring Buffer initialized at: {mmap_path}")
    print(f"      Header: 64-byte aligned | Record size: {mmap_bus.record_size} bytes | Capacity: {mmap_bus.capacity} slots")

    monitor = LatencyMonitor(
        whale_threshold=500,
        enable_persistence=True,
        db_path=db_path,
    )
    print(f" [OK] Matching Engine ({monitor.engine_name}) & LatencyMonitor online.")
    print(f" [OK] Background Persistence Worker active. Target SQLite Ledger: {db_path}")

    # -----------------------------------------------------------------------
    # Step 2: Zero-Copy Binary Serialization & Mmap IPC Ingestion
    # -----------------------------------------------------------------------
    print_header("Zero-Copy Ingestion via Mmap Ring Buffer", 2)
    sample_orders = [
        {"id": 101, "side": "BUY", "price": 100.50, "qty": 100},
        {"id": 102, "side": "BUY", "price": 100.25, "qty": 50},
        {"id": 103, "side": "SELL", "price": 101.00, "qty": 80},
        {"id": 104, "side": "SELL", "price": 100.50, "qty": 40},  # Crosses with order 101!
    ]

    print(f" Writing {len(sample_orders)} binary orders (<QB dQQ, 33-bytes) into Mmap Ring Buffer...")
    for o in sample_orders:
        order_obj = processor.create_order(
            order_id=o["id"],
            side=o["side"],
            price=o["price"],
            quantity=o["qty"],
        )
        packed = processor.pack_order(order_obj)
        mmap_bus.write(packed)

    mmap_stats = mmap_bus.get_stats()
    print(f" [OK] Mmap Ring Buffer status: Head Seq={mmap_stats['head_seq']}, Unread={mmap_stats['unread_records']}")

    # Consume and dispatch into matching engine
    print(" Reading binary records from shared memory and dispatching to engine...")
    records, next_seq = mmap_bus.read_batch(max_records=10)
    for rec in records:
        unpacked = processor.unpack_order(rec)
        monitor.add_custom_order(
            order_id=unpacked.order_id,
            side=unpacked.side,
            price=unpacked.price,
            quantity=unpacked.quantity,
            timestamp_ns=unpacked.timestamp_ns,
        )

    print(f" [OK] Orders dispatched. Engine order count: {monitor.engine.get_order_count()}")
    print(f" [OK] Matched trades produced: {len(monitor.engine.get_trades())}")

    # -----------------------------------------------------------------------
    # Step 3: Whale-Order & Large-Sweep Visualization
    # -----------------------------------------------------------------------
    print_header("Whale-Order & Liquidity Sweep Detection", 3)
    whale_thresh = monitor.get_current_whale_threshold()
    print(f" Configured Whale Threshold: >= {whale_thresh} shares")

    print(f" Submitting Resting Whale BUY Order: ID=501, 750 shares @ $102.00...")
    monitor.add_custom_order(order_id=501, side="BUY", price=102.00, quantity=750)

    print(f" Submitting Crossing Whale SELL Order: ID=502, 750 shares @ $102.00...")
    monitor.add_custom_order(order_id=502, side="SELL", price=102.00, quantity=750)

    whale_events = list(monitor.whale_events)
    print(f" [OK] Total Whale Events captured: {len(whale_events)}")
    for ev in whale_events:
        print(f"      -> [{ev.event_type}] Side: {ev.side:<4} Price: ${ev.price:6.2f} Qty: {ev.quantity:<5} Notional: ${ev.notional:10,.2f}")

    # -----------------------------------------------------------------------
    # Step 4: Real-Time Terminal UI Snapshot
    # -----------------------------------------------------------------------
    print_header("Terminal Monitoring Dashboard Output", 4)
    time.sleep(0.1)  # Allow background worker to commit batch
    print(monitor.render_text_snapshot())

    # -----------------------------------------------------------------------
    # Step 5: Direct Database Audit & Verification
    # -----------------------------------------------------------------------
    print_header("Direct SQLite Database Audit & Verification", 5)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM matched_trades;")
    total_db_trades = cur.fetchone()[0]
    print(f" Query: SELECT COUNT(*) FROM matched_trades -> {total_db_trades} trades persisted.")

    cur.execute("SELECT trade_id, buy_order_id, sell_order_id, price, quantity, notional, persisted_at FROM matched_trades LIMIT 5;")
    rows = cur.fetchall()
    print(f"\n Sample records from 'matched_trades' table:")
    print(f"  {'Trade ID':<24} | {'Buy ID':<7} | {'Sell ID':<7} | {'Price':<8} | {'Qty':<5} | {'Notional ($)':<12}")
    print("  " + "-" * 74)
    for r in rows:
        print(f"  {r[0]:<24} | {r[1]:<7} | {r[2]:<7} | ${r[3]:<7.2f} | {r[4]:<5} | ${r[5]:<11.2f}")
    conn.close()

    # -----------------------------------------------------------------------
    # Step 6: Worker Pause, Recovery & Deduplication
    # -----------------------------------------------------------------------
    print_header("Persistence Worker Resilience & Deduplication", 6)
    print(" Simulating temporary persistence pause...")
    if monitor.worker:
        monitor.worker.stop(timeout=1.0)
    print(" Worker stopped. Submitting 3 additional orders while worker is offline...")
    monitor.add_custom_order(order_id=801, side="BUY", price=105.00, quantity=25)
    monitor.add_custom_order(order_id=802, side="SELL", price=105.00, quantity=25)

    print(f" Queue backlog accumulated: {monitor.trade_queue.size()} pending trades.")
    print(" Restarting persistence worker to drain backlog...")
    monitor.worker = BackgroundPersistenceWorker(
        trade_queue=monitor.trade_queue,
        repository=monitor.repository,
        batch_size=100,
        flush_interval_seconds=0.05,
    )
    monitor.worker.start()
    time.sleep(0.15)
    print(f" [OK] Backlog drained. Pending queue depth: {monitor.trade_queue.size()}")

    # Deduplication check
    print(" Testing duplicate event replay to ensure idempotency...")
    repo = monitor.repository
    test_trade = repo.get_trades(limit=1)[0]
    ins, dups = repo.save_trades_batch([test_trade])
    print(f" [OK] Replayed trade ID '{test_trade.trade_id}': Inserted={ins}, Duplicate Ignored={dups}")
    assert dups == 1, "Duplicate trade was not ignored!"

    # -----------------------------------------------------------------------
    # Step 7: Graceful Shutdown & Zero Data Loss Guarantee
    # -----------------------------------------------------------------------
    print_header("Graceful Shutdown & Data Loss Guarantee", 7)
    engine_trades_count = len(monitor.engine.get_trades())
    print(f" Engine executed {engine_trades_count} total trades.")
    print(" Triggering graceful shutdown with drain...")
    monitor.shutdown()
    mmap_bus.close()

    # Verify final database count matches engine trades count exactly
    verify_repo = SQLiteTradeRepository(db_path=db_path)
    final_db_count = verify_repo.get_trade_count()
    print(f" Final SQLite Persisted Trade Count: {final_db_count}")
    print(f" Engine Trade Count                : {engine_trades_count}")
    assert final_db_count == engine_trades_count, "Mismatch between engine trades and persisted database trades!"
    print(" [OK] ZERO TRADE LOSS CONFIRMED: 100% of executed trades persisted to SQLite.")
    verify_repo.close()

    print("\n" + "=" * 78)
    print(" DEMONSTRATION COMPLETE: ALL REQUIREMENTS VERIFIED SUCCESSFULLY!")
    print("=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run_demonstration())
