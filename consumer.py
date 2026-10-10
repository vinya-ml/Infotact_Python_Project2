"""
ChronosMatch Buffer Consumer.

Reads orders from the shared mmap ring buffer (written by firehose.py)
and feeds them into the matching engine. This is the piece that closes
the loop: firehose -> mmap buffer -> matching engine -> matched trades,
proving the whole pipeline is genuinely zero-copy end-to-end, not just
the buffer mechanism working in isolation.

Uses the compiled Cython MatchingEngine if available, falling back to
the pure-Python baseline if the Cython extension hasn't been built on
this machine (same fallback pattern used in latency_monitor.py).

Run from the project root, in a SEPARATE terminal from firehose.py:
    python consumer.py --orders 5000
"""

import argparse
import time

from src.data_processing.ipc_buffer import open_buffer
from src.data_processing.processor import DataProcessor

BUFFER_PATH = "chronosmatch_orders.mmap"

# Side constants matching the Cython engine / SCHEMA.md
BUY = 0
SELL = 1

try:
    from src.engine.matching_engine import MatchingEngine
    USING_CYTHON = True
except (ImportError, ModuleNotFoundError):
    from src.model.baseline_matcher import OrderBook, Order
    USING_CYTHON = False


def run_consumer(num_orders: int, poll_timeout_sec: float = 10.0):
    processor = DataProcessor()
    buf = open_buffer(BUFFER_PATH, create=False)

    if USING_CYTHON:
        engine = MatchingEngine()
    else:
        engine = OrderBook()

    print("ChronosMatch Buffer Consumer")
    print(f"Engine: {'Cython MatchingEngine' if USING_CYTHON else 'Pure-Python OrderBook (fallback)'}")
    print(f"Waiting for {num_orders:,} orders from the buffer...\n")

    received = 0
    t_start = time.perf_counter()
    last_progress = t_start

    while received < num_orders:
        data = buf.read()

        if data is None:
            # Nothing new yet - avoid spinning the CPU too hard
            if time.perf_counter() - t_start > poll_timeout_sec and received == 0:
                print("Timed out waiting for orders. Is firehose.py running?")
                buf.close()
                return
            time.sleep(0.001)
            continue

        order = processor.unpack_order(data)

        if USING_CYTHON:
            side_value = BUY if order.side == "BUY" else SELL
            engine.add_order(order.order_id, side_value, order.price,
                              order.quantity, order.timestamp_ns)
        else:
            side_str = "buy" if order.side == "BUY" else "sell"
            engine.add_order(Order(order.order_id, side_str, order.price,
                                    order.quantity, order.timestamp_ns))

        received += 1

        if time.perf_counter() - last_progress > 1.0:
            print(f"  ...{received:,}/{num_orders:,} orders processed")
            last_progress = time.perf_counter()

    elapsed = time.perf_counter() - t_start
    rate = num_orders / elapsed if elapsed > 0 else 0

    trades = engine.get_trades() if USING_CYTHON else engine.trades

    print(f"\nConsumer finished: {received:,} orders processed in {elapsed:.3f}s "
          f"({rate:,.0f} orders/sec)")
    print(f"Matched trades: {len(trades):,}")

    if trades:
        print("\nSample trades:")
        for t in trades[:5]:
            print(f"  buy_id={t['buy_id']} sell_id={t['sell_id']} "
                  f"price={t['price']} qty={t['qty']}")

    buf.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChronosMatch Buffer Consumer")
    parser.add_argument("--orders", type=int, default=5_000,
                         help="Number of orders to read and match (default: 5,000, "
                              "should match firehose.py's --orders)")
    args = parser.parse_args()

    run_consumer(args.orders)