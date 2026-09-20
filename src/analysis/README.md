# Member 3 — Analysis, Visualization & Testing (`src/analysis/`)

ChronosMatch (Project 2) integration layer owned by **Member 3**.
Does **not** modify Member 1 (`src/data_processing/`) or Member 2 (`src/model/`);
it imports and measures them.

## What Member 1 & 2 built (as found on `main`)

- **Member 1** — `src/data_processing/processor.py`: `DataProcessor` validates
  orders and packs them into fixed 33-byte records (`<QBdQQ`:
  order_id U64, side U8, price F64, quantity U64, timestamp U64) for the
  zero-copy mmap ring buffer. `pack_order` / `unpack_order` round-trip.
- **Member 2** — `src/model/baseline_matcher.py`: `Order` + `OrderBook` with
  Price-Time Priority matching (`buy` matches lowest ask ≤ bid, `sell` matches
  highest bid ≥ ask, partial fills rest in book). `test_baseline.py` covers
  full / partial / no-match / multi-fill cases.

## What Member 3 adds

| File | Purpose |
|---|---|
| `metrics.py` | `LatencyTracker` (mean/p50/p95/p99, throughput), `OrderBookAnalytics` (best bid/ask, spread, depth-by-price, VWAP, fill-rate, whale detection), `percentile()` |
| `visualizer.py` | Windows-safe ASCII order-book / trades / latency dashboard + optional matplotlib PNGs (latency histogram, depth chart, trade-price trail) |
| `simulator.py` | Deterministic `MockMarketSimulator` firehose (seeded RNG, whale orders, `balanced_book()` ladder that guarantees matches) |
| `benchmark.py` | `run_benchmark()` end-to-end pipeline (dict → Member1 pack/unpack → Member2 match, timed with `perf_counter_ns`), `ipc_audit()` serialization throughput, `save_report()` |
| `run_demo.py` | CLI demo tying everything together |
| `tests/test_analysis.py` | 19 pytest cases: unit + integration + benchmark + priority checks |

## Run

From repo root:

```bash
pip install pytest matplotlib
pytest src/analysis/tests/test_analysis.py -v
python -m src.analysis.run_demo --orders 5000 --seed 42
# outputs: reports/chronosmatch_dashboard.txt + *_latency.png, *_depth.png, *_trades.png
```

## Design notes

- **Adapter**: Member 1 uses `side="BUY"/quantity/timestamp_ns`; Member 2 uses
  `side="buy"/qty/timestamp`. `_to_member2_order()` in `benchmark.py` bridges them.
- **Latency**: measured per order around pack→unpack→match with
  `time.perf_counter_ns()`; stats reported in ns and µs.
- **Whales**: trades with `qty >= threshold` (default 500) flagged `<<< WHALE`
  in terminal and counted in benchmark results.
- **No new deps required**: everything except PNG plots is stdlib-only;
  matplotlib import is lazy with graceful fallback.
