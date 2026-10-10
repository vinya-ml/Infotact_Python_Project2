"""
ChronosMatch Week 3 — Metrics Tracking (Member 2: Performance/Metrics)

PDF requirement: "Metrics Tracking: Embed time.perf_counter_ns() timestamps
to measure the exact nanosecond a trade enters and exits the engine."

This benchmarks the REAL compiled Cython MatchingEngine (src/engine/
matching_engine.pyx) -- not the pure-Python baseline -- since that is
the thing the project is actually trying to prove is fast and GC-safe.

It reuses Member 3's existing LatencyTracker (src/analysis/metrics.py)
and MockMarketSimulator (src/analysis/simulator.py) rather than
duplicating that infrastructure.

Two latency measurements are taken per order:
  - "pipeline" latency: pack -> unpack -> engine.add_order() combined,
    directly comparable to the existing pure-Python baseline benchmark.
  - "engine-only" latency: just the engine.add_order() call itself,
    which is the literal "enters and exits the engine" the PDF asks for.

It also checks whether Python's Garbage Collector actually ran during
the timed loop, to test the PDF's specific claim that the engine
"guarantees the Garbage Collector never triggers during a trade."

Run from the project root:
    python cython_benchmark.py --orders 5000
"""

import argparse
import gc
import time

from src.analysis.metrics import LatencyTracker
from src.analysis.simulator import MockMarketSimulator
from src.data_processing.processor import DataProcessor
from src.engine.matching_engine import MatchingEngine

BUY = 0
SELL = 1


def _gc_collection_counter():
    """
    Returns (counter, callback). Register the callback with gc.callbacks
    to count how many times garbage collection actually runs. This is a
    direct, observable test of whether GC fires during the timed loop --
    not an assumption, an actual measurement.
    """
    count = {"collections": 0}

    def callback(phase, info):
        if phase == "start":
            count["collections"] += 1

    return count, callback


def run_cython_benchmark(n_orders: int = 5000, seed: int = 42,
                          whale_prob: float = 0.02) -> dict:
    sim = MockMarketSimulator(seed=seed)
    orders = sim.generate(n_orders, whale_prob=whale_prob)

    processor = DataProcessor()
    engine = MatchingEngine()

    pipeline_tracker = LatencyTracker()
    engine_only_tracker = LatencyTracker()

    gc_count, gc_callback = _gc_collection_counter()
    gc.callbacks.append(gc_callback)

    t0 = time.perf_counter_ns()
    pipeline_tracker.start(t0)
    engine_only_tracker.start(t0)

    for o in orders:
        pipeline_enter = time.perf_counter_ns()

        packed = processor.process_order(
            order_id=int(o["order_id"]), side=str(o["side"]),
            price=float(o["price"]), quantity=int(o["quantity"]),
        )
        unpacked = processor.unpack_order(packed)
        side_value = BUY if unpacked.side == "BUY" else SELL

        engine_enter = time.perf_counter_ns()
        engine.add_order(
            unpacked.order_id, side_value, unpacked.price,
            unpacked.quantity, unpacked.timestamp_ns,
        )
        engine_exit = time.perf_counter_ns()

        pipeline_tracker.add(engine_exit - pipeline_enter)
        engine_only_tracker.add(engine_exit - engine_enter)

    t1 = time.perf_counter_ns()
    pipeline_tracker.stop(t1)
    engine_only_tracker.stop(t1)

    gc.callbacks.remove(gc_callback)

    trades = engine.get_trades()

    return {
        "num_orders": n_orders,
        "pipeline_stats": pipeline_tracker.stats(),
        "engine_only_stats": engine_only_tracker.stats(),
        "gc_collections_during_run": gc_count["collections"],
        "trades_matched": len(trades),
    }


def print_stats_block(title: str, stats: dict):
    print(f"\n{title}")
    print("-" * len(title))
    print(f"  Samples : {stats['count']}")
    print(f"  Mean    : {stats['mean_us']:.3f} us")
    print(f"  Min     : {stats['min_us']:.3f} us")
    print(f"  Max     : {stats['max_us']:.3f} us")
    print(f"  p50     : {stats['p50_us']:.3f} us")
    print(f"  p95     : {stats['p95_us']:.3f} us")
    print(f"  p99     : {stats['p99_us']:.3f} us")
    print(f"  Throughput: {stats['throughput_ops']:,.1f} orders/sec")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChronosMatch Cython Engine Benchmark")
    parser.add_argument("--orders", type=int, default=5_000,
                         help="Number of orders to benchmark (default: 5,000)")
    parser.add_argument("--seed", type=int, default=42,
                         help="Random seed for reproducible order generation")
    args = parser.parse_args()

    print(f"ChronosMatch Cython Engine Benchmark -- {args.orders:,} orders")
    print("=" * 60)

    result = run_cython_benchmark(args.orders, seed=args.seed)

    print_stats_block("PIPELINE LATENCY (pack -> unpack -> engine.add_order)",
                       result["pipeline_stats"])
    print_stats_block("ENGINE-ONLY LATENCY (engine.add_order only)",
                       result["engine_only_stats"])

    print(f"\nTrades matched: {result['trades_matched']:,}")
    print(f"Garbage collections triggered during run: {result['gc_collections_during_run']}")

    if result["gc_collections_during_run"] == 0:
        print("  -> No GC activity observed during the timed loop.")
    else:
        print("  -> GC activity WAS observed -- see note below.")

    print("\n" + "=" * 60)
    print("Note: this counts whole-process GC activity, which can include")
    print("collections triggered by unrelated objects (e.g. the simulator")
    print("or data processor), not strictly the matching loop in isolation.")
    print("A zero count here is strong evidence; a non-zero count needs")
    print("closer inspection of what triggered it before drawing conclusions.")