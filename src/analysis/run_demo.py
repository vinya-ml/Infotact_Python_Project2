"""ChronosMatch Member 3 demo: firehose -> match -> analyze -> visualize.

Usage (from repo root):
    python -m src.analysis.run_demo --orders 5000 --seed 42
    python src/analysis/run_demo.py --orders 2000
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.analysis.benchmark import ipc_audit, run_benchmark, save_report
from src.analysis.visualizer import render_dashboard


def main() -> None:
    ap = argparse.ArgumentParser(description="ChronosMatch Member 3 demo")
    ap.add_argument("--orders", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="reports")
    args = ap.parse_args()

    print(f"[demo] benchmark: {args.orders} orders (seed={args.seed}) ...")
    result, book, _ = run_benchmark(n_orders=args.orders, seed=args.seed)
    print(f"[demo] throughput : {result.orders_per_sec:,.1f} orders/sec")
    print(f"[demo] latency mean/p95/p99 : "
          f"{result.latency_stats['mean_us']:.3f} / "
          f"{result.latency_stats['p95_us']:.3f} / "
          f"{result.latency_stats['p99_us']:.3f} us")
    print(f"[demo] trades: {result.trade_stats['count']} | "
          f"fill_rate: {result.fill_rate:.3f} | whales: {result.whale_count}")

    print("\n[demo] IPC audit (Member 1 serialization path) ...")
    audit = ipc_audit(num_orders=min(20000, max(2000, args.orders * 2)))
    print(f"[demo] IPC: {audit['orders_per_sec']:,.1f} orders/sec "
          f"({audit['mb_per_sec']:.2f} MB/s, record={audit['record_size']}B)")

    print("\n" + render_dashboard(book, list(getattr(book, 'trades', [])),
                                 result.latency_stats, whale_threshold=500))

    paths = save_report(result, book, out_dir=args.out)
    print("\n[demo] saved:")
    for k, v in paths.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
