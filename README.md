# ChronosMatch — Zero-Copy High-Frequency Trading Engine

## Overview

ChronosMatch is a simulated High-Frequency Trading (HFT) matching engine built in Python. It demonstrates how Python can be engineered to handle extreme low-latency workloads by bypassing its traditional weaknesses — the Global Interpreter Lock (GIL), garbage collection pauses, and slow serialization — using **Cython**, **memory-mapped IPC**, and **asyncio**.

### Problem Statement

In High-Frequency Trading, microseconds equal millions of dollars. Python is typically dismissed for HFT because its Garbage Collector causes unpredictable latency spikes, and passing data between processes normally requires slow serialization (JSON/Pickle).

### What This Project Proves

- Two independent Python processes can share data instantly through a **zero-copy memory-mapped ring buffer**, with no serialization overhead.
- A **Cython-compiled matching engine** can process trades in microseconds by using C-level types instead of Python objects.
- A live terminal dashboard can visualize order book activity and latency in real time.

---

## Architecture

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│    Market Firehose     │ ---> │    mmap Ring Buffer    │ ---> │    Matching Engine     │
│       (asyncio)        │      │    (Zero-Copy IPC)     │      │        (Cython)        │
│    generates orders    │      │   shared memory bus    │      │    matches Buy/Sell    │
└────────────────────────┘      └────────────────────────┘      └────────────┬───────────┘
                                                                             │
                                                                             v
                                                                ┌────────────────────────┐
                                                                │   Terminal Dashboard   │
                                                                │    (curses / Rich)     │
                                                                │   live order book +    │
                                                                │    latency metrics     │
                                                                └────────────────────────┘
```

**Data flow:** Orders are generated → serialized into fixed-size binary records → written into a shared-memory ring buffer → read and matched by the Cython engine → matched trades are displayed live on a terminal dashboard.

---

## Project Structure

```
Infotact_Python_Project2/
├── README.md                  # This file
├── SCHEMA.md                  # Shared data formats between all modules
├── .gitignore
│
├── src/
│   ├── data_processing/        # Order creation & binary serialization for IPC (Member 1 - Week 1)
│   │   └── processor.py
│   │
│   ├── model/                  # Pure-Python baseline order matcher (Member 2 - Week 1)
│   │   ├── baseline_matcher.py
│   │   └── test_baseline.py
│   │
│   ├── engine/                 # Cython matching engine (Member 1 foundation + Member 2 LOB)
│   │   ├── matching_engine.pyx
│   │   └── __init__.py
│   │
│   ├── monitor/                # Raw curses Latency Dashboard & Order Book UI (Member 3 - Week 2)
│   │   ├── latency_monitor.py
│   │   └── tests/
│   │
│   └── analysis/                # Market simulation, analytics, and benchmarking (Member 3 - Week 1)
│       ├── simulator.py
│       ├── metrics.py
│       ├── visualizer.py
│       └── benchmark.py
```

---

## Key Technologies

- **Python `struct` / `mmap`** — zero-copy binary serialization and shared memory
- **Cython** — statically-typed compiled extension for the matching engine
- **asyncio** — high-throughput asynchronous order generation
- **curses / Rich** — terminal-based live dashboard
- **SQLite** — persistent trade ledger for auditing

---

## How It Works — End to End

1. **Order Generation:** The market firehose (`src/analysis/firehose.py`) generates randomized Buy/Sell orders and serializes them using `src/data_processing/processor.py`.
2. **Zero-Copy IPC:** Serialized orders are written into a shared `mmap` ring buffer, readable by any process with access to the same memory-mapped file.
3. **Matching:** The Cython matching engine (`src/model/matching_engine.pyx`) reads orders directly from the buffer and matches Buy/Sell orders using price-time priority, without triggering Python's Garbage Collector during the matching loop.
4. **Visualization:** Matched trades and order book state are displayed live on a terminal dashboard (`src/analysis/dashboard.py`).
5. **Benchmarking:** Performance is measured and compared against the pure-Python baseline matcher to demonstrate the speedup achieved through Cython.

---

## Setup & Installation

```bash
# Clone the repository
git clone <repo-url>
cd Infotact_Python_Project2

# Install dependencies
pip install -r requirements.txt

# Build the Cython extension
cd src/model
python setup.py build_ext --inplace
```

---

## Running the Project

```bash
# Launch Member 3's real-time raw curses Latency Dashboard & Order Book UI
python -m src.monitor.latency_monitor

# Launch dashboard in headless / ASCII snapshot mode
python -m src.monitor.latency_monitor --once
python -m src.monitor.latency_monitor --no-curses --rate 50

# Run all tests (baseline matcher, analysis, and latency monitor)
pytest

# Run Week 1 analysis demo
python -m src.analysis.run_demo --orders 1000
```