import statistics
import time

from src.engine.matching_engine import MatchingEngine

N = 5000
ORDERS = [
    (i, 0 if i % 2 else 1, 100.0 if i % 2 else 99.0, 10, i)
    for i in range(1, N + 1)
]


def run_single():
    engine = MatchingEngine()
    start = time.perf_counter_ns()

    for order in ORDERS:
        engine.add_order(*order)

    elapsed = time.perf_counter_ns() - start
    assert engine.get_order_count() == N
    assert engine.get_trade_count() == N // 2
    return elapsed


def run_batch():
    engine = MatchingEngine()
    start = time.perf_counter_ns()

    processed = engine.add_orders_batch(ORDERS)

    elapsed = time.perf_counter_ns() - start
    assert processed == N
    assert engine.get_order_count() == N
    assert engine.get_trade_count() == N // 2
    return elapsed


if __name__ == "__main__":
    # Warm-up; these runs are not included in the results.
    run_single()
    run_batch()

    single_times = []
    batch_times = []

    for _ in range(7):
        single_times.append(run_single())
        batch_times.append(run_batch())

    single_ns = statistics.median(single_times)
    batch_ns = statistics.median(batch_times)

    single_rate = N / (single_ns / 1e9)
    batch_rate = N / (batch_ns / 1e9)

    print("=" * 52)
    print("CHRONOSMATCH: SINGLE vs BATCH BENCHMARK")
    print("=" * 52)
    print(f"Orders per run       : {N:,}")
    print("Measurement          : median of 7 runs")
    print(f"Single time           : {single_ns / 1e6:.3f} ms")
    print(f"Batch time            : {batch_ns / 1e6:.3f} ms")
    print(f"Single throughput     : {single_rate:,.0f} orders/sec")
    print(f"Batch throughput      : {batch_rate:,.0f} orders/sec")

    if batch_ns < single_ns:
        print(f"Batch speedup         : {single_ns / batch_ns:.2f}x")
    elif batch_ns > single_ns:
        print(f"Batch relative speed  : {single_ns / batch_ns:.2f}x")
        print("Batch was slower in this test.")
    else:
        print("Measured times were equal.")

    print("Correctness checks    : PASSED")
    print("=" * 52)
