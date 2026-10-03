"""
ChronosMatch Mid-Project Review Proof:
"Prove the zero-copy architecture works by sending orders between two
Python processes without hitting the CPU bottleneck of Pickling."

This script spawns TWO SEPARATE OS PROCESSES using multiprocessing:
  - A writer process that generates N orders and writes them into the
    shared mmap ring buffer.
  - A reader process that reads all N orders back from that same buffer.

It then runs an equivalent test using pickle (simulating the "normal"
slow approach) so the two can be compared directly.

Note: the buffer is sized to hold ALL N orders at once (num_slots = N),
so the reader can never fall behind the writer by more than the whole
test size. This avoids wraparound overwriting data the reader hasn't
read yet -- a real production version would add backpressure (the
writer waiting when the buffer is full) instead of just sizing around it.

Run from the project root:
    python ipc_proof.py
    python ipc_proof.py --orders 1000000   (for the full 1M proof)
"""

import argparse
import multiprocessing
import os
import pickle
import time

from src.data_processing.ipc_buffer import open_buffer
from src.data_processing.processor import DataProcessor

BUFFER_PATH = "proof_orders.mmap"


# ---------------------------------------------------------
# Process 1: Writer (writes N orders into the mmap buffer)
# ---------------------------------------------------------

def writer_process(path: str, num_orders: int, num_slots: int, start_event):
    processor = DataProcessor()
    buf = open_buffer(path, num_slots=num_slots, create=False)

    start_event.wait()  # wait for the reader to be ready

    for i in range(num_orders):
        order = processor.create_order(
            order_id=i,
            side="BUY" if i % 2 == 0 else "SELL",
            price=100.0 + (i % 50),
            quantity=10,
        )
        packed = processor.pack_order(order)
        buf.write(packed)

    buf.close()


# ---------------------------------------------------------
# Process 2: Reader (reads N orders back from the mmap buffer)
# ---------------------------------------------------------

def reader_process(path: str, num_orders: int, num_slots: int, start_event, result_queue):
    processor = DataProcessor()
    buf = open_buffer(path, num_slots=num_slots, create=False)

    start_event.set()  # signal writer to begin
    t0 = time.perf_counter()

    received = 0
    while received < num_orders:
        data = buf.read()
        if data is not None:
            processor.unpack_order(data)
            received += 1

    elapsed = time.perf_counter() - t0
    buf.close()
    result_queue.put(elapsed)


def run_mmap_ipc_test(num_orders: int) -> float:
    """
    Runs the real two-process mmap test and returns elapsed seconds
    for the reader to receive all num_orders orders.

    num_slots is set equal to num_orders so the whole run fits in the
    buffer without wrapping -- see module docstring.
    """
    num_slots = num_orders

    if os.path.exists(BUFFER_PATH):
        os.remove(BUFFER_PATH)

    # Create the buffer file up front so both processes can open it
    setup_buf = open_buffer(BUFFER_PATH, num_slots=num_slots, create=True)
    setup_buf.close()

    start_event = multiprocessing.Event()
    result_queue = multiprocessing.Queue()

    writer = multiprocessing.Process(
        target=writer_process,
        args=(BUFFER_PATH, num_orders, num_slots, start_event),
    )
    reader = multiprocessing.Process(
        target=reader_process,
        args=(BUFFER_PATH, num_orders, num_slots, start_event, result_queue),
    )

    reader.start()
    writer.start()

    elapsed = result_queue.get()  # blocks until reader finishes and reports time

    writer.join()
    reader.join()

    os.remove(BUFFER_PATH)

    return elapsed


# ---------------------------------------------------------
# Comparison: Pickle-based approach (the "slow" baseline)
# ---------------------------------------------------------

def run_pickle_baseline(num_orders: int) -> float:
    """
    Simulates the traditional approach: serialize each order with
    pickle, as if sending it through a pipe/queue between processes.
    This does NOT use shared memory - it's the baseline we're
    proving mmap beats.
    """
    processor = DataProcessor()

    t0 = time.perf_counter()

    for i in range(num_orders):
        order = processor.create_order(
            order_id=i,
            side="BUY" if i % 2 == 0 else "SELL",
            price=100.0 + (i % 50),
            quantity=10,
        )
        pickled = pickle.dumps(order)
        restored = pickle.loads(pickled)

    elapsed = time.perf_counter() - t0
    return elapsed


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChronosMatch IPC Audit")
    parser.add_argument("--orders", type=int, default=10_000,
                         help="Number of orders to test with (default: 10,000)")
    args = parser.parse_args()

    n = args.orders

    print(f"ChronosMatch IPC Audit -- {n:,} orders")
    print("=" * 50)

    print("\nRunning mmap zero-copy test (two separate processes)...")
    mmap_time = run_mmap_ipc_test(n)
    mmap_rate = n / mmap_time if mmap_time > 0 else 0
    print(f"  mmap IPC:   {mmap_time:.3f} sec  ({mmap_rate:,.0f} orders/sec)")

    print("\nRunning pickle baseline (single process, for comparison)...")
    pickle_time = run_pickle_baseline(n)
    pickle_rate = n / pickle_time if pickle_time > 0 else 0
    print(f"  Pickle:     {pickle_time:.3f} sec  ({pickle_rate:,.0f} orders/sec)")

    print("\n" + "=" * 50)
    if mmap_time > 0:
        speedup = pickle_time / mmap_time
        print(f"RESULT: mmap zero-copy IPC is {speedup:.1f}x faster than pickle")
    print("=" * 50)