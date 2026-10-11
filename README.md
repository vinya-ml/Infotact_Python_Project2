# ChronosMatch — Zero-Copy High-Frequency Trading Engine

## Overview

ChronosMatch is a simulated High-Frequency Trading (HFT) matching engine built in Python. It demonstrates how Python can be engineered to handle extreme low-latency workloads by bypassing its traditional weaknesses — the Global Interpreter Lock (GIL), garbage collection pauses, and slow serialization — using **Cython**, **zero-copy memory-mapped IPC**, **asyncio**, and **asynchronous WAL SQLite persistence**.

### Problem Statement

In High-Frequency Trading, microseconds equal millions of dollars. Python is typically dismissed for HFT because its Garbage Collector causes unpredictable latency spikes, and passing data between processes normally requires slow serialization (JSON/Pickle). Furthermore, persisting trade audit records to disk synchronously can stall a sub-microsecond matching core by milliseconds.

### What This Project Proves

- Two independent Python processes can share data instantly through a **zero-copy memory-mapped ring buffer**, with no serialization overhead — verified end-to-end with a real producer process and a real consumer process (`firehose.py` / `consumer.py` / `ipc_proof.py`).
- A **Cython-compiled matching engine** can process trades in microseconds by using C-level types instead of Python objects (with pure-Python fallback for environments without C++ build tools).
- A **decoupled background persistence architecture** (in-memory lock-free/bounded queue + WAL-mode SQLite worker) persists matched trades at high throughput without polluting or stalling matching execution latency (~13.5 µs matching vs. ~4.9 ms batched SQLite writes).
- A live terminal dashboard (Curses & ASCII fallback) visualizes order book depth ladders, execution tape, p50/p95/p99 latency distributions, and real-time whale-order sweeps.

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

**Data flow:** `firehose.py` generates orders → serializes them into fixed-size binary records (`src/data_processing/processor.py`) → writes them into the shared-memory ring buffer (`src/ipc/mmap_buffer.py` / `src/data_processing/ipc_buffer.py`) → `consumer.py` reads from that same buffer in a separate process and feeds orders into the matching engine (`src/engine/matching_engine.pyx`) → matched trades flow concurrently to the Terminal Dashboard (`src/monitor/latency_monitor.py`) and into a bounded thread-safe queue (`src/ipc/trade_queue.py`) where a background daemon worker batches writes into SQLite (`src/persistence/repository.py`).

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
│   │   └── tests/                   # IPC test suite (27 passing tests)
│   │
│   ├── persistence/               # Asynchronous SQLite audit ledger & worker
│   │   ├── repository.py            # SQLite repository with WAL mode & batch transactions
│   │   ├── worker.py                # Background daemon worker with flush/drain
│   │   └── tests/                   # Persistence test suite (23 passing tests)
│   │
│   ├── monitor/                   # Terminal Latency Dashboard & Whale Monitor
│   │   ├── latency_monitor.py       # Live curses / ASCII depth ladder & trade tape
│   │   └── tests/                   # UI test suite (19 passing tests)
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
- **SQLite (WAL mode)** — crash-resilient asynchronous trade ledger with zero engine jitter
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

### 6. Automated Test Suite

Run the full automated test suite across all modules:
```bash
pytest -v
```

---

## Mid-Project Review Status

| Requirement | Status |
|---|---|
| mmap zero-copy ring buffer | ✅ Done (`src/ipc/mmap_buffer.py` / `src/data_processing/ipc_buffer.py`) |
| Asyncio market firehose | ✅ Done (`firehose.py`) |
| Cython matching engine, Price-Time Priority | ✅ Done (`src/engine/matching_engine.pyx` + fallback) |
| Terminal latency & depth dashboard | ✅ Done (`src/monitor/latency_monitor.py`) |
| Asynchronous WAL SQLite persistence | ✅ Done (`src/persistence/repository.py` & `worker.py`) |
| Bounded queue & backpressure policies | ✅ Done (`src/ipc/trade_queue.py`) |
| IPC Audit — 1M orders, two processes, beats pickle | ✅ Done (`IPC_AUDIT_RESULTS.md`) |
| Engine Verification — Buy matches Sell correctly | ✅ Done (`test_matching_engine.py`) |
| End-to-end pipeline (firehose → buffer → engine) | ✅ Done (`consumer.py`) |

See `SCHEMA.md` for shared data formats, buffer header definitions, and database schemas.
