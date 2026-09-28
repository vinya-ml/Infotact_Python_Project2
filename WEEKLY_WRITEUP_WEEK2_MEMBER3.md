# Weekly Write-Up – Week 2

**Project Name:** ChronosMatch – Zero-Copy High-Frequency Trading Engine  
**Role:** Member 3 (Latency Dashboard & Order Book Monitor)  
**Week:** 2  

---

## 1. Overview

During Week 2, following the foundation set by **Member 1** (Cython engine foundation and C-types) and **Member 2** (Cython Price-Time Priority limit order book and order matching), I developed the **Latency Dashboard & Real-Time Market Monitor** (`src/monitor/latency_monitor.py`).

My primary objective for Week 2 was to construct a high-performance **raw curses terminal user interface** that visualizes the state of the ChronosMatch trading engine in real time. The dashboard tracks the **Top of Order Book (best bid/ask)**, monitors the **Bid/Ask spread**, measures **order processing latency** down to nanosecond precision, and streams live trade executions as orders cross the book.

---

## 2. Work Completed

### Raw Curses Terminal UI (`src/monitor/latency_monitor.py`)

* Designed and implemented a responsive, multi-panel terminal interface using Python's standard `curses` module (and `windows-curses` on Windows).
* Created a clean financial terminal layout inspired by high-frequency trading stations (Bloomberg / TT):
  - **Header Bar:** Displays engine status (`[Cython (Native C)]` or `[Python Engine Fallback]`), current simulation status (`RUNNING` / `PAUSED`), target rate (orders/sec), order count, and elapsed time.
  - **Top of Order Book Panel:** Live Bid/Ask depth ladder, Best Bid, Best Ask, Mid Price, and highlighted Spread bar.
  - **Latency & Performance Panel:** Real-time metrics including latest latency, mean, min, max, p50 (median), p95, and p99 percentiles, accompanied by a dynamic ASCII/Unicode sparkline and throughput counter.
  - **Matched Trade Tape:** Scrolling execution tape displaying Trade #, Buy Order ID, Sell Order ID, execution price, quantity, notional value, and large "whale" sweep indicators (`[WHALE]`).
  - **Command Footer & Help Modal:** Interactive keybindings for dynamic control without stopping the process (`[q]` quit, `[p]` pause/resume, `[s]` single step, `[+]`/`[-]` speed up/down, `[r]` reset, `[h]` help).

### Top of Order Book & Bid / Ask / Spread Tracking

* Integrated directly with Member 2's Cython engine API (`engine.get_order_book_snapshot()`).
* Extracted the highest resting buy price (`best_bid`) and lowest resting sell price (`best_ask`).
* Formatted real-time spread calculations:
  $$\text{Spread} = \text{Best Ask} - \text{Best Bid}$$
  $$\text{Spread (bps)} = \left(\frac{\text{Spread}}{\text{Mid Price}}\right) \times 10{,}000$$
* Implemented market status safeguards:
  - Gracefully handles empty books (`EMPTY`) and one-sided books (`N/A (One-Sided)`).
  - Flags crossed or locked markets if $\text{Best Bid} \ge \text{Best Ask}$ (`CROSSED!`).
* Built a depth ladder consolidating resting order quantities across distinct price levels, complete with proportional depth indicator bars (`#`).

### High-Resolution Latency Monitoring & Throughput

* Implemented microsecond and nanosecond per-order latency tracking using `time.perf_counter_ns()` wrapped directly around order dispatch into the matching engine.
* Built statistical aggregations:
  - Minimum, mean, and maximum latencies.
  - Non-parametric percentiles: p50 (median), p95, and p99 using linear interpolation.
  - Moving-window rolling average over the most recent 100 orders.
  - Instantaneous and lifetime throughput calculations (orders per second).
  - ASCII sparkline generation (`generate_sparkline`) illustrating latency trends.

### Fallback & Headless Support

* Provided a seamless pure-Python reference engine matching the exact interface defined in `SCHEMA.md` and `matching_engine.pyx` (`BUY=0`, `SELL=1`, `add_order`, `get_trades`, `get_order_book_snapshot`, `get_order_count`, `buy_orders`, `sell_orders`). This allows the monitor and automated test suites to run immediately on any developer machine even if Microsoft C++ Build Tools have not yet compiled the `.pyx` extension.
* Added support for headless environments, CI pipelines, and piped outputs (`--no-curses`, `--once`, `--max-orders`).

### Testing and Validation (`src/monitor/tests/test_latency_monitor.py`)

* Developed a comprehensive 16-test suite covering:
  - Initialization and state resets.
  - Deterministic order streaming and matching.
  - Manual order injection and partial/full fill tracking.
  - Top of book, spread, mid-price, and basis points calculations.
  - Empty, one-sided, and crossed book states.
  - Percentile metrics calculations (p50, p95, p99).
  - Sparkline string generation for varied inputs.
  - Terminal resizing guards, mock window drawing, and safe string clipping.
  - Command-line interface verification (`--once`, `--max-orders`).
* All 40 tests across the entire repository now pass cleanly.

---

## 3. Technologies Used

* **Python 3.11**
* **Curses / windows-curses** (Terminal graphics, windowing, color pairs, key event handling)
* **Cython & C-types Data Structures** (Interfacing with `COrderObj`, `MatchingEngine`)
* **Statistics & Numerics** (Percentiles, rolling moving averages, sparklines)
* **Pytest** (Automated unit and integration testing)
* **Git & GitHub** (Branching, clean modular commits)

---

## 4. Learning Outcomes

During Week 2, I gained hands-on experience in:

* Implementing raw terminal user interfaces with Python `curses`, including non-blocking event loops, color attribute handling, and terminal resizing resilience.
* Designing real-time dashboards for financial trading systems, focusing on top-of-book dynamics, spread monitoring, and execution tape feeds.
* Measuring extreme low-latency operations with nanosecond-resolution timers without perturbing the performance of the underlying engine.
* Writing robust fallbacks and mock window drivers to enable automated unit testing of terminal applications in CI/headless environments.
* Collaborating with team members by consuming shared data interfaces (`SCHEMA.md`) and integrating disparate modules into a cohesive user experience.

---

## 5. Conclusion

Member 3's Week 2 deliverables are complete and fully tested. The raw curses Latency Dashboard (`src/monitor/latency_monitor.py`) bridges Member 1's serialization and Member 2's matching engine into an interactive, real-time command center for the ChronosMatch project.
