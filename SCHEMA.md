# SCHEMA.md

This document is the shared source of truth for data formats used
across ChronosMatch modules. Update this file whenever a format
changes — before changing the code, if possible — so the whole team
stays in sync.

---

## Week 1 — Order Format (produced by Member 1 - data_processing)

| Field | Type | Notes |
|---|---|---|
| order_id | int (uint64) | |
| side | str | 'BUY' or 'SELL' (uppercase) |
| price | float (double) | |
| quantity | int (uint64) | |
| timestamp_ns | int (uint64) | nanoseconds, from time.perf_counter_ns() |

Binary format (struct): `"<QBdQQ"` — 33 bytes total per order (this
exact size is referred to as `ORDER_SIZE` elsewhere in the codebase,
including the IPC ring buffer below).

### Internal Order Format (used by Member 2 - model/OrderBook, pure-Python baseline)

| Field | Type | Notes |
|---|---|---|
| order_id | int | |
| side | str | 'buy' or 'sell' (lowercase) |
| price | float | |
| qty | int | |
| timestamp | int | |

Note: `adapt_order()` in `src/model/baseline_matcher.py` converts
Member 1's format into this one for the pure-Python baseline engine.

### Matched Trade Output Format (produced by OrderBook.trades)

| Field | Type |
|---|---|
| buy_id | int |
| sell_id | int |
| price | float |
| qty | int |

---

## Week 1 — Zero-Copy IPC Ring Buffer (src/data_processing/ipc_buffer.py)

A memory-mapped (`mmap`) shared-memory ring buffer used to pass orders
between two separate Python processes with no pickling/serialization
overhead beyond the fixed binary format above. This is the actual
shared-memory bus referenced throughout the project spec.

### File Layout

```
[ write_index (8 bytes) ][ read_index (8 bytes) ][ slot 0 ][ slot 1 ] ...
```

- `write_index` (uint64) — next slot index to write to. **Only ever
  written by the writer process.**
- `read_index` (uint64) — next slot index to read from. **Only ever
  written by the reader process.**
- Each slot is exactly `ORDER_SIZE` (33) bytes — the packed binary
  order format from `processor.py`.

This single-writer/single-reader design (each index owned by exactly
one process) avoids a read-modify-write race that a shared/combined
header would cause. It supports exactly **one writer and one reader
at a time** — multiple concurrent writers or readers would need real
locking, which this version does not implement.

### Interface

```python
from src.data_processing.ipc_buffer import open_buffer

buf = open_buffer(path, num_slots=100_000, create=True)  # writer/creator side
buf = open_buffer(path, num_slots=100_000, create=False) # reader/attacher side

buf.write(packed_order_bytes)   # writes one order (33 bytes), writer side only
buf.read()                      # returns bytes or None, reader side only
buf.pending_count()             # how many orders written but not yet read
buf.close()
```

`write()` and `read()` operate purely on raw bytes — the buffer has no
knowledge of order fields, it just stores/retrieves whatever
`processor.pack_order()` / `processor.unpack_order()` produce/consume.

---

## Week 1 — Market Firehose (firehose.py, project root)

An `asyncio`-based script that generates randomized BUY/SELL orders
and writes them into the IPC ring buffer above, simulating a live
market data feed.

```bash
python firehose.py --orders 100000 --rate 100000 --slots 100000
```

- Creates the buffer file (`create=True`) if it doesn't exist
- Packs each order via `processor.pack_order()` before writing
- Yields control periodically (`await asyncio.sleep(0)`) to behave
  like a real asynchronous producer rather than a blocking loop

---

## Week 2 — Cython Matching Engine (src/engine/matching_engine.pyx)

### Engine Input — `add_order()`

```python
engine.add_order(order_id, side, price, quantity, timestamp)
```

| Parameter | Type | Notes |
|---|---|---|
| order_id | uint64 | |
| side | uint8 | **0 = BUY, 1 = SELL** (integer constants, not strings — differs from Week 1 baseline) |
| price | double | |
| quantity | uint64 | |
| timestamp | uint64 | nanoseconds |

Constants defined in `matching_engine.pyx`:
```python
BUY = 0
SELL = 1
```

### Engine Output — `get_trades()`

Returns a list of matched trade dicts, same shape as the Week 1 baseline:

```python
[
    {'buy_id': ..., 'sell_id': ..., 'price': ..., 'qty': ...},
    ...
]
```

### Engine Output — `get_order_book_snapshot()`

Returns the current top-of-book state:

```python
[best_bid, best_ask]
```

- `best_bid` — highest resting BUY price (or `None` if no buy orders resting)
- `best_ask` — lowest resting SELL price (or `None` if no sell orders resting)

This is used by the **Terminal Dashboard** to display live Bid/Ask spread.

### Engine Output — `get_order_count()`

Returns total number of orders received (int) — set up by Member 1, unchanged in Week 2.

### Internal Engine Components (not exposed outside the module)

| Component | Purpose |
|---|---|
| `COrder` (C struct) | Raw C-level order representation defined by Member 1 |
| `COrderObj` (cdef class) | Python-visible wrapper around order fields, used to store resting orders in `buy_orders` / `sell_orders` lists (since raw C structs can't be stored in Python lists) |
| `buy_orders` | Resting BUY orders, sorted highest price / earliest timestamp first |
| `sell_orders` | Resting SELL orders, sorted lowest price / earliest timestamp first |
| `trades` | Internal log of all matched trades so far |

---

## Week 2 — Buffer Consumer (consumer.py, project root)

Reads orders from the IPC ring buffer (written by `firehose.py`) and
feeds them into the matching engine, closing the loop:
**firehose → mmap buffer → matching engine → matched trades.**

```bash
python consumer.py --orders 100000
```

- Uses the compiled Cython `MatchingEngine` if available (`src/engine/matching_engine`)
- Falls back to the pure-Python reference `MatchingEngine` (`src/engine/matching_engine.py`)
  or `OrderBook` (`src/model/baseline_matcher.py`) if the Cython extension hasn't been built
- Translates Member 1's uppercase `'BUY'`/`'SELL'` strings into the
  engine's integer constants (`BUY = 0`, `SELL = 1`) before calling
  `add_order()`

---

## Week 2 — Latency Dashboard & Terminal UI (src/monitor/latency_monitor.py)

### Dashboard Integration with Matching Engine

The terminal dashboard consumes the engine API:

| Engine Method | Purpose in Dashboard |
|---|---|
| `get_order_book_snapshot()` | Fetches `[best_bid, best_ask]` to compute top-of-book, spread (`best_ask - best_bid`), mid price, and spread bps |
| `get_trades()` | Provides real-time matched trade log for execution tape (buy_id, sell_id, price, qty, notional, whale flags) |
| `get_order_count()` | Supplies cumulative order throughput counters |
| `buy_orders` / `sell_orders` | Provides depth ladder visualization (top price levels & resting quantities) |
| `add_order(...)` | Timed with `time.perf_counter_ns()` to monitor sub-microsecond matching latency (min, mean, p50, p95, p99) |

---

## Mid-Project Review — IPC Audit (ipc_proof.py, project root)

Proves the zero-copy architecture by spawning two genuinely separate
OS processes (via `multiprocessing`) — one writer, one reader — both
sharing the same mmap-backed ring buffer file, and compares throughput
against an equivalent pickle-based baseline.

```bash
python ipc_proof.py --orders 1000000
```

Full results and methodology documented in `IPC_AUDIT_RESULTS.md`.
Verified result: 1,000,000 orders moved in ~3.09 sec (~324K orders/sec),
~2.2x faster than the pickle-based comparison.

---

## Weeks 3 & 4 — IPC Resilience, Background Persistence & Whale Visualization

### Zero-Copy Shared Memory Ring Buffer Header Layout (`src/ipc/mmap_buffer.py`)

Fixed 64-byte cache-line aligned header struct: `"<8s H I Q Q Q Q I 14s"`

| Offset | Field | Type | Description |
|---|---|---|---|
| 0–8 | `magic` | 8 bytes | `b"CMMAP\x01\x00\x00"` magic signature |
| 8–10 | `version` | uint16 | Header version (1) |
| 10–14 | `record_size` | uint32 | Fixed byte size per record (33 bytes for order) |
| 14–22 | `capacity` | uint64 | Maximum number of slots in the ring buffer |
| 22–30 | `head_seq` | uint64 | Monotonic write sequence |
| 30–38 | `tail_seq` | uint64 | Monotonic read sequence (default consumer) |
| 38–46 | `dropped_count`| uint64 | Total records dropped due to buffer overflow |
| 46–50 | `flags` | uint32 | 0 = Active, 1 = Closed |
| 50–64 | `reserved` | 14 bytes | Padding for 64-byte cache-line alignment |

### Decoupled Trade Event Schema (`src/ipc/trade_queue.py`)

Standardized dataclass passed from matching engine to background persistence worker:

| Field | Type | Description |
|---|---|---|
| `trade_id` | str | Stable deduplication key (`TRD_{buy_id}_{sell_id}_{timestamp_ns}_{seq}`) |
| `buy_order_id` | int (uint64) | Originating buy order ID |
| `sell_order_id` | int (uint64) | Originating sell order ID |
| `price` | float (double) | Match price |
| `quantity` | int (uint64) | Matched trade volume |
| `notional` | float (double) | Notional value (`price * quantity`) |
| `timestamp_ns` | int (uint64) | Matching execution timestamp |
| `enqueue_time_ns`| int (uint64)| Timestamp when pushed to persistence queue |
| `symbol` | str | Financial instrument symbol (`DEFAULT`) |

### Persistent SQLite Database Schema (`src/persistence/repository.py`)

Table: `matched_trades` (WAL mode enabled via `PRAGMA journal_mode=WAL`)

```sql
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
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON matched_trades(timestamp_ns);
CREATE INDEX IF NOT EXISTS idx_trades_buy_order ON matched_trades(buy_order_id);
CREATE INDEX IF NOT EXISTS idx_trades_sell_order ON matched_trades(sell_order_id);
```

### Whale Order & Sweep Event Schema (`src/monitor/latency_monitor.py`)

| Field | Type | Description |
|---|---|---|
| `event_type` | str | `RESTING_ORDER` or `MATCHED_TRADE` |
| `side` | str | `BUY`, `SELL`, or `TRADE` |
| `price` | float | Order or execution price |
| `quantity` | int | Quantity (`>= threshold`, default 500) |
| `notional` | float | `price * quantity` |
| `order_id` | int / None | Order ID if resting order |
| `buy_id` / `sell_id` | int / None | Trade order IDs if executed sweep |
| `timestamp_ns` | int | Nanosecond timestamp |

---

## Open Items & Verifications

- [x] Confirmed: `get_order_book_snapshot()`, `get_trades()`, and resting book access fully satisfy terminal dashboard depth ladder and tape requirements.
- [x] Confirmed: Zero-copy ring buffer with 64-byte aligned header supports crash-recovery and head/tail sequence tracking.
- [x] Confirmed: Background SQLite persistence operates with WAL mode and batch transactions, decoupled from matching latency.
