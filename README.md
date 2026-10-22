# ChronosMatch — Zero-Copy High-Frequency Trading Engine

## Overview

ChronosMatch is a simulated High-Frequency Trading (HFT) matching engine built in Python. It demonstrates how Python can be engineered to handle extreme low-latency workloads by bypassing its traditional weaknesses — the Global Interpreter Lock (GIL), garbage collection pauses, and slow serialization — using **Cython**, **zero-copy memory-mapped IPC**, **asyncio**, and **asynchronous WAL SQLite persistence**.

### Problem Statement

In High-Frequency Trading, microseconds equal millions of dollars. Python is typically dismissed for HFT because its Garbage Collector causes unpredictable latency spikes, and passing data between processes normally requires slow serialization (JSON/Pickle). Furthermore, persisting trade audit records to disk synchronously can stall a sub-microsecond matching core by milliseconds.

### What This Project Proves

- Two independent Python processes can share data instantly through a **zero-copy memory-mapped ring buffer**, with no serialization overhead — verified end-to-end with a real producer process and a real consumer process (`firehose.py` / `consumer.py` / `ipc_proof.py`).
- A **Cython-compiled matching engine** can process trades in microseconds by using C-level types instead of Python objects (with pure-Python fallback for environments without C++ build tools).
- A **decoupled background persistence architecture** (in-memory lock-free/bounded queue + WAL-mode SQLite worker) persists matched trades from a separate thread, so disk writes stay off the matching path. A failed write is retried instead of dropped, and any trade that still cannot be written at shutdown is counted in `trades_abandoned`, never lost silently.
- A live terminal dashboard (Curses & ASCII fallback) visualizes order book depth ladders, execution tape, p50/p95/p99 latency distributions, and real-time whale-order alerts, including multi-level sweeps (one large order that clears several price levels).

---

## Architecture

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│    Market Firehose     │ ---> │    mmap Ring Buffer    │ ---> │    Matching Engine     │
│       (asyncio)        │      │    (Zero-Copy IPC)     │      │   (Cython / Python)    │
│    generates orders    │      │   shared memory bus    │      │    matches Buy/Sell    │
│      firehose.py       │      │   64B aligned header   │      │   price-time priority  │
└────────────────────────┘      └────────────────────────┘      └────────────┬───────────┘
                                                                             │
                                                                             ├──> [ Trade Queue ]
                                                                             │         │
                                                                             │         v
                                                                             │    ┌────────────────────────┐
                                                                             │    │ Background Persistence │
                                                                             │    │     (SQLite + WAL)     │
                                                                             │    │   batch audit ledger   │
                                                                             │    └────────────────────────┘
                                                                             │
                                                                             v
                                                                ┌────────────────────────┐
                                                                │   Terminal Dashboard   │
                                                                │    (curses / ASCII)    │
                                                                │   live order book +    │
                                                                │    latency metrics     │
                                                                └────────────────────────┘
```

**Real pipeline (separate processes):** `firehose.py` generates orders → serializes them into fixed-size binary records (`src/data_processing/processor.py`) → writes them into the shared-memory ring buffer (`src/data_processing/ipc_buffer.py`) → `consumer.py` reads that buffer in another process and feeds the matching engine (`src/engine/matching_engine.pyx`). This is the path verified by `ipc_proof.py` (1M orders) and the benchmarks.

**Dashboard and persistence stack:** the dashboard (`src/monitor/latency_monitor.py`) drives the engine with its own simulated orders. Matched trades go into a bounded thread-safe queue (`src/ipc/trade_queue.py`), where a background worker batches them into SQLite (`src/persistence/`). `src/demo_member3.py` and `src/monitor/tests/test_integration_flow.py` run the whole chain (mmap buffer → engine → queue → SQLite) in one scripted scenario. `consumer.py` itself does not write to the database.

---

## Project Structure

```
Infotact_Python_Project2/
├── README.md                     # This file
├── SCHEMA.md                     # Shared data formats between all modules
├── IPC_AUDIT_RESULTS.md          # Mid-Project Review IPC proof results
├── setup.py                      # Build script for Cython extension
├── requirements.txt              # Project dependencies
├── .gitignore
│
├── firehose.py                   # asyncio market firehose — writes into IPC buffer
├── consumer.py                   # Reads from IPC buffer, feeds matching engine
├── ipc_proof.py                  # Two-process IPC audit (mmap vs pickle)
├── test_matching_engine.py       # Unit test suite for matching engine
├── test_ipc_buffer.py            # Correctness tests for the mmap ring buffer
├── test_matching_engine_batch.py     # Tests for the engine's batch API
├── test_matching_engine_accessors.py # Tests for get_trades_since / get_book_depth
├── cython_benchmark.py           # Nanosecond latency + GC check for the Cython engine
├── benchmark_suite.py            # Baseline vs Cython across load sizes (--save writes BENCHMARK_RESULTS.md)
├── pytest.ini                    # Test discovery settings
│
├── src/
│   ├── data_processing/           # Order creation & binary serialization for IPC
│   │   ├── processor.py             # Order creation + binary pack/unpack (struct)
│   │   └── ipc_buffer.py            # mmap-based zero-copy ring buffer
│   │
│   ├── model/                     # Pure-Python baseline order matcher
│   │   ├── baseline_matcher.py
│   │   └── test_baseline.py
│   │
│   ├── engine/                    # Cython & reference matching engine
│   │   ├── matching_engine.pyx      # Cython engine implementation
│   │   ├── matching_engine.py       # High-performance reference fallback engine
│   │   └── __init__.py
│   │
│   ├── ipc/                       # Zero-copy shared memory mmap bus & trade queue
│   │   ├── mmap_buffer.py           # 64-byte aligned header ring buffer with crash recovery
│   │   ├── trade_queue.py           # Bounded trade event queue with backpressure policies
│   │   ├── consumer.py              # IPC consumer loop
│   │   ├── ring_buffer.py           # Ring buffer implementation
│   │   └── tests/                   # IPC test suite (12 tests)
│   │
│   ├── persistence/               # Asynchronous SQLite audit ledger & worker
│   │   ├── repository.py            # SQLite repository with WAL mode & batch transactions
│   │   ├── worker.py                # Background daemon worker: flush/drain, retry of failed writes, bounded shutdown
│   │   └── tests/                   # Persistence test suite (17 tests)
│   │
│   ├── monitor/                   # Terminal Latency Dashboard & Whale Monitor
│   │   ├── latency_monitor.py       # Live curses / ASCII depth ladder & trade tape
│   │   └── tests/                   # UI test suite (43 tests)
│   │
│   └── analysis/                  # Market simulation, analytics, and benchmarking
│       ├── simulator.py             # Synchronous order generator
│       ├── metrics.py               # Latency percentiles & stats
│       ├── visualizer.py            # Order book & spread visualizer
│       ├── benchmark.py             # In-process baseline vs engine benchmark
│       ├── benchmark_persistence.py # Matching engine decoupling & persistence benchmark
│       └── run_demo.py
│
├── src/demo_member3.py           # End-to-end integration and resilience demonstration
└── reports/                      # Verification reports and audit outputs
```

---

## Key Technologies

- **Python `struct` / `mmap`** — zero-copy binary serialization and shared memory
- **Cython** — statically-typed compiled extension for the matching engine
- **asyncio** — high-throughput asynchronous order generation
- **multiprocessing** — real separate-process IPC verification
- **SQLite (WAL mode)** — asynchronous trade ledger written from a separate thread, off the matching path
- **curses / ASCII UI** — cross-platform live terminal dashboard

---

## Setup & Installation

```bash
# Clone the repository
git clone https://github.com/vinya-ml/Infotact_Python_Project2.git
cd Infotact_Python_Project2

# Install dependencies
pip install -r requirements.txt

# (Optional) Build the Cython extension (requires C++ Build Tools on Windows)
python setup.py build_ext --inplace
```

> **Note:** If Cython compilation is not run or build tools are unavailable, ChronosMatch seamlessly falls back to its optimized pure-Python matching engine without any code changes.

---

## Running the Project

### 1. Live Terminal Latency Dashboard & Order Book UI

Launch the live interactive terminal dashboard:
```bash
python -m src.monitor.latency_monitor
```

Options:
```bash
# Launch with custom generation rate (Hz) and whale-order detection threshold:
python -m src.monitor.latency_monitor --rate 50 --whale-threshold 300

# Run in headless / ASCII single snapshot mode:
python -m src.monitor.latency_monitor --once

# Run continuous ASCII streaming mode (ideal for basic terminals):
python -m src.monitor.latency_monitor --no-curses --rate 50
```

### 2. End-to-End Resilience Demonstration

Runs a complete 7-step pipeline demonstrating zero-copy IPC mmap buffer, matching engine, background SQLite persistence, crash-recovery, whale detection, and idempotent trade deduplication:
```bash
python src/demo_member3.py
```

### 3. Asynchronous Persistence Benchmark

Demonstrates that background persistence decouples matching latency from disk I/O:
```bash
python -m src.analysis.benchmark_persistence --orders 2000
```

### 4. IPC Multi-Process Pipeline

Run the real two-process zero-copy pipeline across two terminals:
```bash
# Terminal 1 (Consumer):
python consumer.py --orders 5000

# Terminal 2 (Market Firehose):
python firehose.py --orders 5000 --rate 5000
```

### 5. Mid-Project Review Zero-Copy IPC Audit

Proves zero-copy mmap beats pickle-based IPC across separate OS processes:
```bash
python ipc_proof.py --orders 1000000
```

### 6. Performance Benchmarks

Build the Cython engine first (`python setup.py build_ext --inplace`); these scripts refuse to run on the Python fallback so they never report its numbers as "Cython":
```bash
python cython_benchmark.py --orders 5000        # nanosecond latency, p50/p95/p99, GC collections
python benchmark_suite.py --runs 7 --save       # baseline vs Cython at 1K-20K orders; writes BENCHMARK_RESULTS.md
```
Numbers vary by machine and between runs, so quote them with the load size.

### 7. Automated Test Suite

```bash
pytest -q                                                                  # src/ tests (96)
pytest -q src test_matching_engine_batch.py test_matching_engine_accessors.py   # plus the engine tests that need the compiled build (109)
python test_matching_engine.py                                              # compiled-engine script tests
python test_ipc_buffer.py                                                   # ring buffer script tests
```

---

## Mid-Project Review Status

| Requirement | Status |
|---|---|
| mmap zero-copy ring buffer | ✅ Done (`src/data_processing/ipc_buffer.py` is used by the pipeline and the 1M-order audit; `src/ipc/mmap_buffer.py` is used by the dashboard demo and integration tests) |
| Asyncio market firehose | ✅ Done (`firehose.py`) |
| Cython matching engine, Price-Time Priority | ✅ Done (`src/engine/matching_engine.pyx` + fallback) |
| Terminal latency & depth dashboard | ✅ Done (`src/monitor/latency_monitor.py`) |
| Asynchronous WAL SQLite persistence | ✅ Done (`src/persistence/repository.py` & `worker.py`) |
| Bounded queue & backpressure policies | ✅ Done (`src/ipc/trade_queue.py`) |
| IPC Audit — 1M orders, two processes, beats pickle | ✅ Done (`IPC_AUDIT_RESULTS.md`) |
| Engine Verification — Buy matches Sell correctly | ✅ Done (`test_matching_engine.py`) |
| End-to-end pipeline (firehose → buffer → engine) | ✅ Done (`consumer.py`; it does not persist trades, see Week 3 & 4 Status) |

## Week 3 & 4 Status

| Requirement | Status |
|---|---|
| C-level engine optimization (no Python objects in the matching loop) | ✅ Done (`src/engine/matching_engine.pyx`) |
| Metrics tracking: nanosecond timing, p50/p95/p99, GC collection counts | ✅ Done (`cython_benchmark.py`, `benchmark_suite.py`) |
| Baseline vs Cython benchmark across load sizes | ✅ Done (`benchmark_suite.py`) |
| Failed database writes retried; bounded shutdown; abandoned trades counted | ✅ Done (`src/persistence/worker.py`) |
| Whale orders that clear multiple price levels highlighted | ✅ Done (`src/monitor/latency_monitor.py`, option `--sweep-levels`) |
| Persistence fed by the real firehose → mmap → consumer pipeline | ⚠️ Not connected: the dashboard and the demo/integration tests feed the persistence queue; `consumer.py` does not |

## Known Limitations

- Each ring buffer supports exactly one writer and one reader.
- The engine's cost per order grows with the number of resting orders (it scans and shifts an array). Pushing 1M orders through the matching engine took over a minute in our test, so keep live demos to about 100K orders or fewer. The 1M-order figure is for the IPC audit, which only moves orders through the buffer.
- If the database stays down, new trades wait in the queue (default capacity 50,000; beyond that the queue drops the oldest and counts them). At shutdown, trades that still cannot be written are counted in `trades_abandoned`. A hard process kill loses trades that were queued but not yet written.
- Three ring-buffer implementations exist: `src/data_processing/ipc_buffer.py` (pipeline and 1M-order audit), `src/ipc/mmap_buffer.py` (dashboard demo and integration tests) and `src/ipc/ring_buffer.py` (used by `src/ipc/consumer.py`).
- A sweep is reported only when one order fills at two or more distinct price levels and its executed quantity reaches the whale threshold.
- Benchmark numbers differ by up to about 2x between runs on the same laptop.

See `SCHEMA.md` for shared data formats, buffer header definitions, and database schemas.