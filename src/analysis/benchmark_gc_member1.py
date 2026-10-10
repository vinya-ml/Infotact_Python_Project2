import gc
import statistics
import time

from src.engine.matching_engine import MatchingEngine

N = 5000
REPEATS = 15

ORDERS = [
    (i, 0 if i % 2 else 1, 100.0 if i % 2 else 99.0, 10, i)
    for i in range(1, N + 1)
]

gc_pause_ns = 0
gc_start_ns = None


def gc_callback(phase, info):
    global gc_pause_ns, gc_start_ns

    if phase == "start":
        gc_start_ns = time.perf_counter_ns()
    elif phase == "stop" and gc_start_ns is not None:
        gc_pause_ns += time.perf_counter_ns() - gc_start_ns
        gc_start_ns = None


def run_once():
    engine = MatchingEngine()
    start = time.perf_counter_ns()
    processed = engine.add_orders_batch(ORDERS)
    elapsed = time.perf_counter_ns() - start

    assert processed == N
    assert engine.get_order_count() == N
    assert engine.get_trade_count() == N // 2

    return elapsed


def run_group(enabled):
    global gc_pause_ns

    gc.collect()
    gc_pause_ns = 0

    if enabled:
        gc.enable()
    else:
        gc.disable()

    before = gc.get_stats()
    times = []

    try:
        for _ in range(REPEATS):
            times.append(run_once())
    finally:
        after = gc.get_stats()
        gc.enable()

    collections = [
        after[i]["collections"] - before[i]["collections"]
        for i in range(3)
    ]

    return statistics.median(times), collections, gc_pause_ns


if __name__ == "__main__":
    gc.callbacks.append(gc_callback)

    try:
        run_once()

        enabled_ns, enabled_collections, enabled_pause = run_group(True)
        disabled_ns, disabled_collections, disabled_pause = run_group(False)

        enabled_ms = enabled_ns / 1_000_000
        disabled_ms = disabled_ns / 1_000_000
        delta_ms = enabled_ms - disabled_ms

        print("=" * 54)
        print("CHRONOSMATCH: GARBAGE COLLECTION BENCHMARK")
        print("=" * 54)
        print(f"Orders per run        : {N:,}")
        print(f"Runs per mode         : {REPEATS}")
        print("Measurement           : median")
        print(f"GC enabled time       : {enabled_ms:.4f} ms")
        print(f"GC disabled time      : {disabled_ms:.4f} ms")
        print(f"Time difference       : {delta_ms:.4f} ms")

        if enabled_ms > 0:
            print(
                f"Difference percentage : "
                f"{(delta_ms / enabled_ms) * 100:.2f}%"
            )

        print(f"GC collections enabled: {enabled_collections}")
        print(f"GC collections disabled: {disabled_collections}")
        print(f"Measured GC pause     : {enabled_pause / 1000:.2f} us")
        print("Correctness           : PASSED")
        print("=" * 54)

    finally:
        gc.enable()
        gc.callbacks.remove(gc_callback)
