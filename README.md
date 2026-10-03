# ChronosMatch — Zero-Copy High-Frequency Trading Engine

## Overview

ChronosMatch is a simulated High-Frequency Trading (HFT) matching engine built in Python. It demonstrates how Python can be engineered to handle extreme low-latency workloads by bypassing its traditional weaknesses — the Global Interpreter Lock (GIL), garbage collection pauses, and slow serialization — using **Cython**, **memory-mapped IPC**, and **asyncio**.

### Problem Statement

In High-Frequency Trading, microseconds equal millions of dollars. Python is typically dismissed for HFT because its Garbage Collector causes unpredictable latency spikes, and passing data between processes normally requires slow serialization (JSON/Pickle).

### What This Project Proves

- Two independent Python processes can share data instantly through a **zero-copy memory-mapped ring buffer**, with no serialization overhead — verified end-to-end with a real producer process and a real consumer process (see `firehose.py` / `consumer.py` / `ipc_proof.py`).
- A **Cython-compiled matching engine** can process trades in microseconds by using C-level types instead of Python objects.
- A live terminal dashboard can visualize order book activity and latency in real time.

---

## Architecture

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│    Market Firehose     │ ---> │    mmap Ring Buffer    │ ---> │    Matching Engine     │
│       (asyncio)        │      │    (Zero-Copy IPC)     │      │        (Cython)        │
│    generates orders    │      │   shared memory bus    │      │    matches Buy/Sell    │
│      firehose.py       │      │ src/data_processing/   │      │   src/engine/          │
│                         │      │   ipc_buffer.py        │      │   matching_engine.pyx  │
└────────────────────────┘      └────────────────────────┘      └────────────┬───────────┘
                                                                              │
                                      read via consumer.py                   v
                                                                 ┌────────────────────────┐
                                                                 │   Terminal Dashboard   │
                                                                 │        (curses)        │
                                                                 │   live order book +    │
                                                                 │    latency metrics     │
                                                                 │ src/monitor/           │
                                                                 │  latency_monitor.py    │
                                                                 └────────────────────────┘
```

**Data flow:** `firehose.py` generates orders → serializes them into fixed-size binary records (`src/data_processing/processor.py`) → writes them into the shared-memory ring buffer (`src/data_processing/ipc_buffer.py`) → `consumer.py` reads from that same buffer in a separate process and feeds orders into the Cython matching engine (`src/engine/matching_engine.pyx`) → matched trades can be displayed live on the terminal dashboard (`src/monitor/latency_monitor.py`).

---

## Project Structure

```
Infotact_Python_Project2/
├── README.md                     # This file
├── SCHEMA.md                     # Shared data formats between all modules
├── IPC_AUDIT_RESULTS.md          # Mid-Project Review IPC proof results
├── .gitignore
├── setup.py                      # Build script for the Cython extension
│
├── firehose.py                   # asyncio market firehose (Week 1) — writes into IPC buffer
├── consumer.py                   # Reads from IPC buffer, feeds matching engine (end-to-end pipeline)
├── ipc_proof.py                  # Mid-Review two-process IPC audit (mmap vs pickle)
├── test_matching_engine.py       # Tests for the compiled Cython engine
├── test_ipc_buffer.py            # Correctness tests for the mmap ring buffer
│
├── src/
│   ├── data_processing/           # Order creation, serialization, and IPC buffer (Member 1 - Week 1)
│   │   ├── processor.py             # Order creation + binary pack/unpack (struct)
│   │   └── ipc_buffer.py            # mmap-based zero-copy ring buffer
│   │
│   ├── model/                     # Pure-Python baseline order matcher (Member 2 - Week 1)
│   │   ├── baseline_matcher.py
│   │   └── test_baseline.py
│   │
│   ├── engine/                    # Cython matching engine (Member 1 foundation + Member 2 LOB)
│   │   ├── matching_engine.pyx
│   │   └── __init__.py
│   │
│   ├── monitor/                   # Raw curses Latency Dashboard & Order Book UI (Member 3 - Week 2)
│   │   ├── latency_monitor.py
│   │   └── tests/
│   │
│   └── analysis/                  # Market simulation, analytics, and benchmarking (Member 3 - Week 1)
│       ├── simulator.py             # Synchronous order generator (in-process benchmarking)
│       ├── metrics.py
│       ├── visualizer.py
│       ├── benchmark.py
│       └── run_demo.py
```

---

## Key Technologies

- **Python `struct` / `mmap`** — zero-copy binary serialization and shared memory
- **Cython** — statically-typed compiled extension for the matching engine
- **asyncio** — high-throughput asynchronous order generation
- **multiprocessing** — real separate-process IPC verification
- **curses** — terminal-based live dashboard

---

## How It Works — End to End

1. **Order Generation (IPC path):** `firehose.py` generates randomized Buy/Sell orders using `asyncio`, and serializes them with `src/data_processing/processor.py`.
2. **Zero-Copy IPC:** Serialized orders are written into a shared `mmap` ring buffer (`src/data_processing/ipc_buffer.py`), readable by any separate process with access to the same memory-mapped file.
3. **Consumption + Matching:** `consumer.py` runs as its own process, reads orders directly from the buffer, and feeds them into the Cython matching engine (`src/engine/matching_engine.pyx`), which matches Buy/Sell orders using price-time priority.
4. **Visualization:** The terminal dashboard (`src/monitor/latency_monitor.py`) displays live order book state and latency metrics, reading from the same matching engine API (`get_trades()`, `get_order_book_snapshot()`).
5. **Benchmarking:** `src/analysis/benchmark.py` compares the pure-Python baseline matcher against the Cython engine (in-process), while `ipc_proof.py` separately proves the IPC layer itself beats pickle-based serialization across two real processes.

---

## Setup & Installation

```bash
# Clone the repository
git clone <repo-url>
cd Infotact_Python_Project2

# Install Cython
pip install cython

# Build the Cython extension (run from the project root)
python setup.py build_ext --inplace
```

> **Windows note:** building the Cython extension requires Microsoft C++ Build Tools ("Desktop development with C++" workload) to be installed.

---

## Running the Project

```bash
# Run the engine's own test suite
python test_matching_engine.py

# Run the IPC ring buffer's own test suite
python test_ipc_buffer.py

# Prove the zero-copy IPC architecture (Mid-Project Review requirement)
python ipc_proof.py --orders 1000000

# Run the real end-to-end pipeline (two terminals):
# Terminal 1:
python consumer.py --orders 5000
# Terminal 2:
python firehose.py --orders 5000 --rate 5000

# Launch Member 3's real-time raw curses Latency Dashboard & Order Book UI
python -m src.monitor.latency_monitor

# Launch dashboard in headless / ASCII snapshot mode
python -m src.monitor.latency_monitor --once
python -m src.monitor.latency_monitor --no-curses --rate 50

# Run all tests (baseline matcher, analysis, and latency monitor)
pytest

# Run Week 1 in-process analysis demo (does not use the IPC buffer)
python -m src.analysis.run_demo --orders 1000
```

---

## Mid-Project Review Status

| Requirement | Status |
|---|---|
| mmap zero-copy ring buffer | ✅ Done |
| Asyncio market firehose | ✅ Done |
| Cython matching engine, Price-Time Priority | ✅ Done |
| Raw curses latency dashboard | ✅ Done |
| IPC Audit — 1M orders, two processes, beats pickle | ✅ Done (`IPC_AUDIT_RESULTS.md`) |
| Engine Verification — Buy matches Sell correctly | ✅ Done |
| End-to-end pipeline (firehose → buffer → engine) | ✅ Done |

See `IPC_AUDIT_RESULTS.md` for full benchmark numbers and methodology,
and `SCHEMA.md` for the exact data formats and interfaces used between
every module.