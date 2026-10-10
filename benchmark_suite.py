"""
ChronosMatch Week 4 -- Performance Testing & Benchmark Results (Member 2)

Runs the pure-Python baseline matcher and the compiled Cython engine
across several load sizes, repeating each run several times, and
reports a comparison table.

Why repeated runs: a single run can be thrown off by a one-off spike
(OS scheduling, a GC pause). Repeating each test and reporting the
MEDIAN of each metric makes the numbers reproducible and defensible.

Both engines are measured the same way (pack -> unpack -> add_order,
timed with time.perf_counter_ns()) so the comparison is fair. The
Cython engine is additionally measured "engine-only" (just add_order),
which is the literal "enters and exits the engine" from the PDF.

Reuses existing code rather than duplicating it:
  - src/analysis/benchmark.py  -> run_benchmark()   (baseline)
  - cython_benchmark.py        -> run_cython_benchmark()  (Cython)

Run from the project root:
    python benchmark_suite.py
    python benchmark_suite.py --sizes 1000 5000 20000 --runs 5
    python benchmark_suite.py --save      (also writes BENCHMARK_RESULTS.md)
"""

import argparse
import platform
import statistics
import sys
from datetime import date

from src.analysis.benchmark import run_benchmark
from cython_benchmark import run_cython_benchmark

DEFAULT_SIZES = [1_000, 5_000, 10_000, 20_000]
DEFAULT_RUNS = 3

SUB_MS_US = 1000.0    # PDF Final Review target: sub-millisecond
USE_CASE_US = 50.0    # PDF use case: "matching trades in under 50 microseconds"


def median(values):
    return statistics.median(values) if values else 0.0


def bench_baseline(n_orders: int, runs: int) -> dict:
    stats_list = []
    for i in range(runs):
        result, _book, _tracker = run_benchmark(n_orders, seed=42 + i)
        stats_list.append(result.latency_stats)
    return {
        "mean_us": median([s["mean_us"] for s in stats_list]),
        "p50_us": median([s["p50_us"] for s in stats_list]),
        "p95_us": median([s["p95_us"] for s in stats_list]),
        "p99_us": median([s["p99_us"] for s in stats_list]),
        "max_us": median([s["max_us"] for s in stats_list]),
        "throughput": median([s["throughput_ops"] for s in stats_list]),
    }


def bench_cython(n_orders: int, runs: int) -> dict:
    results = [run_cython_benchmark(n_orders, seed=42 + i) for i in range(runs)]
    pipe = [r["pipeline_stats"] for r in results]
    eng = [r["engine_only_stats"] for r in results]
    return {
        "mean_us": median([s["mean_us"] for s in pipe]),
        "p50_us": median([s["p50_us"] for s in pipe]),
        "p95_us": median([s["p95_us"] for s in pipe]),
        "p99_us": median([s["p99_us"] for s in pipe]),
        "max_us": median([s["max_us"] for s in pipe]),
        "throughput": median([s["throughput_ops"] for s in pipe]),
        "engine_p50_us": median([s["p50_us"] for s in eng]),
        "engine_p99_us": median([s["p99_us"] for s in eng]),
        "gc_collections": [r["gc_collections_during_run"] for r in results],
    }


def build_report(sizes, runs, rows) -> str:
    lines = []
    lines.append("# ChronosMatch -- Performance Benchmark Results\n")
    lines.append(f"Date: {date.today().isoformat()}  ")
    lines.append(f"Machine: {platform.system()} {platform.release()}, "
                 f"Python {platform.python_version()}  ")
    lines.append(f"Method: each load size run {runs}x, median of each metric "
                 f"reported. Latency timed with `time.perf_counter_ns()`.\n")

    lines.append("## Pipeline latency: baseline vs Cython "
                 "(pack -> unpack -> add_order)\n")
    lines.append("| Orders | Engine | Mean (us) | p50 (us) | p95 (us) | "
                 "p99 (us) | Max (us) | Throughput (orders/sec) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for n in sizes:
        b, c = rows[n]["baseline"], rows[n]["cython"]
        for name, s in (("Python baseline", b), ("Cython", c)):
            lines.append(f"| {n:,} | {name} | {s['mean_us']:.2f} | "
                         f"{s['p50_us']:.2f} | {s['p95_us']:.2f} | "
                         f"{s['p99_us']:.2f} | {s['max_us']:.1f} | "
                         f"{s['throughput']:,.0f} |")

    lines.append("\n## Speedup (Cython vs baseline, by median mean latency)\n")
    lines.append("| Orders | Speedup |")
    lines.append("|---|---|")
    for n in sizes:
        b, c = rows[n]["baseline"], rows[n]["cython"]
        speedup = b["mean_us"] / c["mean_us"] if c["mean_us"] else 0.0
        lines.append(f"| {n:,} | {speedup:.2f}x |")

    lines.append("\n## Cython engine-only latency (`add_order` call alone)\n")
    lines.append("| Orders | p50 (us) | p99 (us) | GC collections per run |")
    lines.append("|---|---|---|---|")
    for n in sizes:
        c = rows[n]["cython"]
        lines.append(f"| {n:,} | {c['engine_p50_us']:.2f} | "
                     f"{c['engine_p99_us']:.2f} | {c['gc_collections']} |")

    lines.append("\n## Checks against the PDF targets\n")
    lines.append("| Orders | p99 under 1 ms (Final Review) | "
                 "Engine p50 under 50 us (use case) |")
    lines.append("|---|---|---|")
    for n in sizes:
        c = rows[n]["cython"]
        sub_ms = "YES" if c["p99_us"] < SUB_MS_US else "NO"
        under_50 = "YES" if c["engine_p50_us"] < USE_CASE_US else "NO"
        lines.append(f"| {n:,} | {sub_ms} (p99 = {c['p99_us']:.1f} us) | "
                     f"{under_50} (p50 = {c['engine_p50_us']:.1f} us) |")

    lines.append("\n## Notes\n")
    lines.append("- Numbers vary by machine; compare runs on the same machine.")
    lines.append("- GC counts are whole-process (they can include collections "
                 "from the simulator or serializer, not only the matching loop).")
    lines.append("- Re-run this script after the Cython engine is optimized to "
                 "get a before/after comparison.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="ChronosMatch benchmark suite")
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES,
                        help="Load sizes (orders) to test")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="Repeats per size; median is reported")
    parser.add_argument("--save", action="store_true",
                        help="Write results to BENCHMARK_RESULTS.md")
    args = parser.parse_args()

    print(f"ChronosMatch Benchmark Suite -- sizes={args.sizes}, runs={args.runs}")
    print("=" * 60)

    rows = {}
    for n in args.sizes:
        print(f"\nBenchmarking {n:,} orders ({args.runs} runs each)...")
        print("  baseline ...", end=" ", flush=True)
        baseline = bench_baseline(n, args.runs)
        print("done")
        print("  cython   ...", end=" ", flush=True)
        cython = bench_cython(n, args.runs)
        print("done")
        rows[n] = {"baseline": baseline, "cython": cython}

    report = build_report(args.sizes, args.runs, rows)
    print("\n" + report)

    if args.save:
        with open("BENCHMARK_RESULTS.md", "w", encoding="utf-8") as f:
            f.write(report)
        print("Saved to BENCHMARK_RESULTS.md")


if __name__ == "__main__":
    sys.exit(main())